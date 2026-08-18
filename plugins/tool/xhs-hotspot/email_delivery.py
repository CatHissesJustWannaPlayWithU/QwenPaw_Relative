"""个性化报告的 SMTP 邮件发送；只发送已保存、可追溯的报告。"""

from __future__ import annotations

import asyncio
import hashlib
import re
import smtplib
import ssl
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid, parseaddr
from pathlib import Path
from typing import Any

from qwenpaw.drivers.credentials.store import AsyncCredentialStore
from qwenpaw.drivers.errors import CredentialNotFoundError, DriverCardError

from .storage import load_personalized_report, save_email_receipt


class EmailDeliveryError(ValueError):
    """邮件配置或发送过程不满足安全要求时抛出。"""


_EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_RESERVED_RECIPIENT_DOMAINS = frozenset(
    {"example.com", "example.org", "example.net", "invalid", "localhost"},
)


@dataclass(frozen=True)
class SmtpSettings:
    """从加密凭据记录中解析出的最小 SMTP 连接参数。"""

    host: str
    port: int
    username: str
    password: str
    from_email: str
    from_name: str
    use_ssl: bool
    use_starttls: bool


def _validate_email(value: str, *, field_name: str) -> str:
    """解析并校验邮箱地址，只返回可安全写入邮件头的纯邮箱地址。"""
    _display_name, address = parseaddr(value)
    if not address or not _EMAIL_PATTERN.fullmatch(address):
        raise EmailDeliveryError(f"{field_name} 不是有效的邮箱地址")
    return address


def _validate_recipient_email(value: str) -> str:
    """校验真实收件人，拒绝常见示例域名，避免把占位符发送到 SMTP 服务。"""
    address = _validate_email(value, field_name="收件人")
    domain = address.rsplit("@", 1)[1].lower()
    if domain in _RESERVED_RECIPIENT_DOMAINS or domain.endswith(".test"):
        raise EmailDeliveryError(
            "收件人邮箱使用了示例域名；请在页面中手动填写真实测试邮箱后再发送",
        )
    return address


def _as_bool(value: Any, *, field_name: str) -> bool:
    """把凭据配置中的布尔值统一为 bool，只接受 true 或 false。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
        return value.strip().lower() == "true"
    raise EmailDeliveryError(f"{field_name} 必须是 true 或 false")


def smtp_settings_from_credential(
    *,
    public: dict[str, Any],
    secrets: dict[str, Any],
) -> SmtpSettings:
    """校验 SMTP 凭据；拒绝明文或未加密的网络传输配置。"""
    host = str(public.get("host", "")).strip()
    username = str(secrets.get("username", "")).strip()
    password = str(secrets.get("password", ""))
    from_email = _validate_email(str(public.get("from_email", "")).strip(), field_name="发件人")
    if not host or not username or not password:
        raise EmailDeliveryError("SMTP 凭据必须包含 host、username 和 password")
    try:
        port = int(public.get("port", 465))
    except (TypeError, ValueError) as exc:
        raise EmailDeliveryError("SMTP 端口必须是有效整数") from exc
    if not 1 <= port <= 65535:
        raise EmailDeliveryError("SMTP 端口不合法")
    use_ssl = _as_bool(public.get("use_ssl", True), field_name="use_ssl")
    use_starttls = _as_bool(public.get("use_starttls", False), field_name="use_starttls")
    if use_ssl == use_starttls:
        raise EmailDeliveryError("SMTP 必须且只能启用 SSL 或 STARTTLS 之一")
    return SmtpSettings(
        host=host,
        port=port,
        username=username,
        password=password,
        from_email=from_email,
        from_name=str(public.get("from_name", "小红书热点简报")).strip() or "小红书热点简报",
        use_ssl=use_ssl,
        use_starttls=use_starttls,
    )


async def load_smtp_settings(*, workspace_dir: Path, credential_ref: str) -> SmtpSettings:
    """从当前智能体的加密 credentials.yaml 读取 SMTP 密钥。"""
    normalized_ref = credential_ref.strip()
    if not normalized_ref:
        raise EmailDeliveryError("尚未配置 smtp_credential_ref，不能发送邮件")
    try:
        record = await AsyncCredentialStore(workspace_dir / "credentials.yaml").get(normalized_ref)
    except (CredentialNotFoundError, DriverCardError, OSError) as exc:
        raise EmailDeliveryError(f"无法读取 SMTP 凭据：{exc}") from exc
    if record.kind != "smtp":
        raise EmailDeliveryError("SMTP 凭据 kind 必须是 smtp")
    return smtp_settings_from_credential(public=record.public, secrets=record.secrets)


def _send_message(*, settings: SmtpSettings, message: EmailMessage) -> None:
    """通过加密 SMTP 连接发送邮件；密码不会被写入日志、回执或返回值。"""
    context = ssl.create_default_context()
    if settings.use_ssl:
        with smtplib.SMTP_SSL(
            settings.host,
            settings.port,
            timeout=20,
            context=context,
        ) as client:
            client.login(settings.username, settings.password)
            client.send_message(message)
        return
    with smtplib.SMTP(settings.host, settings.port, timeout=20) as client:
        client.ehlo()
        client.starttls(context=context)
        client.ehlo()
        client.login(settings.username, settings.password)
        client.send_message(message)


def _build_report_message(
    *,
    settings: SmtpSettings,
    recipient: str,
    markdown: str,
    workbook: bytes,
    workbook_filename: str,
) -> EmailMessage:
    """组装带标准日期、正文和 Excel 附件的报告邮件。"""
    message = EmailMessage()
    message["Subject"] = "小红书个性化热点简报"
    message["From"] = formataddr((settings.from_name, settings.from_email))
    message["To"] = recipient
    message["Date"] = formatdate(localtime=False)
    message["Message-ID"] = make_msgid(domain=settings.from_email.rsplit("@", 1)[1])
    message.set_content(markdown)
    message.add_attachment(
        workbook,
        maintype="application",
        subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=workbook_filename,
    )
    return message


async def send_personalized_report_email(
    *,
    workspace_dir: Path,
    report_id: str,
    recipient_email: str,
    credential_ref: str,
) -> dict[str, Any]:
    """发送指定的已保存报告，并写入不含明文邮箱的最小审计回执。"""
    recipient = _validate_recipient_email(recipient_email.strip())
    report, markdown, report_path = load_personalized_report(
        workspace_dir=workspace_dir,
        report_id=report_id,
    )
    workbook_path = report_path.with_suffix("").with_suffix(".xlsx")
    try:
        workbook = workbook_path.read_bytes()
    except OSError as exc:
        raise EmailDeliveryError(f"Excel 简报文件无法读取：{exc}") from exc
    if not workbook.startswith(b"PK"):
        raise EmailDeliveryError("Excel 简报文件格式异常，已拒绝作为附件发送")
    settings = await load_smtp_settings(
        workspace_dir=workspace_dir,
        credential_ref=credential_ref,
    )
    message = _build_report_message(
        settings=settings,
        recipient=recipient,
        markdown=markdown,
        workbook=workbook,
        workbook_filename=workbook_path.name,
    )
    await asyncio.to_thread(_send_message, settings=settings, message=message)

    sent_at = datetime.now(timezone.utc).isoformat()
    receipt = {
        "report_id": report["report_id"],
        "sent_at": sent_at,
        "recipient_sha256": hashlib.sha256(recipient.lower().encode("utf-8")).hexdigest(),
        "smtp_credential_ref": credential_ref,
        "transport": "ssl" if settings.use_ssl else "starttls",
        "attachment_filename": workbook_path.name,
        "attachment_sha256": hashlib.sha256(workbook).hexdigest(),
    }
    receipt_path = save_email_receipt(
        workspace_dir=workspace_dir,
        report_id=report["report_id"],
        receipt=receipt,
    )
    local_part, domain = recipient.split("@", 1)
    masked_recipient = f"{local_part[:1]}***@{domain}"
    return {
        "ok": True,
        "report_id": report["report_id"],
        "sent_at": sent_at,
        "recipient": masked_recipient,
        "receipt_path": receipt_path,
        "attachment_file_name": workbook_path.name,
    }
