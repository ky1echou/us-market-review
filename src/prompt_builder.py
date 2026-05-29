from __future__ import annotations

from datetime import datetime
from typing import Any

from .fetch_market import assets_by_category
from .indicators import breadth, format_number, format_percent, format_rsi, rank_by_metric, safe_float


INDEX_ORDER = ["^GSPC", "^IXIC", "^DJI", "^RUT", "^SOX", "^VIX"]
FOCUS_ETFS = ["SPY", "QQQ", "SMH", "SOXX", "IWM", "XLK", "XLE", "XLF", "XLV"]
MEGA_TECH = ["NVDA", "AMD", "AVGO", "MSFT", "GOOGL", "META", "AMZN", "AAPL", "TSLA"]
AI_CHAIN = ["NVDA", "AMD", "AVGO", "MU", "MRVL", "ARM", "SNOW", "NOW", "MSFT", "GOOGL", "META", "AMZN", "TSLA"]


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


def valid_asset(asset: dict[str, Any] | None) -> bool:
    return bool(asset and safe_float(asset.get("last_close")) is not None and safe_float(asset.get("daily_change")) is not None)


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
        return "观察"
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
    count_text = "、".join(f"{provider} {count}项" for provider, count in counts.items()) or "暂无"
    return (
        f"数据来源：{metadata.get('source', 'N/A')}；"
        f"获取时间：{metadata.get('fetched_at', 'N/A')}；"
        f"实时成功：{metadata.get('live_success_count', 0)}/{metadata.get('total_count', 0)}；"
        f"来源分布：{count_text}。"
    )


def price_table(assets: list[dict[str, Any]]) -> str:
    rows = []
    for asset in valid_assets(assets):
        rows.append(
            [
                asset_title(asset),
                format_number(asset.get("last_close")),
                format_percent(asset.get("daily_change")),
                format_percent(asset.get("ma5_deviation")),
                format_percent(asset.get("ma20_deviation")),
                format_rsi(asset.get("rsi14")),
                status_label(asset),
            ]
        )
    if not rows:
        return "本节核心标的实时行情不足，已在日志记录原因，本报告不补写缺失数字。"
    return markdown_table(["标的", "收盘", "涨跌幅", "MA5偏离", "MA20偏离", "RSI", "状态"], rows)


def news_source_text(item: dict[str, Any]) -> str:
    parts = [item.get("source") or "N/A"]
    if item.get("published_at"):
        parts.append(f"发布: {item['published_at']}")
    parts.append(f"获取: {item.get('fetched_at', 'N/A')}")
    return "；".join(parts)


def news_bullets(items: list[dict[str, Any]], limit: int = 5) -> str:
    if not items:
        return "- 公开 RSS 窗口内未抓取到高相关增量，暂不强行归因。"
    lines: list[str] = []
    for item in items[:limit]:
        title = item.get("title") or "Untitled"
        link = item.get("link") or ""
        title_text = f"[{title}]({link})" if link else title
        lines.append(f"- {title_text}。来源：{news_source_text(item)}")
    return "\n".join(lines)


def filter_news_by_tag(news_items: list[dict[str, Any]], tag: str) -> list[dict[str, Any]]:
    return [item for item in news_items if tag in set(item.get("tags", []))]


def filter_news_for_ticker(news_items: list[dict[str, Any]], ticker: str) -> list[dict[str, Any]]:
    return [item for item in news_items if ticker in set(item.get("tags", []))]


def match_news_for_asset(asset: dict[str, Any], news_items: list[dict[str, Any]]) -> str:
    ticker = str(asset.get("ticker", ""))
    matches = filter_news_for_ticker(news_items, ticker)
    if not matches:
        return "未匹配到明确公司级催化，先按盘口强弱跟踪"
    item = matches[0]
    title = item.get("title") or "Untitled"
    link = item.get("link") or ""
    title_text = f"[{title}]({link})" if link else title
    return f"{title_text}；来源：{news_source_text(item)}"


def market_tone(index_assets: list[dict[str, Any]]) -> str:
    valid = valid_assets(index_assets)
    if not valid:
        return "主要指数实时数据不足，无法形成正式复盘判断。"
    counts = breadth(valid)
    avg_change = sum(asset["daily_change"] for asset in valid) / len(valid)
    direction = "偏积极" if avg_change > 0.003 else "偏防御" if avg_change < -0.003 else "震荡分化"
    return f"主要指数平均涨跌幅 {format_percent(avg_change)}，上涨 {counts['up']} 个、下跌 {counts['down']} 个，整体呈现{direction}。"


def one_sentence(index_assets: list[dict[str, Any]], etfs: list[dict[str, Any]], stocks: list[dict[str, Any]]) -> str:
    ranked_etfs = rank_by_metric(valid_assets(etfs), "daily_change", reverse=True)
    ranked_stocks = rank_by_metric(valid_assets(stocks), "daily_change", reverse=True)
    lead_etf = asset_title(ranked_etfs[0]) if ranked_etfs else "行业ETF"
    lead_stock = asset_title(ranked_stocks[0]) if ranked_stocks else "科技龙头"
    return f"一句话结论：昨夜美股不是简单看指数涨跌，核心在于{lead_etf}与{lead_stock}所代表的结构方向，盘前重点看强势线索能否扩散。"


def three_questions(index_assets: list[dict[str, Any]], etfs: list[dict[str, Any]]) -> str:
    lookup = by_ticker(index_assets + etfs)
    spx = lookup.get("^GSPC")
    ndq = lookup.get("^IXIC") or lookup.get("QQQ")
    rut = lookup.get("^RUT") or lookup.get("IWM")
    sox = lookup.get("^SOX") or lookup.get("SMH") or lookup.get("SOXX")
    spx_change = safe_float(spx.get("daily_change")) if spx else None
    ndq_change = safe_float(ndq.get("daily_change")) if ndq else None
    rut_change = safe_float(rut.get("daily_change")) if rut else None
    sox_change = safe_float(sox.get("daily_change")) if sox else None
    risk = "偏全面回暖" if spx_change and spx_change > 0 and ndq_change and ndq_change > 0 else "更偏局部修复/结构分化"
    size = "小盘相对占优" if rut_change is not None and spx_change is not None and rut_change > spx_change else "大盘相对占优"
    tech = "科技/半导体仍是主导" if sox_change is not None and spx_change is not None and sox_change > spx_change else "科技/半导体主导性需要继续确认"
    return markdown_table(
        ["问题", "回答", "观察口径"],
        [
            ["风险偏好是全面回暖还是局部修复？", risk, "看标普、纳指、VIX与上涨家数"],
            ["大盘/小盘谁更强？", size, "看罗素2000/IWM相对标普"],
            ["科技/半导体是否仍是主导？", tech, "看费城半导体、SMH/SOXX相对SPY"],
        ],
    )


def sector_summary(etfs: list[dict[str, Any]]) -> str:
    ranked = rank_by_metric(valid_assets(etfs), "daily_change", reverse=True)
    if not ranked:
        return "ETF 数据不足，暂不做行业强弱排序。"
    strongest = ranked[0]
    weakest = ranked[-1]
    return (
        f"行业/ETF结构上，{asset_title(strongest)}居前，涨跌幅 {format_percent(strongest.get('daily_change'))}；"
        f"{asset_title(weakest)}居后，涨跌幅 {format_percent(weakest.get('daily_change'))}。"
        "若科技与半导体同步领先，说明资金仍偏成长；若金融、能源、医疗领先，则更偏防御或再通胀交易。"
    )


def flow_direction(etfs: list[dict[str, Any]]) -> str:
    lookup = by_ticker(etfs)
    qqq = lookup.get("QQQ")
    spy = lookup.get("SPY")
    iwm = lookup.get("IWM")
    smh = lookup.get("SMH") or lookup.get("SOXX")
    growth = safe_float(qqq.get("daily_change")) if qqq else None
    broad = safe_float(spy.get("daily_change")) if spy else None
    small = safe_float(iwm.get("daily_change")) if iwm else None
    semi = safe_float(smh.get("daily_change")) if smh else None
    if semi is not None and broad is not None and semi > broad:
        return "资金切换方向：半导体强于宽基，AI算力链仍是第一观察方向。"
    if growth is not None and broad is not None and growth > broad:
        return "资金切换方向：成长强于宽基，市场仍愿意为科技权重定价。"
    if small is not None and broad is not None and small > broad:
        return "资金切换方向：小盘开始修复，关注风险偏好是否从权重扩散到中小市值。"
    return "资金切换方向：宽基内部缺少清晰扩散，盘前以结构验证为主。"


def macro_block(title: str, items: list[dict[str, Any]], us_impact: str, ah_impact: str) -> str:
    return "\n".join(
        [
            f"### {title}",
            news_bullets(items, limit=3),
            f"- 对美股影响：{us_impact}",
            f"- 对AH影响：{ah_impact}",
            "",
        ]
    )


def mover_table(stocks: list[dict[str, Any]], news_items: list[dict[str, Any]], limit: int) -> str:
    ranked_up = rank_by_metric(valid_assets(stocks), "daily_change", reverse=True)[:limit]
    ranked_down = rank_by_metric(valid_assets(stocks), "daily_change", reverse=False)[:limit]
    rows: list[list[Any]] = []
    for direction, assets in (("领涨", ranked_up), ("领跌", ranked_down)):
        for asset in assets:
            rows.append(
                [
                    direction,
                    asset_title(asset),
                    asset.get("theme") or "",
                    format_percent(asset.get("daily_change")),
                    status_label(asset),
                    match_news_for_asset(asset, news_items),
                ]
            )
    return markdown_table(["方向", "股票", "主线", "涨跌幅", "状态", "异动点评"], rows) if rows else "重点科技股数据不足。"


def ai_chain_judgement(stocks: list[dict[str, Any]], etfs: list[dict[str, Any]], ai_news: list[dict[str, Any]]) -> str:
    ai_assets = pick_assets(stocks, AI_CHAIN)
    counts = breadth(ai_assets)
    etf_lookup = by_ticker(etfs)
    smh = etf_lookup.get("SMH") or etf_lookup.get("SOXX")
    semi_text = f"半导体ETF {format_percent(smh.get('daily_change'))}" if smh else "半导体ETF数据不足"
    return (
        f"AI链条观察：AI股票池上涨 {counts['up']} 家、下跌 {counts['down']} 家，{semi_text}；"
        f"高相关AI新闻 {len(ai_news)} 条。若股票池与新闻催化共振，AH端优先看算力、光模块、PCB、液冷、HBM和服务器。"
    )


def ah_mapping_table(config: dict[str, Any], stocks: list[dict[str, Any]], etfs: list[dict[str, Any]]) -> str:
    lookup = by_ticker(stocks + etfs)
    rows: list[list[Any]] = []
    for item in config.get("ah_mapping", []):
        ticker = item.get("us_ticker")
        asset = lookup.get(ticker, {})
        if not valid_asset(asset):
            continue
        rows.append(
            [
                f"{item.get('us_name', ticker)}({ticker})",
                item.get("mapping_strength", "情绪映射"),
                item.get("theme", ""),
                format_percent(asset.get("daily_change")),
                status_label(asset),
                item.get("a_h_mapping", ""),
                item.get("watch_points", ""),
            ]
        )
    return markdown_table(["美股线索", "映射强度", "主题", "美股表现", "状态", "A/H方向", "盘前关注"], rows) if rows else "AH映射标的数据不足。"


def update_matrix(index_assets: list[dict[str, Any]], etfs: list[dict[str, Any]], stocks: list[dict[str, Any]], news_items: list[dict[str, Any]]) -> str:
    ai_news = filter_news_by_tag(news_items, "AI")
    macro_news = filter_news_by_tag(news_items, "宏观")
    rows = [
        ["指数风险偏好", "高", market_tone(index_assets), "主要指数与VIX", "验证风险偏好是否扩散"],
        ["行业/ETF结构", "高", sector_summary(etfs), "SPY/QQQ/SMH/SOXX/IWM/行业ETF", "看成长、半导体、小盘谁领涨"],
        ["科技龙头", "高", f"科技观察池上涨 {breadth(valid_assets(stocks))['up']} 家、下跌 {breadth(valid_assets(stocks))['down']} 家", "重点科技股涨跌与MA20", "关注强弱是否集中在AI链"],
        ["AI主线", "高", f"高相关AI新闻 {len(ai_news)} 条", "AI新闻评分+AI股票池", "看算力、云Capex、数据中心、AI软件"],
        ["宏观与地缘", "中", f"宏观/地缘新闻 {len(macro_news) + len(filter_news_by_tag(news_items, '地缘'))} 条", "公开RSS+利率商品", "关注利率、美元、油金与风险资产联动"],
    ]
    return markdown_table(["主题", "重要性", "昨夜变化", "证据", "下一步跟踪"], rows)


def final_watchlist(etfs: list[dict[str, Any]], stocks: list[dict[str, Any]], news_items: list[dict[str, Any]]) -> list[str]:
    ranked_etfs = rank_by_metric(valid_assets(etfs), "daily_change", reverse=True)
    ranked_stocks = rank_by_metric(valid_assets(stocks), "daily_change", reverse=True)
    lead_etf = asset_title(ranked_etfs[0]) if ranked_etfs else "强势ETF"
    lead_stock = asset_title(ranked_stocks[0]) if ranked_stocks else "强势科技股"
    ai_count = len(filter_news_by_tag(news_items, "AI"))
    return [
        f"1. 盘前先看 {lead_etf} 能否延续，判断昨夜结构强势是否具备扩散性。",
        f"2. 科技股看 {lead_stock} 代表的主线能否带动 NVDA/AMD/AVGO/MSFT/GOOGL/META/AMZN 同步。",
        f"3. AI主线关注高相关新闻 {ai_count} 条背后的方向：大模型、AI芯片、云Capex、数据中心、AI软件。",
        "4. AH映射优先看强映射方向，尤其算力、光模块、PCB、液冷、HBM、服务器；弱映射方向只做情绪参考。",
        "5. 若美债收益率、美元或油价反向扰动，盘前降低追高权重，等待开盘后资金确认。",
    ]


def report_date_from_metadata(market_data: dict[str, Any]) -> tuple[str, str]:
    fetched_at = market_data.get("metadata", {}).get("fetched_at") or datetime.now().isoformat(timespec="minutes")
    clean = str(fetched_at).replace("T", " ")
    report_date = clean[:10]
    generated_minute = clean[:16]
    return report_date, generated_minute


def build_markdown_report(config: dict[str, Any], market_data: dict[str, Any], news_data: dict[str, Any]) -> str:
    market_meta = market_data.get("metadata", {})
    news_items = news_data.get("items", [])
    report_date, generated_minute = report_date_from_metadata(market_data)

    index_assets = pick_assets(assets_by_category(market_data, "index"), INDEX_ORDER)
    all_etfs = assets_by_category(market_data, "sector_etf")
    focus_etfs = pick_assets(all_etfs, FOCUS_ETFS)
    stocks = assets_by_category(market_data, "key_stock")
    mega_tech = pick_assets(stocks, MEGA_TECH)
    ai_assets = pick_assets(stocks, AI_CHAIN)
    macro_assets = valid_assets(assets_by_category(market_data, "macro_asset"))

    ai_news = filter_news_by_tag(news_items, "AI")
    macro_news = filter_news_by_tag(news_items, "宏观")
    china_news = filter_news_by_tag(news_items, "中概/AH")
    geo_news = filter_news_by_tag(news_items, "地缘")

    title = f"{report_date} 美股复盘（昨夜）"
    data_line = f"生成时间：{generated_minute} | 数据：{market_meta.get('source', '行情源')} + 公开新闻 | 分析：{config.get('report', {}).get('author', 'AI投研 · 美股版')}"

    sections = [
        f"# {title}",
        data_line,
        "",
        "## 一、美股指数概况",
        "",
        market_tone(index_assets),
        "",
        one_sentence(index_assets, focus_etfs, stocks),
        "",
        price_table(index_assets),
        "",
        source_note(market_data),
        "",
        "### 三个问题回答",
        "",
        three_questions(index_assets, focus_etfs),
        "",
        "## 二、宏观与地缘",
        "",
        "### 宏观利率与大宗商品",
        price_table(macro_assets),
        "",
        macro_block("美国经济及美联储降息", macro_news, "利率和降息预期仍是估值锚，科技股对收益率变化更敏感。", "若美债利率下行，AH成长与恒生科技情绪更容易修复；若利率上行，则偏压制估值。"),
        macro_block("中国经济及政策", china_news, "中概和跨国消费链更敏感，但不能替代美股自身业绩线索。", "政策和中概风险偏好会直接影响港股互联网、恒生科技和A股平台经济映射。"),
        macro_block("地缘冲突", geo_news, "地缘升温通常提高避险和油金波动，压制风险资产风险偏好。", "油价、黄金和避险情绪会影响AH资源品、防务及出口链风险偏好。"),
        "## 三、行业与ETF结构",
        "",
        sector_summary(all_etfs),
        "",
        flow_direction(focus_etfs),
        "",
        price_table(focus_etfs),
        "",
        source_note(market_data),
        "",
        "## 四、美股科技股跟踪",
        "",
        "### 大型科技股",
        price_table(mega_tech),
        "",
        "### 自选美股异动",
        mover_table(stocks, news_items, int(config.get("report", {}).get("top_movers_count", 5))),
        "",
        "### AI 主线跟踪",
        ai_chain_judgement(stocks, all_etfs, ai_news),
        "",
        price_table(ai_assets),
        "",
        "### 重要信息 / AI产业催化",
        news_bullets(ai_news, limit=8),
        "",
        "## 五、AH盘前参考",
        "",
        "### 美股映射",
        ah_mapping_table(config, stocks, all_etfs),
        "",
        "### A股/港股盘前参考",
        "- 强映射：美股AI芯片、半导体ETF、HBM/存储、AI网络若走强，优先映射A股算力硬件链。",
        "- 弱映射：云厂商、AI软件、SaaS更多影响AI应用和软件情绪，需要结合国内订单和政策验证。",
        "- 情绪映射：中概互联网、特斯拉链更多影响风险偏好，不宜直接替代AH基本面判断。",
        "",
        "### Update Matrix",
        update_matrix(index_assets, all_etfs, stocks, news_items),
        "",
        "### 最终回答",
        *final_watchlist(focus_etfs, stocks, news_items),
        "",
        "---",
        "风险提示：本报告仅基于已获取的行情源和公开新闻自动生成，不构成投资建议；盘后财报、监管新闻、宏观数据和流动性变化可能改变结论。",
        source_note(market_data),
    ]

    return "\n".join(sections).strip() + "\n"
