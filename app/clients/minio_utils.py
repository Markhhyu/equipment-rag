# 导入Python内置模块
import os
import json
from datetime import timedelta
from threading import Lock
from time import monotonic
from urllib.parse import quote, unquote, urlparse
# 导入MinIO官方Python SDK核心类
from minio import Minio
from minio.error import S3Error
# 项目内部配置与日志
from app.conf.minio_config import minio_config
from app.core.logger import logger
from app.security.config import load_security_config

# 全局MinIO客户端对象，初始化后供全项目调用
minio_client = None
minio_public_client = None
_minio_lock = Lock()
_retry_after = 0.0


def _initialize_minio_clients():
    global minio_client, minio_public_client, _retry_after
    if minio_client is not None:
        return minio_client
    if monotonic() < _retry_after:
        return None

    with _minio_lock:
        if minio_client is not None:
            return minio_client
        try:
            private_client = Minio(
                endpoint=minio_config.endpoint,
                access_key=minio_config.access_key,
                secret_key=minio_config.secret_key,
                secure=minio_config.minio_secure,
            )
            public_client = Minio(
                endpoint=minio_config.public_endpoint,
                access_key=minio_config.access_key,
                secret_key=minio_config.secret_key,
                secure=minio_config.minio_secure,
            )
            bucket_name = minio_config.bucket_name
            if not private_client.bucket_exists(bucket_name):
                logger.info(f"MinIO存储桶[{bucket_name}]不存在，开始创建")
                private_client.make_bucket(bucket_name)
                logger.info(f"MinIO存储桶[{bucket_name}]创建成功")

            security_config = load_security_config()
            if security_config.minio_public_read:
                bucket_policy = {
                    "Version": "2012-10-17",
                    "Statement": [{
                        "Effect": "Allow",
                        "Principal": {"AWS": ["*"]},
                        "Action": ["s3:GetObject"],
                        "Resource": [f"arn:aws:s3:::{bucket_name}/*"],
                    }],
                }
                private_client.set_bucket_policy(bucket_name, json.dumps(bucket_policy))
                logger.warning(f"MinIO存储桶[{bucket_name}]已启用匿名只读访问")
            else:
                try:
                    private_client.delete_bucket_policy(bucket_name)
                except S3Error as exc:
                    if exc.code != "NoSuchBucketPolicy":
                        raise
                logger.info(f"MinIO存储桶[{bucket_name}]保持私有，浏览器访问使用短期签名URL")

            minio_client = private_client
            minio_public_client = public_client
            _retry_after = 0.0
        except Exception as exc:
            logger.error(f"MinIO客户端初始化失败，错误信息：{str(exc)}")
            minio_client = None
            minio_public_client = None
            _retry_after = monotonic() + 5
        return minio_client


def get_minio_client():
    """
    获取全局初始化的MinIO客户端实例
    :return: 已初始化的Minio对象 / None（初始化失败时）
    """
    return _initialize_minio_clients()


def minio_object_uri(bucket_name: str, object_name: str) -> str:
    return f"minio://{bucket_name}/{quote(object_name.lstrip('/'), safe='/')}"


def resolve_object_url(value: str) -> str:
    if not value.startswith("minio://"):
        return value
    parsed = urlparse(value)
    bucket_name = parsed.netloc
    object_name = unquote(parsed.path.lstrip("/"))
    if not bucket_name or not object_name:
        return value

    security_config = load_security_config()
    if security_config.minio_public_read:
        protocol = "https" if minio_config.minio_secure else "http"
        return f"{protocol}://{minio_config.public_endpoint}/{bucket_name}/{object_name}"
    if minio_public_client is None:
        _initialize_minio_clients()
    if minio_public_client is None:
        logger.warning("无法为MinIO对象生成签名URL：公开端点客户端未初始化")
        return value
    return minio_public_client.presigned_get_object(
        bucket_name,
        object_name,
        expires=timedelta(seconds=security_config.minio_presigned_url_ttl_seconds),
    )


def resolve_object_urls(values: list[str]) -> list[str]:
    return [resolve_object_url(value) for value in values]


def reset_minio_clients_for_tests() -> None:
    global minio_client, minio_public_client, _retry_after
    with _minio_lock:
        minio_client = None
        minio_public_client = None
        _retry_after = 0.0
