"""MinIO connection and object storage configuration."""

import os
from dataclasses import dataclass

from dotenv import load_dotenv


# MinIO 配置既可能来自本地 .env，也可能由 Compose/集群环境直接注入。
load_dotenv()


@dataclass
class MinIOConfig:
    """MinIO 内部连接和浏览器外部访问配置。"""

    endpoint: str  # SDK 连接地址，格式为 host:port，不包含 http:// 或 https://。
    public_endpoint: str  # 生成给浏览器的地址，不能使用仅容器可解析的服务名 minio。
    access_key: str  # 部署者自定义的 MinIO 用户名，不是第三方平台 Token。
    secret_key: str  # 与 Access Key 配套的密码，生产环境必须使用长随机值。
    bucket_name: str  # 保存知识库文件和图片的桶；应用会按需创建。
    minio_img_dir: str  # 桶内图片对象前缀，不是宿主机目录。
    minio_secure: bool  # true 使用 HTTPS，必须与服务实际证书和端口一致。
    region: str  # 签名URL使用的区域；明确设置后无需从浏览器公开端点查询桶位置。


# 内部地址用于后端上传，公开地址用于把可访问图片 URL 返回给浏览器。
minio_config = MinIOConfig(
    endpoint=os.getenv("MINIO_ENDPOINT") or "127.0.0.1:9000",
    public_endpoint=os.getenv("MINIO_PUBLIC_ENDPOINT") or os.getenv("MINIO_ENDPOINT") or "127.0.0.1:9000",
    access_key=os.getenv("MINIO_ACCESS_KEY") or "minioadmin",
    secret_key=os.getenv("MINIO_SECRET_KEY") or "minioadmin",
    bucket_name=os.getenv("MINIO_BUCKET_NAME") or "equipment-rag",
    minio_img_dir=os.getenv("MINIO_IMG_DIR") or "images",
    minio_secure=(os.getenv("MINIO_SECURE") or "false").lower() == "true",
    region=(os.getenv("MINIO_REGION") or "us-east-1").strip(),
)
