from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


def safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except TypeError:
        pass
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def pick_close_column(frame: pd.DataFrame) -> str:
    for column in ("Close", "Adj Close"):
        if column in frame.columns:
            return column
    raise KeyError("Neither Close nor Adj Close exists in price frame.")


def pct_change_latest(close: pd.Series) -> float | None:
    clean = close.dropna()
    if len(clean) < 2:
        return None
    prev = safe_float(clean.iloc[-2])
    latest = safe_float(clean.iloc[-1])
    if prev in (None, 0) or latest is None:
        return None
    return latest / prev - 1


def moving_average(close: pd.Series, window: int) -> float | None:
    clean = close.dropna()
    if len(clean) < window:
        return None
    return safe_float(clean.rolling(window).mean().iloc[-1])


def ma_deviation(close: pd.Series, window: int) -> float | None:
    clean = close.dropna()
    if clean.empty:
        return None
    latest = safe_float(clean.iloc[-1])
    ma_value = moving_average(clean, window)
    if latest is None or ma_value in (None, 0):
        return None
    return latest / ma_value - 1


def rsi(close: pd.Series, window: int = 14) -> float | None:
    clean = close.dropna().astype(float)
    if len(clean) <= window:
        return None

    delta = clean.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1 / window, min_periods=window, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / window, min_periods=window, adjust=False).mean()
    latest_loss = safe_float(avg_loss.iloc[-1])

    if latest_loss is None:
        return None
    if latest_loss == 0:
        return 100.0

    rs = avg_gain.iloc[-1] / latest_loss
    return safe_float(100 - (100 / (1 + rs)))


def summarize_price_frame(frame: pd.DataFrame) -> dict[str, Any]:
    if frame is None or frame.empty:
        return {
            "as_of": None,
            "last_close": None,
            "daily_change": None,
            "ma5_deviation": None,
            "ma20_deviation": None,
            "rsi14": None,
            "volume": None,
        }

    close_column = pick_close_column(frame)
    clean = frame.dropna(how="all").copy()
    close = clean[close_column]
    latest_row = clean.iloc[-1]
    latest_index = clean.index[-1]

    as_of = latest_index.date().isoformat() if hasattr(latest_index, "date") else str(latest_index)

    return {
        "as_of": as_of,
        "last_close": safe_float(latest_row.get(close_column)),
        "daily_change": pct_change_latest(close),
        "ma5_deviation": ma_deviation(close, 5),
        "ma20_deviation": ma_deviation(close, 20),
        "rsi14": rsi(close, 14),
        "volume": safe_float(latest_row.get("Volume")),
    }


def rank_by_metric(items: list[dict[str, Any]], metric: str, reverse: bool = True) -> list[dict[str, Any]]:
    filtered = [item for item in items if item.get(metric) is not None]
    return sorted(filtered, key=lambda item: item[metric], reverse=reverse)


def format_number(value: Any, digits: int = 2) -> str:
    number = safe_float(value)
    if number is None:
        return "N/A"
    if abs(number) >= 1000:
        return f"{number:,.{digits}f}"
    return f"{number:.{digits}f}"


def format_percent(value: Any, digits: int = 2) -> str:
    number = safe_float(value)
    if number is None:
        return "N/A"
    return f"{number * 100:+.{digits}f}%"


def format_rsi(value: Any) -> str:
    number = safe_float(value)
    if number is None:
        return "N/A"
    return f"{number:.1f}"


def breadth(items: list[dict[str, Any]]) -> dict[str, int]:
    up = 0
    down = 0
    flat = 0
    for item in items:
        change = safe_float(item.get("daily_change"))
        if change is None:
            continue
        if np.isclose(change, 0):
            flat += 1
        elif change > 0:
            up += 1
        else:
            down += 1
    return {"up": up, "down": down, "flat": flat}

