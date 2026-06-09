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

from .fetch_market import fetch_market_data, load_config, trading_days_since
from .fetch_news import fetch_news
from .prompt_builder import build_markdown_report
from .send_report import PushResult, send_outputs, send_telegram_message, telegram_enabled
from .symbol_validation import validate_market_symbols


MARKET_FAILURE_MESSAGE = "今日行情数据抓取失败或不足，未生成正式美股复盘，请检查数据源。"
MARKET_TIMEOUT_MESSAGE = "us-market-review 运行超时，未生成正式美股复盘，请检查数据源。"
CACHE_ONLY_FAILURE_MESSAGE = "今日行情源异常，本次仅命中缓存，未生成正式美股复盘。"
REPORT_QUALITY_FAILURE_MESSAGE = "正式报告生成质量校验失败，未发送报告文件，请检查 logs/daily.log。"
LIVE_MIN_SUCCESS_RATIO = 0.70
CACHE_MAX_SUCCESS_RATIO = 0.30
KEY_TICKERS_FOR_FRESH_CACHE = [
    "SPY", "QQQ", "DIA", "IWM", "SMH", "SOXX", "VIX",
    "NVDA", "MSFT", "AAPL", "AMZN", "GOOGL", "META", "TSLA",
    "AMD", "AVGO", "MU", "MRVL", "ARM",
    "US10Y", "DXY", "TLT", "GLD", "USO", "BTCUSD",
]
MACRO_DEGRADE_TICKERS = ["US10Y", "DXY"]
MACRO_BLOCK_TICKERS = ["US10Y", "DXY", "TLT"]
FORBIDDEN_REPORT_TERMS = [
    "Too Many Requests",
    "HTTP 402",
    "HTTP 429",
    "no cache fallback available",
    "attempt 1/",
    "attempt 1 of",
    "抓取异常",
    "数据审计",
    "Traceback",
    "HTTPError",
    "ReadTimeout",
    "ConnectionError",
    "Python",
    "N/A",
]


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
    @page {{ size: A4 landscape; margin: 10mm; }}
    html, body {{ margin: 0; padding: 0; }}
    body {{
      font-family: "WenQuanYi Zen Hei", "WenQuanYi Micro Hei", "Noto Sans CJK SC",
        "Noto Sans CJK", "DejaVu Sans", "Microsoft YaHei", Arial, sans-serif;
      font-size: 11px;
      line-height: 1.52;
      color: #17202a;
      background: #ffffff;
    }}
    h1 {{ font-size: 23px; margin: 0 0 12px; color: #0f172a; }}
    h2 {{ font-size: 17px; margin: 20px 0 9px; color: #111827; page-break-after: avoid; }}
    h3 {{ font-size: 13px; margin: 14px 0 7px; color: #1f2937; page-break-after: avoid; }}
    h4 {{ font-size: 12px; margin: 12px 0 6px; color: #374151; page-break-after: avoid; }}
    p, li {{ word-break: normal; overflow-wrap: anywhere; }}
    table {{ border-collapse: collapse; width: 100%; margin: 9px 0 15px; font-size: 8.8px; page-break-inside: auto; table-layout: fixed; }}
    thead {{ display: table-header-group; }}
    tr {{ page-break-inside: avoid; page-break-after: auto; }}
    th, td {{ border: 1px solid #d8dee9; padding: 4px 5px; vertical-align: top; overflow-wrap: anywhere; word-break: normal; }}
    th {{ background: #f3f6f9; font-weight: 700; }}
    th:nth-child(1), td:nth-child(1) {{ width: 15%; }}
    th:nth-child(2), td:nth-child(2) {{ width: 12%; }}
    th:nth-child(3), td:nth-child(3),
    th:nth-child(4), td:nth-child(4),
    th:nth-child(5), td:nth-child(5),
    th:nth-child(6), td:nth-child(6) {{ width: 7%; white-space: nowrap; text-align: right; }}
    th:nth-child(7), td:nth-child(7) {{ width: 12%; }}
    th:nth-child(8), td:nth-child(8) {{ width: 29%; }}
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
    configured = os.getenv("PDF_WKHTMLTOPDF_PATH") or config.get("report", {}).get("pdf", {}).get("wkhtmltopdf_path") or ""
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
        "--encoding", "utf-8",
        "--enable-local-file-access",
        "--print-media-type",
        "--page-size", "A4",
        "--orientation", "Landscape",
        "--margin-top", "10mm",
        "--margin-bottom", "10mm",
        "--margin-left", "10mm",
        "--margin-right", "10mm",
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
            page.pdf(path=str(pdf_path.resolve()), format="A4", landscape=True, print_background=True, margin={"top": "10mm", "bottom": "10mm", "left": "10mm", "right": "10mm"})
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
    base_name = f"us-market-review-{slug_date(timezone_name)}"
    markdown_path = markdown_dir / f"{base_name}.md"
    html_path = html_dir / f"{base_name}.html"
    pdf_path = pdf_dir / f"{base_name}.pdf"
    warnings: list[str] = []
    markdown_path.write_text(markdown_text, encoding="utf-8")
    html_path.write_text(markdown_to_html(markdown_text, config.get("report", {}).get("title", base_name)), encoding="utf-8")
    if config.get("report", {}).get("pdf", {}).get("enabled", True):
        warning = render_pdf_from_html(html_path, pdf_path, config)
        if warning:
            warnings.append(warning)
            pdf_path = None
    else:
        pdf_path = None
    return markdown_path, pdf_path, warnings


def market_fetch_summary(market_data: dict[str, Any]) -> str:
    metadata = market_data.get("metadata", {})
    return f"完整池行情成功 {metadata.get('success_count', 0)}/{metadata.get('total_count', 0)}，实时 {metadata.get('live_success_count', 0)}，缓存 {metadata.get('cache_success_count', 0)}，失败 {metadata.get('failed_count', 0)}"


def news_fetch_summary(news_data: dict[str, Any]) -> str:
    return f"新闻 {len(news_data.get('items', []))} 条，RSS 失败 {len(news_data.get('errors', []))} 个"


def log_push_results(logger: logging.Logger, results: list[PushResult]) -> None:
    for result in results:
        status = "成功" if result.success else "跳过" if not result.enabled else "失败"
        logger.info("推送结果: channel=%s status=%s detail=%s", result.channel, status, result.detail)


def push_alerts_from_run(warnings: list[str]) -> list[str]:
    alerts: list[str] = []
    for warning in warnings:
        if warning not in alerts:
            alerts.append(warning)
    return alerts


def compact_list(values: list[Any], limit: int = 30) -> str:
    clean = [str(value) for value in values if str(value)]
    if not clean:
        return "无"
    if len(clean) <= limit:
        return ", ".join(clean)
    return ", ".join(clean[:limit]) + f" 等 {len(clean)} 项"


def compact_failed_details(details: list[dict[str, Any]], limit: int = 30) -> str:
    items = []
    for item in details:
        ticker = item.get("ticker") or "UNKNOWN"
        reason = item.get("reason") or "quote_failed"
        quote = "Q✓" if item.get("quote_success") else "Q×"
        hist = "H✓" if item.get("historical_success") else "H×"
        company = f",{item.get('company_name')}" if item.get("company_name") else ""
        items.append(f"{ticker}({reason},{quote},{hist}{company})")
    return compact_list(items, limit=limit)


def critical_group_text(groups: dict[str, Any]) -> str:
    if not groups:
        return "未配置"
    parts = []
    for name, item in groups.items():
        failed = item.get("failed_tickers", [])
        status = "通过" if item.get("passed") else f"失败: {compact_list(failed)}"
        parts.append(f"{name}={status}")
    return "；".join(parts)


def asset_is_usable(asset: dict[str, Any] | None) -> bool:
    return bool(asset and (asset.get("last_close") is not None or asset.get("quote_success")))


def asset_lookup(market_data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(asset.get("ticker", "")).upper(): asset for asset in market_data.get("assets", [])}


def cache_ticker_details(market_data: dict[str, Any]) -> list[str]:
    details = []
    for asset in market_data.get("assets", []):
        source = asset.get("source", {}) if isinstance(asset.get("source"), dict) else {}
        if asset.get("from_cache") or source.get("from_cache"):
            details.append(f"{asset.get('ticker')}({asset.get('as_of') or source.get('as_of') or '日期暂缺'})")
    return details


def parse_asset_date(value: Any):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except ValueError:
        return None


def cache_dates(market_data: dict[str, Any]) -> list[str]:
    dates: list[str] = []
    for asset in market_data.get("assets", []):
        source = asset.get("source", {}) if isinstance(asset.get("source"), dict) else {}
        if asset.get("from_cache") or source.get("from_cache"):
            value = asset.get("as_of") or source.get("as_of")
            dates.append(str(value or "unknown")[:10])
    return sorted(set(dates))


def enforce_cache_freshness(market_data: dict[str, Any], max_trading_days: int = 3) -> list[str]:
    blockers: list[str] = []
    for asset in market_data.get("assets", []):
        source = asset.get("source", {}) if isinstance(asset.get("source"), dict) else {}
        if not (asset.get("from_cache") or source.get("from_cache")):
            continue
        as_of_date = parse_asset_date(asset.get("as_of") or source.get("as_of"))
        if not as_of_date or trading_days_since(as_of_date) > max_trading_days:
            blockers.append(f"cache_stale: {asset.get('ticker')} as_of={asset.get('as_of') or source.get('as_of') or 'unknown'}")
    return blockers


def enforce_realtime_and_cache_quality(config: dict[str, Any], market_data: dict[str, Any]) -> list[str]:
    market_config = config.get("market", {})
    live_min_ratio = float(market_config.get("live_min_success_ratio", LIVE_MIN_SUCCESS_RATIO))
    cache_max_ratio = float(market_config.get("cache_max_success_ratio", CACHE_MAX_SUCCESS_RATIO))
    metadata = market_data.get("metadata", {})
    total = int(metadata.get("total_count") or 0)
    live_success = int(metadata.get("live_success_count") or 0)
    cache_success = int(metadata.get("cache_success_count") or 0)
    blockers: list[str] = []
    if total <= 0:
        blockers.append("live_data_unavailable: total_count=0")
        return blockers
    live_ratio = live_success / total
    cache_ratio = cache_success / total
    if live_success == 0:
        blockers.append("live_data_zero: 今日行情源异常，本次仅命中缓存")
    if live_ratio < live_min_ratio:
        blockers.append(f"live_data_insufficient: live={live_success}/{total} threshold={live_min_ratio:.0%}")
    if cache_ratio > cache_max_ratio:
        blockers.append(f"cache_ratio_too_high: cache={cache_success}/{total} threshold={cache_max_ratio:.0%}")
    dates = cache_dates(market_data)
    if len(dates) > 1:
        blockers.append(f"cache_date_mismatch: {', '.join(dates)}")
    lookup = asset_lookup(market_data)
    for ticker in KEY_TICKERS_FOR_FRESH_CACHE:
        asset = lookup.get(ticker)
        if not asset:
            continue
        source = asset.get("source", {}) if isinstance(asset.get("source"), dict) else {}
        if not (asset.get("from_cache") or source.get("from_cache")):
            continue
        as_of_date = parse_asset_date(asset.get("as_of") or source.get("as_of"))
        if not as_of_date or trading_days_since(as_of_date) > 1:
            blockers.append(f"critical_cache_stale: {ticker} as_of={asset.get('as_of') or source.get('as_of') or 'unknown'}")
    return blockers


def enforce_macro_quality(market_data: dict[str, Any]) -> list[str]:
    metadata = market_data.setdefault("metadata", {})
    lookup = asset_lookup(market_data)
    missing_macro = [ticker for ticker in MACRO_DEGRADE_TICKERS if not asset_is_usable(lookup.get(ticker))]
    blockers: list[str] = []
    if missing_macro:
        metadata["macro_degraded"] = True
        metadata["macro_degraded_tickers"] = missing_macro
        metadata["macro_degraded_message"] = "宏观利率/美元数据缺失，宏观判断降级。"
        warnings = metadata.setdefault("warnings", [])
        if metadata["macro_degraded_message"] not in warnings:
            warnings.append(metadata["macro_degraded_message"])
    if all(not asset_is_usable(lookup.get(ticker)) for ticker in MACRO_BLOCK_TICKERS):
        blockers.append("macro_core_missing: US10Y,DXY,TLT all unavailable")
    return blockers


def add_quality_blockers(metadata: dict[str, Any], blockers: list[str]) -> None:
    target = metadata.setdefault("quality_blockers", [])
    for blocker in blockers:
        if blocker not in target:
            target.append(blocker)


def apply_quality_gates(config: dict[str, Any], market_data: dict[str, Any], logger: logging.Logger) -> None:
    metadata = market_data.setdefault("metadata", {})
    validation = validate_market_symbols(config, market_data)
    for item in validation.get("checked", []):
        logger.info(
            "symbol校验: ticker=%s ok=%s provider=%s symbol=%s company=%s exchange=%s currency=%s reason=%s",
            item.get("ticker"), item.get("ok"), item.get("provider"), item.get("provider_symbol"),
            item.get("company_name"), item.get("exchange"), item.get("currency"), item.get("failure_reason"),
        )
    blockers = []
    blockers.extend(enforce_cache_freshness(market_data, 3))
    blockers.extend(enforce_realtime_and_cache_quality(config, market_data))
    blockers.extend(enforce_macro_quality(market_data))
    add_quality_blockers(metadata, blockers)
    if metadata.get("quality_blockers"):
        metadata["formal_report_allowed"] = False
        metadata["needs_data_source_upgrade"] = True
        warnings = metadata.setdefault("warnings", [])
        warning = "正式报告质量门槛未通过：实时行情不足、缓存占比过高、缓存日期异常或关键数据缺失。"
        if warning not in warnings:
            warnings.append(warning)
        logger.error("正式报告质量门槛未通过: %s", " | ".join(metadata.get("quality_blockers", [])))


def market_failure_headline(market_data: dict[str, Any]) -> str:
    metadata = market_data.get("metadata", {})
    if metadata.get("market_fetch_timed_out"):
        return MARKET_TIMEOUT_MESSAGE
    if int(metadata.get("live_success_count") or 0) == 0 and int(metadata.get("cache_success_count") or 0) > 0:
        return CACHE_ONLY_FAILURE_MESSAGE
    return MARKET_FAILURE_MESSAGE


def market_failure_status_message(market_data: dict[str, Any]) -> str:
    metadata = market_data.get("metadata", {})
    total = int(metadata.get("total_count") or 0)
    success = int(metadata.get("success_count") or 0)
    live_success = int(metadata.get("live_success_count") or 0)
    cache_success = int(metadata.get("cache_success_count") or 0)
    failed = int(metadata.get("failed_count") or max(total - success, 0))
    success_ratio = float(metadata.get("success_ratio") or 0.0) * 100
    live_ratio = live_success / total * 100 if total else 0.0
    cache_ratio = cache_success / total * 100 if total else 0.0
    min_ratio = float(metadata.get("min_success_ratio") or 0.9) * 100
    timed_out = bool(metadata.get("market_fetch_timed_out"))
    needs_upgrade = bool(metadata.get("needs_data_source_upgrade"))
    lines = [
        market_failure_headline(market_data),
        f"行情成功数量: {success}/{total}",
        f"行情失败数量: {failed}",
        f"行情成功率: {success_ratio:.1f}%（正式报告门槛 {min_ratio:.1f}%）",
        f"实时获取数量: {live_success}（实时占比 {live_ratio:.1f}%）",
        f"缓存降级数量: {cache_success}（缓存占比 {cache_ratio:.1f}%）",
        f"失败 ticker: {compact_failed_details(metadata.get('failed_details', []))}",
        f"缓存 ticker 和日期: {compact_list(cache_ticker_details(market_data), limit=80)}",
        f"缓存日期集合: {compact_list(cache_dates(market_data))}",
        f"阻断原因: {compact_list(metadata.get('quality_blockers', []), limit=80)}",
        f"数据源: {metadata.get('source') or '未披露'}",
        f"获取时间: {metadata.get('fetched_at') or '未披露'}",
        f"成功 ticker: {compact_list(metadata.get('success_tickers', []), limit=80)}",
        f"关键分组: {critical_group_text(metadata.get('critical_groups', {}))}",
    ]
    if metadata.get("macro_degraded"):
        lines.append(f"宏观降级: {metadata.get('macro_degraded_message')} 缺失 {compact_list(metadata.get('macro_degraded_tickers', []))}")
    if timed_out:
        lines.extend([
            f"未完成 ticker: {compact_list(metadata.get('unfinished_tickers', []))}",
            f"当前 provider: {metadata.get('current_provider') or '未披露'}",
            f"当前 ticker: {metadata.get('current_ticker') or '未披露'}",
            f"失败原因: {metadata.get('last_error') or 'market_fetch_timeout'}",
        ])
    lines.append(f"是否需要升级/接入正式数据源: {'是' if needs_upgrade else '否'}")
    if needs_upgrade:
        lines.append(metadata.get("upgrade_message") or "免费行情源无法满足完整报告，需要接入/升级正式数据源。")
    lines.extend([
        "建议动作:",
        "1. 等待下一次自动运行，避免在数据源限流时反复触发。",
        "2. 手动运行 python -m src.warm_market_cache --config config.yaml 预热缓存。",
        "3. 接入或升级 Finnhub / FMP / Twelve Data 等正式行情源。",
    ])
    quote_only = metadata.get("quote_only_tickers", [])
    if quote_only:
        lines.append(f"quote 成功但历史指标暂缺: {compact_list(quote_only)}")
    return "\n".join(lines)


def send_market_failure_alert(market_data: dict[str, Any]) -> list[PushResult]:
    message = market_failure_status_message(market_data)
    if telegram_enabled():
        return [send_telegram_message(message)]
    return [PushResult("telegram", False, False, "disabled")]


def formal_report_allowed(market_data: dict[str, Any]) -> bool:
    metadata = market_data.get("metadata", {})
    if metadata.get("quality_blockers"):
        return False
    total = int(metadata.get("total_count") or 0)
    explicit_flag = metadata.get("formal_report_allowed")
    if explicit_flag is not None:
        return bool(explicit_flag) and total > 0
    success_ratio = float(metadata.get("success_ratio") or 0.0)
    min_success_ratio = float(metadata.get("min_success_ratio") or 0.9)
    return total > 0 and success_ratio >= min_success_ratio


def validate_report_text(markdown_text: str) -> str | None:
    lower_text = markdown_text.lower()
    for term in FORBIDDEN_REPORT_TERMS:
        if term.lower() in lower_text:
            return f"正式报告包含禁止内容: {term}"
    return None


def run(config_path: str | Path, logger: logging.Logger | None = None) -> tuple[Path | None, Path | None, list[str], list[PushResult]]:
    logger = logger or setup_logger()
    logger.info("开始时间: %s", datetime.now().astimezone().isoformat(timespec="seconds"))
    config = load_config(config_path)
    logger.info("配置文件: %s", config_path)
    market_data = fetch_market_data(config)
    apply_quality_gates(config, market_data, logger)
    metadata = market_data.get("metadata", {})
    logger.info("数据获取结果: %s", market_fetch_summary(market_data))
    logger.info("缓存降级ticker: %s", compact_list(cache_ticker_details(market_data), limit=200))
    logger.info("缓存日期集合: %s", compact_list(cache_dates(market_data), limit=200))
    logger.info("质量阻断: %s", compact_list(metadata.get("quality_blockers", []), limit=200))
    logger.info("行情成功ticker: %s", compact_list(metadata.get("success_tickers", []), limit=200))
    logger.info("行情失败ticker: %s", compact_failed_details(metadata.get("failed_details", []), limit=200))
    logger.info("quote成功ticker: %s", compact_list(metadata.get("quote_success_tickers", []), limit=200))
    logger.info("historical成功ticker: %s", compact_list(metadata.get("historical_success_tickers", []), limit=200))
    logger.info("关键分组: %s", critical_group_text(metadata.get("critical_groups", {})))
    if metadata.get("market_fetch_timed_out"):
        logger.error("失败原因: market_fetch_timeout provider=%s ticker=%s last_error=%s", metadata.get("current_provider"), metadata.get("current_ticker"), metadata.get("last_error"))
    for warning in metadata.get("warnings", []):
        logger.warning("行情/质量警告: %s", warning)
    for asset in market_data.get("assets", []):
        source = asset.get("source", {}) if isinstance(asset.get("source"), dict) else {}
        logger.info(
            "行情明细: ticker=%s from_cache=%s provider=%s as_of=%s company=%s exchange=%s currency=%s failure_reason=%s",
            asset.get("ticker"), asset.get("from_cache"), source.get("provider"), asset.get("as_of") or source.get("as_of"),
            source.get("company_name"), source.get("exchange"), source.get("currency"), asset.get("failure_reason"),
        )
        if asset.get("error"):
            logger.warning("行情失败或降级: ticker=%s from_cache=%s provider=%s failure_reason=%s reason=%s", asset.get("ticker"), asset.get("from_cache"), source.get("provider"), asset.get("failure_reason"), asset.get("error"))
        elif asset.get("indicator_reason"):
            logger.info("行情quote可用但指标暂缺: ticker=%s provider=%s reason=%s", asset.get("ticker"), source.get("provider"), asset.get("indicator_reason"))

    if not formal_report_allowed(market_data):
        message = market_failure_status_message(market_data)
        logger.error("失败原因: %s", message.replace("\n", " | "))
        push_results = send_market_failure_alert(market_data)
        log_push_results(logger, push_results)
        logger.info("运行结束: market_or_quality_failed_no_formal_report")
        return None, None, [message], push_results

    news_data = fetch_news(config)
    logger.info("数据获取结果: %s", news_fetch_summary(news_data))
    for error in news_data.get("errors", []):
        logger.warning("新闻失败: source=%s reason=%s", error.get("source"), error.get("error"))
    markdown_text = build_markdown_report(config, market_data, news_data)
    quality_error = validate_report_text(markdown_text)
    if quality_error:
        logger.error("报告质量校验失败: %s", quality_error)
        result = send_telegram_message(REPORT_QUALITY_FAILURE_MESSAGE) if telegram_enabled() else PushResult("telegram", False, False, "disabled")
        push_results = [result]
        log_push_results(logger, push_results)
        logger.info("运行结束: report_quality_failed_no_files_sent")
        return None, None, [REPORT_QUALITY_FAILURE_MESSAGE], push_results

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
    except Exception as exc:  # noqa: BLE001
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
