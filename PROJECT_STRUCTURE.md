# 项目文件结构说明

当前工作区主要分成三块：

## 1. `Agent/`：家具智能客服主项目

这是你的核心项目目录。

常用入口：

- `Agent/app.py`：应用入口之一。
- `Agent/api.py`：接口服务入口之一。
- `Agent/agent/`：Agent、多轮任务、LangGraph、多 Agent、记忆、路由等核心逻辑。
- `Agent/rag/`：RAG 检索、向量库、重排序、文档解析等逻辑。
- `Agent/db/`：MySQL、Redis、Chroma、Outbox、任务/会话/记忆仓储。
- `Agent/workers/`：异步索引 Worker。
- `Agent/config/`：Redis、MySQL、Chroma、RAG、Agent 配置。
- `Agent/data/`：家具和扫地机器人知识库资料。
- `Agent/tests/`：测试用例。
- `Agent/evaluation/`：评测集和评测脚本。
- `Agent/scripts/`：项目脚本，包括简历/手册生成脚本。

文档资料：

- `Agent/docs/interview-handbook/`：项目理解与面试手册相关资料。

## 2. `tmp/`：临时资料和对比项目

- `tmp/super_biz_agent_py_release_compare/`：之前用于对比的另一个 Agent 项目解压目录。
- `tmp/docx_render_attempts/`：Word 文档渲染检查的临时目录。

这里不是主项目源码，后续如果确认不需要，可以再单独归档或清理。

## 3. 根目录其他文件

- `tools/`：文档生成、DOCX 检查和题库修订脚本，主要用于维护面试手册，不属于运行时业务代码。
- `python314.exe`：根目录下的 Python 可执行文件，目前未移动，避免影响已有环境。
- `.agents/`、`.claude/`、`.git/`：工具或版本管理相关目录，不建议手动改。

## 整理原则

1. 不随便移动源码、配置、数据和测试，避免项目跑不起来。
2. 文档产物集中到 `Agent/docs/`。
3. 临时渲染、对比项目集中到 `tmp/`。
4. 如果要继续清理，建议下一步先确认哪些临时目录可以删除，例如 `.pytest_cache`、`__pycache__`、空渲染目录等。
