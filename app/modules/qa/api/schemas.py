"""HTTP request schemas for the question-answering API."""

from typing import Literal

from pydantic import BaseModel, Field


class QueryRequest(BaseModel):
    query: str = Field(default="", max_length=10000, description="查询内容；上传图片时可以留空")
    session_id: str | None = Field(None, description="会话ID")
    is_stream: bool = Field(False, description="是否流式返回")
    image_refs: list[str] = Field(default_factory=list, description="当前租户和会话已上传的图片对象引用")
    version_scope_id: str = Field(default="", max_length=64, description="用户从上一轮澄清中选择的版本范围")
    reset_version_context: bool = Field(default=False, description="忽略会话锁定版本并重新选择适用范围")


class FeedbackRequest(BaseModel):
    trace_id: str = Field(..., min_length=32, max_length=32, description="Langfuse Trace ID")
    value: Literal[0, 1] = Field(..., description="1表示点赞，0表示点踩")
    comment: str | None = Field(default=None, max_length=500, description="用户反馈说明")


class ResolutionRequest(BaseModel):
    trace_id: str = Field(..., min_length=32, max_length=32, description="问答 Trace ID")
    status: Literal["solved", "partial", "unsolved"]
    comment: str | None = Field(default=None, max_length=500)
