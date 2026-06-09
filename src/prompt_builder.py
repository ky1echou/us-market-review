from __future__ import annotations

from datetime import datetime
from typing import Any

from .fetch_market import assets_by_category
from .indicators import breadth, rank_by_metric, safe_float


INDEX_ORDER = ["SPY", "QQQ", "DIA", "IWM", "SMH", "SOXX", "VIX"]
FOCUS_ETFS = ["SPY", "QQQ", "DIA", "IWM", "SMH", "SOXX", "XLK", "XLF", "XLE", "XLV", "TLT", "GLD", "USO"]
MACRO_ASSETS = ["US10Y", "DXY", "TLT", "GLD", "USO", "CPER", "BTCUSD"]
MEGA_TECH = ["NVDA", "MSFT", "AAPL", "AMZN", "GOOGL", "META", "TSLA"]
AI_HARDWARE = ["AMD", "AVGO", "MU", "MRVL", "ARM"]
AI_APPS = ["SNOW", "NOW"]
AI_CHAIN = MEGA_TECH + AI_HARDWARE + AI_APPS


def md_escape(value: Any) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", " ").strip()


def markdown_table(headers: list[str], rows: list[list[Any]]) -> str:
    if not rows:
        return "本节标的数据不足；系统不会用空表替代正式判断。"
    lines = [
        "| " + " | ".join(md_escape(header) for header in headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(md_escape(cell) for cell in row) + " |")
    return "\n".join(lines)


def fmt_number(value: Any, digits: int = 2) -> str:
    number = safe_float(value)
    if number is None:
        return "暂缺"
    if abs(number) >= 1000:
        return f"{number:,.{digits}f}"
    return f"{number:.{digits}f}"


def fmt_percent(value: Any, digits: int = 2) -> str:
    number = safe_float(value)
    if number is None:
        return "暂缺"
    return f"{number * 100:+.{digits}f}%"


def fmt_rsi(value: Any) -> str:
    number = safe_float(value)
    return "暂缺" if number is None else f"{number:.1f}"


def valid_asset(asset: dict[str, Any] | None) -> bool:
    return bool(asset and safe_float(asset.get("last_close")) is not None and (safe_float(asset.get("daily_change")) is not None or asset.get("quote_success")))


def valid_assets(assets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [asset for asset in assets if valid_asset(asset)]


def by_ticker(assets: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(asset.get("ticker")): asset for asset in assets}


def pick_assets(assets: list[dict[str, Any]], tickers: list[str]) -> list[dict[str, Any]]:
    lookup = by_ticker(assets)
    return [lookup[ticker] for ticker in tickers if valid_asset(lookup.get(ticker))]


def asset_title(asset: dict[str, Any]) -> str:
    return f"{asset.get('name', asset.get('ticker'))}({asset.get('ticker')})"


def ticker_list(values: list[Any], limit: int = 80) -> str:
    clean = [str(value) for value in values if str(value)]
    if not clean:
        return "无"
    if len(clean) <= limit:
        return "、".join(clean)
    return "、".join(clean[:limit]) + f" 等{len(clean)}项"


def status_label(asset: dict[str, Any]) -> str:
    change = safe_float(asset.get("daily_change"))
    ma20 = safe_float(asset.get("ma20_deviation"))
    rsi = safe_float(asset.get("rsi14"))
    if change is None:
        return "价格可用/指标暂缺"
    if rsi is not None and rsi >= 70:
        return "高位强势"
    if rsi is not None and rsi <= 30:
        return "低位承压"
    if change > 0 and (ma20 or 0) > 0:
        return "强势延续"
    if change > 0:
        return "修复"
    if change < 0 and (ma20 or 0) < 0:
        return "弱势延续"
    if change < 0:
        return "回撤"
    return "震荡"


def source_label(asset: dict[str, Any]) -> str:
    source = asset.get("source", {}) if isinstance(asset.get("source"), dict) else {}
    provider = source.get("provider") or "未披露"
    as_of = asset.get("as_of") or source.get("as_of") or "日期暂缺"
    if asset.get("from_cache") or source.get("from_cache"):
        return f"缓存/{as_of}"
    return f"{provider}/{as_of}"


def source_note(market_data: dict[str, Any]) -> str:
    metadata = market_data.get("metadata", {})
    counts = metadata.get("provider_counts", {}) or {}
    count_text = "、".join(f"{provider} {count}项" for provider, count in counts.items()) or "未披露"
    success_ratio = float(metadata.get("success_ratio") or 0.0) * 100
    total = int(metadata.get("total_count") or 0)
    live = int(metadata.get("live_success_count") or 0)
    cache = int(metadata.get("cache_success_count") or 0)
    live_ratio = live / total * 100 if total else 0.0
    cache_ratio = cache / total * 100 if total else 0.0
    assets = market_data.get("assets", [])
    cache_detail = [f"{asset.get('ticker')}({asset.get('as_of') or asset.get('source', {}).get('as_of') or '日期暂缺'})" for asset in assets if asset.get("from_cache")]
    failed_tickers = metadata.get("failed_tickers", []) or [item.get("ticker") for item in metadata.get("failed_details", [])]
    return (
        f"数据口径：行情源 {metadata.get('source') or '未披露'}；获取时间 {metadata.get('fetched_at') or '未披露'}；"
        f"完整池行情可用 {metadata.get('success_count', 0)}/{total}，可用率 {success_ratio:.1f}%；"
        f"实时获取 {live}（{live_ratio:.1f}%），缓存降级 {cache}（{cache_ratio:.1f}%），失败 {metadata.get('failed_count', 0)}；"
        f"来源分布：{count_text}。\n\n"
        f"缓存降级 ticker：{ticker_list(cache_detail)}。\n\n"
        f"失败 ticker：{ticker_list([ticker for ticker in failed_tickers if ticker])}。"
    )


def price_table(assets: list[dict[str, Any]]) -> str:
    rows = []
    for asset in valid_assets(assets):
        rows.append([
            asset_title(asset),
            fmt_number(asset.get("last_close")),
            fmt_percent(asset.get("daily_change")),
            fmt_percent(asset.get("ma5_deviation")),
            fmt_percent(asset.get("ma20_deviation")),
            fmt_rsi(asset.get("rsi14")),
            status_label(asset),
            source_label(asset),
        ])
    return markdown_table(["标的", "收盘", "涨跌幅", "MA5", "MA20", "RSI", "状态", "数据源/日期"], rows)


def news_source_text(item: dict[str, Any]) -> str:
    parts = [item.get("source") or "未披露"]
    if item.get("classification"):
        parts.append("级别: " + "/".join(item.get("classification", [])))
    if item.get("published_at"):
        parts.append(f"发布: {item['published_at']}")
    parts.append(f"获取: {item.get('fetched_at') or '未披露'}")
    return "；".join(parts)


def news_bullets(items: list[dict[str, Any]], limit: int = 5) -> str:
    if not items:
        return "- 公开新闻窗口内未发现高置信增量催化，暂不强行归因。"
    lines: list[str] = []
    for item in items[:limit]:
        title = item.get("title") or "未披露标题"
        link = item.get("link") or ""
        title_text = f"[{title}]({link})" if link else title
        lines.append(f"- {title_text}。来源：{news_source_text(item)}")
    return "\n".join(lines)


def filter_news_by_tag(news_items: list[dict[str, Any]], tag: str) -> list[dict[str, Any]]:
    return [item for item in news_items if tag in set(item.get("tags", []))]


def company_news_for_ticker(news_items: list[dict[str, Any]], ticker: str) -> list[dict[str, Any]]:
    return [item for item in news_items if ticker in set(item.get("company_tickers", [])) and "公司级" in set(item.get("classification", []))]


def match_news_for_asset(asset: dict[str, Any], news_items: list[dict[str, Any]]) -> str:
    matches = company_news_for_ticker(news_items, str(asset.get("ticker", "")))
    if not matches:
        return "未匹配到明确公司级催化，按盘面强弱跟踪"
    item = matches[0]
    title = item.get("title") or "未披露标题"
    link = item.get("link") or ""
    title_text = f"[{title}]({link})" if link else title
    return f"{title_text}；来源：{news_source_text(item)}"


def change_of(lookup: dict[str, dict[str, Any]], ticker: str) -> float | None:
    asset = lookup.get(ticker)
    return safe_float(asset.get("daily_change")) if asset else None


def market_regime(assets: list[dict[str, Any]], stocks: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    lookup = by_ticker(assets + (stocks or []))
    spy = change_of(lookup, "SPY")
    qqq = change_of(lookup, "QQQ")
    iwm = change_of(lookup, "IWM")
    smh = change_of(lookup, "SMH")
    soxx = change_of(lookup, "SOXX")
    vix = change_of(lookup, "VIX")
    broad_values = [value for value in [spy, qqq, iwm] if value is not None]
    broad_avg = sum(broad_values) / len(broad_values) if broad_values else None
    risk_off = (spy is not None and spy <= -0.01) or (qqq is not None and qqq <= -0.01) or (vix is not None and vix >= 0.10)
    return {"spy": spy, "qqq": qqq, "iwm": iwm, "smh": smh, "soxx": soxx, "vix": vix, "broad_avg": broad_avg, "risk_off": risk_off}


def market_tone(index_assets: list[dict[str, Any]], stocks: list[dict[str, Any]] | None = None) -> str:
    valid = [asset for asset in valid_assets(index_assets) if asset.get("ticker") != "VIX"]
    if not valid:
        return "判断：主要宽基 ETF 可用数据不足，系统不应生成正式复盘。"
    counts = breadth(valid)
    regime = market_regime(index_assets, stocks)
    if regime["risk_off"]:
        return (
            f"判断：昨夜不是全面风险偏好回暖，而是宽基承压、波动率抬升下的结构分化。"
            f"宽基平均涨跌幅 {fmt_percent(regime['broad_avg'])}，上涨 {counts['up']} 个、下跌 {counts['down']} 个；"
            f"VIX {fmt_percent(regime['vix'])}，半导体或存储链若逆势走强，也只能理解为局部主线占优。"
        )
    if regime["broad_avg"] is not None and regime["broad_avg"] > 0.003 and counts["up"] >= counts["down"]:
        return f"判断：风险偏好温和修复，但仍需看科技、半导体和小盘能否同步扩散。宽基平均涨跌幅 {fmt_percent(regime['broad_avg'])}。"
    return f"判断：盘面以震荡分化为主，尚未形成全面扩散。宽基平均涨跌幅 {fmt_percent(regime['broad_avg'])}，上涨 {counts['up']} 个、下跌 {counts['down']} 个。"


def one_sentence(etfs: list[dict[str, Any]], stocks: list[dict[str, Any]]) -> str:
    regime = market_regime(etfs, stocks)
    if regime["risk_off"]:
        return "一句话总判断：昨夜美股并非全面风险偏好回暖，而是大型科技权重、宽基和波动率共同约束下的结构分化；AH盘前不宜全面追高，优先看强映射方向和开盘承接。"
    ranked_stocks = rank_by_metric(valid_assets(stocks), "daily_change", reverse=True)
    lead_stock = asset_title(ranked_stocks[0]) if ranked_stocks else "强势科技股"
    return f"一句话总判断：昨夜美股风险偏好边际修复，但主线仍要看 {lead_stock} 代表的AI链能否从个股扩散到ETF和AH映射。"


def three_questions(assets: list[dict[str, Any]]) -> str:
    regime = market_regime(assets)
    risk = "全面回暖" if not regime["risk_off"] and (regime["spy"] or 0) > 0 and (regime["qqq"] or 0) > 0 else "局部修复/结构分化"
    size = "小盘相对占优" if regime["iwm"] is not None and regime["spy"] is not None and regime["iwm"] > regime["spy"] else "大盘相对占优或小盘承接不足"
    semi = regime["smh"] if regime["smh"] is not None else regime["soxx"]
    tech = "科技/半导体相对占优" if semi is not None and regime["spy"] is not None and semi > regime["spy"] else "科技/半导体主导性需要确认"
    return markdown_table(
        ["问题", "回答", "观察口径"],
        [
            ["风险偏好是全面回暖还是局部修复？", risk, "SPY、QQQ、IWM 与 VIX 同时验证"],
            ["大盘/小盘谁更强？", size, "IWM 相对 SPY"],
            ["科技/半导体是否仍是主导？", tech, "SMH/SOXX 相对 SPY，叠加NVDA/AMD/MU/MRVL"],
        ],
    )


def sector_summary(etfs: list[dict[str, Any]]) -> str:
    ranked = rank_by_metric(valid_assets(etfs), "daily_change", reverse=True)
    if not ranked:
        return "判断：ETF 数据不足，暂不做行业强弱排序。"
    strongest = ranked[0]
    weakest = ranked[-1]
    return (
        f"判断：行业结构看{asset_title(strongest)}相对最强，{asset_title(weakest)}相对最弱；"
        f"前者涨跌幅 {fmt_percent(strongest.get('daily_change'))}，后者 {fmt_percent(weakest.get('daily_change'))}。"
        "交易含义：若强势集中在半导体/AI硬件，AH以强映射链条为主；若防御或能源领先，则降低成长股追高冲动。"
    )


def flow_direction(assets: list[dict[str, Any]]) -> str:
    lookup = by_ticker(assets)
    pairs = {
        "成长/价值": (lookup.get("QQQ"), lookup.get("XLF")),
        "大盘/小盘": (lookup.get("SPY"), lookup.get("IWM")),
        "科技/能源": (lookup.get("XLK"), lookup.get("XLE")),
        "半导体/宽基": (lookup.get("SMH") or lookup.get("SOXX"), lookup.get("SPY")),
        "防御/风险": (lookup.get("XLV"), lookup.get("QQQ")),
    }
    judgements = []
    for label, pair in pairs.items():
        left, right = pair
        if not left or not right:
            continue
        left_change = safe_float(left.get("daily_change"))
        right_change = safe_float(right.get("daily_change"))
        if left_change is None or right_change is None:
            continue
        stronger = asset_title(left) if left_change >= right_change else asset_title(right)
        judgements.append(f"{label}看{stronger}更强")
    return "资金切换方向：" + "；".join(judgements) + "。" if judgements else "资金切换方向：等待开盘后验证扩散。"


def macro_block(title: str, items: list[dict[str, Any]], us_impact: str, ah_impact: str) -> str:
    return "\n".join([f"### {title}", news_bullets(items, limit=3), f"- 对美股影响：{us_impact}", f"- 对AH影响：{ah_impact}", ""])


def grouped_stock_table(title: str, stocks: list[dict[str, Any]], tickers: list[str], news_items: list[dict[str, Any]]) -> str:
    rows = []
    lookup = by_ticker(stocks)
    for ticker in tickers:
        asset = lookup.get(ticker)
        if not valid_asset(asset):
            continue
        rows.append([asset_title(asset), asset.get("theme") or "未归类", fmt_percent(asset.get("daily_change")), fmt_percent(asset.get("ma20_deviation")), fmt_rsi(asset.get("rsi14")), status_label(asset), source_label(asset), match_news_for_asset(asset, news_items)])
    return f"### {title}\n" + markdown_table(["股票", "主线", "涨跌幅", "MA20", "RSI", "状态", "数据源/日期", "公司级催化"], rows)


def ai_layered_summary(stocks: list[dict[str, Any]], etfs: list[dict[str, Any]], ai_news: list[dict[str, Any]]) -> str:
    lookup = by_ticker(stocks + etfs)
    layers = [
        ("算力/AI芯片", ["NVDA", "AMD", "AVGO", "ARM"]),
        ("半导体/HBM/网络", ["SMH", "SOXX", "MU", "MRVL"]),
        ("云厂商AI Capex", ["MSFT", "AMZN", "GOOGL", "META"]),
        ("SaaS / AI应用", ["SNOW", "NOW"]),
        ("端侧/机器人/自动驾驶", ["AAPL", "TSLA"]),
    ]
    rows = []
    for layer, tickers in layers:
        assets = [lookup[ticker] for ticker in tickers if valid_asset(lookup.get(ticker))]
        counts = breadth(assets)
        leader = rank_by_metric(assets, "daily_change", reverse=True)
        judgement = "强化" if counts["up"] > counts["down"] else "分化" if counts["up"] else "削弱"
        rows.append([layer, f"上涨 {counts['up']} / 下跌 {counts['down']}", asset_title(leader[0]) if leader else "暂缺", judgement])
    return markdown_table(["AI分层", "盘面广度", "代表强势", "主线判断"], rows) + f"\n\n产业级 AI 催化新闻 {len(ai_news)} 条，新闻只作为催化，不替代行情判断。"


def ah_action(strength: str, asset: dict[str, Any]) -> str:
    change = safe_float(asset.get("daily_change"))
    if "强" in strength and change is not None and change > 0:
        return "可关注，等开盘承接确认"
    if "强" in strength:
        return "仅观察强映射是否抗跌"
    if "弱" in strength:
        return "仅情绪验证，不追高"
    return "等待确认，避免直接追高"


def ah_rows(config: dict[str, Any], stocks: list[dict[str, Any]], etfs: list[dict[str, Any]]) -> tuple[list[list[Any]], list[list[Any]]]:
    lookup = by_ticker(stocks + etfs)
    strong_rows: list[list[Any]] = []
    other_rows: list[list[Any]] = []
    for item in config.get("ah_mapping", []):
        ticker = item.get("us_ticker")
        asset = lookup.get(ticker, {})
        if not valid_asset(asset):
            continue
        strength = item.get("mapping_strength", "情绪映射")
        base = [
            f"{item.get('us_name', ticker)}({ticker})",
            item.get("theme", "未归类"),
            fmt_percent(asset.get("daily_change")),
            item.get("a_h_mapping", "未配置"),
            ah_action(strength, asset),
        ]
        if "强" in strength:
            strong_rows.append(base)
        else:
            other_rows.append([base[0], strength, base[2], base[3], base[4]])
    return strong_rows, other_rows


def ah_mapping_table(config: dict[str, Any], stocks: list[dict[str, Any]], etfs: list[dict[str, Any]]) -> str:
    strong_rows, other_rows = ah_rows(config, stocks, etfs)
    parts = [
        "#### 强映射方向",
        markdown_table(["美股线索", "主题", "美股表现", "A/H方向", "盘前动作"], strong_rows),
        "#### 弱映射 / 情绪映射",
        markdown_table(["美股线索", "映射强度", "美股表现", "A/H方向", "盘前动作"], other_rows),
    ]
    return "\n\n".join(parts)


def update_matrix(index_assets: list[dict[str, Any]], etfs: list[dict[str, Any]], stocks: list[dict[str, Any]], news_items: list[dict[str, Any]], market_meta: dict[str, Any]) -> str:
    ai_news = filter_news_by_tag(news_items, "AI")
    macro_news = filter_news_by_tag(news_items, "宏观")
    geo_news = filter_news_by_tag(news_items, "地缘")
    stock_breadth = breadth(valid_assets(stocks))
    regime = market_regime(index_assets + etfs, stocks)
    risk_status = "削弱" if regime["risk_off"] else "强化"
    macro_status = "降级" if market_meta.get("macro_degraded") else "不变"
    rows = [
        ["风险偏好取决于宽基扩散", market_tone(index_assets, stocks), risk_status, "若SPY/QQQ低开且VIX继续上行，AH不做全面追高"],
        ["AI仍是最重要主线", f"科技观察池上涨 {stock_breadth['up']} 家、下跌 {stock_breadth['down']} 家；产业级AI新闻 {len(ai_news)} 条", "分化" if stock_breadth["down"] else "强化", "只追踪算力、HBM、AI网络、数据中心等强映射链"],
        ["行业结构决定AH映射强度", sector_summary(etfs), "强化" if valid_assets(etfs) else "不变", "强映射方向看光模块、PCB、服务器、HBM、数据中心；弱映射仅验证情绪"],
        ["宏观扰动仍需压估值", f"宏观/地缘新闻 {len(macro_news) + len(geo_news)} 条；{market_meta.get('macro_degraded_message', '宏观数据可用')}。", macro_status, "观察美债、美元、黄金、原油、铜、比特币联动"],
    ]
    return markdown_table(["昨日判断", "隔夜新事实", "状态", "今日盘前动作"], rows)


def final_watchlist(etfs: list[dict[str, Any]], stocks: list[dict[str, Any]], news_items: list[dict[str, Any]], market_meta: dict[str, Any]) -> list[str]:
    regime = market_regime(etfs, stocks)
    ranked_stocks = rank_by_metric(valid_assets(stocks), "daily_change", reverse=True)
    lead_stock = asset_title(ranked_stocks[0]) if ranked_stocks else "相对强势科技股"
    ai_count = len(filter_news_by_tag(news_items, "AI"))
    headline = "AH整体情绪：偏谨慎，结构分化强于全面风险偏好修复；先看强映射链承接，不做指数级乐观外推。" if regime["risk_off"] else "AH整体情绪：边际修复，但仍需要开盘成交和行业扩散验证。"
    if market_meta.get("macro_degraded"):
        headline += " 宏观利率/美元数据缺失，宏观判断降级。"
    return [
        f"- {headline}",
        f"- 最强映射方向：围绕 {lead_stock} 所在链条，优先看AI算力、HBM/存储、AI网络、光模块、PCB、服务器、液冷、数据中心。",
        "- 不追高方向：大型科技权重若拖累宽基，弱映射的软件、云、港股互联网只做情绪验证；没有ETF和成交扩散前不做全面追高。",
        "- 风险点：VIX继续上行、美债/美元反向扰动、地缘科技和出口管制消息，都会压制估值和AH风险偏好。",
        f"- 盘前关注：产业级AI新闻 {ai_count} 条的真实映射方向、SMH/SOXX相对SPY、IWM小盘承接、TSLA机器人/自动驾驶、港股互联网情绪验证。",
    ]


def report_date_from_metadata(market_data: dict[str, Any]) -> tuple[str, str]:
    fetched_at = market_data.get("metadata", {}).get("fetched_at") or datetime.now().isoformat(timespec="minutes")
    clean = str(fetched_at).replace("T", " ")
    return clean[:10], clean[:16]


def build_markdown_report(config: dict[str, Any], market_data: dict[str, Any], news_data: dict[str, Any]) -> str:
    market_meta = market_data.get("metadata", {})
    news_items = news_data.get("items", [])
    report_date, generated_minute = report_date_from_metadata(market_data)

    index_assets = pick_assets(assets_by_category(market_data, "index"), INDEX_ORDER)
    sector_assets = assets_by_category(market_data, "sector_etf")
    macro_assets = pick_assets(assets_by_category(market_data, "macro_asset"), MACRO_ASSETS)
    tradable_assets = index_assets + sector_assets + macro_assets
    focus_etfs = pick_assets(tradable_assets, FOCUS_ETFS)
    stocks = assets_by_category(market_data, "key_stock")
    mega_tech = pick_assets(stocks, MEGA_TECH)
    hardware_assets = pick_assets(stocks, AI_HARDWARE)
    app_assets = pick_assets(stocks, AI_APPS)
    ai_assets = pick_assets(stocks, AI_CHAIN)

    ai_news = filter_news_by_tag(news_items, "AI")
    macro_news = filter_news_by_tag(news_items, "宏观")
    china_news = [item for item in filter_news_by_tag(news_items, "中概/AH") if "地缘" not in set(item.get("tags", []))]
    geo_news = filter_news_by_tag(news_items, "地缘")
    macro_degraded = bool(market_meta.get("macro_degraded"))
    macro_intro = "判断：宏观利率/美元数据缺失，宏观判断降级；本节只保留已取得的大宗和风险资产，不输出正式宏观方向。" if macro_degraded else "判断：宏观变量决定估值弹性，地缘科技消息决定AI链风险溢价，二者都不能用单条新闻直接替代行情判断。"
    macro_trade = "交易含义：US10Y/DXY 缺失时，不对利率、美元和估值方向做正式判断；等待数据补齐后再确认AH成长/价值切换。" if macro_degraded else "交易含义：美债、美元、黄金、原油、铜、比特币共同决定估值、通胀和风险偏好的边际方向。"
    macro_us_impact = "降级：等待US10Y/DXY补齐后再确认利率和美元对估值的影响。" if macro_degraded else "利率和降息预期仍是估值锚，科技股对收益率变化更敏感。"
    macro_ah_impact = "降级：AH成长/恒生科技只做情绪观察，不做宏观方向外推。" if macro_degraded else "若美债利率下行，AH成长与恒生科技情绪更容易修复；若利率上行，则偏压制估值。"

    title = f"{report_date} 美股复盘（昨夜）"
    data_line = f"生成时间：{generated_minute} | 数据：{market_meta.get('source', '行情源')} + 公开新闻 | 分析：{config.get('report', {}).get('author', 'AI投研 · 美股版')}"

    sections = [
        f"# {title}", data_line, "", source_note(market_data), "", one_sentence(focus_etfs, stocks), "",
        "## 一、美股指数概况", "", market_tone(index_assets, stocks), "", price_table(index_assets), "", "交易含义：宽基承压且VIX上行时，AH不宜按全面风险偏好修复交易；若半导体局部强势，只能映射到强产业链。", "",
        "### 三个问题回答", "", three_questions(focus_etfs + index_assets), "",
        "## 二、宏观与地缘", "", macro_intro, "", "### 宏观利率与大宗商品", price_table(macro_assets), "", macro_trade, "",
        macro_block("美国经济及美联储降息", macro_news, macro_us_impact, macro_ah_impact),
        macro_block("中国经济及政策", china_news, "中概和跨国消费链更敏感，但不替代美股自身业绩线索。", "政策和中概风险偏好会影响港股互联网、恒生科技和A股平台经济映射。"),
        macro_block("地缘冲突 / 地缘科技", geo_news, "地缘升温通常提高避险和油金波动，出口管制会提高AI链风险溢价。", "油价、黄金和地缘科技消息会影响AH资源品、半导体设备、AI硬件和出口链风险偏好。"),
        "## 三、行业与 ETF 结构", "", sector_summary(focus_etfs), "", flow_direction(focus_etfs), "", price_table(focus_etfs), "", "交易含义：本节不只看涨跌幅，更看成长/价值、大盘/小盘、科技/半导体/能源/金融/防御之间的资金切换。", "",
        "## 四、美股科技股跟踪", "", "判断：科技股必须拆成大型平台、AI硬件、AI应用三层，不能把单一个股或单一新闻误读成整条AI链共振。", "", grouped_stock_table("大型科技股", stocks, MEGA_TECH, news_items), "", grouped_stock_table("半导体 / AI硬件", stocks, AI_HARDWARE, news_items), "", grouped_stock_table("AI应用 / SaaS", stocks, AI_APPS, news_items), "", "### 科技股异动总表", price_table(mega_tech + hardware_assets + app_assets), "", "交易含义：大型科技承压时压制指数，但若MU/MRVL/AVGO等硬件链逆势，AH只做结构映射，不做全面乐观。", "",
        "## 五、AI主线与重要催化", "", "判断：AI主线要按算力、半导体、云厂商、应用、端侧分层，产业新闻只作为催化，不能替代个股行情。", "", ai_layered_summary(stocks, focus_etfs, ai_news), "", price_table(ai_assets), "", "### 重要信息 / AI产业催化", news_bullets(ai_news, limit=10), "", "交易含义：产业级新闻进入主题判断，公司级新闻才进入个股异动归因；无明确公司级催化时不强行解释个股涨跌。", "",
        "## 六、AH盘前映射", "", "判断：AH盘前只对强映射方向给动作，弱映射和情绪映射必须等待A股/港股开盘验证。", "", "### 美股映射", ah_mapping_table(config, stocks, focus_etfs), "", "### A股/港股盘前参考", "- 强映射：AI芯片、半导体ETF、HBM/存储、AI网络、光模块、PCB、服务器、液冷、数据中心。", "- 弱映射：云厂商、AI软件、SaaS更多影响AI应用和软件情绪，需要结合国内订单和政策验证。", "- 情绪映射：特斯拉链、机器人、自动驾驶和港股互联网更多影响风险偏好，不宜直接替代AH基本面判断。", "", "交易含义：强映射可关注但等承接，弱映射仅情绪验证，情绪映射不追高。", "",
        "## 七、Update Matrix", "", update_matrix(index_assets, focus_etfs, stocks, news_items, market_meta), "",
        "## 八、最终结论", "", *final_watchlist(focus_etfs, stocks, news_items, market_meta), "",
        "---", "风险提示：本报告仅基于已获取的行情源和公开新闻自动生成，不构成投资建议；盘后财报、监管新闻、宏观数据和流动性变化可能改变结论。", source_note(market_data),
    ]
    return "\n".join(sections).strip() + "\n"
