from __future__ import annotations

import json
import os
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import Any, Protocol

import pandas as pd
import requests
import yfinance as yf


@dataclass(frozen=True)
class ProviderResult:
    frame: pd.DataFrame
    provider: str


@dataclass
class FMPQuote:
    symbol: str
    price: float
    previous_close: float | None
    change: float | None
    change_percent: float | None
    volume: float | None
    raw: dict[str, Any]


class FMPProviderError(Exception):
    def __init__(self, category: str, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.category = category
        self.status_code = status_code


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


def utc_now_naive() -> pd.Timestamp:
    now = pd.Timestamp.utcnow()
    if now.tzinfo is not None:
        return now.tz_convert(None)
    return now


def safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except TypeError:
        pass
    if isinstance(value, str):
        value = value.strip().replace("%", "")
        if value == "":
            return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class FMPProvider:
    name = "Financial Modeling Prep"
    base_url = "https://financialmodelingprep.com/stable"
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
        base_url: str | None = None,
    ) -> None:
        self.symbol_map = {**self.default_symbol_map, **(symbol_map or {})}
        self.api_key_env = api_key_env or "FMP_API_KEY"
        self.api_key_raw = api_key if api_key is not None else os.getenv(self.api_key_env, "")
        self.api_key = str(self.api_key_raw).strip()
        self.quote_batch_size = max(1, int(quote_batch_size or 80))
        self.timeout = int(timeout or 20)
        self.base_url = (base_url or self.base_url).rstrip("/")
        self.session = requests.Session()
        self.quote_cache: dict[str, FMPQuote] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    @classmethod
    def is_configured(cls, options: dict[str, Any] | None = None) -> bool:
        options = options or {}
        api_key_env = str(options.get("api_key_env") or "FMP_API_KEY")
        return bool(str(options.get("api_key") or os.getenv(api_key_env, "")).strip())

    def api_key_metadata(self, env_path: str | Path = ".env") -> dict[str, Any]:
        raw_env_value: str | None = None
        path = Path(env_path)
        if path.exists():
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    if line.startswith(f"{self.api_key_env}="):
                        raw_env_value = line.split("=", 1)[1]
                        break
            except OSError:
                raw_env_value = None

        env_value = str(self.api_key_raw or "")
        raw_has_space = raw_env_value is not None and raw_env_value != raw_env_value.strip()
        env_has_space = env_value != env_value.strip()
        return {
            "env_name": self.api_key_env,
            "exists": bool(self.api_key),
            "length": len(env_value),
            "trimmed_length": len(self.api_key),
            "has_outer_whitespace": raw_has_space or env_has_space,
            "loaded_from_dotenv": raw_env_value is not None,
        }

    def fmp_symbol(self, ticker: str) -> str:
        if ticker in self.symbol_map:
            return self.symbol_map[ticker]
        cleaned = ticker.strip().replace("-", ".").upper()
        return self.symbol_map.get(cleaned, cleaned)

    def redacted_url(self, path: str, params: dict[str, Any]) -> str:
        visible = {**params, "apikey": "***"}
        query = "&".join(f"{key}={value}" for key, value in visible.items())
        return f"{self.base_url}/{path.lstrip('/')}?{query}"

    def _classify_response_error(self, status_code: int, text: str) -> str:
        lower = text.lower()
        if status_code == 429 or "rate limit" in lower or "limit" in lower and "exceed" in lower:
            return "rate_limited"
        if status_code in {401, 402} or "invalid api" in lower or "invalid key" in lower:
            return "invalid_api_key"
        if status_code == 403 or "forbidden" in lower or "permission" in lower or "not available" in lower:
            return "permission_denied"
        if status_code >= 400:
            return "network_error"
        return "schema_parse_error"

    def _request_json(self, endpoint_type: str, path: str, params: dict[str, Any]) -> tuple[Any, int, str]:
        if not self.enabled:
            raise FMPProviderError("missing_api_key", "FMP_API_KEY is not configured")

        request_params = {**params, "apikey": self.api_key}
        try:
            response = self.session.get(
                f"{self.base_url}/{path.lstrip('/')}",
                params=request_params,
                headers={"User-Agent": "us-market-review/0.1"},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise FMPProviderError("network_error", f"{endpoint_type}: {exc}") from exc

        text = response.text or ""
        if response.status_code >= 400:
            category = self._classify_response_error(response.status_code, text)
            raise FMPProviderError(category, f"{endpoint_type}: HTTP {response.status_code}", response.status_code)

        try:
            payload = response.json()
        except ValueError as exc:
            raise FMPProviderError("schema_parse_error", f"{endpoint_type}: invalid JSON", response.status_code) from exc

        if payload in (None, "", [], {}):
            raise FMPProviderError("empty_response", f"{endpoint_type}: empty response", response.status_code)
        if isinstance(payload, dict):
            message = payload.get("Error Message") or payload.get("error") or payload.get("message")
            if message:
                category = self._classify_response_error(response.status_code, str(message))
                raise FMPProviderError(category, f"{endpoint_type}: {message}", response.status_code)
        return payload, response.status_code, text

    def _payload_preview(self, payload: Any, raw_text: str = "", limit: int = 300) -> tuple[str, str]:
        json_type = type(payload).__name__
        try:
            preview = json.dumps(payload, ensure_ascii=False) if payload is not None else raw_text
        except TypeError:
            preview = raw_text or str(payload)
        if self.api_key:
            preview = preview.replace(self.api_key, "***")
        preview = " ".join(preview.split())
        if len(preview) > limit:
            preview = preview[: limit - 3] + "..."
        return json_type, preview

    def _quote_from_payload(self, payload: Any, symbol: str) -> FMPQuote:
        rows = payload if isinstance(payload, list) else [payload]
        rows = [row for row in rows if isinstance(row, dict)]
        if not rows:
            raise FMPProviderError("empty_response", "quote: empty response")

        upper_symbol = symbol.upper()
        row = next((item for item in rows if str(item.get("symbol", "")).upper() == upper_symbol), rows[0])
        price = safe_float(row.get("price") or row.get("lastPrice") or row.get("close"))
        if price is None:
            raise FMPProviderError("schema_parse_error", "quote: missing price field")

        previous_close = safe_float(row.get("previousClose") or row.get("prevClose"))
        change = safe_float(row.get("change") or row.get("changes"))
        change_percent = safe_float(row.get("changesPercentage") or row.get("changePercentage") or row.get("changePercent"))
        volume = safe_float(row.get("volume"))
        return FMPQuote(
            symbol=str(row.get("symbol") or symbol).upper(),
            price=price,
            previous_close=previous_close,
            change=change,
            change_percent=change_percent,
            volume=volume,
            raw=row,
        )

    def fetch_quote(self, ticker: str) -> FMPQuote:
        symbol = self.fmp_symbol(ticker)
        if symbol in self.quote_cache:
            return self.quote_cache[symbol]
        payload, _, _ = self._request_json("quote", "quote", {"symbol": symbol})
        quote = self._quote_from_payload(payload, symbol)
        self.quote_cache[symbol] = quote
        return quote

    def prefetch_quotes(self, tickers: list[str]) -> dict[str, FMPQuote]:
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
            payload, _, _ = self._request_json("batch_quote", "batch-quote", {"symbols": ",".join(chunk)})
            rows = payload if isinstance(payload, list) else [payload]
            for row in rows:
                if not isinstance(row, dict):
                    continue
                symbol = str(row.get("symbol") or "").upper()
                if not symbol:
                    continue
                try:
                    self.quote_cache[symbol] = self._quote_from_payload([row], symbol)
                except FMPProviderError:
                    continue
        return self.quote_cache

    def quote_to_frame(self, quote: FMPQuote) -> pd.DataFrame:
        previous_close = quote.previous_close
        if previous_close is None and quote.change is not None:
            previous_close = quote.price - quote.change
        if previous_close is None and quote.change_percent not in (None, -100):
            previous_close = quote.price / (1 + quote.change_percent / 100)

        today = utc_now_naive().normalize()
        rows: list[dict[str, Any]] = []
        if previous_close not in (None, 0):
            rows.append({"Date": today - pd.Timedelta(days=1), "Close": previous_close, "Volume": None})
        rows.append({"Date": today, "Close": quote.price, "Volume": quote.volume})
        frame = normalize_history_frame(pd.DataFrame(rows))
        frame.attrs["quote_success"] = True
        frame.attrs["fmp_symbol"] = quote.symbol
        frame.attrs["fmp_quote_only"] = True
        return frame

    def _historical_rows(self, payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            rows = payload
        elif isinstance(payload, dict) and isinstance(payload.get("historical"), list):
            rows = payload["historical"]
        elif isinstance(payload, dict) and isinstance(payload.get("data"), list):
            rows = payload["data"]
        else:
            raise FMPProviderError("schema_parse_error", "historical_eod: unexpected response schema")
        return [row for row in rows if isinstance(row, dict)]

    def fetch_historical_frame(self, ticker: str, period: str) -> pd.DataFrame:
        symbol = self.fmp_symbol(ticker)
        payload, _, _ = self._request_json("historical_eod", "historical-price-eod/full", {"symbol": symbol})
        rows = self._historical_rows(payload)
        if not rows:
            raise FMPProviderError("empty_response", "historical_eod: empty response")

        frame = pd.DataFrame(rows)
        date_column = "date" if "date" in frame.columns else "Date" if "Date" in frame.columns else ""
        close_column = "close" if "close" in frame.columns else "Close" if "Close" in frame.columns else ""
        if not date_column or not close_column:
            raise FMPProviderError("schema_parse_error", "historical_eod: missing date/close fields")

        frame = frame.rename(
            columns={
                date_column: "Date",
                "open": "Open",
                "high": "High",
                "low": "Low",
                close_column: "Close",
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
            cutoff = utc_now_naive() - pd.Timedelta(days=days + 5)
            frame = frame[frame.index >= cutoff]
        frame.attrs["quote_success"] = True
        frame.attrs["fmp_symbol"] = symbol
        frame.attrs["fmp_quote_only"] = False
        return frame

    def fetch_history(self, ticker: str, period: str, interval: str) -> pd.DataFrame:
        if interval not in {"1d", "1D", "d", "D"}:
            raise ValueError("FMP provider only supports daily history")

        quote = self.fetch_quote(ticker)
        try:
            return self.fetch_historical_frame(ticker, period)
        except FMPProviderError as exc:
            frame = self.quote_to_frame(quote)
            frame.attrs["historical_error_category"] = exc.category
            frame.attrs["historical_error"] = str(exc)
            return frame

    def diagnose_symbol(self, ticker: str) -> dict[str, Any]:
        symbol = self.fmp_symbol(ticker)
        result: dict[str, Any] = {
            "ticker": ticker,
            "symbol": symbol,
            "api_key": self.api_key_metadata(),
            "endpoints": [],
            "quote_parse_success": False,
            "historical_parse_success": False,
            "failure_category": "",
        }

        endpoint_specs = [
            ("quote", "quote", {"symbol": symbol}),
            ("historical_eod", "historical-price-eod/full", {"symbol": symbol}),
        ]
        for endpoint_type, path, params in endpoint_specs:
            endpoint_result: dict[str, Any] = {
                "endpoint_type": endpoint_type,
                "url": self.redacted_url(path, params),
                "http_status": None,
                "json_type": "",
                "preview": "",
                "parse_success": False,
                "failure_category": "",
                "failure_reason": "",
            }
            try:
                payload, status_code, raw_text = self._request_json(endpoint_type, path, params)
                json_type, preview = self._payload_preview(payload, raw_text)
                endpoint_result["http_status"] = status_code
                endpoint_result["json_type"] = json_type
                endpoint_result["preview"] = preview
                if endpoint_type == "quote":
                    self._quote_from_payload(payload, symbol)
                    result["quote_parse_success"] = True
                else:
                    rows = self._historical_rows(payload)
                    if not rows:
                        raise FMPProviderError("empty_response", "historical_eod: empty response", status_code)
                    result["historical_parse_success"] = True
                endpoint_result["parse_success"] = True
            except FMPProviderError as exc:
                endpoint_result["http_status"] = exc.status_code
                endpoint_result["failure_category"] = exc.category
                endpoint_result["failure_reason"] = str(exc)
                if not result["failure_category"]:
                    result["failure_category"] = exc.category
            except Exception as exc:  # noqa: BLE001 - diagnostics must continue.
                endpoint_result["failure_category"] = "schema_parse_error"
                endpoint_result["failure_reason"] = str(exc)
                if not result["failure_category"]:
                    result["failure_category"] = "schema_parse_error"
            result["endpoints"].append(endpoint_result)

        if result["quote_parse_success"]:
            result["failure_category"] = ""
        elif not result["failure_category"]:
            result["failure_category"] = "empty_response"
        return result


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
            cutoff = utc_now_naive() - pd.Timedelta(days=days + 5)
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
            base_url=options.get("base_url") if isinstance(options, dict) else None,
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
            base_url=options.get("base_url") if isinstance(options, dict) else None,
        )
        return provider.fmp_symbol(ticker)
    if normalized == "stooq":
        provider = make_provider("stooq", options)
        if isinstance(provider, StooqProvider):
            return provider.stooq_symbol(ticker)
    if normalized in {"cache", "local_cache", "local market cache"}:
        return ticker
    return ticker
