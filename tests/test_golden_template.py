from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.prompt_builder import build_markdown_report


FORBIDDEN_TERMS = [
    "HTTP",
    "Too Many Requests",
    "provider_chain",
    "完整池行情可用",
    "实时获取",
    "缓存降级ticker",
    "失败 ticker",
    "attempt",
    "Python",
    "N/A",
]

REQUIRED_TERMS = [
    "美股复盘 ·",
    "一、美股指数概况",
    "二、宏观与地缘",
    "三、行业与ETF结构",
    "四、美股科技股跟踪",
    "五、AH盘前参考",
    "风险偏好是全面回暖还是局部修复",
    "4.4 重要信息 / AI产业催化",
    "Update Matrix",
    "最终判断",
    "仅供研究参考，不构成投资建议",
]


def asset(ticker: str, category: str, change: float, name: str | None = None, theme: str = "") -> dict:
    return {
        "ticker": ticker,
        "name": name or ticker,
        "category": category,
        "theme": theme,
        "required": True,
        "as_of": "2026-06-09",
        "last_close": 100.0,
        "daily_change": change,
        "ma5_deviation": change / 2,
        "ma20_deviation": change,
        "rsi14": 55.0,
        "volume": 1000000,
        "quote_success": True,
        "historical_success": True,
        "from_cache": False,
        "source": {"provider": "test", "as_of": "2026-06-09", "fetched_at": "2026-06-09T07:30:00+08:00"},
    }


def sample_market_data() -> dict:
    tickers = []
    tickers += [asset("SPY", "index", -0.012, "标普500ETF"), asset("QQQ", "index", -0.015, "纳斯达克100ETF"), asset("DIA", "index", -0.006, "道琼斯ETF"), asset("IWM", "index", -0.020, "罗素2000ETF"), asset("SMH", "index", 0.011, "半导体ETF"), asset("SOXX", "index", 0.009, "半导体ETF"), asset("VIX", "index", 0.18, "VIX")]
    tickers += [asset("XLK", "sector_etf", -0.010, "科技ETF"), asset("XLF", "sector_etf", -0.004, "金融ETF"), asset("XLE", "sector_etf", 0.006, "能源ETF"), asset("XLV", "sector_etf", 0.002, "医疗ETF")]
    tickers += [asset("NVDA", "key_stock", -0.03, "英伟达", "AI算力"), asset("MSFT", "key_stock", -0.01, "微软", "云与AI应用"), asset("AAPL", "key_stock", -0.006, "苹果", "端侧AI"), asset("AMZN", "key_stock", -0.004, "亚马逊", "云计算"), asset("GOOGL", "key_stock", -0.008, "Alphabet", "AI应用"), asset("META", "key_stock", -0.012, "Meta", "AI广告"), asset("TSLA", "key_stock", -0.025, "特斯拉", "机器人"), asset("AMD", "key_stock", 0.018, "AMD", "AI芯片"), asset("AVGO", "key_stock", 0.022, "博通", "AI网络"), asset("MU", "key_stock", 0.041, "美光", "HBM"), asset("MRVL", "key_stock", 0.036, "Marvell", "AI网络"), asset("ARM", "key_stock", 0.012, "Arm", "AI芯片IP"), asset("SNOW", "key_stock", 0.032, "Snowflake", "AI数据云"), asset("NOW", "key_stock", -0.005, "ServiceNow", "AI软件")]
    tickers += [asset("US10Y", "macro_asset", 0.002, "10Y美债收益率"), asset("DXY", "macro_asset", 0.001, "美元指数"), asset("TLT", "macro_asset", -0.004, "长债ETF"), asset("GLD", "macro_asset", 0.007, "黄金ETF"), asset("USO", "macro_asset", 0.006, "原油ETF"), asset("CPER", "macro_asset", 0.003, "铜ETF"), asset("BTCUSD", "macro_asset", -0.010, "比特币")]
    return {"metadata": {"fetched_at": "2026-06-09T07:30:00+08:00", "source": "test", "macro_degraded": False}, "assets": tickers}


def sample_news() -> dict:
    return {
        "items": [
            {"title": "Nvidia supplier demand points to AI data center strength", "source": "Test News", "published_at": "2026-06-09T02:00:00Z", "fetched_at": "2026-06-09T07:30:00+08:00", "tags": ["AI"], "classification": ["产业级"], "company_tickers": []},
            {"title": "Fed officials keep rate cut optionality after inflation data", "source": "Test News", "published_at": "2026-06-09T01:00:00Z", "fetched_at": "2026-06-09T07:30:00+08:00", "tags": ["宏观"], "classification": ["宏观级"], "company_tickers": []},
        ],
        "errors": [],
    }


def test_golden_template_markdown() -> None:
    config = {"ah_mapping": [{"us_ticker": "NVDA", "us_name": "英伟达", "mapping_strength": "强映射", "a_h_mapping": "光模块、PCB、服务器"}], "report": {"author": "AI投研 · 美股版"}}
    text = build_markdown_report(config, sample_market_data(), sample_news())
    for term in REQUIRED_TERMS:
        assert term in text, f"missing required term: {term}"
    for term in FORBIDDEN_TERMS:
        assert term not in text, f"forbidden technical term leaked into report: {term}"


def warn_pdf_pages(pdf_path: str | None = None) -> None:
    if not pdf_path:
        return
    try:
        from PyPDF2 import PdfReader  # type: ignore
    except Exception:
        print("warning: PyPDF2 not installed; skipped PDF page-count check")
        return
    pages = len(PdfReader(pdf_path).pages)
    if pages > 6:
        print(f"warning: PDF has {pages} pages; Golden Template target is about 4 pages")


if __name__ == "__main__":
    test_golden_template_markdown()
    warn_pdf_pages(sys.argv[1] if len(sys.argv) > 1 else None)
    print("golden_template_test=ok")
