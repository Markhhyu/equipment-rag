import os


# 单元测试不得把模拟问题、答案或Span写入真实Langfuse项目。
# conftest会在测试模块导入前执行，因此该值也会阻止SDK客户端和后台上传线程初始化；
# 应用容器仍使用compose/.env中的LANGFUSE_TRACING_ENABLED配置，不受这里影响。
os.environ["LANGFUSE_TRACING_ENABLED"] = "false"
