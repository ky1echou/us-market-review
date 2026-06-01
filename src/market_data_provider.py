from __future__ import annotations

import os
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


class FMPProvider:
    name = "Financial Modeling Prep"
    base_url = "https://financialmodelingprep.com/api/v3"
    default_symbol_map = {
        "^GSPC": "SPY",
        "^SPX": "SPY",
        "^IXIC": "QQQ",
        "^NDX": "QQQ",
        "^DJI": "DIA",
        "^RUT": "IWM",
        "^SOX": "SMH",
        "SPY": "SPY",
        "QQQ": "QQQ",
        "DIA": "DIA",
        "IWM": "IWM",
        "SMH": "SMH",
        "SOXX": "SOXX",
        "XLK": "XLK",
        "XLE": "XLE",
        "XLF": "XLF",
        "XLV": "XLV",
        "TLT": "TLT",
        "GLD": "GLD",
        "USO": "USO",
        "NVDA": "NVDA",
        "AMD": "AMD",
        "AVGO": "AVGO",
        "MSFT": "MSFT",
        "GOOGL": "GOOGL",
        "META": "META",
        "AMZN": "AMZN",
        "AAPL": "AAPL",
        "TSLA": "TSLA",
        "MU": "MU",
        "MRVL": "MRVL",
        "ARM": "ARM",
        "SNOW": "SNOW",
        "NOW": "NOW",
    }

    def __init__(
        self,
        symbol_map: dict[str, str] | None = None,
        api_key: str | None = None,
        api_key_env: str = "FMP_API_KEY",
        quote_batch_size: int = 80,
        timeout: int = 20,
    ) -> None:
        self.symbol_map = {**self.default_symbol_map, **(symbol_map or {})}
        self.api_key_env = api_key_env or "FMP_API_KEY"
        self.api_key = (api_key or os.getenv(self.api_key_env, "")).strip()
        self.quote_batch_size = max(1, int(quote_batch_size or 80))
        self.timeout = int(timeout or 20)
        self.session = requests.Session()
        self.quote_cache: dict[str, dict[str, Any]] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    @classmethod
    def is_configured(cls, options: dict[str, Any] | None = None) -> bool:
        options = options or {}
        api_key_env = str(options.get("api_key_env") or "FMP_API_KEY")
        return bool(str(options.get("api_key") or os.getenv(api_key_env, "")).strip())

    def fmp_symbol(self, ticker: str) -> str:
        if ticker in self.symbol_map:
            return self.symbol_map[ticker]
        cleaned = ticker.strip().replace("-", ".").upper()
        return self.symbol_map.get(cleaned, cleaned)

    def _request_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if not self.enabled:
            raise ValueError("FMP provider disabled: FMP_API_KEY is not configured")

        request_params = {**(params or {}), "apikey": self.api_key}
        response = self.session.get(
            f"{self.base_url}/{path.lstrip('/')}",
            params=request_params,
            headers={"User-Agent": "us-market-review/0.1"},
            timeout=self.timeout,
        )
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            raise ValueError("invalid FMP JSON response") from exc

        if isinstance(payload, dict):
            message = payload.get("Error Message") or payload.get("error") or payload.get("message")
            if message:
                raise ValueError(f"FMP API error: {message}")
        return payload

    def prefetch_quotes(self, tickers: list[str]) -> dict[str, dict[str, Any]]:
        if not self.enabled:
            return {}

        symbols: list[str] = []
        seen: set[str] = set()
        for ticker in tickers:
            symbol = self.fmp_symbol(ticker)
            if symbol and symbol not in seen:
                seen.add(symbol)
                symbols.append(symbol)

        for start in range(0, len(symbols), self.quote_batch_size):
            chunk = symbols[start : start + self.quote_batch_size]
            if not chunk:
                continue
            payload = self._request_json(f"quote/{','.join(chunk)}")
            if not isinstance(payload, list):
                raise ValueError("invalid FMP quote response")
            for row in payload:
                if isinstance(row, dict) and row.get("symbol"):
                    self.quote_cache[str(row["symbol"]).upper()] = row
        return self.quote_cache

    def _timeseries_count(self, period: str) -> int:
        days = period_to_days(period)
        if days is None:
            return 365
        return max(30, min(days + 25, 1500))

    def fetch_history(self, ticker: str, period: str, interval: str) -> pd.DataFrame:
        if interval not in {"1d", "1D", "d", "D"}:
            raise ValueError("FMP provider only supports daily history")

        symbol = self.fmp_symbol(ticker)
        payload = self._request_json(
            f"historical-price-full/{symbol}",
            params={"timeseries": self._timeseries_count(period)},
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("historical"), list):
            raise ValueError("invalid FMP historical response")

        frame = pd.DataFrame(payload["historical"])
        if frame.empty or "date" not in frame.columns or "close" not in frame.columns:
            raise ValueError("empty FMP historical response")

        frame = frame.rename(
            columns={
                "date": "Date",
                "open": "Open",
                "high": "High",
                "low": "Low",
                "close": "Close",
                "adjClose": "Adj Close",
                "volume": "Volume",
            }
        )
        keep_columns = [column for column in ["Date", "Open", "High", "Low", "Close", "Adj Close", "Volume"] if column in frame.columns]
        frame = frame[keep_columns].copy()
        for column in ["Open", "High", "Low", "Close", "Adj Close", "Volume"]:
            if column in frame.columns:
                frame[column] = pd.to_numeric(frame[column], errors="coerce")

        frame = normalize_history_frame(frame)
        days = period_to_days(period)
        if days:
            cutoff = pd.Timestamp.utcnow().tz_localize(None) - pd.Timedelta(days=days + 5)
            frame = frame[frame.index >= cutoff]
        return frame


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
    if normalized in {"fmp", "financial_modeling_prep", "financialmodelingprep"}:
        symbol_map = options.get("symbol_map") if isinstance(options, dict) else None
        return FMPProvider(
            symbol_map=symbol_map if isinstance(symbol_map, dict) else None,
            api_key=options.get("api_key") if isinstance(options, dict) else None,
            api_key_env=str(options.get("api_key_env") or "FMP_API_KEY"),
            quote_batch_size=int(options.get("quote_batch_size") or 80),
            timeout=int(options.get("timeout") or 20),
        )
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


def provider_is_enabled(name: str, options: dict[str, Any] | None = None) -> bool:
    normalized = name.strip().lower()
    options = options or {}
    if normalized in {"fmp", "financial_modeling_prep", "financialmodelingprep"}:
        return FMPProvider.is_configured(options)
    return True


def provider_display_name(name: str) -> str:
    normalized = name.strip().lower()
    if normalized in {"fmp", "financial_modeling_prep", "financialmodelingprep"}:
        return FMPProvider.name
    if normalized in {"cache", "local_cache", "local market cache"}:
        return "Local market cache"
    return make_provider(name).name


def resolve_provider_symbol(name: str, ticker: str, options: dict[str, Any] | None = None) -> str:
    normalized = name.strip().lower()
    options = options or {}
    if normalized in {"fmp", "financial_modeling_prep", "financialmodelingprep"}:
        provider = FMPProvider(
            symbol_map=options.get("symbol_map") if isinstance(options.get("symbol_map"), dict) else None,
            api_key_env=str(options.get("api_key_env") or "FMP_API_KEY"),
        )
        return provider.fmp_symbol(ticker)
    if normalized == "stooq":
        provider = make_provider("stooq", options)
        if isinstance(provider, StooqProvider):
            return provider.stooq_symbol(ticker)
    if normalized in {"cache", "local_cache", "local market cache"}:
        return ticker
    return ticker
