from __future__ import annotations

from dataclasses import dataclass
from io import StringIO
from typing import Any, Protocol

import pandas as pd
import requests
import yfinance as yf


@dataclass(frozen=True)
class ProviderResult:
    frame: pd.DataFrame
    provider: str


class MarketDataProvider(Protocol):
    name: str

    def fetch_history(self, ticker: str, period: str, interval: str) -> pd.DataFrame:
        ...


def normalize_history_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame()

    clean = frame.copy()
    if "Date" in clean.columns or "Datetime" in clean.columns:
        date_column = "Date" if "Date" in clean.columns else "Datetime"
        clean[date_column] = pd.to_datetime(clean[date_column])
        clean = clean.set_index(date_column)
    clean = clean.sort_index()
    clean.index.name = "Date"
    return clean


def period_to_days(period: str) -> int | None:
    value = str(period or "").strip().lower()
    if value in {"", "max"}:
        return None
    try:
        if value.endswith("d"):
            return int(value[:-1])
        if value.endswith("mo"):
            return int(value[:-2]) * 31
        if value.endswith("y"):
            return int(value[:-1]) * 365
    except ValueError:
        return None
    return None


class YFinanceProvider:
    name = "Yahoo Finance via yfinance"

    def fetch_history(self, ticker: str, period: str, interval: str) -> pd.DataFrame:
        frame = yf.Ticker(ticker).history(period=period, interval=interval, auto_adjust=False)
        return normalize_history_frame(frame)


class StooqProvider:
    name = "Stooq daily CSV"
    base_url = "https://stooq.com/q/d/l/"
    default_symbol_map = {
        "^GSPC": "spy.us",
        "^SPX": "spy.us",
        "^IXIC": "qqq.us",
        "^NDX": "qqq.us",
        "^DJI": "dia.us",
        "^RUT": "iwm.us",
        "^SOX": "smh.us",
        "^VIX": "^vix",
        "^TNX": "10usy.b",
        "SPY": "spy.us",
        "QQQ": "qqq.us",
        "DIA": "dia.us",
        "IWM": "iwm.us",
        "SMH": "smh.us",
        "SOXX": "soxx.us",
        "XLK": "xlk.us",
        "XLE": "xle.us",
        "XLF": "xlf.us",
        "XLV": "xlv.us",
        "TLT": "tlt.us",
        "GLD": "gld.us",
        "USO": "uso.us",
        "NVDA": "nvda.us",
        "AMD": "amd.us",
        "AVGO": "avgo.us",
        "MSFT": "msft.us",
        "GOOGL": "googl.us",
        "META": "meta.us",
        "AMZN": "amzn.us",
        "AAPL": "aapl.us",
        "TSLA": "tsla.us",
        "MU": "mu.us",
        "MRVL": "mrvl.us",
        "ARM": "arm.us",
        "SNOW": "snow.us",
        "NOW": "now.us",
        "BTC-USD": "btcusd",
    }

    def __init__(self, symbol_map: dict[str, str] | None = None) -> None:
        self.symbol_map = {**self.default_symbol_map, **(symbol_map or {})}

    def stooq_symbol(self, ticker: str) -> str:
        if ticker in self.symbol_map:
            return self.symbol_map[ticker]
        cleaned = ticker.replace("-", ".").lower()
        if cleaned.startswith("^"):
            return cleaned
        if "." not in cleaned:
            return f"{cleaned}.us"
        return cleaned

    def fetch_history(self, ticker: str, period: str, interval: str) -> pd.DataFrame:
        if interval not in {"1d", "1D", "d", "D"}:
            raise ValueError("Stooq fallback only supports daily history")

        response = requests.get(
            self.base_url,
            params={"s": self.stooq_symbol(ticker), "i": "d"},
            headers={"User-Agent": "us-market-review/0.1"},
            timeout=20,
        )
        response.raise_for_status()
        if "No data" in response.text or not response.text.strip():
            raise ValueError("empty Stooq response")

        frame = pd.read_csv(StringIO(response.text))
        if frame.empty or "Date" not in frame.columns or "Close" not in frame.columns:
            raise ValueError("invalid Stooq CSV response")

        frame = normalize_history_frame(frame)
        days = period_to_days(period)
        if days:
            cutoff = pd.Timestamp.utcnow().tz_localize(None) - pd.Timedelta(days=days + 5)
            frame = frame[frame.index >= cutoff]
        return frame


def make_provider(name: str, options: dict[str, Any] | None = None) -> MarketDataProvider:
    normalized = name.strip().lower()
    options = options or {}
    if normalized == "yfinance":
        return YFinanceProvider()
    if normalized == "stooq":
        symbol_map = options.get("symbol_map") if isinstance(options, dict) else None
        return StooqProvider(symbol_map=symbol_map if isinstance(symbol_map, dict) else None)
    if normalized in {"cache", "local_cache", "local market cache"}:
        raise NotImplementedError("Cache is handled by fetch_market.py after live providers fail.")
    if normalized in {"twelve_data", "twelvedata", "alpha_vantage", "alphavantage", "polygon"}:
        raise NotImplementedError(
            f"Market provider '{name}' is configured but not implemented yet. "
            "The provider abstraction is ready for Twelve Data, Alpha Vantage, and Polygon."
        )
    raise ValueError(f"Unsupported market provider: {name}")


def provider_display_name(name: str) -> str:
    normalized = name.strip().lower()
    if normalized in {"cache", "local_cache", "local market cache"}:
        return "Local market cache"
    return make_provider(name).name


def resolve_provider_symbol(name: str, ticker: str, options: dict[str, Any] | None = None) -> str:
    normalized = name.strip().lower()
    options = options or {}
    if normalized == "stooq":
        provider = make_provider("stooq", options)
        if isinstance(provider, StooqProvider):
            return provider.stooq_symbol(ticker)
    if normalized in {"cache", "local_cache", "local market cache"}:
        return ticker
    return ticker
