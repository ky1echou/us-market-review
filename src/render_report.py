from __future__ import annotations

import argparse
import logging
import os
import re
import textwrap
from datetime import datetime
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

import markdown as markdown_lib
from dotenv import load_dotenv
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import PageBreak, Paragraph, Preformatted, SimpleDocTemplate, Spacer

from .fetch_market import fetch_market_data, load_config
from .fetch_news import fetch_news
from .prompt_builder import build_markdown_report
from .send_report import PushResult, send_outputs


def slug_date(timezone_name: str) -> str:
    return datetime.now(ZoneInfo(timezone_name)).date().isoformat()


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


def ensure_output_dir(config: dict[str, Any]) -> Path:
    timezone_name = config.get("app", {}).get("timezone", "Asia/Shanghai")
    output_root = Path(config.get("app", {}).get("output_dir", "reports"))
    output_dir = output_root / slug_date(timezone_name)
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def markdown_to_html(markdown_text: str, title: str) -> str:
    body = markdown_lib.markdown(markdown_text, extensions=["tables", "fenced_code"])
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>{title}</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif; line-height: 1.55; color: #17202a; }}
    table {{ border-collapse: collapse; width: 100%; margin: 12px 0; font-size: 12px; }}
    th, td {{ border: 1px solid #d8dee9; padding: 6px 8px; vertical-align: top; }}
    th {{ background: #f3f6f9; }}
    h1, h2, h3 {{ color: #101820; }}
    code, pre {{ white-space: pre-wrap; }}
  </style>
</head>
<body>
{body}
</body>
</html>
"""


def find_cjk_font(config: dict[str, Any]) -> str | None:
    configured = (
        os.getenv("REPORT_FONT_PATH")
        or config.get("report", {}).get("pdf", {}).get("font_path")
        or ""
    )
    candidates = [
        configured,
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\simsun.ttc",
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    return None


def register_pdf_font(config: dict[str, Any]) -> tuple[str, str | None]:
    font_path = find_cjk_font(config)
    if not font_path:
        return "Helvetica", "未找到中文字体，PDF 中文可能无法正确显示；可在 .env 设置 REPORT_FONT_PATH。"
    try:
        pdfmetrics.registerFont(TTFont("CJKFont", font_path))
        return "CJKFont", None
    except Exception as exc:  # noqa: BLE001 - PDF should not block Markdown output.
        return "Helvetica", f"中文字体注册失败: {exc}"


def is_markdown_table_line(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("|") and stripped.endswith("|")


def flush_table(table_lines: list[str], story: list[Any], style: ParagraphStyle) -> None:
    if not table_lines:
        return
    table_text = "\n".join(table_lines)
    wrapped = []
    for line in table_text.splitlines():
        wrapped.append("\n".join(textwrap.wrap(line, width=110, replace_whitespace=False)) or line)
    story.append(Preformatted(escape("\n".join(wrapped)), style))
    story.append(Spacer(1, 4 * mm))
    table_lines.clear()


def render_basic_pdf(markdown_text: str, pdf_path: Path, config: dict[str, Any]) -> str | None:
    font_name, warning = register_pdf_font(config)
    styles = getSampleStyleSheet()
    normal = ParagraphStyle(
        "NormalCJK",
        parent=styles["Normal"],
        fontName=font_name,
        fontSize=9,
        leading=14,
        textColor=colors.HexColor("#17202a"),
        spaceAfter=4,
    )
    h1 = ParagraphStyle("H1CJK", parent=normal, fontSize=18, leading=24, spaceBefore=6, spaceAfter=10)
    h2 = ParagraphStyle("H2CJK", parent=normal, fontSize=14, leading=20, spaceBefore=10, spaceAfter=6)
    h3 = ParagraphStyle("H3CJK", parent=normal, fontSize=11, leading=16, spaceBefore=8, spaceAfter=4)
    table_style = ParagraphStyle(
        "TableText",
        parent=normal,
        fontName=font_name,
        fontSize=6.5,
        leading=8.5,
        backColor=colors.HexColor("#f8fafc"),
        borderColor=colors.HexColor("#d8dee9"),
        borderWidth=0.25,
        borderPadding=4,
    )

    doc = SimpleDocTemplate(
        str(pdf_path),
        pagesize=A4,
        rightMargin=12 * mm,
        leftMargin=12 * mm,
        topMargin=14 * mm,
        bottomMargin=14 * mm,
        title=config.get("report", {}).get("title", "us-market-review"),
    )
    story: list[Any] = []
    table_lines: list[str] = []

    for raw_line in markdown_text.splitlines():
        line = raw_line.rstrip()
        if is_markdown_table_line(line):
            table_lines.append(line)
            continue

        flush_table(table_lines, story, table_style)

        if not line:
            story.append(Spacer(1, 2 * mm))
            continue
        if line.startswith("# "):
            story.append(Paragraph(escape(line[2:].strip()), h1))
            continue
        if line.startswith("## "):
            if story:
                story.append(Spacer(1, 2 * mm))
            story.append(Paragraph(escape(line[3:].strip()), h2))
            continue
        if line.startswith("### "):
            story.append(Paragraph(escape(line[4:].strip()), h3))
            continue
        if line == "\\pagebreak":
            story.append(PageBreak())
            continue

        html_line = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", line)
        story.append(Paragraph(escape(html_line), normal))

    flush_table(table_lines, story, table_style)
    doc.build(story)
    return warning


def write_outputs(config: dict[str, Any], markdown_text: str) -> tuple[Path, Path | None, list[str]]:
    output_dir = ensure_output_dir(config)
    report_date = output_dir.name
    base_name = f"us-market-review-{report_date}"
    markdown_path = output_dir / f"{base_name}.md"
    html_path = output_dir / f"{base_name}.html"
    pdf_path = output_dir / f"{base_name}.pdf"
    warnings: list[str] = []

    markdown_path.write_text(markdown_text, encoding="utf-8")
    html_path.write_text(markdown_to_html(markdown_text, config.get("report", {}).get("title", base_name)), encoding="utf-8")

    pdf_enabled = config.get("report", {}).get("pdf", {}).get("enabled", True)
    if pdf_enabled:
        try:
            warning = render_basic_pdf(markdown_text, pdf_path, config)
            if warning:
                warnings.append(warning)
        except Exception as exc:  # noqa: BLE001 - Markdown is the source of truth for MVP.
            warnings.append(f"PDF 生成失败: {exc}. 已保留 Markdown 和 HTML。")
            pdf_path = None
    else:
        pdf_path = None

    return markdown_path, pdf_path, warnings


def market_fetch_summary(market_data: dict[str, Any]) -> str:
    assets = market_data.get("assets", [])
    failed = [asset for asset in assets if asset.get("error")]
    return f"行情 {len(assets) - len(failed)}/{len(assets)} 成功，失败 {len(failed)} 个"


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


def run(config_path: str | Path, logger: logging.Logger | None = None) -> tuple[Path, Path | None, list[str], list[PushResult]]:
    logger = logger or setup_logger()
    logger.info("开始时间: %s", datetime.now().astimezone().isoformat(timespec="seconds"))

    config = load_config(config_path)
    logger.info("配置文件: %s", config_path)

    market_data = fetch_market_data(config)
    logger.info("数据获取结果: %s", market_fetch_summary(market_data))
    for asset in market_data.get("assets", []):
        if asset.get("error"):
            logger.warning("行情失败: ticker=%s reason=%s", asset.get("ticker"), asset.get("error"))

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
        logger.warning("报告生成路径: pdf=未生成")
    for warning in warnings:
        logger.warning("报告生成警告: %s", warning)

    push_results = send_outputs(markdown_path, pdf_path)
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
