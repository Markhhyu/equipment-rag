# 集中日志与线上排障

本项目使用 `Loguru JSON stdout -> Grafana Alloy -> Loki -> Grafana` 保存和检索应用日志。Prometheus负责数值指标，Langfuse负责单次LLM/RAG Trace，三者职责不同。

## 1. 启动与访问

```powershell
docker compose --profile observability up -d loki alloy prometheus grafana
docker compose --profile observability ps
```

默认地址：

| 服务 | 地址 | 用途 |
|---|---|---|
| Grafana | <http://127.0.0.1:3001> | 打开 `Equipment RAG 日志与排障` 看板 |
| Loki | <http://127.0.0.1:3100/ready> | 日志存储健康检查 |
| Alloy | <http://127.0.0.1:12345> | 采集组件和运行状态 |
| Prometheus | <http://127.0.0.1:9090/alerts> | 查看告警规则状态 |

首次进入Grafana使用 `.env` 中的 `GRAFANA_ADMIN_USER` 和 `GRAFANA_ADMIN_PASSWORD`。生产部署必须修改默认密码并限制这些地址只允许内网或运维入口访问。

## 2. 按关联ID排障

每个HTTP响应都包含 `X-Request-ID`。前端或调用方报错时先记录该值，然后：

1. 打开Grafana的 `Equipment RAG 日志与排障` 看板；
2. 在“关联ID或关键字”输入完整 `request_id`；
3. 从同一条JSON日志读取 `service`、`run_id`、`trace_id`、状态码和异常；
4. 有 `trace_id` 时在Langfuse查对应Trace，确认模型、检索或节点错误；
5. 有 `run_id` 时调用对应API的 `/runs/{run_id}` 查看持久化状态并判断能否重试。

也可以在Grafana Explore中直接使用LogQL：

```logql
{project="equipment-rag"} |= "完整的request_id"
{project="equipment-rag", service="query-api"} |= "完整的trace_id"
{project="equipment-rag", service="import-api"} |= "完整的run_id"
{project="equipment-rag"} |~ "(?i)(error|exception|failed|critical)"
```

`request_id`、`trace_id`、`tenant_id` 和 `run_id` 不会成为Loki标签。它们的取值数量很大，只保留在JSON内容中可避免标签爆炸。

Loki和Alloy自身日志不会回灌到Loki，以免采集链路故障形成反馈环；这两个服务通过下面的Docker兜底命令检查。

## 3. Loki不可用时

Docker自身保留有限的轮转日志，可作为采集链路故障时的兜底：

```powershell
docker compose logs --since 30m --tail 500 query-api
docker compose logs --since 30m --tail 500 import-api workflow-api
docker compose logs --tail 200 alloy loki grafana
```

先检查Loki `/ready`，再检查Alloy页面中的组件状态和Alloy容器日志。Alloy通过只读Docker socket发现容器；即使只读，该接口仍具有较高宿主机可见性，只应部署在受控节点。Kubernetes环境应替换为集群级日志采集方式。

## 4. 保留期与容量

- Loki默认保留7天，通过 `LOKI_RETENTION_PERIOD` 调整；
- Docker每个容器默认保留5个、每个最大20 MiB的本地日志文件；
- Prometheus默认保留15天指标；
- 修改保留期前应先评估日均日志量和磁盘空间；
- `loki-data` 是命名卷，普通 `docker compose down` 不会删除，`down -v` 会永久删除。

## 5. 日志安全

允许记录关联ID、服务、接口模板、状态码、耗时、异常类型和必要的资源ID。禁止记录密码、API Key、Token、Cookie、完整问题和答案、文档正文、向量内容、邮件验证码以及外部OA凭据。

生产环境保持：

```env
LOG_DIAGNOSE=false
CONTAINER_LOG_FORMAT=json
CONTAINER_LOG_FILE_ENABLE=false
```

Grafana通知渠道由部署方在受控环境配置，不在Git仓库中写死邮箱、Webhook、企微、钉钉、飞书或OA地址。
