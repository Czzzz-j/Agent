# 家具智能客服 Agent

面向家具售前、售后、导购与保养场景的智能客服项目，基于 LangChain 与 LangGraph 构建，集成多 Agent 编排、RAG 知识检索、分层记忆、异步索引和流式对话服务。

## 项目亮点

- 多 Agent 路由：按问题类型选择知识库、报表等专业能力，并统一生成回复。
- RAG 检索：支持文本与 PDF 知识库解析、向量召回和重排序。
- 分层记忆：结合短期会话、摘要与用户长期记忆，提升连续对话体验。
- 工程化能力：包含 MySQL 持久化、Outbox 异步任务、数据库迁移、Docker Compose、测试和评估入口。

## 快速开始

```bash
cd Agent
cp .env.example .env
uv sync --frozen
make up
```

真实的 API Key 与数据库密码仅应写入本地 `.env`，不要提交到仓库。

## 项目结构

- `Agent/`：应用源码、测试、部署文件与详细文档
- `PROJECT_STRUCTURE.md`：目录结构说明

更多架构、运行和评估说明请阅读 [详细项目文档](Agent/README.md)。

## 技术栈

Python · FastAPI · Streamlit · LangChain · LangGraph · Chroma · MySQL · Redis · Docker

## License

MIT
