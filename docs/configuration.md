# 配置与密钥获取指南

本文面向第一次接触 Agent 项目的开发者，解释 `.env` 中的连接地址、Token、API Key 和密码应该从哪里获得、怎么填写，以及这些配置分别影响什么。

## 1. 创建本地配置

Windows PowerShell：

```powershell
Copy-Item .env.example .env
notepad .env
```

修改后先验证 Compose 能否读取：

```powershell
docker compose config --quiet
docker compose --env-file .env config --quiet
```

`.env` 已在 `.gitignore` 中忽略。不要使用 `git add -f .env`，也不要把真实密钥粘贴到 Issue、PR、日志或截图中。

## 2. 先分清三类凭据

| 类型 | 配置项 | 谁来生成 |
|---|---|---|
| 外部模型服务密钥 | `OPENAI_API_KEY`、`MCP_DASHSCOPE_API_KEY` | OpenAI、阿里云百炼等服务商控制台 |
| 可观测性/数据库凭据 | `LANGFUSE_*`、`NEO4J_*` | 对应平台或自部署服务 |
| 本项目自定义凭据 | `AUTH_API_KEYS_JSON`、MongoDB/MinIO 密码 | 项目部署者自己生成 |

`OPENAI_API_KEY` 用于“本项目调用大模型”；`AUTH_API_KEYS_JSON` 用于“用户调用本项目 API”。两者用途完全不同，不能互换。

## 3. 配置大模型

### OpenAI 官方

1. 登录 [OpenAI API Key 页面](https://platform.openai.com/api-keys)。
2. 创建 Project API Key，并立即保存完整密钥；完整值通常只在创建时显示。
3. 在 `.env` 中填写：

```env
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_API_KEY=你的ProjectAPIKey
LLM_DEFAULT_MODEL=gpt-4.1-mini
VL_MODEL=gpt-4.1-mini
```

ChatGPT Plus/Pro 与 API 是不同的产品计费入口，订阅 ChatGPT 不代表 API 账户一定有可用额度。生产环境建议为密钥设置最小必要权限、费用限制和轮换策略。

### 阿里云百炼

1. 登录[百炼 API Key 页面](https://help.aliyun.com/zh/model-studio/get-api-key)。
2. 选择实际使用地域和业务空间，创建 API Key。
3. 创建成功时立即保存完整 Key 和 API Host。新版 Key 的明文只显示一次。
4. `OPENAI_BASE_URL` 必须填写同地域的 OpenAI 兼容 API Host，不要填写 Anthropic 兼容地址。
5. `LLM_DEFAULT_MODEL` 和 `VL_MODEL` 填写该业务空间有权限调用的模型 ID。

示意：

```env
OPENAI_BASE_URL=创建密钥时显示的OpenAI兼容APIHost
OPENAI_API_KEY=sk-ws-替换为真实百炼密钥
LLM_DEFAULT_MODEL=替换为对话模型ID
VL_MODEL=替换为视觉模型ID
```

不同地域的 API Host 可能不同，不要盲目复制其他人的地址。

### 其他 OpenAI 兼容服务

需要同时确认：

1. 服务商明确支持 OpenAI Chat Completions 兼容协议；
2. `base_url` 是否需要 `/v1`；
3. 模型 ID 是否支持对话、图片和流式 Token 用量；
4. API Key 是否已开通模型权限、余额和 IP 白名单。

如果接口不支持流式用量统计：

```env
LLM_STREAM_USAGE=false
```

## 4. 配置本项目 API Key

本项目在 `AUTH_MODE=api_key` 时读取请求头：

```text
X-API-Key: 你生成的应用密钥
```

PowerShell 生成随机密钥：

```powershell
$bytes = New-Object byte[] 32
[Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
$appKey = [Convert]::ToBase64String($bytes)
$appKey
```

单租户管理员示例：

```env
APP_ENVIRONMENT=production
AUTH_MODE=api_key
AUTH_API_KEYS_JSON=[{"id":"factory-a-admin","key":"替换为上面生成的密钥","tenant_id":"factory-a","roles":["admin"]}]
```

角色说明：

- `admin`：拥有当前全部接口和管理页面权限；
- `import`：可以上传文档、查看导入任务和触发重试；
- `query`：可以问答、读取会话历史和提交反馈。
- `workflow`：可以查看和处理人工复核工单。

各前端页面会先请求同源的 `/auth/me`，根据返回的租户和角色决定是否进入模块，并过滤应用中心入口。
直接访问没有角色权限的页面会显示拒绝访问，但真正的授权仍由后端接口执行。

独立线上部署也可以启用邮箱账号：

```env
APP_ENVIRONMENT=production
AUTH_MODE=password
AUTH_REGISTRATION_ENABLED=true
AUTH_REGISTRATION_TENANT_ID=public
AUTH_EMAIL_VERIFICATION_REQUIRED=true
AUTH_EMAIL_VERIFICATION_TTL_SECONDS=1800
AUTH_PUBLIC_BASE_URL=https://agent.example.com
AUTH_SESSION_TTL_SECONDS=604800
AUTH_COOKIE_SECURE=true
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USERNAME=你的SMTP账号
SMTP_PASSWORD=你的SMTP密码或授权码
SMTP_FROM_ADDRESS=noreply@example.com
SMTP_SECURITY=starttls
```

公开注册用户固定获得 `query` 角色，不能从注册请求指定角色或租户。浏览器使用 HttpOnly 会话 Cookie，
已配置的 API Key 仍可供自动任务使用。生产环境开放注册时强制开启邮箱验证；`AUTH_PUBLIC_BASE_URL` 必须是用户能访问的
HTTPS 前端地址。`SMTP_PASSWORD` 只能写入本地 `.env` 或部署平台的密钥管理服务，不得提交。邮箱验证不等于机器人防护，
公网部署仍应在网关配置限流、验证码或 WAF 规则。本地受控开发可以保持 `AUTH_EMAIL_VERIFICATION_REQUIRED=false`。

### GitHub 快速登录

代码仓库托管在 GitHub 并不会自动开通 GitHub 登录。每个部署环境需要创建自己的 OAuth App：

1. 进入 GitHub `Settings -> Developer settings -> OAuth Apps -> New OAuth App`。
2. `Homepage URL` 填项目前端地址，例如 `https://agent.example.com`。
3. `Authorization callback URL` 填 `https://agent.example.com/auth/oauth/github/callback`。
4. 创建 Client Secret，将凭据写入本地 `.env` 或部署平台密钥管理。

```env
AUTH_MODE=password
AUTH_PUBLIC_BASE_URL=https://agent.example.com
AUTH_COOKIE_SECURE=true
AUTH_GITHUB_OAUTH_ENABLED=true
AUTH_GITHUB_CLIENT_ID=你的GitHubClientID
AUTH_GITHUB_CLIENT_SECRET=你的GitHubClientSecret
AUTH_GITHUB_OAUTH_TIMEOUT_SECONDS=10
```

本地回调可使用 `http://127.0.0.1:8080/auth/oauth/github/callback`。项目只接受 GitHub 已验证邮箱；首次登录固定创建
`query` 角色用户。如果已存在相同邮箱的本地账号，会关联到原账号并保留原租户和角色。`AUTH_GITHUB_CLIENT_SECRET`
不得提交到 GitHub。

调用示例：

```powershell
$headers = @{ "X-API-Key" = "替换为应用密钥" }
Invoke-RestMethod http://127.0.0.1:8001/health
Invoke-RestMethod http://127.0.0.1:8001/history/demo -Headers $headers
```

`tenant_id` 会进入会话 ID、对象存储路径和向量检索过滤条件。不同企业、工厂或客户必须使用不同租户 ID。

## 5. Docker 地址为什么不能都写 localhost

容器里的 `localhost` 指当前容器自己，不是宿主机，也不是其他容器。

| 调用方向 | 正确地址 |
|---|---|
| 应用容器 → MongoDB 容器 | `mongo:27017` |
| 应用容器 → MinIO 容器 | `minio:9000` |
| 应用容器 → Milvus 容器 | `milvus:19530` |
| 应用容器 → 宿主机 MinerU | `host.docker.internal:8002` |
| 浏览器 → MinIO | `localhost:9000` 或公开域名 |

所以 Docker 默认配置是：

```env
MONGO_URL=mongodb://equipment:equipment-local@mongo:27017/equipment_rag?authSource=admin
MINIO_ENDPOINT=minio:9000
MINIO_PUBLIC_ENDPOINT=localhost:9000
MILVUS_URL=http://milvus:19530
MINERU_API_BASE_URL=http://host.docker.internal:8002
```

直接在宿主机运行 Python 时，才将服务名改成 `127.0.0.1`。

## 6. MongoDB 用户名和密码

`MONGO_INITDB_ROOT_USERNAME` 和 `MONGO_INITDB_ROOT_PASSWORD` 是部署者自己设置的初始化管理员凭据，`MONGO_URL` 必须使用相同值。

```env
MONGO_INITDB_ROOT_USERNAME=equipment
MONGO_INITDB_ROOT_PASSWORD=替换为长随机密码
MONGO_URL=mongodb://equipment:URL编码后的密码@mongo:27017/equipment_rag?authSource=admin
```

注意：

- MongoDB 只在首次创建空数据卷时执行初始化；
- 修改 `.env` 不会自动修改已有数据库用户；
- 密码含 `@`、`:`、`/`、`?`、`#` 时需要 URL 编码；
- `docker compose down -v` 会删除数据库、向量库和对象存储数据，不能作为生产环境改密码的方法。

## 7. MinIO 账号、密码和地址

MinIO 的 Access Key 和 Secret Key 由部署者自己定义，不需要向第三方申请：

```env
MINIO_ACCESS_KEY=替换为管理账号
MINIO_SECRET_KEY=替换为长随机密码
MINIO_ENDPOINT=minio:9000
MINIO_PUBLIC_ENDPOINT=localhost:9000
```

两种地址用途不同：

- `MINIO_ENDPOINT`：后端上传文件时使用，Docker 内不能写 `localhost`；
- `MINIO_PUBLIC_ENDPOINT`：生成给浏览器的图片链接，浏览器不能访问 `minio:9000`。

生产环境应使用 HTTPS 域名：

```env
MINIO_ENDPOINT=minio.internal.example.com:443
MINIO_PUBLIC_ENDPOINT=files.example.com
MINIO_SECURE=true
MINIO_PUBLIC_READ=false
```

## 8. MinerU 地址和 Token

自部署 `mineru-api` 默认不需要 Token：

```env
MINERU_API_BASE_URL=http://host.docker.internal:8002
MINERU_API_TOKEN=
MINERU_BACKEND=pipeline
MINERU_IMAGE_ANALYSIS=false
```

`MINERU_API_TOKEN` 只在你为 MinerU 配置了 API 网关、反向代理或企业鉴权时填写，Token 由该网关管理员发放。项目会自动把它放进 Bearer Authorization 请求头。

CPU 首次启动使用 `pipeline`。只有 MinerU 环境已经正确识别 GPU 和对应模型时，才切换 `hybrid-engine` 或启用图片分析。

## 9. 百炼 WebSearch MCP

该集成是可选的，用于让 Agent 获取实时网页信息。

1. 在百炼 MCP 广场开通“联网搜索”；
2. 按[官方联网搜索 MCP 指南](https://help.aliyun.com/zh/model-studio/web-search-mcp)获取连接地址；
3. 在百炼 API Key 页面创建“通用 API Key”；
4. 配置：

```env
MCP_DASHSCOPE_BASE_URL=https://dashscope.aliyuncs.com/api/v1/mcps/WebSearch/mcp
MCP_DASHSCOPE_API_KEY=替换为百炼通用APIKey
MCP_DASHSCOPE_TRANSPORT=streamable_http
MCP_DASHSCOPE_TOOL_NAME=bailian_web_search
```

代码会自动添加 `Authorization: Bearer ...`，所以 Key 中不要手工添加 `Bearer `。旧版 `/sse` 端点可以把 transport 改为 `sse`，新项目应使用 Streamable HTTP。

## 10. Langfuse 密钥

Langfuse 用于记录 Trace、Token、评分和反馈。关闭时无需填写 Key：

```env
LANGFUSE_TRACING_ENABLED=false
```

启用步骤：

1. 启动自部署 Langfuse，或登录 Langfuse Cloud；
2. 创建项目；
3. 进入 Project Settings → API Keys；
4. 创建并复制 Public Key 与 Secret Key；
5. 填写项目容器真正能访问的 Host。

```env
LANGFUSE_TRACING_ENABLED=true
LANGFUSE_HOST=http://host.docker.internal:3000
LANGFUSE_PUBLIC_KEY=pk-lf-替换为真实值
LANGFUSE_SECRET_KEY=sk-lf-替换为真实值
```

Public Key 用于标识项目，Secret Key 用于鉴权，两者必须属于同一个 Langfuse 项目。

## 11. Neo4j 凭据

Neo4j 当前是预留集成，不影响核心 RAG 流程，可以全部留空。

使用 Neo4j Aura 时，创建实例时下载 credentials 文件，其中包含连接 URI、用户名和密码：

```env
NEO4J_URI=neo4j+s://你的实例ID.databases.neo4j.io
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=创建实例时保存的密码
```

密码遗失后不能从项目源码找回，需要在 Aura 控制台按平台能力创建新用户、重置或重建凭据。

## 12. 启动前检查清单

```powershell
# 检查最终展开的 Compose 配置语法，不会启动容器。
docker compose --env-file .env config --quiet

# 构建并后台启动。
docker compose up -d --build

# 查看容器和健康状态。
docker compose ps

# 查看核心服务日志。
docker compose logs -f import-api query-api
```

健康检查：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
Invoke-RestMethod http://127.0.0.1:8001/health
```

## 13. 常见配置错误

### `401` 或 `Invalid API key`

- 确认 Key 没有引号、空格和 `Bearer `；
- 确认 Key 与 `OPENAI_BASE_URL` 属于同一服务商和地域；
- 确认账户余额、模型权限和 IP 白名单；
- 如果是本项目接口，检查的是 `X-API-Key`，不是 `OPENAI_API_KEY`。

### 容器连接被拒绝

- 容器连接其他容器时不要使用 `localhost`；
- `docker compose ps` 确认中间件健康；
- 检查端口是否被其他软件占用；
- `MINIO_ENDPOINT` 不带协议，`MILVUS_URL` 当前示例带 `http://`。

### 修改 MongoDB 密码后仍认证失败

旧数据卷仍保存旧用户。应通过 MongoDB 管理命令修改已有用户密码，并同步更新 `MONGO_URL`，不要直接删除生产数据卷。

### 浏览器图片无法打开

检查 `MINIO_PUBLIC_ENDPOINT` 是否是浏览器可访问的地址。`minio:9000` 只在 Docker 网络内有效。
