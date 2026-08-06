# 飞书审批连接器

飞书连接器只负责把本地标准工单转换成飞书审批实例。问答、工单状态机和知识库治理不依赖飞书字段；
后续接入企微、钉钉或公司 OA 时，实现相同的 `WorkflowConnector` 边界即可。

## 当前能力

1. 用户从未解决问答创建人工工单。
2. `workflow-api` 先持久化本地工单，再调用已启用的外部连接器。
3. 飞书连接器获取并缓存 `tenant_access_token`，发起指定审批定义。
4. 本地工单保存通用的 `connector_type` 和飞书审批实例编号。
5. `case_id` 同时作为飞书 `uuid`，网络失败后使用原请求重试不会创建新的本地工单。

本阶段只实现“发起审批”。审批完成事件、处理结果回写和知识库候选生成将在后续同步连接器中实现。

## 标准处理结果契约

飞书、企微、钉钉或其他 OA 的同步连接器不直接修改知识库。连接器先把厂商表单字段转换为平台标准结果，
再调用 `POST /workflow/cases/{case_id}/actions`：

```json
{
  "action": "resolve",
  "result": {
    "root_cause": "接线端子松动",
    "solution": "重新紧固端子",
    "verification": "连续运行两小时无报警"
  },
  "knowledge_decision": "include",
  "idempotency_key": "oa-instance-code:resolved"
}
```

`knowledge_decision` 只允许在 `resolve` 动作中使用：`include` 表示进入知识候选，`exclude` 表示不沉淀。
选择 `include` 时必须同时提供 `solution` 和 `verification`，避免只有“流程已结束”却没有可复用处理经验。
完成后发出的 `review.resolved` 标准事件会携带工单状态、处理结果和知识沉淀决定，后续知识候选消费者无需理解飞书字段。

## 飞书侧准备

- 企业自建应用已经安装到审批所在租户，并发布了包含审批 API 权限的版本。
- “设备问题处理”审批已启用，流程节点能够找到有效审批人。
- 连接器使用的发起人是租户内有效用户。
- API 自动填写的初始字段不能要求空的处理结果字段在提交时必填。

审批定义编码可以从飞书审批定义详情取得。连接信息不绑定固定项目字段；后续增加多个审批表单时，
审批模板、业务路由和该模板的数据装配规则应作为独立配置维护。

## 页面配置（推荐）

1. 从统一入口 <http://127.0.0.1:8080/apps> 打开“人工处理”（兼容地址为 <http://127.0.0.1:8002/workflow.html>）。
2. 点击右上角“飞书设置”。
3. 填写 App ID、App Secret、审批定义 Code 和发起人 ID。
4. 打开“启用”开关，点击“保存并测试”。

保存后配置按当前租户写入 MongoDB。`APP_SECRET` 使用 Fernet 加密，页面和查询接口只显示是否已经配置，
不会返回原文。开发环境会在 `output/workflow-config.key` 自动生成本机密钥；该目录已被 Git 忽略。
生产环境必须配置稳定的主密钥：

```powershell
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

把输出写入部署环境的 `WORKFLOW_CONFIG_ENCRYPTION_KEY`，不要提交到 Git。密钥丢失或变更后，已保存的
`APP_SECRET` 无法恢复，只能清除连接器配置并重新填写。

页面配置保存后立即生效，无需重启 `workflow-api`。只有更新后端代码或部署级主密钥时才需要重启。

## 环境变量配置（兼容）

没有数据库配置时，仍可把真实值写入根目录 `.env`。数据库中的页面配置优先级更高。
`FEISHU_APPROVAL_FORM_FIELDS_JSON` 仅用于兼容当前单表单的数据预填，不在连接设置页面中维护：

```env
FEISHU_WORKFLOW_ENABLED=true
FEISHU_APP_ID=cli_xxxxxxxxxxxxx
FEISHU_APP_SECRET=替换为真实密钥
FEISHU_APPROVAL_CODE=替换为设备问题处理审批编码
FEISHU_APPROVAL_INITIATOR_ID=ou_xxxxxxxxxxxxx
FEISHU_APPROVAL_USER_ID_TYPE=open_id
FEISHU_APPROVAL_FORM_FIELDS_JSON={"case_id":{"id":"控件ID-工单编号","type":"input"},"subject.question":{"id":"控件ID-问题描述","type":"textarea"},"subject.device_models":{"id":"控件ID-设备型号","type":"input"},"subject.version_labels":{"id":"控件ID-版本信息","type":"input"},"context.answer":{"id":"控件ID-助手原回答","type":"textarea"},"context.review_reason":{"id":"控件ID-转人工原因","type":"textarea"},"subject.trace_id":{"id":"控件ID-Trace","type":"input"}}
```

映射左侧是本项目稳定字段路径，右侧完全属于飞书适配器：

| 字段路径 | 当前来源 | 推荐飞书控件类型 |
|---|---|---|
| `case_id` | 本地工单编号 | `input` |
| `subject.question` | 用户问题 | `textarea` |
| `subject.device_models` | 设备型号列表 | `input` |
| `subject.version_labels` | 关联知识版本 | `input` |
| `subject.trace_id` | 问答 Trace | `input` |
| `context.answer` | 助手原回答 | `textarea` |
| `context.review_reason` | 转人工原因 | `textarea` |
| `context.resolution_status` | 发起时解决状态 | `input` |

不存在或为空的可选字段不会发送。数组会用顿号连接，复杂对象会序列化为 JSON。可在控件配置中增加
`max_length` 限制发送长度，例如 `{"id":"控件ID","type":"textarea","max_length":1000}`；完整上下文仍保存在本地工单。

修改 `.env` 后重启 `workflow-api`。使用 Docker Compose 时执行：

```powershell
docker compose up -d --build workflow-api
```

然后在聊天页面把某条回答标记为“未解决”并点击“发起处理”。成功后，工单详情的
`external_workflows` 会保存 `feishu_approval` 和审批实例编号。失败时接口返回本地 `case_id` 和飞书安全错误摘要，
再次点击会重试外部触发，不会重复创建本地工单。
