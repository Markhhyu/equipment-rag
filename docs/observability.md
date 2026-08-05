# RAG 可观测、评测与调优指南

本文面向刚接触 Agent 和 RAG 的开发者，说明项目为什么同时使用 Langfuse、Prometheus/Grafana 和黄金评测集，以及看到异常指标后应该检查哪里。

## 1. 三类工具分别解决什么问题

| 层级 | 工具 | 回答的问题 |
|---|---|---|
| 单次业务链路 | Langfuse | 这一次文件或问答经过了哪些节点？哪个节点慢？检索到了什么？模型用了多少 Token？ |
| 长期运行趋势 | Prometheus + Grafana | 最近错误率是否升高？P95延迟是否恶化？哪个节点持续变慢？导入完整率是否下降？ |
| 问答业务结果 | 运营看板 | 每天有多少问答？用户确认解决多少？未解决、待确认和人工复核分别有多少？ |
| 实际效果验证 | 黄金数据集评测 | 调整Chunk、TopK或权重以后，正确Chunk召回率和答案质量到底提高还是下降？ |

这三层不能互相替代：Trace正常不等于答案正确；Grafana全绿也不等于检索有效；单次人工感觉不错也不能证明整体效果稳定。

运营看板位于`http://localhost:8001/analytics.html`。其中“技术成功”只表示Agent正常完成，
“确认解决”来自用户独立提交的解决结果；点赞/点踩不会被自动换算成问题是否解决。

## 2. 文件导入链路观察什么

一次导入会在Langfuse中产生以下节点：

```text
node_entry
  → node_pdf_to_md（PDF才执行）
  → node_md_img
  → node_document_split
  → node_item_name_recognition
  → node_bge_embedding
  → node_import_milvus
```

最终会计算以下确定性指标：

| 指标 | 含义 | 常见异常原因 |
|---|---|---|
| `markdown_chars` | 解析得到的Markdown字符数 | MinerU返回包异常、OCR失败或读错文件 |
| `replacement_character_count` | Unicode乱码替换符数量 | 源文件编码、字体映射或OCR语言错误 |
| `healthy_length_ratio` | 长度位于健康区间的Chunk比例 | Chunk上下限不合适、标题结构异常 |
| `duplicate_ratio` | 内容完全重复的Chunk比例 | 重复标题继承、重复导入或切片逻辑错误 |
| `embedding_success_ratio` | 同时生成Dense和Sparse向量的比例 | 模型批处理异常、内存不足或输出不完整 |
| `milvus_storage_ratio` | 成功获得Milvus主键的Chunk比例 | insert_count不匹配、Schema或连接异常 |
| `item_name_coverage_ratio` | 拥有设备名称的Chunk比例 | 识别Prompt不稳定、文件名缺少型号 |

`import_quality_proxy`只是异常检测代理分，不是人工正确率。它适合发现“昨天都是0.95，今天突然变成0.4”，但不能证明表格内容或操作步骤一定解析正确。

## 3. 问答链路观察什么

一次问答会记录设备确认、普通检索、HyDE检索、联网搜索、RRF、Rerank和答案生成。

重点关注：

- 普通检索与HyDE各自返回多少条；
- 两路结果是否有合理重合；
- RRF和Rerank以后还剩多少条；
- Reranker Top1分数及Top1/Top2差距；
- 最终上下文长度、答案长度和引用数量；
- 用户点赞/点踩以及点踩说明；
- 每个节点耗时、错误状态和LLM Token。

不同Reranker模型的原始分数范围可能完全不同，因此不能把BGE的0.5和Qwen的0.5直接解释成同一种相关性。

## 4. 启动观测组件

### 4.1 推荐：使用仓库统一脚本

Windows 下直接从仓库根目录执行：

```powershell
.\start-all.ps1
```

默认会同时启动业务服务、Langfuse、Prometheus、Grafana 和 Attu，并在每一步执行配置校验和健康检查。
首次运行时，脚本会自动生成 `deploy/langfuse/.env` 中的本地部署密钥；真实模型 API Key 和稍后创建的
Langfuse 项目 API Key 仍应填写在根目录 `.env`。一键暂停使用：

```powershell
.\stop-all.ps1
```

暂停不会删除 MongoDB、Milvus、MinIO、Langfuse、Prometheus 或 Grafana 的命名卷数据。

### 4.2 Langfuse

按`README.md`中的Langfuse部署步骤创建项目和API Key，然后在主项目`.env`配置：

```env
LANGFUSE_TRACING_ENABLED=true
LANGFUSE_HOST=http://host.docker.internal:3000
LANGFUSE_PUBLIC_KEY=pk-lf-替换为真实值
LANGFUSE_SECRET_KEY=sk-lf-替换为真实值
```

### 4.3 Prometheus和Grafana

```powershell
docker compose --profile observability up -d
docker compose --profile observability ps
```

访问地址：

- Prometheus：`http://localhost:9090`
- Grafana：`http://localhost:3001`
- 导入API原始指标：`http://localhost:8000/metrics`
- 查询API原始指标：`http://localhost:8001/metrics`

Grafana首次登录使用`.env`里的`GRAFANA_ADMIN_USER`和`GRAFANA_ADMIN_PASSWORD`。进入`Equipment RAG`文件夹即可看到预置仪表盘。

## 5. 建立真正有意义的黄金数据集

示例数据只能验证评测程序能运行，不能代表你的设备业务。建议从真实需求中挑选至少30~100个问题，并由熟悉设备的人员标注：

1. 用户原始问法；
2. 必须出现的关键事实；
3. 禁止出现的危险或错误说法；
4. 正确来源Chunk ID；
5. 是否应该要求用户补充型号；
6. 是否必须提供引用；
7. 可以接受的最大延迟。

不要把客户文档、个人信息、生产Trace或密钥提交到公开仓库。公开仓库只能放合成数据或已获授权的数据。

运行离线回放：

```powershell
uv run python -m app.evaluation.cli replay `
  --predictions evals/fixtures/smoke_predictions.jsonl `
  --fail-on-threshold
```

对正在运行的API做真实回归：

```powershell
uv run python -m app.evaluation.cli api `
  --base-url http://127.0.0.1:8001 `
  --fail-on-threshold
```

如果开启了API Key，需要同时传入评测专用Key；不要在命令历史或报告中写入密钥。

当前CI默认使用Recall、Precision、MRR、关键词、安全词和引用等确定性指标，原因是结果可重复、
不产生额外模型费用，也不会因“裁判模型”波动导致同一提交一会儿通过、一会儿失败。
当你已经建立真实黄金集后，可以在Langfuse中增加LLM-as-a-Judge，或单独接入Ragas/DeepEval，
评估Faithfulness、Answer Relevancy和Context Relevancy；这类模型评分应作为补充，不能替代人工标注来源。

## 6. 正确的调优顺序

一次只改变一个变量，并记录实验名称：

1. 先固定一版文档和黄金问题，运行基线评测；
2. 优先修复解析空内容、乱码、重复Chunk和入库不完整；
3. 再调整Chunk最大/最小长度；
4. 检索召回率不足时调整候选数、Dense/Sparse权重；
5. 普通检索稳定后再判断HyDE是否带来增益；
6. 最后调整RRF权重和Rerank动态TopK；
7. 对比质量、P95延迟和Token成本，确认综合收益后再保留新配置。

不要根据单个问题或单次Trace修改全局参数。设备RAG通常存在型号查询、错误码查询、操作步骤和安全规范等不同类型，某个参数可能改善一种问题却损害另一种问题。

## 7. 常见指标与处理建议

| 现象 | 优先检查 |
|---|---|
| Markdown字符数为0 | MinerU任务、下载包、`md_path`和OCR语言 |
| 短Chunk比例很高 | 提高`RAG_CHUNK_MIN_CHARS`或优化同标题合并 |
| 召回数量正常但黄金Recall低 | 设备名称过滤、Chunk内容、Dense/Sparse权重 |
| HyDE与普通检索完全不重合 | HyDE Prompt是否偏题，必要时降低HyDE RRF权重 |
| Rerank后只有一条证据 | 断崖阈值是否过激、候选数量是否过少 |
| 答案没有引用 | 答案Prompt和引用格式，检查Chunk ID是否传入 |
| P95突然升高 | Grafana节点耗时；再到Langfuse查看具体慢Trace |
| 点踩增加但系统指标正常 | 读取点踩说明，补充黄金用例并检查事实正确性 |

## 8. 数据安全

- Prometheus标签不包含用户问题、答案、文件名、设备名、Trace ID或Session ID；
- Langfuse会记录业务输入输出，生产部署应使用可信的自托管实例并设置访问权限；
- 不要把完整向量写入Trace；当前代码只记录向量维度和Sparse非零数量；
- 评测数据和报告可能包含业务内容，生产环境应放在受控存储中。
