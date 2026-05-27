from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import os
import smtplib
import time
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import Iterable

import requests
from dotenv import load_dotenv


@dataclass
class PushResult:
    channel: str
    enabled: bool
    success: bool
    detail: str


def str_to_bool(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def truncate(text: str, limit: int = 1000) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def report_message(markdown_path: Path, pdf_path: Path | None = None) -> str:
    lines = [
        "美股复盘报告已生成",
        f"Markdown: {markdown_path}",
    ]
    if pdf_path and pdf_path.exists():
        lines.append(f"PDF: {pdf_path}")
    return "\n".join(lines)


def email_enabled() -> bool:
    load_dotenv()
    return str_to_bool(os.getenv("ENABLE_EMAIL"), default=False)


def telegram_enabled() -> bool:
    load_dotenv()
    return str_to_bool(os.getenv("ENABLE_TELEGRAM"), default=False)


def feishu_enabled() -> bool:
    load_dotenv()
    return str_to_bool(os.getenv("ENABLE_FEISHU"), default=False)


def send_email(attachments: Iterable[Path], subject: str, body: str) -> PushResult:
    load_dotenv()
    channel = "email"
    if not email_enabled():
        return PushResult(channel, False, False, "disabled")

    host = os.getenv("SMTP_HOST", "")
    port = env_int("SMTP_PORT", 587)
    username = os.getenv("SMTP_USERNAME", "")
    password = os.getenv("SMTP_PASSWORD", "")
    use_tls = str_to_bool(os.getenv("SMTP_USE_TLS"), default=True)
    sender = os.getenv("EMAIL_FROM") or username
    recipients = [item.strip() for item in os.getenv("EMAIL_TO", "").split(",") if item.strip()]

    if not all([host, port, sender, recipients]):
        return PushResult(channel, True, False, "SMTP_HOST/EMAIL_FROM/EMAIL_TO is incomplete")

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message.set_content(body)

    for attachment in attachments:
        if not attachment.exists():
            continue
        message.add_attachment(
            attachment.read_bytes(),
            maintype="application",
            subtype="octet-stream",
            filename=attachment.name,
        )

    try:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            if use_tls:
                smtp.starttls()
            if username and password:
                smtp.login(username, password)
            smtp.send_message(message)
        return PushResult(channel, True, True, f"sent to {len(recipients)} recipient(s)")
    except Exception as exc:  # noqa: BLE001 - report push failures without hiding other channels.
        return PushResult(channel, True, False, str(exc))


def telegram_api_url(method: str) -> str:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    return f"https://api.telegram.org/bot{token}/{method}"


def send_telegram_message(text: str) -> PushResult:
    load_dotenv()
    channel = "telegram"
    if not telegram_enabled():
        return PushResult(channel, False, False, "disabled")

    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    timeout = env_int("PUSH_TIMEOUT_SEC", 30)
    if not token or not chat_id:
        return PushResult(channel, True, False, "TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID is incomplete")

    payload = {
        "chat_id": chat_id,
        "text": truncate(text, 3900),
        "disable_web_page_preview": True,
    }
    parse_mode = os.getenv("TELEGRAM_PARSE_MODE", "").strip()
    if parse_mode:
        payload["parse_mode"] = parse_mode

    try:
        response = requests.post(telegram_api_url("sendMessage"), json=payload, timeout=timeout)
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            return PushResult(channel, True, False, truncate(str(data)))
        return PushResult(channel, True, True, "message sent")
    except Exception as exc:  # noqa: BLE001
        return PushResult(channel, True, False, str(exc))


def send_telegram_document(path: Path, caption: str = "") -> PushResult:
    load_dotenv()
    channel = "telegram"
    if not telegram_enabled():
        return PushResult(channel, False, False, "disabled")

    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    timeout = env_int("PUSH_TIMEOUT_SEC", 30)
    if not token or not chat_id:
        return PushResult(channel, True, False, "TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID is incomplete")
    if not path.exists():
        return PushResult(channel, True, False, f"file not found: {path}")

    payload = {"chat_id": chat_id, "caption": truncate(caption, 900)}
    try:
        with path.open("rb") as file:
            response = requests.post(
                telegram_api_url("sendDocument"),
                data=payload,
                files={"document": (path.name, file)},
                timeout=timeout,
            )
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            return PushResult(channel, True, False, truncate(str(data)))
        return PushResult(channel, True, True, f"document sent: {path.name}")
    except Exception as exc:  # noqa: BLE001
        return PushResult(channel, True, False, str(exc))


def feishu_signature(secret: str, timestamp: str) -> str:
    string_to_sign = f"{timestamp}\n{secret}".encode("utf-8")
    digest = hmac.new(string_to_sign, b"", digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def send_feishu_message(text: str) -> PushResult:
    load_dotenv()
    channel = "feishu"
    if not feishu_enabled():
        return PushResult(channel, False, False, "disabled")

    webhook_url = os.getenv("FEISHU_WEBHOOK_URL", "")
    timeout = env_int("PUSH_TIMEOUT_SEC", 30)
    if not webhook_url:
        return PushResult(channel, True, False, "FEISHU_WEBHOOK_URL is incomplete")

    payload: dict[str, object] = {
        "msg_type": "text",
        "content": {"text": truncate(text, env_int("FEISHU_TEXT_LIMIT", 3000))},
    }
    secret = os.getenv("FEISHU_SECRET", "").strip()
    if secret:
        timestamp = str(int(time.time()))
        payload["timestamp"] = timestamp
        payload["sign"] = feishu_signature(secret, timestamp)

    try:
        response = requests.post(webhook_url, json=payload, timeout=timeout)
        response.raise_for_status()
        data = response.json()
        code = data.get("code", data.get("StatusCode", 0))
        if code not in (0, "0"):
            return PushResult(channel, True, False, truncate(str(data)))
        return PushResult(channel, True, True, "message sent")
    except Exception as exc:  # noqa: BLE001
        return PushResult(channel, True, False, str(exc))


def send_outputs(markdown_path: Path, pdf_path: Path | None = None) -> list[PushResult]:
    load_dotenv()
    prefix = os.getenv("EMAIL_SUBJECT_PREFIX", "美股复盘")
    subject = f"{prefix} - {markdown_path.stem}"
    message = report_message(markdown_path, pdf_path)
    results: list[PushResult] = []

    if telegram_enabled():
        results.append(send_telegram_message(message))
        if str_to_bool(os.getenv("TELEGRAM_SEND_MARKDOWN"), default=True):
            results.append(send_telegram_document(markdown_path, caption=markdown_path.name))
        if pdf_path and str_to_bool(os.getenv("TELEGRAM_SEND_PDF"), default=True):
            results.append(send_telegram_document(pdf_path, caption=pdf_path.name))
    else:
        results.append(PushResult("telegram", False, False, "disabled"))

    if feishu_enabled():
        results.append(send_feishu_message(message))
    else:
        results.append(PushResult("feishu", False, False, "disabled"))

    if email_enabled():
        attachments = [markdown_path]
        if pdf_path and pdf_path.exists():
            attachments.append(pdf_path)
        results.append(send_email(attachments, subject, "自动生成的中文美股复盘报告见附件。"))
    else:
        results.append(PushResult("email", False, False, "disabled"))

    return results


def send_test_message(message: str) -> list[PushResult]:
    load_dotenv()
    results: list[PushResult] = []
    if telegram_enabled():
        results.append(send_telegram_message(message))
    else:
        results.append(PushResult("telegram", False, False, "disabled"))

    if feishu_enabled():
        results.append(send_feishu_message(message))
    else:
        results.append(PushResult("feishu", False, False, "disabled"))

    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Send generated report by enabled push channels.")
    parser.add_argument("markdown_path")
    parser.add_argument("--pdf", default="")
    args = parser.parse_args()

    markdown_path = Path(args.markdown_path)
    pdf_path = Path(args.pdf) if args.pdf else None
    results = send_outputs(markdown_path, pdf_path)
    for result in results:
        status = "ok" if result.success else "skip" if not result.enabled else "failed"
        print(f"{result.channel}: {status} - {result.detail}")


if __name__ == "__main__":
    main()
