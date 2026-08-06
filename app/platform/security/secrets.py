"""Encryption for connector credentials persisted outside environment variables."""

from __future__ import annotations

import os
import threading
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from app.shared.paths import PROJECT_ROOT


class SecretEncryptionError(RuntimeError):
    pass


class SecretCipher:
    def __init__(self, key: str | bytes) -> None:
        try:
            self._fernet = Fernet(key.encode("ascii") if isinstance(key, str) else key)
        except (TypeError, ValueError) as exc:
            raise SecretEncryptionError("工作流配置加密密钥格式无效") from exc

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str) -> str:
        try:
            return self._fernet.decrypt(value.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError) as exc:
            raise SecretEncryptionError("无法解密工作流连接器密钥，请检查主加密密钥是否发生变化") from exc


_cipher: SecretCipher | None = None
_cipher_lock = threading.RLock()


def _development_key_path() -> Path:
    configured = str(os.getenv("WORKFLOW_CONFIG_KEY_FILE") or "").strip()
    return Path(configured).expanduser().resolve() if configured else PROJECT_ROOT / "output" / "workflow-config.key"


def _load_or_create_development_key() -> bytes:
    path = _development_key_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        return path.read_bytes().strip()
    except FileNotFoundError:
        key = Fernet.generate_key()
        try:
            with path.open("xb") as stream:
                stream.write(key)
            try:
                path.chmod(0o600)
            except OSError:
                pass
            return key
        except FileExistsError:
            return path.read_bytes().strip()


def get_workflow_secret_cipher() -> SecretCipher:
    global _cipher
    if _cipher is not None:
        return _cipher
    with _cipher_lock:
        if _cipher is None:
            configured_key = str(os.getenv("WORKFLOW_CONFIG_ENCRYPTION_KEY") or "").strip()
            environment = str(os.getenv("APP_ENVIRONMENT") or "development").strip().lower()
            if configured_key:
                key: str | bytes = configured_key
            elif environment in {"production", "prod"}:
                raise SecretEncryptionError("生产环境必须配置 WORKFLOW_CONFIG_ENCRYPTION_KEY")
            else:
                key = _load_or_create_development_key()
            _cipher = SecretCipher(key)
        return _cipher


def reset_workflow_secret_cipher_for_tests() -> None:
    global _cipher
    with _cipher_lock:
        _cipher = None
