# 后端模块边界

后端采用模块化单体结构。服务入口、业务模块、查询与导入 Graph 已归入正式目录；
安全和 Agent 运行时能力由平台层统一提供，旧路径仅承担迁移期兼容。

## 目录职责

| 目录 | 职责 |
|---|---|
| `app/apps` | 可独立部署的 FastAPI 服务入口，只负责组合应用 |
| `app/modules` | 按业务能力组织的模块，不按数据库或框架组织 |
| `app/platform` | 配置、安全、可观测性、运行时和外部集成基础设施 |
| `app/workers` | 后台任务入口 |
| `app/shared` | 没有明确业务归属的少量通用类型和工具 |

当前正式模块：

- `app/modules/analytics`：问答运营统计、历史投影和解决结果汇总。
- `app/modules/knowledge`：文档生命周期、可信度、版本适用范围、仓储协议和实现。
- `app/modules/ingestion`：导入 HTTP API、知识治理路由、LangGraph、共享状态和导入节点。
- `app/modules/qa`：问答执行 API、会话接口、反馈统计路由、LangGraph、共享状态和问答节点。
- `app/modules/workflow`：厂商无关的人工复核工单、事件、订阅和投递协议。

当前平台能力：

- `app/platform/security`：认证、HTTP 安全、上传校验和租户隔离。
- `app/platform/runtime`：运行记录、任务租约和 LangGraph 检查点。
- `app/platform/config`：模型、存储、解析器和 RAG 调优配置。
- `app/platform/observability`：Langfuse 链路、Prometheus 指标和 RAG 质量分析。
- `app/platform/ai`：聊天与视觉模型客户端、Embedding 和 Reranker 工厂。
- `app/platform/storage`：MinIO 对象存储连接和对象引用解析。
- `app/platform/vector_store`：Milvus 客户端、混合检索请求和结果归一化。

`app/shared/paths.py` 只负责项目路径发现。日志、Prompt、SSE、任务进度和向量辅助函数均已归入对应平台或业务目录，不再新增笼统的 `core`、`utils` 文件。

## 依赖规则

1. `app/apps` 可以组合模块和平台能力，但不承载业务规则。
2. 模块之间不得导入对方的数据库实现，应通过公开服务或协议协作。
3. FastAPI 路由和 LangGraph 节点只做输入输出及流程编排。
4. MongoDB、Milvus、MinIO 和模型 SDK 不得进入领域规则文件。
5. 新代码不得继续放入通用 `clients` 或 `utils` 目录，应放回拥有该能力的模块。
6. 跨模块调用优先依赖 `application` 公开接口，不直接依赖其他模块的 `infrastructure`。

## 迁移兼容

以下旧路径暂时保留兼容导入，现有部署和第三方代码不会立即失效：

- `app.workflow.*`
- `app.query_process.analytics`
- `app.knowledge_trust`
- `app.clients.*`
- `app.query_process.version_context`
- `app.query_process.api.query_service`
- `app.import_process.api.file_import_service`
- `app.import_process.agent.main_graph`、`app.import_process.agent.state` 和 `app.import_process.page_attribution`
- `app.tasks.image_enrichment_worker`
- `app.query_process.agent.main_graph` 和 `app.query_process.agent.state`
- `app.security.*`
- `app.runtime.*`
- `app.conf.*`
- `app.observability.*`
- `app.lm.*`
- `app.model.*`
- `app.core.*`
- `app.utils.*`

新代码必须使用 `app.modules.*`、`app.apps.*`、`app.platform.*` 或 `app.workers.*`。旧兼容路径将在外部调用方完成迁移后删除。

`app/modules/qa/infrastructure/history_legacy.py` 是当前无人引用的旧聊天历史实现，仅为确认外部调用方后安全删除而保留；新代码不得使用。

服务统一从以下入口启动：

```text
app.apps.import_api:app
app.apps.query_api:app
app.apps.workflow_api:app
```
