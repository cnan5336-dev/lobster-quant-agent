import csv
import json
import math
import os
import re
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

try:
    from .. import http_client as requests
except ImportError:  # Direct CLI execution adds lobster_quant_agent/ to sys.path.
    import http_client as requests


STATE_DIR = os.path.abspath(os.path.expanduser(
    os.environ.get("LOBSTER_QUANT_HOME", "~/.openclaw/lobster-quant-agent")
))
CACHE_DIR = os.path.join(STATE_DIR, "cache", "backtest")
HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://quote.eastmoney.com/",
}
SOURCE_NAMES = {
    "eastmoney": "东方财富",
    "sina_daily": "新浪日K兜底",
    "sina_minute": "新浪分钟K兜底",
    "akshare": "akshare兜底",
}


def _secid(symbol: str) -> str:
    return f"1.{symbol}" if str(symbol).startswith(("5", "6", "9")) else f"0.{symbol}"


def _normalize_date(value: str) -> str:
    return value.replace("-", "")


def _cache_paths(symbol: str, interval: str, start: str, end: str):
    stem = f"{symbol}_{interval}_{_normalize_date(start)}_{_normalize_date(end)}"
    return (
        os.path.join(CACHE_DIR, f"{stem}.csv"),
        os.path.join(CACHE_DIR, f"{stem}.meta.json"),
    )


def _read_cache(csv_path: str, meta_path: str) -> Optional[Dict[str, Any]]:
    if not os.path.exists(csv_path):
        return None
    bars = []
    with open(csv_path, "r", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            bars.append({
                "time": row["time"],
                "open": float(row["open"]),
                "close": float(row["close"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "volume": float(row["volume"]),
                "amount": float(row["amount"]) if row.get("amount") else None,
            })
    meta = {}
    if os.path.exists(meta_path):
        with open(meta_path, "r", encoding="utf-8") as handle:
            meta = json.load(handle)
    bars = _validate_bars(bars, "本地缓存")
    original_source = meta.get("source")
    source_key = next((key for key, value in SOURCE_NAMES.items() if value == original_source), None)
    if meta.get("volume_unit") != "share":
        if source_key is None:
            raise RuntimeError("旧缓存缺少可核验的成交量单位")
        if source_key in {"eastmoney", "akshare"}:
            for bar in bars:
                bar["volume"] *= 100
    adjustment = meta.get("price_adjustment") or (
        "qfq" if source_key in {"eastmoney", "akshare"} else "none"
    )
    age_seconds = time.time() - os.path.getmtime(csv_path)
    return {
        "bars": bars,
        "source": "本地缓存",
        "original_source": original_source,
        "volume_unit": "share",
        "amount_unit": "CNY",
        "price_adjustment": adjustment,
        "cache_path": csv_path,
        "cache_time": meta.get("cached_at"),
        "cache_stale": age_seconds > (86400 if "_1d_" not in csv_path else 259200),
        "used_cache": True,
        "available_start": bars[0]["time"] if bars else None,
        "available_end": bars[-1]["time"] if bars else None,
    }


def _write_cache(csv_path: str, meta_path: str, bars: List[Dict[str, Any]], source: str, price_adjustment: str = "unknown") -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(csv_path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["time", "open", "close", "high", "low", "volume", "amount"],
        )
        writer.writeheader()
        writer.writerows(bars)
    with open(meta_path, "w", encoding="utf-8") as handle:
        json.dump({
            "source": source,
            "schema_version": "bars/v2",
            "volume_unit": "share",
            "amount_unit": "CNY",
            "price_adjustment": price_adjustment,
            "cached_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "rows": len(bars),
        }, handle, ensure_ascii=False, indent=2)


def _validate_bars(bars: List[Dict[str, Any]], source: str) -> List[Dict[str, Any]]:
    if not bars:
        raise RuntimeError(f"{source}返回空数据")
    required = ("time", "open", "close", "high", "low", "volume")
    clean = {}
    for index, bar in enumerate(bars):
        missing = [field for field in required if field not in bar or bar[field] is None]
        if missing:
            raise RuntimeError(f"{source}字段缺失：第{index + 1}行缺少 {','.join(missing)}")
        try:
            timestamp = str(bar["time"])
            parsed_time = datetime.fromisoformat(timestamp)
            timestamp = parsed_time.strftime("%Y-%m-%d %H:%M:%S" if " " in timestamp or "T" in timestamp else "%Y-%m-%d")
            row = {"time": timestamp}
            for field in ("open", "close", "high", "low", "volume", "amount"):
                value = bar.get(field)
                if field == "amount" and value in (None, ""):
                    row[field] = None
                    continue
                value = float(value)
                if not math.isfinite(value) or value < 0 or (field in {"open", "close", "high", "low"} and value == 0):
                    raise ValueError(f"{field}不是有效行情数值")
                row[field] = value
            if row["high"] < max(row["open"], row["close"], row["low"]) or row["low"] > min(row["open"], row["close"]):
                raise ValueError("OHLC价格关系异常")
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"{source}第{index + 1}行字段格式异常：{exc}") from exc
        if timestamp in clean and clean[timestamp] != row:
            raise RuntimeError(f"{source}重复时间戳数据冲突：{timestamp}")
        clean[timestamp] = row
    return [clean[key] for key in sorted(clean)]


def _source_result(
    bars: List[Dict[str, Any]],
    source_key: str,
    csv_path: str,
    meta_path: str,
    errors: List[str],
) -> Dict[str, Any]:
    source = SOURCE_NAMES[source_key]
    bars = _validate_bars(bars, source)
    if source_key in {"eastmoney", "akshare"}:
        for bar in bars:
            bar["volume"] *= 100
    adjustment = "qfq" if source_key in {"eastmoney", "akshare"} and "_1m_" not in csv_path else "none"
    _write_cache(csv_path, meta_path, bars, source, adjustment)
    cached_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return {
        "bars": bars,
        "source": source,
        "volume_unit": "share",
        "amount_unit": "CNY",
        "price_adjustment": adjustment,
        "cache_path": csv_path,
        "cache_time": cached_at,
        "cache_stale": False,
        "used_cache": False,
        "available_start": bars[0]["time"],
        "available_end": bars[-1]["time"],
        "fallback_errors": errors,
    }


def _short_error(source: str, exc: Exception) -> str:
    text = " ".join(str(exc).split())
    return f"{source}失败：{text[:240]}"


def _fetch_eastmoney(symbol: str, interval: str, start: str, end: str, timeout: float = 15, retries: int = 3) -> List[Dict[str, Any]]:
    klt_map = {"1m": "1", "5m": "5", "1d": "101"}
    if interval not in klt_map:
        raise ValueError(f"不支持的数据级别：{interval}")
    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    params = {
        "secid": _secid(symbol),
        "klt": klt_map[interval],
        "fqt": "1",
        "beg": _normalize_date(start),
        "end": _normalize_date(end),
        "lmt": "100000",
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57",
        "ut": "fa5fd1943c7b386f172d6893dbfba10b",
    }
    last_error = None
    for attempt in range(retries):
        try:
            response = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
            response.raise_for_status()
            payload = response.json() or {}
            klines = (payload.get("data") or {}).get("klines") or []
            bars = []
            for line in klines:
                parts = str(line).split(",")
                if len(parts) < 7:
                    continue
                bars.append({
                    "time": parts[0],
                    "open": float(parts[1]),
                    "close": float(parts[2]),
                    "high": float(parts[3]),
                    "low": float(parts[4]),
                    "volume": float(parts[5]),
                    "amount": float(parts[6]),
                })
            if bars:
                return bars
            last_error = RuntimeError("东方财富未返回历史K线")
        except Exception as exc:
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(0.5)
    raise RuntimeError(f"东方财富历史K线请求失败：{last_error}")


def _fetch_sina_daily(symbol: str, start: str, end: str, timeout: float = 15) -> List[Dict[str, Any]]:
    market = "sh" if str(symbol).startswith(("5", "6", "9")) else "sz"
    url = "https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_backtest=/CN_MarketDataService.getKLineData"
    params = {
        "symbol": f"{market}{symbol}",
        "scale": "240",
        "ma": "no",
        "datalen": "1023",
    }
    response = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
    response.raise_for_status()
    match = re.search(r"var\s+_backtest=\((\[.*\])\);", response.text, re.S)
    if not match:
        raise RuntimeError("新浪日K返回JSON解析失败")
    rows = json.loads(match.group(1))
    bars = []
    for row in rows:
        day = str(row.get("day", ""))
        if not day or day[:10] < start or day[:10] > end:
            continue
        bars.append({
            "time": day[:10],
            "open": row.get("open"),
            "close": row.get("close"),
            "high": row.get("high"),
            "low": row.get("low"),
            "volume": row.get("volume"),
            "amount": row.get("amount"),
        })
    return _validate_bars(bars, "新浪日K")


def _fetch_akshare(symbol: str, interval: str, start: str, end: str) -> List[Dict[str, Any]]:
    import akshare as ak

    if interval == "1d":
        frame = ak.stock_zh_a_hist(
            symbol=symbol,
            period="daily",
            start_date=_normalize_date(start),
            end_date=_normalize_date(end),
            adjust="qfq",
        )
    else:
        raise ValueError("第一版 akshare 兜底仅用于日K")
    if frame is None or frame.empty:
        raise RuntimeError("akshare 未返回历史K线")

    def pick(row, *names):
        for name in names:
            if name in row:
                return row[name]
        raise KeyError(names[0])

    bars = []
    for _, row in frame.iterrows():
        data = row.to_dict()
        bars.append({
            "time": str(pick(data, "日期", "时间")),
            "open": float(pick(data, "开盘")),
            "close": float(pick(data, "收盘")),
            "high": float(pick(data, "最高")),
            "low": float(pick(data, "最低")),
            "volume": float(pick(data, "成交量")),
            "amount": float(pick(data, "成交额")),
        })
    return bars


def _fetch_sina_minute(symbol: str, interval: str, start: str, end: str, timeout: float = 15) -> List[Dict[str, Any]]:
    if interval not in {"1m", "5m"}:
        raise ValueError("新浪分钟K线兜底仅支持 1m/5m")
    market = "sh" if str(symbol).startswith(("5", "6", "9")) else "sz"
    url = "https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_backtest=/CN_MarketDataService.getKLineData"
    params = {
        "symbol": f"{market}{symbol}",
        "scale": "1" if interval == "1m" else "5",
        "ma": "no",
        "datalen": "1023",
    }
    response = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
    response.raise_for_status()
    match = re.search(r"var\s+_backtest=\((\[.*\])\);", response.text, re.S)
    if not match:
        raise RuntimeError("新浪分钟K线返回格式异常")
    rows = json.loads(match.group(1))
    bars = []
    for row in rows:
        day = str(row.get("day", ""))
        if not day or day[:10] < start or day[:10] > end:
            continue
        bars.append({
            "time": day,
            "open": float(row["open"]),
            "close": float(row["close"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "volume": float(row["volume"]),
            "amount": float(row["amount"]) if row.get("amount") is not None else None,
        })
    if not bars:
        raise RuntimeError("新浪未返回指定区间分钟K线")
    return bars


def load_bars(
    symbol: str,
    interval: str,
    start: str,
    end: str,
    timeout_seconds: Optional[float] = None,
) -> Dict[str, Any]:
    if not re.fullmatch(r"[0-9]{6}", str(symbol)):
        raise ValueError("历史行情代码必须为六位数字")
    if interval not in {"1d", "1m", "5m"}:
        raise ValueError("历史行情级别仅支持 1d、1m、5m")
    resolve_range(1, start, end)
    bounded = {} if timeout_seconds is None else {"timeout": float(timeout_seconds)}
    if bounded and (not math.isfinite(bounded["timeout"]) or bounded["timeout"] <= 0):
        raise ValueError("数据源超时必须为有限正数")
    csv_path, meta_path = _cache_paths(symbol, interval, start, end)
    errors = []
    try:
        cached = _read_cache(csv_path, meta_path)
    except Exception as exc:
        cached = None
        errors.append(_short_error("本地缓存", exc))

    try:
        bars = _fetch_eastmoney(symbol, interval, start, end, **({**bounded, "retries": 1} if bounded else {}))
        return _source_result(bars, "eastmoney", csv_path, meta_path, errors)
    except Exception as exc:
        errors.append(_short_error("东方财富", exc))

    if interval == "1d":
        try:
            bars = _fetch_sina_daily(symbol, start, end, **bounded)
            return _source_result(bars, "sina_daily", csv_path, meta_path, errors)
        except Exception as exc:
            errors.append(_short_error("新浪日K", exc))

        try:
            if bounded:
                raise RuntimeError("限时模式跳过无法指定超时的 akshare")
            bars = _fetch_akshare(symbol, interval, start, end)
            return _source_result(bars, "akshare", csv_path, meta_path, errors)
        except Exception as exc:
            errors.append(_short_error("akshare", exc))

    if interval in {"1m", "5m"}:
        try:
            bars = _fetch_sina_minute(symbol, interval, start, end, **bounded)
            return _source_result(bars, "sina_minute", csv_path, meta_path, errors)
        except Exception as exc:
            errors.append(_short_error("新浪分钟K", exc))

    if cached:
        cached["fallback_errors"] = errors
        cached["source"] = "本地缓存"
        cached["cache_notice"] = (
            f"接口均失败，已使用本地缓存；缓存时间 {cached.get('cache_time') or '未知'}"
        )
        return cached

    raise RuntimeError(
        f"历史行情暂时不可用（{symbol} {interval} {start} 至 {end}）。"
        f"已依次尝试可用数据源：{'；'.join(errors)}。本地无可用缓存，请稍后重试。"
    )


def resolve_range(days: int, start: Optional[str], end: Optional[str]):
    if not isinstance(days, int) or isinstance(days, bool) or days <= 0:
        raise ValueError("回测天数必须为正整数")
    end_date = datetime.strptime(end, "%Y-%m-%d") if end else datetime.now()
    if start:
        start_date = datetime.strptime(start, "%Y-%m-%d")
    else:
        calendar_days = max(int(days * 1.8), days + 30)
        start_date = end_date - timedelta(days=calendar_days)
    if start_date > end_date:
        raise ValueError("回测开始日期不能晚于结束日期")
    return start_date.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d")
