from __future__ import annotations

from datetime import datetime
from typing import Any

from .fetch_market import assets_by_category
from .indicators import breadth, rank_by_metric, safe_float


INDEX_ORDER = ["SPY", "QQQ", "DIA", "IWM", "SMH", "SOXX", "VIX"]
MEGA_TECH = ["MSFT", "AMZN", "NVDA", "AAPL", "GOOGL", "META", "TSLA"]
WATCH_MOVERS = ["SNOW", "NOW", "MDB", "AMD", "MU", "AVGO", "MRVL", "ARM", "PLTR", "ORCL", "CRWD", "DDOG"]
AI_REPS = ["SNOW", "NOW", "AMD", "AVGO", "MU", "MRVL", "ARM"]
ETF_LOOK = ["QQQ", "SPY", "SMH", "SOXX", "XLK", "XLF", "XLE", "XLV"]
MACRO_ASSETS = ["US10Y", "DXY", "TLT", "GLD", "USO", "CPER", "BTCUSD"]


INDEX_NAMES = {
    "SPY": "标普500",
    "QQQ": "纳斯达克",
    "DIA": "道琼斯",
    "IWM": "罗素2000",
    "SMH": "费城半导体",
    "SOXX": "费城半导体",
    "VIX": "VIX",
}


BANNED_FOOTNOTE_WORDS = ["HTTP", "provider_chain", "Too Many Requests", "缓存降级ticker"]


def md_escape(value: Any) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", " ").strip()


def markdown_table(headers: list[str], rows: list[list[Any]]) -> str:
    if not rows:
        rows = [["无重大增量", "当日公开信息", "不强行归因，等待下一交易日验证"]]
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


def valid_asset(asset: dict[str, Any] | None) -> bool:
    return bool(asset and safe_float(asset.get("last_close")) is not None)


def by_ticker(assets: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(asset.get("ticker", "")).upper(): asset for asset in assets}


def pick_assets(assets: list[dict[str, Any]], tickers: list[str]) -> list[dict[str, Any]]:
    lookup = by_ticker(assets)
    return [lookup[ticker] for ticker in tickers if valid_asset(lookup.get(ticker))]


def asset_name(asset: dict[str, Any]) -> str:
    return str(asset.get("name") or asset.get("ticker") or "")


def asset_title(asset: dict[str, Any]) -> str:
    return f"{asset_name(asset)}({asset.get('ticker')})"


def change(asset: dict[str, Any] | None) -> float | None:
    return safe_float(asset.get("daily_change")) if asset else None


def avg_change(assets: list[dict[str, Any]]) -> float | None:
    values = [change(asset) for asset in assets]
    values = [value for value in values if value is not None]
    if not values:
        return None
    return sum(values) / len(values)


def signal_for(asset: dict[str, Any]) -> str:
    daily = change(asset)
    ma20 = safe_float(asset.get("ma20_deviation"))
    if daily is None:
        return "价格可用，涨跌幅暂缺"
    if daily <= -0.03:
        return "明显承压，盘前先看修复承接"
    if daily >= 0.03:
        return "明显走强，强者恒强但不宜盲目追高"
    if daily > 0 and (ma20 or 0) > 0:
        return "温和偏强，趋势仍在"
    if daily < 0 and (ma20 or 0) < 0:
        return "弱势延续，等待止跌"
    if daily > 0:
        return "边际修复"
    if daily < 0:
        return "小幅回撤"
    return "震荡"


def market_regime(all_assets: list[dict[str, Any]]) -> dict[str, Any]:
    lookup = by_ticker(all_assets)
    spy = change(lookup.get("SPY"))
    qqq = change(lookup.get("QQQ"))
    iwm = change(lookup.get("IWM"))
    smh = change(lookup.get("SMH")) if change(lookup.get("SMH")) is not None else change(lookup.get("SOXX"))
    vix = change(lookup.get("VIX"))
    broad_values = [value for value in [spy, qqq, iwm] if value is not None]
    broad_avg = sum(broad_values) / len(broad_values) if broad_values else None
    risk_off = (spy is not None and spy <= -0.01) or (qqq is not None and qqq <= -0.01) or (vix is not None and vix >= 0.10)
    return {"spy": spy, "qqq": qqq, "iwm": iwm, "smh": smh, "vix": vix, "broad_avg": broad_avg, "risk_off": risk_off}


def headline_judgement(index_assets: list[dict[str, Any]], stocks: list[dict[str, Any]]) -> str:
    regime = market_regime(index_assets + stocks)
    mega = avg_change(pick_assets(stocks, MEGA_TECH))
    semis = avg_change(pick_assets(stocks, ["AMD", "AVGO", "MU", "MRVL", "ARM"]))
    if regime["risk_off"]:
        return "昨夜美股并非全面风险偏好回暖，而是宽基承压、波动率抬升下的结构分化；大型科技权重对指数形成拖累，半导体内部若有强势也只能视为局部主线占优。"
    if mega is not None and semis is not None and semis > mega:
        return "昨夜美股风险偏好边际修复，但主线并非平均扩散，半导体/AI硬件相对大型科技平台更有弹性。"
    if regime["broad_avg"] is not None and regime["broad_avg"] > 0:
        return "昨夜美股整体偏修复，宽基与科技共同回暖，但AH盘前仍需确认成交扩散而非单点脉冲。"
    return "昨夜美股整体震荡分化，指数方向不强，盘前更应重视强弱结构而非简单看涨看跌。"


def one_sentence(index_assets: list[dict[str, Any]], stocks: list[dict[str, Any]]) -> str:
    regime = market_regime(index_assets + stocks)
    if regime["risk_off"]:
        return "一句话结论：大型科技与宽基压力仍在，AI链只做结构映射，AH盘前承接为主、不追高。"
    return "一句话结论：风险偏好边际修复但仍需扩散确认，优先跟踪AI链强映射和ETF相对强弱。"


def index_table(index_assets: list[dict[str, Any]]) -> str:
    lookup = by_ticker(index_assets)
    rows: list[list[Any]] = []
    for ticker in ["SPY", "QQQ", "DIA", "IWM", "SMH", "VIX"]:
        asset = lookup.get(ticker) or (lookup.get("SOXX") if ticker == "SMH" else None)
        if not valid_asset(asset):
            continue
        rows.append([INDEX_NAMES.get(ticker, ticker), fmt_number(asset.get("last_close")), fmt_percent(asset.get("daily_change"))])
    return markdown_table(["指数", "收盘", "涨跌幅"], rows)


def ma20_sentence(index_assets: list[dict[str, Any]]) -> str:
    lookup = by_ticker(index_assets)
    pieces = []
    for ticker in ["SPY", "QQQ", "IWM", "SMH", "VIX"]:
        asset = lookup.get(ticker) or (lookup.get("SOXX") if ticker == "SMH" else None)
        if valid_asset(asset):
            pieces.append(f"{INDEX_NAMES.get(ticker, ticker)} {fmt_percent(asset.get('ma20_deviation'))}")
    return "MA20偏离：" + "；".join(pieces) + "。" if pieces else "MA20偏离：关键指数历史数据暂缺，等待补齐后再做趋势判断。"


def three_questions(index_assets: list[dict[str, Any]]) -> str:
    regime = market_regime(index_assets)
    risk = "局部修复/结构分化" if regime["risk_off"] or not ((regime["spy"] or 0) > 0 and (regime["qqq"] or 0) > 0) else "偏全面回暖，但仍需小盘确认"
    size = "小盘强于大盘" if regime["iwm"] is not None and regime["spy"] is not None and regime["iwm"] > regime["spy"] else "大盘强于小盘，小盘承接不足"
    semi = "仍是主导或相对抗跌" if regime["smh"] is not None and regime["spy"] is not None and regime["smh"] > regime["spy"] else "主导性需要重新确认"
    return "\n".join([
        f"1. 风险偏好是全面回暖还是局部修复？{risk}。",
        f"2. 大盘 / 小盘谁更强？{size}。",
        f"3. 科技 / 半导体是否仍是主导？{semi}。",
    ])


def news_source(item: dict[str, Any]) -> str:
    source = item.get("source") or "公开信息"
    date = str(item.get("published_at") or item.get("fetched_at") or "当日")[:10]
    return f"{source} / {date}"


def filter_news(news_items: list[dict[str, Any]], tag: str) -> list[dict[str, Any]]:
    return [item for item in news_items if tag in set(item.get("tags", []))]


def company_news(news_items: list[dict[str, Any]], ticker: str) -> list[dict[str, Any]]:
    return [item for item in news_items if ticker in set(item.get("company_tickers", [])) and "公司级" in set(item.get("classification", []))]


def catalyst_text(news_items: list[dict[str, Any]], ticker: str) -> str:
    items = company_news(news_items, ticker)
    if not items:
        return "未匹配到明确公司级催化，按盘面强弱跟踪"
    return str(items[0].get("title") or "公司级催化")[:72]


def macro_event_table(items: list[dict[str, Any]]) -> str:
    rows = []
    for item in items[:3]:
        rows.append([item.get("title") or "宏观事件", news_source(item), (item.get("summary") or "影响利率与风险偏好预期")[:90]])
    if not rows:
        rows.append(["无重大增量", "公开新闻窗口", "隔夜未见足以改写降息交易的新增宏观事件"])
    return markdown_table(["事件", "来源日期", "要点"], rows)


def geopolitics_table(items: list[dict[str, Any]]) -> str:
    rows = []
    for item in items[:3]:
        title = str(item.get("title") or "地缘事件")
        rows.append([title[:34], news_source(item), "关注油气、黄金、航运与出口管制风险溢价"])
    if not rows:
        rows.append(["主要地缘冲突", "近2日无重大增量", "对油气/黄金/航运影响暂不放大"])
    return markdown_table(["冲突", "近2日状态", "变化"], rows)


def sector_bullets(etfs: list[dict[str, Any]], stocks: list[dict[str, Any]]) -> str:
    lookup = by_ticker(etfs + stocks)
    ai_apps = pick_assets(stocks, ["SNOW", "NOW"])
    mega = pick_assets(stocks, MEGA_TECH)
    semis = pick_assets(stocks, ["AMD", "AVGO", "MU", "MRVL", "ARM"])
    traditional = pick_assets(etfs, ["XLE", "XLF", "XLV"])
    strongest = rank_by_metric([asset for asset in etfs + stocks if valid_asset(asset)], "daily_change", reverse=True)
    judgement = f"判断：资金最强线索落在{asset_title(strongest[0])}，但是否能扩散到AH仍要看开盘承接。" if strongest else "判断：行业强弱数据不足，盘前先看成交验证。"
    return "\n".join([
        f"- AI软件应用：{fmt_percent(avg_change(ai_apps))}，重点看 Snowflake / ServiceNow 是否从情绪转为订单逻辑。",
        f"- 大型科技：{fmt_percent(avg_change(mega))}，决定纳指和港股互联网情绪。",
        f"- SaaS/IT服务：{fmt_percent(avg_change(ai_apps))}，若强于大盘，AH优先看AI应用层。",
        f"- 传统工业/能源：{fmt_percent(avg_change(traditional))}，用于判断资金是否从成长切向防御/资源。",
        judgement,
    ])


def etf_structure_table(etfs: list[dict[str, Any]]) -> str:
    lookup = by_ticker(etfs)
    rows = []
    for ticker, dim in [("QQQ", "成长/纳指权重"), ("SPY", "大盘宽基"), ("SMH", "半导体主线")]:
        asset = lookup.get(ticker) or (lookup.get("SOXX") if ticker == "SMH" else None)
        if valid_asset(asset):
            rows.append([ticker, dim, f"{fmt_percent(asset.get('daily_change'))}，{signal_for(asset)}"])
    spy = lookup.get("SPY")
    qqq = lookup.get("QQQ")
    if valid_asset(spy) and valid_asset(qqq):
        rows.append(["SPY/QQQ量变", "成长相对宽基", "QQQ相对SPY " + fmt_percent((change(qqq) or 0) - (change(spy) or 0))])
    return markdown_table(["ETF", "关注维度", "信号"], rows)


def megatech_table(stocks: list[dict[str, Any]], news_items: list[dict[str, Any]]) -> str:
    lookup = by_ticker(stocks)
    rows = []
    for ticker in MEGA_TECH:
        asset = lookup.get(ticker)
        if valid_asset(asset):
            rows.append([ticker, fmt_percent(asset.get("daily_change")), signal_for(asset)])
    return markdown_table(["股票", "涨跌幅", "信号"], rows)


def movers_bullets(stocks: list[dict[str, Any]], news_items: list[dict[str, Any]]) -> str:
    lookup = by_ticker(stocks)
    candidates = []
    for ticker in WATCH_MOVERS:
        asset = lookup.get(ticker)
        if not valid_asset(asset):
            continue
        daily = abs(change(asset) or 0)
        has_news = bool(company_news(news_items, ticker))
        if daily >= 0.025 or has_news:
            candidates.append(asset)
    candidates = rank_by_metric(candidates, "daily_change", reverse=True)[:6]
    if not candidates:
        return "- 自选池未出现足以单独列示的公司级催化或显著异动，盘前以 ETF 与核心权重承接为主。"
    lines = []
    for asset in candidates:
        ticker = str(asset.get("ticker"))
        lines.append(f"- {asset_title(asset)}：{fmt_percent(asset.get('daily_change'))}，{signal_for(asset)}；{catalyst_text(news_items, ticker)}。")
    return "\n".join(lines)


def ai_three_questions(stocks: list[dict[str, Any]], etfs: list[dict[str, Any]]) -> str:
    lookup = by_ticker(stocks + etfs)
    capex = avg_change([asset for ticker in ["MSFT", "AMZN", "GOOGL", "META"] if valid_asset((asset := lookup.get(ticker)))])
    semis = avg_change([asset for ticker in ["NVDA", "AMD", "AVGO", "MU", "MRVL", "ARM", "SMH", "SOXX"] if valid_asset((asset := lookup.get(ticker)))])
    apps = avg_change([asset for ticker in ["SNOW", "NOW"] if valid_asset((asset := lookup.get(ticker)))])
    return "\n".join([
        f"1. AI主线是在扩散、收敛，还是高位分歧？当前更像结构分化，强弱取决于半导体 {fmt_percent(semis)} 与应用 {fmt_percent(apps)} 的相对表现。",
        f"2. AI Capex、半导体、软件、端侧应用分别强弱如何？云厂商/Capex {fmt_percent(capex)}，半导体 {fmt_percent(semis)}，软件应用 {fmt_percent(apps)}，端侧看 AAPL/TSLA 承接。",
        "3. 业绩与指引是强化还是削弱 AI 主线？若公司级新闻不足，则不强行用新闻解释行情，只按价格与ETF相对强弱判断。",
    ])


def ai_catalyst_table(ai_news: list[dict[str, Any]]) -> str:
    rows = []
    for item in ai_news[:5]:
        title = str(item.get("title") or "AI产业催化")
        rows.append([title[:44], news_source(item), "算力/云Capex/半导体/数据中心/AI应用", "作为产业催化观察，不替代行情判断", "若对应AH强映射开盘承接，再考虑update"])
    if not rows:
        rows.append(["无高相关新增AI产业催化", "公开新闻窗口", "无", "不改变主线判断", "不update"])
    return markdown_table(["事件", "来源日期", "影响链条", "对美股复盘结论", "对A股盘前结论是否update"], rows)


def ah_mapping_rows(config: dict[str, Any], stocks: list[dict[str, Any]], etfs: list[dict[str, Any]]) -> list[list[Any]]:
    lookup = by_ticker(stocks + etfs)
    rows = []
    for item in config.get("ah_mapping", [])[:8]:
        ticker = str(item.get("us_ticker", "")).upper()
        asset = lookup.get(ticker)
        if not valid_asset(asset):
            continue
        strength = str(item.get("mapping_strength") or "情绪映射")
        daily = change(asset) or 0
        if "强" in strength and daily > 0:
            action = "适合追强但只看承接，强者恒强同时注意高位分歧"
        elif "强" in strength:
            action = "强映射但隔夜承压，承接为主，不追高"
        elif "弱" in strength:
            action = "仅情绪验证，等待国内订单/政策确认"
        else:
            action = "情绪映射，不作为盘前主攻方向"
        rows.append([f"{item.get('us_name', ticker)} {fmt_percent(asset.get('daily_change'))}", item.get("a_h_mapping", "AI链/成长方向"), action])
    if not rows:
        rows.append(["美股AI链信号", "光模块/服务器/PCB/半导体/AI应用", "等待开盘承接确认，不追高"])
    return rows


def update_matrix(index_assets: list[dict[str, Any]], etfs: list[dict[str, Any]], stocks: list[dict[str, Any]], ai_news: list[dict[str, Any]], macro_degraded: bool) -> str:
    regime = market_regime(index_assets + etfs + stocks)
    stock_breadth = breadth([asset for asset in stocks if valid_asset(asset)])
    risk_status = "削弱" if regime["risk_off"] else "强化"
    rows = [
        ["昨日A股结论：AI主线仍是最强观察口径", f"隔夜科技池上涨 {stock_breadth['up']} 家、下跌 {stock_breadth['down']} 家", "强化" if stock_breadth["up"] >= stock_breadth["down"] else "削弱", "优先看AI硬件、HBM、光模块、PCB、服务器承接"],
        ["昨日A股结论：指数不宜脱离美股宽基外推", "VIX与SPY/QQQ共同决定风险偏好", risk_status, "若宽基弱而AI强，只做结构，不做全面追高"],
        ["昨日A股结论：产业催化只在高相关时update", f"高相关AI产业新闻 {len(ai_news)} 条", "强化" if ai_news else "不变", "只把公司级催化映射到个股，产业级催化映射到链条"],
        ["昨日A股结论：宏观变量影响估值弹性", "利率/美元关键项" + ("缺失，宏观判断降级" if macro_degraded else "可用于验证"), "削弱" if macro_degraded else "不变", "宏观不完整时降低指数级结论权重"],
    ]
    return markdown_table(["昨日A股结论", "隔夜最新事实", "状态", "今日盘前动作"], rows)


def final_judgement(index_assets: list[dict[str, Any]], stocks: list[dict[str, Any]], macro_degraded: bool) -> list[str]:
    regime = market_regime(index_assets + stocks)
    lead = rank_by_metric([asset for asset in stocks if valid_asset(asset)], "daily_change", reverse=True)
    lead_text = asset_title(lead[0]) if lead else "AI链强映射方向"
    mood = "偏谨慎、结构分化" if regime["risk_off"] else "偏多但需要扩散确认"
    lines = [
        f"1. AH整体情绪：{mood}，不把单个半导体或软件异动外推成全面风险偏好回暖。",
        f"2. 主线共振确认：若 {lead_text} 所在链条开盘有承接，优先看AI硬件、HBM/存储、AI网络、光模块、PCB、服务器。",
        "3. 盘前最优先方向：强映射链条先于弱映射软件，强成交先于低位补涨。",
        "4. 只看承接、不追高：大型科技或宽基承压时，AH只做结构强弱，不做指数级乐观外推。",
        "5. 防兑现/风险提示：VIX上行、美元/美债反向扰动、地缘科技与出口管制消息，都会压制估值弹性。",
    ]
    if macro_degraded:
        lines.append("6. 宏观利率/美元数据缺失，本期宏观判断降级，盘前降低指数级结论权重。")
    return lines


def report_date_from_metadata(market_data: dict[str, Any]) -> tuple[str, str]:
    fetched_at = market_data.get("metadata", {}).get("fetched_at") or datetime.now().isoformat(timespec="minutes")
    clean = str(fetched_at).replace("T", " ")
    date_text = clean[:10]
    try:
        dt = datetime.fromisoformat(str(fetched_at).replace("Z", "+00:00"))
    except ValueError:
        dt = datetime.now()
    return f"{dt.year}年{dt.month}月{dt.day}日", clean[:16]


def footnote(market_data: dict[str, Any], news_data: dict[str, Any]) -> str:
    metadata = market_data.get("metadata", {})
    fetched = str(metadata.get("fetched_at") or "")[:16]
    text = f"行情与公开新闻自动整理，数据获取时间 {fetched}；技术明细见服务器日志。仅供研究参考，不构成投资建议。"
    for term in BANNED_FOOTNOTE_WORDS:
        text = text.replace(term, "")
    return text


def build_markdown_report(config: dict[str, Any], market_data: dict[str, Any], news_data: dict[str, Any]) -> str:
    news_items = news_data.get("items", [])
    report_date, generated_minute = report_date_from_metadata(market_data)
    meta = market_data.get("metadata", {})

    index_assets = pick_assets(assets_by_category(market_data, "index"), INDEX_ORDER)
    etfs = assets_by_category(market_data, "sector_etf") + [asset for asset in index_assets if asset.get("ticker") in {"SPY", "QQQ", "IWM", "SMH", "SOXX"}]
    stocks = assets_by_category(market_data, "key_stock")
    macro_assets = pick_assets(assets_by_category(market_data, "macro_asset"), MACRO_ASSETS)
    macro_news = filter_news(news_items, "宏观")
    china_news = [item for item in filter_news(news_items, "中概/AH") if "地缘" not in set(item.get("tags", []))]
    geo_news = filter_news(news_items, "地缘")
    ai_news = [item for item in filter_news(news_items, "AI") if "产业级" in set(item.get("classification", []))]
    macro_degraded = bool(meta.get("macro_degraded"))

    macro_asset_line = "；".join(f"{asset_name(asset)} {fmt_percent(asset.get('daily_change'))}" for asset in macro_assets[:7]) or "宏观资产数据暂缺"
    china_line = china_news[0].get("title") if china_news else "昨夜中国经济及政策无重大增量，AH更多受美股结构和自身主线承接影响。"
    cut_view = "降息预期一句话判断：若利率数据回落，成长估值弹性修复；若利率/美元走强，则压制大型科技和AH成长。"
    if macro_degraded:
        cut_view = "降息预期一句话判断：宏观利率/美元数据缺失，本节判断降级，等待数据补齐后再确认。"

    sections = [
        f"# 美股复盘 · {report_date}",
        f"生成时间：{generated_minute}",
        "",
        "## 一、美股指数概况",
        headline_judgement(index_assets, stocks),
        "",
        index_table(index_assets),
        "",
        one_sentence(index_assets, stocks),
        ma20_sentence(index_assets),
        "",
        three_questions(index_assets),
        "",
        "## 二、宏观与地缘",
        "### 2.1 美国经济及美联储降息",
        "宏观定价的核心仍是利率、美元与通胀预期，对成长股估值弹性最敏感。" if not macro_degraded else "宏观利率/美元数据缺失，宏观判断降级，本节只保留公开新闻与大类资产线索。",
        macro_event_table(macro_news),
        cut_view,
        f"宏观资产线索：{macro_asset_line}。",
        "",
        "### 2.2 中国经济及政策",
        str(china_line)[:160],
        "对A股/港股影响：若无政策新增，盘前主要跟随美股AI链、港股互联网和人民币/美元情绪；有政策新增时再单独update。",
        "",
        "### 2.3 地缘冲突",
        geopolitics_table(geo_news),
        "结论：地缘扰动主要影响油气、黄金、航运和科技出口管制风险；若油金同强且VIX上行，AH风险偏好应降级。",
        "",
        "## 三、行业与ETF结构",
        "### 3.1 行业涨跌结构",
        sector_bullets(etfs, stocks),
        "",
        "### 3.2 ETF结构",
        etf_structure_table(etfs),
        "判断：ETF只看关键口径，不把所有技术指标堆进正文；QQQ/SPY决定成长相对宽基，SMH/SOXX决定AI硬件强弱。",
        "",
        "## 四、美股科技股跟踪",
        "### 4.1 大型科技股（Magnificent 7）",
        megatech_table(stocks, news_items),
        "结论：大型科技决定指数方向，若权重股承压，即使局部AI硬件走强，也只能定义为结构分化。",
        "",
        "### 4.2 自选美股异动",
        movers_bullets(stocks, news_items),
        "",
        "### 4.3 AI主线跟踪",
        "昨夜是否有新增 AI 关键业绩/指引：若未匹配到明确公司级催化，则以价格、ETF和产业级新闻交叉验证，不强行归因。",
        ai_three_questions(stocks, etfs),
        "",
        "### 4.4 重要信息 / AI产业催化",
        ai_catalyst_table(ai_news),
        "",
        "## 五、AH盘前参考",
        "### 5.1 美股映射",
        markdown_table(["美股信号/异动", "AH映射方向", "盘前判断"], ah_mapping_rows(config, stocks, etfs)),
        "",
        "### 5.2 盘前参考",
        "总判断：AH盘前不看满屏数据，重点看美股最大边际变化是否能映射到A股/港股的可交易方向；强映射看承接，弱映射只做情绪验证。",
        "",
        "#### Update Matrix",
        update_matrix(index_assets, etfs, stocks, ai_news, macro_degraded),
        "",
        "#### 最终判断",
        *final_judgement(index_assets, stocks, macro_degraded),
        "",
        footnote(market_data, news_data),
    ]
    return "\n\n".join(section for section in sections if section is not None).strip() + "\n"
