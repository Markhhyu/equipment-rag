from __future__ import annotations

import smtplib
from email.message import EmailMessage
from functools import lru_cache
from typing import Protocol

from app.platform.security.config import SecurityConfig, load_security_config


class VerificationEmailSender(Protocol):
    def send_verification(self, recipient: str, verification_url: str, expires_minutes: int) -> None: ...


class SmtpVerificationEmailSender:
    def __init__(self, config: SecurityConfig) -> None:
        self._config = config

    def send_verification(self, recipient: str, verification_url: str, expires_minutes: int) -> None:
        message = EmailMessage()
        message["Subject"] = "验证你的设备知识助手账号"
        message["From"] = self._config.smtp_from_address
        message["To"] = recipient
        message.set_content(
            "请打开下面的链接完成邮箱验证：\n\n"
            f"{verification_url}\n\n"
            f"链接将在 {expires_minutes} 分钟后失效。如果不是你发起的注册，请忽略此邮件。"
        )
        message.add_alternative(
            "<p>请点击下面的链接完成邮箱验证：</p>"
            f'<p><a href="{verification_url}">验证邮箱</a></p>'
            f"<p>链接将在 {expires_minutes} 分钟后失效。如果不是你发起的注册，请忽略此邮件。</p>",
            subtype="html",
        )

        smtp_class = smtplib.SMTP_SSL if self._config.smtp_security == "ssl" else smtplib.SMTP
        with smtp_class(
            self._config.smtp_host,
            self._config.smtp_port,
            timeout=self._config.smtp_timeout_seconds,
        ) as smtp:
            if self._config.smtp_security == "starttls":
                smtp.starttls()
            if self._config.smtp_username:
                smtp.login(self._config.smtp_username, self._config.smtp_password)
            smtp.send_message(message)


@lru_cache(maxsize=1)
def get_verification_email_sender() -> VerificationEmailSender:
    return SmtpVerificationEmailSender(load_security_config())


def reset_verification_email_sender_for_tests() -> None:
    get_verification_email_sender.cache_clear()
