# CLAUDE.md

全屋家具智能管家 — LangGraph + LangChain Agent 项目，面向家具客服/导购/保养/售后场景。

## 常用命令

```bash
uv sync --frozen              # 安装依赖
uv run alembic upgrade head   # 数据库迁移
uv run python api.py          # 启动 FastAPI (8008)
uv run streamlit run app.py   # 启动 Streamlit 前端 (8501)
uv run python -m workers.memory_index_worker  # 会话向量索引 Worker
uv run python -m workers.rag_index_worker     # 知识索引 Worker
make test                     # 运行 pytest
make eval                     # 运行评测 (suite=offline, mode=agent)
make benchmark                # 运行基准测试
make migrate                  # 等同于 alembic upgrade head
make up                       # Docker Compose 启动全部服务
make clean                    # 停服 + 清理 uv cache
```

## 技术栈

- **编排**: LangGraph 1.1+ (`StateGraph`, `TypedDict` state)
- **模型**: 阿里 DashScope — `qwen-mt-flash` (chat/memory), `text-embedding-v4` (embedding)
- **RAG**: Chroma 向量检索 + jieba BM25 + BGE Reranker (`models/bge-reranker-base/`), 混合融合 (RRF) → 证据质量判定 → 严格拒绝
- **记忆**: 三层 — 短期窗口 (Redis), 会话摘要 (MySQL + LLM), 用户长期记忆 (MySQL + rule+LLM 提取)
- **持久化**: MySQL (全部业务表) + Redis (缓存) + Chroma (向量)
- **异步 Worker**: Outbox 模式 — memory_index_outbox / knowledge_index_outbox, `FOR UPDATE SKIP LOCKED` 消费
- **前端**: Streamlit, SSE 流式输出
- **部署**: Docker Compose (MySQL + Redis + Chroma + API + Streamlit)
- **迁移**: Alembic + PyMySQL
- **测试**: pytest (8 个测试文件), 评测 CLI (`evaluation/cli.py`)

## 架构概览

```
User → Streamlit (app.py) → FastAPI SSE (/chat, api.py) → ReactAgent
  → AgentGraphWorkflow (LangGraph StateGraph)
    ├─ route_task ──────────┐  (并行)
    ├─ recall_history ──────┘
    ├─ assemble_context
    ├─ route_specialists → dispatch_specialists → compose_answer   (default: use_agentic_mode=False)
    │  或 rewrite_query → agentic_answer                            (agentic: use_agentic_mode=True)
    ├─ persist_turn ────────┬─→ memory_index_outbox
    ├─ update_task_state    │  (并行 tail)
    └─ refresh_user_memory ─┘
```

后端异步消费链路 (不阻塞主对话):
```
memory_index_outbox → MemoryIndexWorker → ConversationVectorStore (Chroma)
knowledge_index_outbox → RagIndexWorker → KnowledgeVectorStore (Chroma) + MySQL chunk 管理
```

## 核心模块

### `agent/` — Agent 编排层

| 文件 | 职责 |
|------|------|
| `tools/react_agent.py` | 顶层入口, 组装依赖并创建 `AgentGraphWorkflow` |
| `graph_workflow.py` | LangGraph 图定义, 两个模式 (expert/agentic), 9 个节点 |
| `multi_agent.py` | `MultiAgentRouter`(规则+LLM 路由), `MultiAgentRunner`(并行专家执行), `AnswerComposer`(答案合成) |
| `context_assembler.py` | 按优先级组装 system_context (MySQL 事实 > session summary > task > user memory > 历史召回), 20000 字符预算 |
| `memory_service.py` | 会话摘要 & 用户记忆的 LLM+规则 增量提取, 触发阈值: 16 条 → 首次摘要, 每 8 条增量刷新 |
| `history_recall_service.py` | 跨会话历史召回: 精确位置 (MySQL), 语义召回 (Chroma → MySQL 验证 → Reranker) |
| `task_service.py` | 对话任务路由 & 状态跟踪, embedding 语义匹配 + 大词相似度 + 意向兼容性评分 |
| `query_rewriter.py` | 指代消解: 纯指代词规则替换 → 不需要改写直接返回 → LLM 改写 |
| `conversation_vector_store.py` | 会话级别的 Chroma collection, 每轮对话 upsert 一条 episode |
| `agentic_tools.py` | Function Calling 工具: `search_knowledge_base`, `query_user_report`, `get_current_weather` |

### `rag/` — 知识检索层

| 文件 | 职责 |
|------|------|
| `rag_service.py` | `RagSummarizeService`: 混合检索 (vector+BM25) → RRF融合 → Reranker → 证据判断 → LLM 回答/拒绝 |
| `reranker.py` | `BGEReranker`: BGE Cross-Encoder, 本地加载 |
| `vector_store.py` | `KnowledgeVectorStore`: Chroma 知识向量库, 支持 metadata 过滤, 批量 upsert, 按 version/document 删除 |
| `document_parser.py` | 文档解析: FAQ/Q&A / 编号段落 / 滑动窗口, PDF 分页, jieba 分词提取 keywords |
| `retrieval_types.py` | `RetrievalCandidate` / `RetrievalResult` dataclass |
| `index_cli.py` | 命令行索引工具 |

### `db/` — 数据库层

MySQL 表: `users`, `chat_sessions`, `chat_messages`, `session_memory`, `user_memory`, `memory_index_outbox`, `knowledge_documents`, `knowledge_document_versions`, `knowledge_chunks`, `knowledge_index_outbox`, `knowledge_index_state`, `conversation_tasks`, `conversation_task_facts`, `conversation_task_events`, `feedbacks`, `external_records`

| 文件 | 职责 |
|------|------|
| `mysql_client.py` | MySQL 连接池 (mysql-connector-python) |
| `redis_client.py` | Redis 客户端 |
| `chroma_client.py` | Chroma 客户端工厂 (兼容 http/standalone 模式) |
| `session_repository.py` | 会话持久化: 写入 chat_messages + outbox enqueue, Redis 缓存近期历史, MySQL 为权威源 |
| `memory_repository.py` | session_memory & user_memory CRUD, Redis 缓存 + MySQL 持久化 |
| `task_repository.py` | 对话任务 CRUD, 乐观锁版本控制, fact upsert 去重 |
| `knowledge_repository.py` | 知识文档版本管理, chunk 替换, 索引健康检查 |
| `outbox_repository.py` | memory_index_outbox: enqueue / claim (SKIP LOCKED) / complete / fail (指数退避) |

### `evaluation/` — 评测框架

三种模式: `agent` (端到端), `rag` (知识检索+回答), `router` (路由规则)

评测指标: accuracy, fact_coverage, forbidden_fact_rate, reject_f1, route_accuracy, recall@10, recall@4, avg/p95 latency

数据集: `datasets/calibration.jsonl` (开发校准集), `datasets/golden.jsonl` (最终评估集)

### `workers/` — 后台 Worker

- `memory_index_worker.py`: 消费 memory_index_outbox → 写入 ConversationVectorStore
- `rag_index_worker.py`: 消费 knowledge_index_outbox → parse → chunk → 写 MySQL + Chroma → activate version → 清理旧 version

### `utils/`

- `config_handler.py`: YAML 配置加载 (chroma.yml, rag.yml)
- `prompt_loader.py`: 提示词加载, 支持动态切换 (系统/报表)
- `logger_handler.py`: 日志配置
- `file_handler.py`: PDF/TXT 文件加载
- `path_tool.py`: 路径工具

### `model/factory.py`

三个工厂类: `ChatModelFactory` (ChatTongyi + qwen-mt-flash), `EmbeddingsFactory` (DashScopeEmbeddings + text-embedding-v4), `MemoryModelFactory` (ChatTongyi + qwen-mt-flash)。模块级单例 `chat_model`, `memory_model`, `embed_model`。

### `config/`

- `rag.yml`: 模型名, 检索参数 (vector_top_k=20, bm25_top_k=20, rerank_top_n=12, final_top_n=4, evidence_threshold=0.55)
- `chroma.yml`: collection 名, 持久化目录, 切块参数 (chunk_size=500, overlap=80)

## 两种执行模式

### 默认: Expert Multi-Agent (`use_agentic_mode=False`)

1. `route_task` + `recall_history` 并行
2. `assemble_context` 组装上下文
3. `route_specialists` — `MultiAgentRouter` 规则匹配 (+ LLM 兜底) 选择 KnowledgeAgent / ReportAgent / DefaultResponder
4. `dispatch_specialists` — `MultiAgentRunner` ThreadPoolExecutor 并行执行专家
5. `compose_answer` — `AnswerComposer` 合成最终答案
6. `persist_turn` → `update_task_state` + `refresh_user_memory` (并行)

### 备选: Agentic Function Calling (`use_agentic_mode=True`)

1. 同上并行开始
2. `assemble_context` → `rewrite_query` → `agentic_answer`
3. `agentic_answer` 内部: model.bind_tools → 最多 5 轮 tool call 迭代 → 最终回答

## 关键设计决策

- **证据严格拒绝**: RAG 检索后经过 3 层判断 (object+intent 覆盖 / required_terms 匹配 / rerank_score 阈值), 不足则返回 `STRICT_REFUSAL`, 不编造
- **Outbox 模式**: 主链路只写 MySQL + enqueue outbox, Worker 异步索引到 Chroma, 不阻塞对话
- **乐观锁**: conversation_tasks 使用 state_version 做乐观并发控制, 冲突时重试一次
- **MySQL 验证**: 语义召回的 candidates 必须回到 MySQL 验证 session/request 完整性, 防止幻觉
- **降级策略**: 每个节点内部 try/except, 失败时返回退化结果 + 记录 node_errors, 主链路不中断
- **Score 阈值分层**: route_task 语义匹配 ≥0.78 可跨 session resume, ≥0.62 同 session continue, ≥0.42 兜底

## 知识库领域

两个领域: `furniture` (家具 — 沙发/床/餐桌/衣柜等 15+ 品类) 和 `robot_vacuum` (扫地机器人 — 滚刷/边刷/尘盒/拖布/基站等)。路由时自动识别 domain, 检索时可按 domain 过滤。
