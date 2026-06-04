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
    if number is None:
        return "暂缺"
    return f"{number:.1f}"


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


def source_note(market_data: dict[str, Any]) -> str:
    metadata = market_data.get("metadata", {})
    counts = metadata.get("provider_counts", {}) or {}
    count_text = "、".join(f"{provider} {count}项" for provider, count in counts.items()) or "未披露"
    success_ratio = float(metadata.get("success_ratio") or 0.0) * 100
    return (
        f"数据口径：行情源 {metadata.get('source') or '未披露'}；获取时间 {metadata.get('fetched_at') or '未披露'}；"
        f"完整池行情可用 {metadata.get('success_count', 0)}/{metadata.get('total_count', 0)}，可用率 {success_ratio:.1f}%；"
        f"实时获取 {metadata.get('live_success_count', 0)}，缓存降级 {metadata.get('cache_success_count', 0)}，失败 {metadata.get('failed_count', 0)}；"
        f"来源分布：{count_text}。"
    )


def price_table(assets: list[dict[str, Any]]) -> str:
    rows = []
    for asset in valid_assets(assets):
        rows.append(
            [
                asset_title(asset),
                fmt_number(asset.get("last_close")),
                fmt_percent(asset.get("daily_change")),
                fmt_percent(asset.get("ma5_deviation")),
                fmt_percent(asset.get("ma20_deviation")),
                fmt_rsi(asset.get("rsi14")),
                status_label(asset),
            ]
        )
    if not rows:
        return "本节标的未达到正式报告数据门槛；系统不会用空表替代正式判断。"
    return markdown_table(["标的", "收盘", "涨跌幅", "MA5偏离", "MA20偏离", "RSI", "状态"], rows)


def news_source_text(item: dict[str, Any]) -> str:
    parts = [item.get("source") or "未披露"]
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


def filter_news_for_ticker(news_items: list[dict[str, Any]], ticker: str) -> list[dict[str, Any]]:
    return [item for item in news_items if ticker in set(item.get("tags", []))]


def match_news_for_asset(asset: dict[str, Any], news_items: list[dict[str, Any]]) -> str:
    matches = filter_news_for_ticker(news_items, str(asset.get("ticker", "")))
    if not matches:
        return "未匹配到明确公司级催化，先按盘面强弱跟踪"
    item = matches[0]
    title = item.get("title") or "未披露标题"
    link = item.get("link") or ""
    title_text = f"[{title}]({link})" if link else title
    return f"{title_text}；来源：{news_source_text(item)}"


def market_tone(index_assets: list[dict[str, Any]]) -> str:
    valid = [asset for asset in valid_assets(index_assets) if asset.get("ticker") != "VIX"]
    if not valid:
        return "主要宽基 ETF 可用数据不足，系统不会生成正式复盘。"
    counts = breadth(valid)
    avg_change = sum(safe_float(asset.get("daily_change")) or 0 for asset in valid) / len(valid)
    direction = "偏积极" if avg_change > 0.003 else "偏防御" if avg_change < -0.003 else "震荡分化"
    return f"主要宽基与半导体 ETF 平均涨跌幅 {fmt_percent(avg_change)}，上涨 {counts['up']} 个、下跌 {counts['down']} 个，整体呈现{direction}。"


def one_sentence(etfs: list[dict[str, Any]], stocks: list[dict[str, Any]]) -> str:
    ranked_etfs = rank_by_metric(valid_assets(etfs), "daily_change", reverse=True)
    ranked_stocks = rank_by_metric(valid_assets(stocks), "daily_change", reverse=True)
    lead_etf = asset_title(ranked_etfs[0]) if ranked_etfs else "宽基资产"
    lead_stock = asset_title(ranked_stocks[0]) if ranked_stocks else "科技权重"
    return f"一句话总判断：昨夜美股核心矛盾在{lead_etf}与{lead_stock}代表的结构强弱，盘前重点看强势主线是否从单点扩散到行业与AH映射。"


def three_questions(assets: list[dict[str, Any]]) -> str:
    lookup = by_ticker(assets)
    spy = lookup.get("SPY")
    qqq = lookup.get("QQQ")
    iwm = lookup.get("IWM")
    smh = lookup.get("SMH") or lookup.get("SOXX")
    spy_change = safe_float(spy.get("daily_change")) if spy else None
    qqq_change = safe_float(qqq.get("daily_change")) if qqq else None
    iwm_change = safe_float(iwm.get("daily_change")) if iwm else None
    smh_change = safe_float(smh.get("daily_change")) if smh else None
    risk = "偏全面回暖" if spy_change is not None and spy_change > 0 and qqq_change is not None and qqq_change > 0 else "更偏局部修复/结构分化"
    size = "小盘相对占优" if iwm_change is not None and spy_change is not None and iwm_change > spy_change else "大盘相对占优"
    tech = "科技/半导体仍是主导" if smh_change is not None and spy_change is not None and smh_change > spy_change else "科技/半导体主导性需要继续确认"
    return markdown_table(
        ["问题", "回答", "观察口径"],
        [
            ["风险偏好是全面回暖还是局部修复？", risk, "看 SPY、QQQ、IWM、VIX 与行业 ETF 扩散"],
            ["大盘/小盘谁更强？", size, "看 IWM 相对 SPY"],
            ["科技/半导体是否仍是主导？", tech, "看 SMH/SOXX 相对 SPY"],
        ],
    )


def sector_summary(etfs: list[dict[str, Any]]) -> str:
    ranked = rank_by_metric(valid_assets(etfs), "daily_change", reverse=True)
    if not ranked:
        return "ETF 数据不足，暂不做行业强弱排序。"
    strongest = ranked[0]
    weakest = ranked[-1]
    return (
        f"行业/ETF 结构上，{asset_title(strongest)}居前，涨跌幅 {fmt_percent(strongest.get('daily_change'))}；"
        f"{asset_title(weakest)}居后，涨跌幅 {fmt_percent(weakest.get('daily_change'))}。"
        "若科技与半导体同步领先，说明资金仍偏成长；若金融、能源、医疗领先，则更偏价值、防御或再通胀交易。"
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
        rows.append([asset_title(asset), asset.get("theme") or "未归类", fmt_percent(asset.get("daily_change")), fmt_percent(asset.get("ma20_deviation")), fmt_rsi(asset.get("rsi14")), status_label(asset), match_news_for_asset(asset, news_items)])
    table = markdown_table(["股票", "主线", "涨跌幅", "MA20偏离", "RSI", "状态", "异动原因/催化"], rows) if rows else "本组标的数据不足，正式报告不会用空表替代判断。"
    return f"### {title}\n{table}"


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
        rows.append([layer, f"上涨 {counts['up']} / 下跌 {counts['down']}", asset_title(leader[0]) if leader else "暂缺", "强化" if counts["up"] >= counts["down"] else "削弱/分化"])
    return markdown_table(["AI分层", "盘面广度", "代表强势", "主线判断"], rows) + f"\n\n高相关 AI 催化新闻 {len(ai_news)} 条，新闻只作为催化，不替代行情判断。"


def ah_mapping_table(config: dict[str, Any], stocks: list[dict[str, Any]], etfs: list[dict[str, Any]]) -> str:
    lookup = by_ticker(stocks + etfs)
    rows: list[list[Any]] = []
    for item in config.get("ah_mapping", []):
        ticker = item.get("us_ticker")
        asset = lookup.get(ticker, {})
        if not valid_asset(asset):
            continue
        rows.append([f"{item.get('us_name', ticker)}({ticker})", item.get("mapping_strength", "情绪映射"), item.get("theme", "未归类"), fmt_percent(asset.get("daily_change")), status_label(asset), item.get("a_h_mapping", "未配置"), item.get("watch_points", "跟踪盘前强弱")])
    return markdown_table(["美股线索", "映射强度", "主题", "美股表现", "状态", "A/H方向", "盘前动作"], rows) if rows else "AH映射标的数据不足。"


def update_matrix(index_assets: list[dict[str, Any]], etfs: list[dict[str, Any]], stocks: list[dict[str, Any]], news_items: list[dict[str, Any]]) -> str:
    ai_news = filter_news_by_tag(news_items, "AI")
    macro_news = filter_news_by_tag(news_items, "宏观")
    geo_news = filter_news_by_tag(news_items, "地缘")
    stock_breadth = breadth(valid_assets(stocks))
    rows = [
        ["风险偏好取决于宽基扩散", market_tone(index_assets), "强化" if breadth(valid_assets(index_assets)).get("up", 0) >= breadth(valid_assets(index_assets)).get("down", 0) else "削弱", "看 SPY/QQQ/IWM 与 VIX 是否同步确认"],
        ["AI仍是最重要主线", f"科技观察池上涨 {stock_breadth['up']} 家、下跌 {stock_breadth['down']} 家；AI新闻 {len(ai_news)} 条", "强化" if stock_breadth["up"] >= stock_breadth["down"] else "分化", "优先跟踪算力、半导体、云Capex、AI应用"],
        ["行业结构决定AH映射强度", sector_summary(etfs), "强化" if valid_assets(etfs) else "不变", "强映射方向先看光模块、PCB、服务器、HBM、数据中心"],
        ["宏观扰动仍需压估值", f"宏观/地缘新闻 {len(macro_news) + len(geo_news)} 条", "不变", "观察美债、美元、黄金、原油、铜、比特币联动"],
    ]
    return markdown_table(["昨日判断", "隔夜新事实", "状态", "今日盘前动作"], rows)


def final_watchlist(etfs: list[dict[str, Any]], stocks: list[dict[str, Any]], news_items: list[dict[str, Any]]) -> list[str]:
    ranked_etfs = rank_by_metric(valid_assets(etfs), "daily_change", reverse=True)
    ranked_stocks = rank_by_metric(valid_assets(stocks), "daily_change", reverse=True)
    lead_etf = asset_title(ranked_etfs[0]) if ranked_etfs else "强势ETF"
    lead_stock = asset_title(ranked_stocks[0]) if ranked_stocks else "强势科技股"
    ai_count = len(filter_news_by_tag(news_items, "AI"))
    return [
        f"- AH整体情绪：结构分化为主，先看 {lead_etf} 与 {lead_stock} 能否延续并扩散。",
        "- 最强映射方向：AI算力、半导体、光模块、PCB、服务器、液冷、HBM、数据中心优先级最高。",
        "- 不追高方向：若单一权重股领涨但 ETF 与广度未跟随，不追高弱扩散方向。",
        "- 风险点：美债收益率、美元、油价和地缘消息若反向扰动，可能压制科技估值和AH风险偏好。",
        f"- 盘前关注：AI新闻 {ai_count} 条背后的方向、半导体ETF相对SPY、IWM小盘扩散、TSLA机器人/自动驾驶、港股互联网情绪映射。",
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
    china_news = filter_news_by_tag(news_items, "中概/AH")
    geo_news = filter_news_by_tag(news_items, "地缘")

    title = f"{report_date} 美股复盘（昨夜）"
    data_line = f"生成时间：{generated_minute} | 数据：{market_meta.get('source', '行情源')} + 公开新闻 | 分析：{config.get('report', {}).get('author', 'AI投研 · 美股版')}"

    sections = [
        f"# {title}", data_line, "", source_note(market_data), "", one_sentence(focus_etfs, stocks), "",
        "## 一、美股指数概况", "", market_tone(index_assets), "", one_sentence(focus_etfs, stocks), "", price_table(index_assets), "", "结论：指数层面重点看宽基、半导体与VIX是否同向确认，单一指数上涨不足以代表全面风险偏好回暖。", "",
        "### 三个问题回答", "", three_questions(focus_etfs + index_assets), "",
        "## 二、宏观与地缘", "", "### 宏观利率与大宗商品", price_table(macro_assets), "", "结论：美债、美元、黄金、原油、铜、比特币共同决定估值、通胀和风险偏好的边际方向。", "",
        macro_block("美国经济及美联储降息", macro_news, "利率和降息预期仍是估值锚，科技股对收益率变化更敏感。", "若美债利率下行，AH成长与恒生科技情绪更容易修复；若利率上行，则偏压制估值。"),
        macro_block("中国经济及政策", china_news, "中概和跨国消费链更敏感，但不能替代美股自身业绩线索。", "政策和中概风险偏好会直接影响港股互联网、恒生科技和A股平台经济映射。"),
        macro_block("地缘冲突", geo_news, "地缘升温通常提高避险和油金波动，压制风险资产风险偏好。", "油价、黄金和避险情绪会影响AH资源品、防务及出口链风险偏好。"),
        "## 三、行业与 ETF 结构", "", sector_summary(focus_etfs), "", flow_direction(focus_etfs), "", price_table(focus_etfs), "", "结论：本节不只看涨跌幅，更看成长/价值、大盘/小盘、科技/半导体/能源/金融/防御之间的资金切换。", "",
        "## 四、美股科技股跟踪", "", grouped_stock_table("大型科技股", stocks, MEGA_TECH, news_items), "", grouped_stock_table("半导体 / AI硬件", stocks, AI_HARDWARE, news_items), "", grouped_stock_table("AI应用 / SaaS", stocks, AI_APPS, news_items), "", "### 科技股异动总表", price_table(mega_tech + hardware_assets + app_assets), "", "结论：科技股跟踪按大型平台、AI硬件和AI应用三层拆分，避免把单一权重波动误读成整条AI链共振。", "",
        "## 五、AI主线与重要催化", "", ai_layered_summary(stocks, focus_etfs, ai_news), "", price_table(ai_assets), "", "### 重要信息 / AI产业催化", news_bullets(ai_news, limit=10), "", "结论：AI主线按算力、半导体、云厂商Capex、SaaS应用、端侧/机器人分层观察；新闻只作为催化，行情仍是判断基础。", "",
        "## 六、AH盘前映射", "", "### 美股映射", ah_mapping_table(config, stocks, focus_etfs), "", "### A股/港股盘前参考", "- 强映射：AI芯片、半导体ETF、HBM/存储、AI网络、光模块、PCB、服务器、液冷、数据中心。", "- 弱映射：云厂商、AI软件、SaaS更多影响AI应用和软件情绪，需要结合国内订单和政策验证。", "- 情绪映射：特斯拉链、机器人、自动驾驶和港股互联网更多影响风险偏好，不宜直接替代AH基本面判断。", "", "结论：AH盘前动作优先跟随强映射方向，弱映射只做情绪验证，情绪映射不追高。", "",
        "## 七、Update Matrix", "", update_matrix(index_assets, focus_etfs, stocks, news_items), "",
        "## 八、最终结论", "", *final_watchlist(focus_etfs, stocks, news_items), "",
        "---", "风险提示：本报告仅基于已获取的行情源和公开新闻自动生成，不构成投资建议；盘后财报、监管新闻、宏观数据和流动性变化可能改变结论。", source_note(market_data),
    ]
    return "\n".join(sections).strip() + "\n"
