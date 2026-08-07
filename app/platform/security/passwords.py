from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets


_EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SALT_BYTES = 16
_KEY_BYTES = 32


def normalize_email(value: str) -> str:
    email = value.strip().lower()
    if len(email) > 254 or not _EMAIL_PATTERN.fullmatch(email):
        raise ValueError("请输入有效的邮箱地址")
    local_part, domain = email.rsplit("@", 1)
    if len(local_part) > 64 or len(domain) > 253:
        raise ValueError("请输入有效的邮箱地址")
    return email


def validate_password(password: str) -> None:
    if len(password) < 10:
        raise ValueError("密码至少需要 10 个字符")
    if len(password) > 128:
        raise ValueError("密码不能超过 128 个字符")


def hash_password(password: str) -> str:
    validate_password(password)
    salt = secrets.token_bytes(_SALT_BYTES)
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_KEY_BYTES,
    )
    return "$".join(
        (
            "scrypt",
            str(_SCRYPT_N),
            str(_SCRYPT_R),
            str(_SCRYPT_P),
            base64.urlsafe_b64encode(salt).decode("ascii"),
            base64.urlsafe_b64encode(derived).decode("ascii"),
        )
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, raw_n, raw_r, raw_p, raw_salt, raw_derived = encoded.split("$", 5)
        if algorithm != "scrypt":
            return False
        n, r, p = int(raw_n), int(raw_r), int(raw_p)
        if (n, r, p) != (_SCRYPT_N, _SCRYPT_R, _SCRYPT_P):
            return False
        salt = base64.urlsafe_b64decode(raw_salt.encode("ascii"))
        expected = base64.urlsafe_b64decode(raw_derived.encode("ascii"))
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=n,
            r=r,
            p=p,
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)
