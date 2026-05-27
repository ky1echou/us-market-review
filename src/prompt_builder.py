from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any

from .fetch_market import assets_by_category
from .indicators import breadth, format_number, format_percent, format_rsi, rank_by_metric, safe_float


def md_escape(value: Any) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", " ").strip()


def markdown_table(headers: list[str], rows: list[list[Any]]) -> str:
    escaped_headers = [md_escape(header) for header in headers]
    lines = [
        "| " + " | ".join(escaped_headers) + " |",
        "| " + " | ".join(["---"] * len(escaped_headers)) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(md_escape(cell) for cell in row) + " |")
    return "\n".join(lines)


def asset_source_text(asset: dict[str, Any]) -> str:
    source = asset.get("source", {})
    provider = source.get("provider", "N/A")
    fetched_at = source.get("fetched_at", "N/A")
    return f"{provider}; 获取: {fetched_at}"


def asset_row(asset: dict[str, Any], include_theme: bool = False) -> list[Any]:
    row = [
        f"{asset.get('name', asset.get('ticker'))} ({asset.get('ticker')})",
        format_number(asset.get("last_close")),
        format_percent(asset.get("daily_change")),
        format_percent(asset.get("ma5_deviation")),
        format_percent(asset.get("ma20_deviation")),
        format_rsi(asset.get("rsi14")),
        asset.get("as_of") or "N/A",
        asset_source_text(asset),
    ]
    if include_theme:
        row.insert(1, asset.get("theme") or "N/A")
    return row


def price_table(assets: list[dict[str, Any]], include_theme: bool = False) -> str:
    headers = ["标的", "最新收盘", "日涨跌幅", "MA5偏离", "MA20偏离", "RSI14", "数据日期", "来源/获取时间"]
    if include_theme:
        headers.insert(1, "主题")
    rows = [asset_row(asset, include_theme=include_theme) for asset in assets]
    if not rows:
        return "暂无可用行情数据。"
    return markdown_table(headers, rows)


def news_source_text(item: dict[str, Any]) -> str:
    parts = [item.get("source") or "N/A"]
    if item.get("published_at"):
        parts.append(f"发布: {item['published_at']}")
    parts.append(f"获取: {item.get('fetched_at', 'N/A')}")
    return "; ".join(parts)


def news_bullets(items: list[dict[str, Any]], limit: int = 8) -> str:
    if not items:
        return "- 未从配置 RSS 源获取到符合时间窗口的新闻。"

    lines: list[str] = []
    for item in items[:limit]:
        title = item.get("title") or "Untitled"
        link = item.get("link") or ""
        tags = f" 标签: {', '.join(item.get('tags', []))}." if item.get("tags") else ""
        title_text = f"[{title}]({link})" if link else title
        lines.append(f"- {title_text}。{tags} 来源: {news_source_text(item)}")
    return "\n".join(lines)


def filter_news_by_tag(news_items: list[dict[str, Any]], tags: list[str]) -> list[dict[str, Any]]:
    wanted = set(tags)
    return [item for item in news_items if wanted.intersection(set(item.get("tags", [])))]


def match_news_for_asset(asset: dict[str, Any], news_items: list[dict[str, Any]]) -> str:
    ticker = asset.get("ticker", "")
    name = asset.get("name", "")
    candidates = []
    for item in news_items:
        text = f"{item.get('title', '')} {item.get('summary', '')}".lower()
        if ticker.lower() in text or (name and name.lower() in text) or ticker in item.get("tags", []):
            candidates.append(item)

    if not candidates:
        return "未在 RSS 中匹配到明确原因"

    item = candidates[0]
    title = item.get("title") or "Untitled"
    link = item.get("link") or ""
    title_text = f"[{title}]({link})" if link else title
    return f"{title_text}；来源: {news_source_text(item)}"


def market_tone(index_assets: list[dict[str, Any]]) -> str:
    valid = [asset for asset in index_assets if safe_float(asset.get("daily_change")) is not None]
    if not valid:
        return "指数数据不足，暂不判断盘面方向。"
    avg_change = sum(asset["daily_change"] for asset in valid) / len(valid)
    counts = breadth(valid)
    if avg_change > 0.005:
        direction = "偏强"
    elif avg_change < -0.005:
        direction = "偏弱"
    else:
        direction = "震荡"
    return f"主要指数平均日涨跌幅为 {format_percent(avg_change)}，上涨 {counts['up']} 个、下跌 {counts['down']} 个，盘面整体呈现{direction}。"


def sector_summary(etfs: list[dict[str, Any]]) -> str:
    ranked = rank_by_metric(etfs, "daily_change", reverse=True)
    if not ranked:
        return "ETF 数据不足，暂不判断行业强弱。"
    strongest = ranked[0]
    weakest = ranked[-1]
    return (
        f"行业/主题 ETF 中，{strongest.get('name')}({strongest.get('ticker')}) 居前，"
        f"日涨跌幅 {format_percent(strongest.get('daily_change'))}；"
        f"{weakest.get('name')}({weakest.get('ticker')}) 居后，"
        f"日涨跌幅 {format_percent(weakest.get('daily_change'))}。"
    )


def mover_tables(stocks: list[dict[str, Any]], news_items: list[dict[str, Any]], limit: int) -> str:
    ranked_up = rank_by_metric(stocks, "daily_change", reverse=True)[:limit]
    ranked_down = rank_by_metric(stocks, "daily_change", reverse=False)[:limit]
    rows: list[list[Any]] = []

    for direction, assets in (("领涨", ranked_up), ("领跌", ranked_down)):
        for asset in assets:
            rows.append(
                [
                    direction,
                    f"{asset.get('name')} ({asset.get('ticker')})",
                    asset.get("theme") or "N/A",
                    format_percent(asset.get("daily_change")),
                    format_percent(asset.get("ma20_deviation")),
                    format_rsi(asset.get("rsi14")),
                    match_news_for_asset(asset, news_items),
                    asset.get("as_of") or "N/A",
                    asset_source_text(asset),
                ]
            )

    return markdown_table(
        ["方向", "股票", "主题", "日涨跌幅", "MA20偏离", "RSI14", "异动原因/新闻证据", "数据日期", "行情来源/获取时间"],
        rows,
    )


def ah_mapping_table(config: dict[str, Any], stocks: list[dict[str, Any]], etfs: list[dict[str, Any]]) -> str:
    by_ticker = {asset.get("ticker"): asset for asset in stocks + etfs}
    rows: list[list[Any]] = []
    for item in config.get("ah_mapping", []):
        ticker = item.get("us_ticker")
        asset = by_ticker.get(ticker, {})
        rows.append(
            [
                f"{item.get('us_name', ticker)} ({ticker})",
                item.get("theme", "N/A"),
                format_percent(asset.get("daily_change")),
                format_percent(asset.get("ma20_deviation")),
                item.get("a_h_mapping", "N/A"),
                item.get("watch_points", "N/A"),
                asset.get("as_of") or "N/A",
                asset_source_text(asset) if asset else "未获取到行情",
            ]
        )
    return markdown_table(
        ["美股/ETF", "主题", "美股表现", "MA20偏离", "潜在A/H映射", "盘前关注", "数据日期", "来源/获取时间"],
        rows,
    )


def update_matrix(
    index_assets: list[dict[str, Any]],
    etfs: list[dict[str, Any]],
    stocks: list[dict[str, Any]],
    news_items: list[dict[str, Any]],
) -> str:
    rows: list[list[Any]] = []
    ranked_etfs = rank_by_metric(etfs, "daily_change", reverse=True)
    ranked_stocks = rank_by_metric(stocks, "daily_change", reverse=True)
    ai_news = filter_news_by_tag(news_items, ["AI"])
    macro_news = filter_news_by_tag(news_items, ["宏观"])

    if index_assets:
        rows.append(["指数风险偏好", "高", market_tone(index_assets), "主要指数行情表", "观察美债收益率与纳指相对强弱"])

    if ranked_etfs:
        rows.append(
            [
                "行业结构",
                "高",
                sector_summary(etfs),
                "行业/ETF结构表",
                "关注强势行业是否由科技/半导体扩散至周期或防御",
            ]
        )

    if ranked_stocks:
        leader = ranked_stocks[0]
        laggard = rank_by_metric(stocks, "daily_change", reverse=False)[0]
        rows.append(
            [
                "科技龙头",
                "高",
                f"{leader.get('ticker')} 领涨 {format_percent(leader.get('daily_change'))}，{laggard.get('ticker')} 居后 {format_percent(laggard.get('daily_change'))}",
                "重点科技股行情表",
                "关注是否有财报、产品或监管新闻形成持续催化",
            ]
        )

    rows.append(
        [
            "AI主线",
            "高",
            f"RSS 时间窗内匹配到 {len(ai_news)} 条 AI 相关新闻",
            "AI新闻标签匹配",
            "重点看 NVDA/AVGO/AMD/云厂商资本开支线索",
        ]
    )
    rows.append(
        [
            "宏观与地缘",
            "中",
            f"RSS 时间窗内匹配到 {len(macro_news)} 条宏观/地缘相关新闻",
            "宏观新闻标签匹配",
            "关注利率、美元、油价与风险资产联动",
        ]
    )
    return markdown_table(["主题", "重要性", "变化", "证据", "下一步跟踪"], rows)


def source_audit_table(market_data: dict[str, Any], news_data: dict[str, Any]) -> str:
    rows: list[list[Any]] = []
    metadata = market_data.get("metadata", {})
    rows.append(
        [
            "行情",
            metadata.get("source", "N/A"),
            f"period={metadata.get('period')}, interval={metadata.get('interval')}",
            metadata.get("fetched_at", "N/A"),
            "价格、涨跌幅、MA5/MA20偏离、RSI",
        ]
    )
    news_meta = news_data.get("metadata", {})
    rows.append(
        [
            "新闻",
            news_meta.get("source", "N/A"),
            f"lookback_hours={news_meta.get('lookback_hours')}",
            news_meta.get("fetched_at", "N/A"),
            "新闻标题、链接、发布时间、标签",
        ]
    )
    return markdown_table(["类型", "来源", "参数", "获取时间", "用途"], rows)


def build_future_llm_prompt(market_data: dict[str, Any], news_data: dict[str, Any]) -> str:
    """Return a future LLM prompt scaffold without using it in the MVP pipeline."""
    return (
        "你是一名卖方策略/科技分析师。请仅基于输入数据生成中文美股复盘，"
        "不得编造数字或新闻原因。所有关键判断必须引用来源、数据日期和获取时间。\n\n"
        f"行情数据: {market_data.get('metadata', {})}\n"
        f"新闻数据: {news_data.get('metadata', {})}\n"
    )


def build_markdown_report(config: dict[str, Any], market_data: dict[str, Any], news_data: dict[str, Any]) -> str:
    report_config = config.get("report", {})
    title = report_config.get("title", "中文美股复盘报告")
    author = report_config.get("author", "us-market-review")
    market_meta = market_data.get("metadata", {})
    news_meta = news_data.get("metadata", {})
    generated_at = market_meta.get("fetched_at") or datetime.now().isoformat(timespec="seconds")

    index_assets = assets_by_category(market_data, "index")
    etfs = assets_by_category(market_data, "sector_etf")
    stocks = assets_by_category(market_data, "key_stock")
    macro_assets = assets_by_category(market_data, "macro_asset")
    news_items = news_data.get("items", [])
    top_movers_count = int(report_config.get("top_movers_count", 5))

    ai_news = filter_news_by_tag(news_items, ["AI"])
    macro_news = filter_news_by_tag(news_items, ["宏观"])
    china_news = filter_news_by_tag(news_items, ["中概/AH"])

    errors_by_type: dict[str, list[str]] = defaultdict(list)
    for asset in market_data.get("assets", []):
        if asset.get("error"):
            errors_by_type["行情"].append(f"{asset.get('ticker')}: {asset.get('error')}")
    for error in news_data.get("errors", []):
        errors_by_type["新闻"].append(f"{error.get('source')}: {error.get('error')}")

    sections = [
        f"# {title}",
        "",
        f"- 生成时间: {generated_at}",
        f"- 作者: {author}",
        f"- 行情来源: {market_meta.get('source', 'N/A')}；获取时间: {market_meta.get('fetched_at', 'N/A')}",
        f"- 新闻来源: {news_meta.get('source', 'N/A')}；获取时间: {news_meta.get('fetched_at', 'N/A')}",
        "- 口径说明: 本报告仅基于配置数据源自动生成；未获取到可靠来源的数字或原因不会被补写。",
        "",
        "## 一、美股指数概况",
        "",
        market_tone(index_assets),
        "",
        price_table(index_assets),
        "",
        "## 二、宏观与地缘",
        "",
        "### 宏观资产表现",
        "",
        price_table(macro_assets),
        "",
        "### 宏观与地缘新闻",
        "",
        news_bullets(macro_news, limit=8),
        "",
        "## 三、行业与ETF结构",
        "",
        sector_summary(etfs),
        "",
        price_table(rank_by_metric(etfs, "daily_change", reverse=True)),
        "",
        "## 四、美股科技股跟踪",
        "",
        price_table(rank_by_metric(stocks, "daily_change", reverse=True), include_theme=True),
        "",
        "### 异动股原因核对",
        "",
        mover_tables(stocks, news_items, top_movers_count),
        "",
        "## 五、AI主线与重要催化",
        "",
        f"RSS 时间窗内匹配到 {len(ai_news)} 条 AI 相关新闻。以下仅列示已抓取来源，不推断未披露催化。",
        "",
        news_bullets(ai_news, limit=10),
        "",
        "## 六、AH盘前映射",
        "",
        ah_mapping_table(config, stocks, etfs),
        "",
        "### 中概/AH相关新闻",
        "",
        news_bullets(china_news, limit=8),
        "",
        "## 七、Update Matrix",
        "",
        update_matrix(index_assets, etfs, stocks, news_items),
        "",
        "## 八、最终结论",
        "",
        f"- 指数层面: {market_tone(index_assets)}",
        f"- 结构层面: {sector_summary(etfs)}",
        f"- 科技股层面: 重点股票池中上涨/下跌家数为 {breadth(stocks)['up']}/{breadth(stocks)['down']}，强弱以表格中日涨跌幅和 MA20 偏离为准。",
        "- AI主线: 若 AI 相关新闻较多且半导体/云厂商同步走强，可作为次日 AH 算力链观察线索；若行情与新闻背离，优先等待公司级证据。",
        "- 风险提示: 自动报告不构成投资建议；盘后财报、监管新闻和宏观数据可能改变结论。",
        "",
    ]

    if report_config.get("include_raw_audit_section", True):
        sections.extend(
            [
                "## 数据审计",
                "",
                source_audit_table(market_data, news_data),
                "",
            ]
        )

    if errors_by_type:
        sections.extend(["## 抓取异常", ""])
        for error_type, errors in errors_by_type.items():
            sections.append(f"### {error_type}")
            sections.append("")
            sections.extend(f"- {error}" for error in errors)
            sections.append("")

    return "\n".join(sections).strip() + "\n"

