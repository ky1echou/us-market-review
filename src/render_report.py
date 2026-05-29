from __future__ import annotations

import argparse
import html
import logging
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import markdown as markdown_lib
from dotenv import load_dotenv

from .fetch_market import fetch_market_data, load_config
from .fetch_news import fetch_news
from .prompt_builder import build_markdown_report
from .send_report import (
    PushResult,
    feishu_enabled,
    send_feishu_message,
    send_outputs,
    send_telegram_message,
    telegram_enabled,
)


MARKET_FAILURE_MESSAGE = "今日行情数据抓取失败，未生成正式美股复盘，请检查数据源。"


def slug_date(timezone_name: str) -> str:
    return datetime.now(ZoneInfo(timezone_name)).date().isoformat()


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def log_path() -> Path:
    return Path(os.getenv("LOG_FILE", "logs/daily.log"))


def setup_logger() -> logging.Logger:
    load_dotenv()
    logger = logging.getLogger("us_market_review")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    if not any(isinstance(handler, logging.FileHandler) and Path(handler.baseFilename) == path.resolve() for handler in logger.handlers):
        formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        file_handler = logging.FileHandler(path, encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger


def output_dirs(config: dict[str, Any]) -> tuple[Path, Path, Path]:
    output_root = Path(config.get("app", {}).get("output_dir", "reports"))
    markdown_dir = output_root / "markdown"
    pdf_dir = output_root / "pdf"
    html_dir = output_root / "html"
    for directory in (markdown_dir, pdf_dir, html_dir):
        directory.mkdir(parents=True, exist_ok=True)
    return markdown_dir, pdf_dir, html_dir


def markdown_to_html(markdown_text: str, title: str) -> str:
    body = markdown_lib.markdown(markdown_text, extensions=["tables", "fenced_code"])
    escaped_title = html.escape(title)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>{escaped_title}</title>
  <style>
    @page {{ size: A4; margin: 12mm; }}
    html, body {{ margin: 0; padding: 0; }}
    body {{
      font-family: "WenQuanYi Zen Hei", "WenQuanYi Micro Hei", "Noto Sans CJK SC",
        "Noto Sans CJK", "DejaVu Sans", "Microsoft YaHei", Arial, sans-serif;
      font-size: 12px;
      line-height: 1.58;
      color: #17202a;
      background: #ffffff;
    }}
    h1 {{ font-size: 24px; margin: 0 0 14px; color: #0f172a; }}
    h2 {{ font-size: 18px; margin: 22px 0 10px; color: #111827; page-break-after: avoid; }}
    h3 {{ font-size: 14px; margin: 16px 0 8px; color: #1f2937; page-break-after: avoid; }}
    p, li {{ word-break: break-word; }}
    table {{ border-collapse: collapse; width: 100%; margin: 10px 0 16px; font-size: 10px; page-break-inside: auto; }}
    thead {{ display: table-header-group; }}
    tr {{ page-break-inside: avoid; page-break-after: auto; }}
    th, td {{ border: 1px solid #d8dee9; padding: 5px 6px; vertical-align: top; word-break: break-word; }}
    th {{ background: #f3f6f9; font-weight: 700; }}
    code, pre {{ white-space: pre-wrap; word-break: break-word; }}
    a {{ color: #1d4ed8; text-decoration: none; }}
  </style>
</head>
<body>
{body}
</body>
</html>
"""


def wkhtmltopdf_path(config: dict[str, Any]) -> str | None:
    configured = (
        os.getenv("PDF_WKHTMLTOPDF_PATH")
        or config.get("report", {}).get("pdf", {}).get("wkhtmltopdf_path")
        or ""
    )
    if configured and Path(configured).exists():
        return configured
    return shutil.which("wkhtmltopdf")


def validate_pdf(pdf_path: Path, config: dict[str, Any]) -> tuple[bool, str]:
    min_bytes = env_int("PDF_MIN_BYTES", int(config.get("report", {}).get("pdf", {}).get("min_bytes", 5000)))
    if not pdf_path.exists():
        return False, f"PDF 文件不存在: {pdf_path}"
    size = pdf_path.stat().st_size
    if size < min_bytes:
        return False, f"PDF 文件过小: {size} bytes，低于阈值 {min_bytes} bytes"
    with pdf_path.open("rb") as file:
        header = file.read(4)
    if header != b"%PDF":
        return False, f"PDF 文件头异常: {header!r}"
    return True, f"PDF 校验通过: {size} bytes"


def render_pdf_with_wkhtmltopdf(html_path: Path, pdf_path: Path, config: dict[str, Any]) -> tuple[bool, str]:
    executable = wkhtmltopdf_path(config)
    if not executable:
        return False, "未找到 wkhtmltopdf"

    command = [
        executable,
        "--encoding",
        "utf-8",
        "--enable-local-file-access",
        "--print-media-type",
        "--page-size",
        "A4",
        "--margin-top",
        "12mm",
        "--margin-bottom",
        "12mm",
        "--margin-left",
        "12mm",
        "--margin-right",
        "12mm",
        str(html_path.resolve()),
        str(pdf_path.resolve()),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=120, check=False)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        return False, f"wkhtmltopdf 失败，exit={completed.returncode}: {detail}"
    return True, "wkhtmltopdf 转换完成"


def render_pdf_with_playwright(html_path: Path, pdf_path: Path) -> tuple[bool, str]:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # noqa: BLE001
        return False, f"Playwright 不可用: {exc}"

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(args=["--no-sandbox"])
            page = browser.new_page()
            page.goto(html_path.resolve().as_uri(), wait_until="networkidle")
            page.pdf(
                path=str(pdf_path.resolve()),
                format="A4",
                print_background=True,
                margin={"top": "12mm", "bottom": "12mm", "left": "12mm", "right": "12mm"},
            )
            browser.close()
        return True, "Playwright/Chromium 转换完成"
    except Exception as exc:  # noqa: BLE001
        return False, f"Playwright/Chromium 失败: {exc}"


def render_pdf_from_html(html_path: Path, pdf_path: Path, config: dict[str, Any]) -> str | None:
    attempts: list[str] = []
    ok, detail = render_pdf_with_wkhtmltopdf(html_path, pdf_path, config)
    attempts.append(detail)
    if not ok:
        ok, detail = render_pdf_with_playwright(html_path, pdf_path)
        attempts.append(detail)

    if not ok:
        return "PDF 生成失败: " + "；".join(attempts)

    valid, validation_detail = validate_pdf(pdf_path, config)
    if valid:
        return None

    try:
        pdf_path.unlink(missing_ok=True)
    except OSError:
        pass
    return f"PDF 生成失败: {validation_detail}。已删除异常 PDF，不会推送坏 PDF。"


def write_outputs(config: dict[str, Any], markdown_text: str) -> tuple[Path, Path | None, list[str]]:
    markdown_dir, pdf_dir, html_dir = output_dirs(config)
    timezone_name = config.get("app", {}).get("timezone", "Asia/Shanghai")
    report_date = slug_date(timezone_name)
    base_name = f"us-market-review-{report_date}"
    markdown_path = markdown_dir / f"{base_name}.md"
    html_path = html_dir / f"{base_name}.html"
    pdf_path = pdf_dir / f"{base_name}.pdf"
    warnings: list[str] = []

    markdown_path.write_text(markdown_text, encoding="utf-8")
    html_path.write_text(markdown_to_html(markdown_text, config.get("report", {}).get("title", base_name)), encoding="utf-8")

    pdf_enabled = config.get("report", {}).get("pdf", {}).get("enabled", True)
    if pdf_enabled:
        warning = render_pdf_from_html(html_path, pdf_path, config)
        if warning:
            warnings.append(warning)
            pdf_path = None
    else:
        pdf_path = None

    return markdown_path, pdf_path, warnings


def market_fetch_summary(market_data: dict[str, Any]) -> str:
    metadata = market_data.get("metadata", {})
    return (
        f"行情成功 {metadata.get('success_count', 0)}/{metadata.get('total_count', 0)}，"
        f"实时 {metadata.get('live_success_count', 0)}，"
        f"缓存 {metadata.get('cache_success_count', 0)}，"
        f"失败 {metadata.get('failed_count', 0)}"
    )


def news_fetch_summary(news_data: dict[str, Any]) -> str:
    errors = news_data.get("errors", [])
    return f"新闻 {len(news_data.get('items', []))} 条，RSS 失败 {len(errors)} 个"


def log_push_results(logger: logging.Logger, results: list[PushResult]) -> None:
    for result in results:
        if result.success:
            status = "成功"
        elif not result.enabled:
            status = "跳过"
        else:
            status = "失败"
        logger.info("推送结果: channel=%s status=%s detail=%s", result.channel, status, result.detail)


def push_alerts_from_run(warnings: list[str]) -> list[str]:
    alerts: list[str] = []
    for warning in warnings:
        if warning not in alerts:
            alerts.append(warning)
    return alerts


def send_market_failure_alert() -> list[PushResult]:
    results: list[PushResult] = []
    if telegram_enabled():
        results.append(send_telegram_message(MARKET_FAILURE_MESSAGE))
    else:
        results.append(PushResult("telegram", False, False, "disabled"))

    if feishu_enabled():
        results.append(send_feishu_message(MARKET_FAILURE_MESSAGE))
    else:
        results.append(PushResult("feishu", False, False, "disabled"))
    return results


def formal_report_allowed(market_data: dict[str, Any]) -> bool:
    metadata = market_data.get("metadata", {})
    return bool(metadata.get("formal_report_allowed", metadata.get("live_data_complete", False)))


def run(config_path: str | Path, logger: logging.Logger | None = None) -> tuple[Path | None, Path | None, list[str], list[PushResult]]:
    logger = logger or setup_logger()
    logger.info("开始时间: %s", datetime.now().astimezone().isoformat(timespec="seconds"))

    config = load_config(config_path)
    logger.info("配置文件: %s", config_path)

    market_data = fetch_market_data(config)
    logger.info("数据获取结果: %s", market_fetch_summary(market_data))
    for warning in market_data.get("metadata", {}).get("warnings", []):
        logger.warning("行情质量警告: %s", warning)
    for asset in market_data.get("assets", []):
        if asset.get("error"):
            logger.warning("行情失败或降级: ticker=%s from_cache=%s provider=%s reason=%s", asset.get("ticker"), asset.get("from_cache"), asset.get("source", {}).get("provider"), asset.get("error"))

    if not formal_report_allowed(market_data):
        logger.error("失败原因: %s %s", MARKET_FAILURE_MESSAGE, market_fetch_summary(market_data))
        push_results = send_market_failure_alert()
        log_push_results(logger, push_results)
        logger.info("运行结束: market_data_failed_no_formal_report")
        return None, None, [MARKET_FAILURE_MESSAGE], push_results

    news_data = fetch_news(config)
    logger.info("数据获取结果: %s", news_fetch_summary(news_data))
    for error in news_data.get("errors", []):
        logger.warning("新闻失败: source=%s reason=%s", error.get("source"), error.get("error"))

    markdown_text = build_markdown_report(config, market_data, news_data)
    markdown_path, pdf_path, warnings = write_outputs(config, markdown_text)
    logger.info("报告生成路径: markdown=%s", markdown_path)
    if pdf_path:
        logger.info("报告生成路径: pdf=%s", pdf_path)
    else:
        logger.warning("报告生成路径: pdf=未生成或校验失败")
    for warning in warnings:
        logger.warning("报告生成警告: %s", warning)

    push_results = send_outputs(markdown_path, pdf_path, alerts=push_alerts_from_run(warnings))
    log_push_results(logger, push_results)
    logger.info("运行结束: success")
    return markdown_path, pdf_path, warnings, push_results


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the daily Chinese US market review report.")
    parser.add_argument("--config", default=os.getenv("CONFIG_PATH", "config.yaml"))
    args = parser.parse_args()

    logger = setup_logger()
    try:
        markdown_path, pdf_path, warnings, push_results = run(args.config, logger=logger)
    except Exception as exc:  # noqa: BLE001 - make cron failures visible in logs.
        logger.exception("失败原因: %s", exc)
        raise

    if markdown_path:
        print(f"Markdown report: {markdown_path}")
    if pdf_path:
        print(f"PDF report: {pdf_path}")
    for warning in warnings:
        print(f"Warning: {warning}")
    for result in push_results:
        status = "ok" if result.success else "skip" if not result.enabled else "failed"
        print(f"Push {result.channel}: {status} - {result.detail}")


if __name__ == "__main__":
    main()
