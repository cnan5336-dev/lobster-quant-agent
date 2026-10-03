#!/usr/bin/env python3
import json
import os
import io
import contextlib
import fcntl
import time
import subprocess
import sys
import re
import hashlib
import importlib
import math
import shlex
from concurrent.futures import ThreadPoolExecutor, as_completed
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

import http_client as requests

from report_pipeline import render_report, fallback_analysis
from channel_adapters import get_channel_adapter

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Referer": "https://finance.sina.com.cn/",
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Connection": "close",
}



STATE_DIR = os.path.abspath(os.path.expanduser(
    os.environ.get("LOBSTER_QUANT_HOME", "~/.openclaw/lobster-quant-agent")
))
DEMO_DATA_PATH = Path(__file__).resolve().parents[2] / "docs" / "demo" / "synthetic-market-data.json"


def synthetic_demo():
    """Return a no-network, no-write preview built only from committed synthetic data."""
    data = json.loads(DEMO_DATA_PATH.read_text(encoding="utf-8"))
    return {
        "ok": True,
        "mode": "synthetic_offline",
        "provenance": data["provenance"],
        "research": {
            "a_share": data["a_share"],
            "us_market": data["us_market"],
        },
        "alert": {
            **data["alert"],
            "delivery": "skipped",
            "fail_closed": True,
        },
        "backtest": data["backtest"],
        "boundaries": [
            "OpenClaw is the only supported runtime.",
            "Research and alerts only.",
            "No broker connection or order execution.",
        ],
    }


def _state_path(*parts):
    return os.path.join(STATE_DIR, *parts)


WATCHLIST_PATH = _state_path("market_watchlist.json")


def _normalize_channel_name(channel):
    value = str(channel or "").strip().lower()
    if value in {"weixin", "wechat", "微信", "openclaw-weixin"}:
        return "weixin"
    if value in {"telegram", "tg"}:
        return "telegram"
    if value in {"qq", "qqbot"}:
        return "qq"
    return value or os.environ.get("LOBSTER_QUANT_CHANNEL", "telegram")


def _default_output_channel():
    return _normalize_channel_name(os.environ.get("LOBSTER_QUANT_CHANNEL", "telegram"))


def _normalize_notify_channels(monitor, include_fallback=True):
    raw = []
    if isinstance(monitor, dict):
        configured = monitor.get("notify_channels")
        if isinstance(configured, list):
            raw.extend(configured)
        elif configured:
            raw.append(configured)
        legacy = monitor.get("notify_channel")
        if legacy:
            raw.append(legacy)
    elif monitor:
        raw.append(monitor)

    normalized = []
    for item in raw:
        channel = _normalize_channel_name(item)
        if channel and channel not in normalized:
            normalized.append(channel)

    if not normalized and include_fallback:
        configured = os.environ.get("LOBSTER_QUANT_NOTIFY_CHANNELS", "")
        normalized = [
            _normalize_channel_name(item)
            for item in configured.split(",")
            if item.strip()
        ]
    return normalized


@contextlib.contextmanager
def _strategy_config_lock():
    lock_path = WATCHLIST_PATH + ".strategy.lock"
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    handle = open(lock_path, "a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _default_monitor_profiles():
    return {
        "normal": {
            "name": "普通盯盘",
            "interval_seconds": 20,
            "targets": ["holding_pool", "watch_pool"],
            "cooldown_minutes": 8,
            "watch_up_percent": 2,
            "watch_down_percent": -2,
            "hold_up_percent": 1,
            "hold_down_percent": -1,
            "fast_move_percent": 1,
            "use_llm": False
        },
        "strategy": {
            "name": "策略盯盘",
            "interval_seconds": 1,
            "targets": ["holding_pool", "watch_pool"],
            "cooldown_minutes": 1,
            "use_llm": False
        }
    }


def _default_watchlist_config():
    return {
        "watch_pool": [],
        "holding_pool": [],
        "monitor": {
            "enabled": False,
            "mode": "normal",
            "profile_name": "普通盯盘",
            "interval_seconds": 20,
            "market_hours_only": True,
            "targets": ["watch_pool", "holding_pool"],
            "source_channel": os.environ.get("LOBSTER_QUANT_CHANNEL", "telegram"),
            "notify_channel": "",
            "notify_channels": [],
            "last_notify_result": None,
            "cooldown_minutes": 8,
            "use_llm": False
        },
        "monitor_profiles": _default_monitor_profiles(),
        "strategy_monitor": {
            "raw_text": "",
            "rules": [],
            "targets": ["holding_pool"],
            "unavailable_conditions": [],
            "updated_at": None
        },
        "alert_rules": {
            "watch_pool": {
                "price_change_percent": 3,
                "five_min_change_percent": 2,
                "volume_spike_ratio": 2
            },
            "holding_pool": {
                "price_change_percent": 2,
                "five_min_drop_percent": -2,
                "break_support": True,
                "large_volume_drop": True
            }
        }
    }


def _normalize_monitor_config(cfg):
    defaults = _default_watchlist_config()
    monitor = cfg.setdefault("monitor", {})
    old_mode = str(monitor.get("mode", "normal"))
    if old_mode in {"low", "medium", "high"}:
        old_mode = "normal"
    if old_mode not in {"normal", "strategy"}:
        old_mode = "normal"

    profiles = _default_monitor_profiles()
    cfg["monitor_profiles"] = profiles
    profile = profiles[old_mode]
    monitor["mode"] = old_mode
    monitor["profile_name"] = profile["name"]
    monitor["interval_seconds"] = profile["interval_seconds"]
    monitor["targets"] = list(profile["targets"])
    monitor["cooldown_minutes"] = profile["cooldown_minutes"]
    monitor["use_llm"] = False
    monitor.setdefault("enabled", False)
    monitor.setdefault("market_hours_only", True)
    monitor.setdefault("source_channel", os.environ.get("LOBSTER_QUANT_CHANNEL", "telegram"))
    channels = _normalize_notify_channels(monitor)
    monitor["notify_channels"] = channels
    monitor["notify_channel"] = channels[0] if channels else ""
    monitor.setdefault("last_notify_result", None)

    strategy = cfg.setdefault("strategy_monitor", {})
    for key, value in defaults["strategy_monitor"].items():
        strategy.setdefault(key, value)
    if not isinstance(strategy.get("rules"), list):
        strategy["rules"] = []
    if not isinstance(strategy.get("unavailable_conditions"), list):
        strategy["unavailable_conditions"] = []
    return cfg


def load_watchlist_config():
    os.makedirs(os.path.dirname(WATCHLIST_PATH), exist_ok=True)
    if not os.path.exists(WATCHLIST_PATH):
        cfg = _default_watchlist_config()
        save_watchlist_config(cfg)
        return cfg
    try:
        with open(WATCHLIST_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        cfg = _default_watchlist_config()

    default = _default_watchlist_config()
    for k, v in default.items():
        if k not in cfg:
            cfg[k] = v
    if "enabled" not in cfg.get("monitor", {}):
        cfg["monitor"] = default["monitor"]
    return _normalize_monitor_config(cfg)


def save_watchlist_config(cfg):
    os.makedirs(os.path.dirname(WATCHLIST_PATH), exist_ok=True)
    tmp = f"{WATCHLIST_PATH}.tmp.{os.getpid()}.{uuid.uuid4().hex[:8]}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, WATCHLIST_PATH)


def _normalize_stock_item(code, name=None, note=None):
    code = re.sub(r"^(?:sh|sz)", "", str(code).strip().lower())
    if not re.fullmatch(r"[0-9]{6}", code):
        raise ValueError("股票池证券代码必须为六位数字，可带 sh/sz 前缀")
    name = (name or "").strip() or code
    market = "sh" if code.startswith(("5", "6", "9")) else "sz"
    return {
        "name": name,
        "code": code,
        "market": market,
        "note": note or ""
    }


def _pool_key(pool):
    if pool in {"watch", "watch_pool", "观察池", "关注池", "自选池"}:
        return "watch_pool"
    if pool in {"hold", "holding", "holding_pool", "持仓池", "持仓"}:
        return "holding_pool"
    raise ValueError(f"未知池子：{pool}")


def watchlist_add(pool, code, name=None, note=None):
    key = _pool_key(pool)
    item = _normalize_stock_item(code, name, note)
    cfg = load_watchlist_config()
    cfg[key] = [x for x in cfg.get(key, []) if str(x.get("code")) != item["code"]]
    cfg[key].append(item)
    save_watchlist_config(cfg)
    return {
        "ok": True,
        "action": "add",
        "pool": key,
        "item": item,
        "current": cfg[key]
    }


def watchlist_remove(pool, code_or_name):
    cfg = load_watchlist_config()
    key = _pool_key(pool)
    q = str(code_or_name).strip()
    old = cfg.get(key, [])
    new = [x for x in old if str(x.get("code")) != q and str(x.get("name")) != q]
    removed = len(old) - len(new)
    cfg[key] = new
    save_watchlist_config(cfg)
    return {
        "ok": True,
        "action": "remove",
        "pool": key,
        "query": q,
        "removed": removed,
        "current": cfg[key]
    }


def watchlist_list(pool=None):
    cfg = load_watchlist_config()
    if pool:
        key = _pool_key(pool)
        return {
            "ok": True,
            "pool": key,
            "items": cfg.get(key, [])
        }
    return {
        "ok": True,
        "watch_pool": cfg.get("watch_pool", []),
        "holding_pool": cfg.get("holding_pool", []),
        "monitor": cfg.get("monitor", {})
    }


def monitor_set(enabled, mode=None, source_channel=None, notify_channels=None):
    cfg = load_watchlist_config()
    cfg.setdefault("monitor", _default_watchlist_config()["monitor"])
    profiles = _default_monitor_profiles()

    if mode is None:
        mode = "normal"

    mode_alias = {
        "普通": "normal",
        "normal": "normal",
        "default": "normal",
        "严密": "normal",
        "高频": "normal",
        "高强度": "normal",
        "high": "normal",
        "策略": "strategy",
        "strategy": "strategy"
    }
    mode = mode_alias.get(str(mode), str(mode))
    if mode not in profiles:
        mode = "normal"

    profile = profiles[mode]

    cfg["monitor"]["enabled"] = bool(enabled)
    cfg["monitor"]["mode"] = mode
    cfg["monitor"]["profile_name"] = profile.get("name", mode)
    cfg["monitor"]["interval_seconds"] = profile.get("interval_seconds", 180)
    cfg["monitor"]["targets"] = profile.get("targets", ["holding_pool", "watch_pool"])
    cfg["monitor"]["cooldown_minutes"] = profile.get("cooldown_minutes", 60)
    cfg["monitor"]["use_llm"] = bool(profile.get("use_llm", False))
    if source_channel:
        cfg["monitor"]["source_channel"] = _normalize_channel_name(source_channel)
    else:
        cfg["monitor"].setdefault(
            "source_channel", os.environ.get("LOBSTER_QUANT_CHANNEL", "telegram")
        )
    channels = _normalize_notify_channels(
        {"notify_channels": notify_channels}
        if notify_channels is not None else
        cfg["monitor"]
    )
    cfg["monitor"]["notify_channels"] = channels
    cfg["monitor"]["notify_channel"] = channels[0] if channels else ""

    save_watchlist_config(cfg)
    return {
        "ok": True,
        "monitor": cfg["monitor"],
        "profile": profile
    }


def monitor_status():
    cfg = load_watchlist_config()
    strategy = cfg.get("strategy_monitor", {}) or {}
    try:
        runtime_state = load_monitor_state()
        runtime_unavailable = runtime_state.get("strategy_last_unavailable", [])
    except Exception:
        runtime_state = {}
        runtime_unavailable = []
    return {
        "ok": True,
        "monitor": cfg.get("monitor", _default_watchlist_config()["monitor"]),
        "trading_time": trading_time_status(),
        "watch_pool_count": len(cfg.get("watch_pool", [])),
        "holding_pool_count": len(cfg.get("holding_pool", [])),
        "last_scan_at": runtime_state.get("last_scan_at"),
        "last_quote_at": runtime_state.get("last_quote_at"),
        "last_checked_symbols": runtime_state.get("last_checked_symbols", []),
        "last_alert_candidates": runtime_state.get("last_alert_candidates", []),
        "last_suppressed": runtime_state.get("last_suppressed", []),
        "last_notify_result": runtime_state.get("last_notify_result"),
        "strategy": {
            "configured": bool(strategy.get("rules")),
            "raw_text": strategy.get("raw_text", ""),
            "rules_count": len(strategy.get("rules", [])),
            "unavailable_conditions": strategy.get("unavailable_conditions", []),
            "runtime_unavailable": runtime_unavailable
        }
    }


def _monitor_status_with_runtime():
    status = monitor_status()
    status["process"] = monitor_pid_status()
    return status


def _monitor_status_request(text):
    raw = str(text or "").strip()
    lowered = raw.lower()
    return (
        lowered == "monitor status"
        or any(
            phrase in raw
            for phrase in ("查看盯盘状态", "盯盘状态", "查看盯盘")
        )
    )


def _monitor_raw_output_requested(text):
    return bool(re.search(r"原始\s*JSON|调试信息", str(text or ""), re.I))


def _monitor_target_name(target):
    return {
        "holding_pool": "持仓池",
        "watch_pool": "观察池",
    }.get(str(target), str(target))


def format_monitor_status_summary(status):
    monitor = status.get("monitor", {}) or {}
    process = status.get("process", {}) or {}
    strategy = status.get("strategy", {}) or {}
    trading = status.get("trading_time", {}) or {}
    running = bool(process.get("running"))
    pid = process.get("pid") if running else None
    unavailable = list(strategy.get("unavailable_conditions") or [])
    runtime_unavailable = strategy.get("runtime_unavailable") or []
    for item in runtime_unavailable:
        if item not in unavailable:
            unavailable.append(item)
    targets = monitor.get("targets") or []
    target_text = "、".join(_monitor_target_name(item) for item in targets) or "无"
    source_channel = _normalize_channel_name(monitor.get("source_channel"))
    notify_channels = _normalize_notify_channels(monitor)
    channel_display = {
        "weixin": "微信",
        "telegram": "Telegram",
        "qq": "QQ",
    }
    notify_text = "、".join(channel_display.get(item, item) for item in notify_channels)
    last_notify = monitor.get("last_notify_result") or status.get("last_notify_result")
    if isinstance(last_notify, dict):
        last_notify_text = (
            f"{'成功' if last_notify.get('ok') else '失败'}"
            f"（{last_notify.get('sent_at') or '-'}）"
        )
    else:
        last_notify_text = "暂无记录"

    lines = [
        "当前盯盘状态：",
        "",
        "一、后台状态",
        f"- 盯盘开关：{'开启' if monitor.get('enabled') else '关闭'}",
        f"- 后台进程：{'运行中' if running else '未运行'}",
        f"- PID：{pid if pid is not None else '无'}",
        f"- 是否只在交易时段运行：{'是' if monitor.get('market_hours_only') else '否'}",
        f"- 当前交易状态：{trading.get('message') or '-'}",
        (
            f"- 预计恢复扫描：{trading.get('next_open')}（按工作日规则）"
            if not trading.get("is_trading_time") and trading.get("next_open")
            else "- 预计恢复扫描：当前已在交易时段"
        ),
        "",
        "二、盯盘模式",
        f"- 模式：{monitor.get('profile_name') or monitor.get('mode') or '-'}",
        f"- 扫描间隔：{monitor.get('interval_seconds', '-')} 秒",
        f"- 冷却时间：{monitor.get('cooldown_minutes', '-')} 分钟",
        f"- source_channel：{channel_display.get(source_channel, source_channel)}",
        f"- notify_channels：{notify_text}",
        f"- last_notify_result：{last_notify_text}",
        f"- 是否使用 LLM：{'是' if monitor.get('use_llm') else '否'}",
        "",
        "三、监控范围",
        f"- 持仓池：{status.get('holding_pool_count', 0)} 只",
        f"- 观察池：{status.get('watch_pool_count', 0)} 只",
        f"- 当前监控目标：{target_text}",
        "",
        "四、策略盯盘",
        f"- 是否配置策略：{'是' if strategy.get('configured') else '否'}",
        f"- 策略规则数量：{strategy.get('rules_count', 0)}",
        f"- 不可用条件：{'、'.join(map(str, unavailable)) if unavailable else '无'}",
        "",
        "五、结论",
        (
            "- 当前后台盯盘正在运行，请注意通知频率。"
            if running else
            "- 当前后台盯盘未运行，安全。"
        ),
        "- 以上后台进程状态来自 PID、进程身份和运行锁的联合检查。",
    ]
    return "\n".join(lines)


def monitor_status_output(text=""):
    status = _monitor_status_with_runtime()
    if _monitor_raw_output_requested(text):
        return status
    return {
        "_output_format": "text",
        "text": format_monitor_status_summary(status),
    }


def strategy_capabilities():
    return {
        "supported": [
            "实时价格", "涨跌幅", "成交量", "成交额",
            "1分钟K线", "3分钟K线", "5分钟K线",
            "日线成交量均量",
            "MACD", "KDJ", "RSI", "MA均线",
            "实时量比", "换手率", "振幅",
            "买一到买五", "卖一到卖五", "盘口委比", "买卖盘强弱"
        ],
        "source_notes": {
            "primary": "新浪实时行情（价格、涨跌幅、成交量、成交额、五档盘口）",
            "supplement": "东方财富（1分钟走势、实时量比、换手率）",
            "local": "3/5分钟K线聚合、日线成交量均量及 MACD/KDJ/RSI/MA 计算"
        },
        "limitations": [
            "接口缺失或休市时，对应条件标记为暂不可用并跳过触发",
            "分钟指标需要接口返回足够历史分钟数据",
            "换手率和量比依赖东方财富补充接口"
        ]
    }


def _strategy_number(pattern, text, default=None):
    match = re.search(pattern, text, re.IGNORECASE)
    if not match:
        return default
    try:
        return float(match.group(1))
    except Exception:
        return default


def _strategy_timeframe(text, keyword, default=1):
    number_map = {"一": 1, "三": 3, "五": 5}
    escaped = re.escape(keyword)
    patterns = (
        rf"([135一三五])\s*分钟\s*{escaped}",
        rf"{escaped}\s*([135一三五])\s*分钟"
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            value = match.group(1)
            return number_map.get(value, int(value) if value.isdigit() else default)
    return default


def _strategy_targets_from_text(text):
    raw = str(text or "")
    if any(term in raw for term in ("持仓池和观察池", "观察池和持仓池", "持仓池、观察池", "观察池、持仓池", "持仓池和观察池都", "持仓池以及观察池")):
        return ["holding_pool", "watch_pool"]
    targets = []
    if any(term in raw for term in ("持仓池", "持仓", "持仓股")):
        targets.append("holding_pool")
    if any(term in raw for term in ("观察池", "观察", "自选池", "关注池")):
        targets.append("watch_pool")
    return targets or ["holding_pool"]


def _strategy_side_label(side):
    return {"buy": "买入", "sell": "卖出"}.get(side, "条件")


def _strategy_condition_summary(condition):
    """Describe the executable DSL, rather than repeating the unparsed request."""
    timeframe = condition.get("timeframe_minutes", 1)
    frame = "日线" if timeframe == 1440 else f"{timeframe}分钟"
    kind = condition.get("type")
    operator = condition.get("operator", ">=")
    if kind in {"macd_cross", "kdj_cross"}:
        indicator = "MACD" if kind == "macd_cross" else "KDJ"
        direction = "金叉" if condition.get("direction") == "golden_cross" else "死叉"
        return f"{frame}{indicator}{direction}"
    if kind == "indicator_threshold":
        return f"{frame}{condition.get('indicator', '').upper()}{operator}{condition.get('value'):g}"
    if kind == "price_vs_ma":
        return f"{frame}最新收盘价{operator}MA{condition.get('period')}"
    if kind == "volume_vs_average":
        current = "当日累计成交量" if timeframe == 1440 else f"当前{frame}成交量"
        return f"{current}{operator}前{condition.get('lookback')}根{frame}均量×{condition.get('factor'):g}"
    if kind == "order_book_strength":
        ratio = "卖盘/买盘" if condition.get("direction") == "sell" else "买盘/卖盘"
        return f"五档{ratio}{operator}{condition.get('min_ratio'):g}倍"
    if kind == "quote_threshold":
        names = {"price": "实时价格", "change_percent": "当日涨跌幅", "volume_ratio": "实时量比",
                 "turnover_rate_percent": "换手率", "amplitude_percent": "振幅"}
        field = condition.get("field")
        unit = "%" if field in {"change_percent", "turnover_rate_percent", "amplitude_percent"} else "元" if field == "price" else ""
        return f"{names.get(field, field)}{operator}{condition.get('value'):g}{unit}"
    return str(kind)


def _strategy_rule_summary(rule, targets=None):
    scope = rule.get("code") or "、".join(_monitor_target_name(target) for target in (targets or ["holding_pool"]))
    logic = "任意一个" if rule.get("logic") == "any" else "全部"
    conditions = "；".join(_strategy_condition_summary(item) for item in rule.get("conditions", []))
    return f"{scope}：满足{logic}条件时发出{_strategy_side_label(rule.get('side'))}提醒（{conditions}）。仅提醒，不自动交易。"


def _parse_strategy_atom(clause):
    """Parse one complete condition. Never accept a recognized substring alone."""
    number = r"-?(?:\d+(?:\.\d+)?|\.\d+)"
    comparator = r">=|<=|>|<|=="
    frame_pattern = r"(?:(?:\d+|一|三|五)分钟|日线)"
    warnings = []
    clause = re.sub(rf"^(MACD|KDJ|RSI)({frame_pattern})", r"\2\1", clause, flags=re.I)
    frame_match = re.match(rf"^({frame_pattern})", clause)
    frame_text = frame_match.group(1) if frame_match else None
    atom = clause[frame_match.end():] if frame_match else clause
    if frame_text == "日线":
        timeframe = 1440
    elif frame_text:
        frame_number = frame_text[:-2]
        timeframe = {"一": 1, "三": 3, "五": 5}.get(frame_number)
        if timeframe is None:
            timeframe = int(frame_number)
    else:
        timeframe = 1
    if timeframe not in {1, 3, 5, 1440}:
        return None, [], "指标周期仅支持1、3、5分钟或日线，不能自动替换周期"

    cross = re.fullmatch(r"(MACD|KDJ)(?:出现|形成|发生)?(金叉|死叉)", atom, re.I)
    if cross:
        if not frame_text:
            warnings.append("未指定指标周期，按1分钟计算")
        return {"type": cross.group(1).lower() + "_cross", "timeframe_minutes": timeframe,
                "direction": "golden_cross" if cross.group(2) == "金叉" else "death_cross"}, warnings, None

    rsi = re.fullmatch(rf"RSI({comparator})({number})", atom, re.I)
    if rsi:
        value = float(rsi.group(2))
        if not 0 <= value <= 100:
            return None, [], "RSI阈值必须在0到100之间"
        if not frame_text:
            warnings.append("未指定指标周期，按1分钟计算")
        warnings.append("RSI使用固定14期算法")
        return {"type": "indicator_threshold", "indicator": "rsi", "timeframe_minutes": timeframe,
                "operator": rsi.group(1), "value": value}, warnings, None

    ma = re.fullmatch(rf"(?:价格|股价|现价)?({comparator})(?:MA(\d+)|(\d+)(日)?均线)", atom, re.I)
    if ma:
        period = int(ma.group(2) or ma.group(3))
        if period not in {5, 10, 20, 30, 60}:
            return None, [], "MA周期仅支持5、10、20、30、60，不能静默改成其他周期"
        if ma.group(4):
            if frame_text and timeframe != 1440:
                return None, [], "分钟周期与日均线冲突，请明确使用日线MA还是分钟MA"
            timeframe = 1440
        elif not frame_text:
            warnings.append("未指定均线K线周期，按1分钟计算")
        return {"type": "price_vs_ma", "timeframe_minutes": timeframe, "period": period,
                "operator": ma.group(1)}, warnings, None

    volume = re.fullmatch(rf"成交量({comparator})(?:过去|近)(\d+)(天|日|分钟|根)(?:平均成交量|均量)(?:的)?({number})倍", atom)
    if volume:
        lookback, unit, factor = int(volume.group(2)), volume.group(3), float(volume.group(4))
        if not 1 <= lookback <= 30 or not math.isfinite(factor) or factor <= 0:
            return None, [], "成交量均量窗口需为1到30，倍数需为正数"
        if unit in {"天", "日"}:
            if frame_text and timeframe != 1440:
                return None, [], "当前分钟成交量与日均量口径不同，请明确日线或分钟线"
            timeframe = 1440
            warnings.append("日成交量为当日累计量，对比此前完整交易日均量，不是同一时刻均量")
        elif unit == "分钟" and timeframe != 1:
            return None, [], "分钟均量窗口与K线周期不一致；请改用明确的前N根均量"
        elif not frame_text:
            warnings.append("未指定成交量K线周期，按1分钟计算")
        return {"type": "volume_vs_average", "timeframe_minutes": timeframe, "lookback": lookback,
                "operator": volume.group(1), "factor": factor}, warnings, None

    book = re.fullmatch(rf"(买|卖)盘(?:明显)?强于(卖|买)盘(?:的)?({number})倍", atom)
    if book and book.group(1) != book.group(2):
        if frame_text:
            return None, [], "五档盘口是实时快照，不支持指定K线周期"
        factor = float(book.group(3))
        if not math.isfinite(factor) or factor < 1:
            return None, [], "盘口强于条件的倍数需大于等于1"
        return {"type": "order_book_strength", "direction": "buy" if book.group(1) == "买" else "sell",
                "operator": ">", "min_ratio": factor}, warnings, None

    quote = re.fullmatch(rf"((?:单日|当日)?(?:涨跌幅|涨幅|跌幅)|价格|股价|现价|量比|换手率|振幅)({comparator})({number})(%|元)?", atom)
    if quote:
        if frame_text:
            return None, [], "价格、涨跌幅和盘口字段使用实时快照；指定分钟或日线收盘条件暂不支持"
        field_text, operator, value, unit = quote.group(1), quote.group(2), float(quote.group(3)), quote.group(4)
        if not math.isfinite(value):
            return None, [], "阈值必须为有限数值"
        field_text = re.sub(r"^(?:单日|当日)", "", field_text)
        field = {"价格": "price", "股价": "price", "现价": "price", "量比": "volume_ratio",
                 "换手率": "turnover_rate_percent", "振幅": "amplitude_percent",
                 "涨跌幅": "change_percent", "涨幅": "change_percent", "跌幅": "change_percent"}[field_text]
        percent = field in {"change_percent", "turnover_rate_percent", "amplitude_percent"}
        if (unit == "%" and not percent) or (unit == "元" and field != "price"):
            return None, [], "阈值单位与字段不一致"
        if field != "change_percent" and value < 0 or field == "price" and value <= 0:
            return None, [], "价格需为正数，量比、换手率和振幅不能为负数"
        if field_text == "跌幅":
            if value < 0:
                return None, [], "跌幅请使用正数百分比，例如跌幅超过3%"
            value = -value
            operator = {">": "<", ">=": "<=", "<": ">", "<=": ">=", "==": "=="}[operator]
        if percent and not unit:
            warnings.append("百分比字段未写%，按百分点解释")
        return {"type": "quote_threshold", "field": field, "operator": operator, "value": value}, warnings, None

    if "放量" in atom or "占优" in atom or "明显" in atom:
        return None, [], "定性条件需要数值定义；请写明均量窗口、倍数或盘口比例"
    if any(term in atom for term in ("突破", "跌破", "站上", "上穿", "下穿")):
        return None, [], "穿越事件不能替换成当前高于/低于；请明确改用阈值状态，或使用MACD/KDJ金叉死叉"
    return None, [], f"无法识别条件片段：{clause}；请补充支持的指标、方向及阈值，不会忽略未识别文字"


def parse_strategy_text(text):
    raw = " ".join(str(text or "").strip().split())

    def reject(message):
        return {"ok": False, "error": "策略包含无法识别或不完整的条件，未生成可执行配置",
                "rules": [], "unavailable_conditions": [message], "needs_clarification": True}

    if not raw:
        return reject("策略内容为空")
    if len(raw) > 2000:
        return reject("策略内容过长，请拆成较短的独立策略")
    compact = re.sub(r"\s+", "", raw).translate(str.maketrans({"％": "%", "＞": ">", "＜": "<", "＝": "=", "≥": ">=", "≤": "<="}))
    # Normalize complete comparison phrases before splitting boolean connectors.
    operators = [("大于或等于", ">="), ("小于或等于", "<="), ("大于等于", ">="), ("小于等于", "<="),
                 ("不低于", ">="), ("不小于", ">="), ("不少于", ">="), ("不高于", "<="), ("不大于", "<="), ("不超过", "<="),
                 ("至少", ">="), ("至多", "<="), ("达到", ">="), ("涨到", ">="), ("跌到", "<="),
                 ("超过", ">"), ("高于", ">"), ("大于", ">"), ("低于", "<"), ("小于", "<"), ("等于", "==")]
    for phrase, symbol in operators:
        compact = compact.replace(phrase, symbol)
    compact = re.sub(r"(?<![<>=])=(?!=)", "==", compact)
    if re.search(r"[()（）]|\bNOT\b|不要|不能|不满足|没有|未出现|不是|除非|否则", compact, re.I):
        return reject("括号分组、否定或例外条件暂不支持，请拆分并明确每条条件；不会忽略这些限制")
    if any(term in compact for term in ("持续", "连续", "先", "再", "之后", "之前", "收盘后", "分钟内", "只提醒一次")):
        return reject("持续时间、先后顺序或提醒次数限制暂不支持，不能当作即时阈值执行")

    # Only consume a complete trailing notification action, never terms inside a condition.
    action = re.search(r"(?:的时候|时候|时|后)?[，,]?(?:就|则)?(?:请)?(?:自动)?提醒(?:我)?(?:(买入|加仓|卖出|止损|减仓|离场)(?:信号)?|(?:一下|通知|信号))?[。！!]*$", compact)
    side = "alert"
    if action:
        side = "sell" if action.group(1) in {"卖出", "止损", "减仓", "离场"} else "buy" if action.group(1) in {"买入", "加仓"} else "alert"
        compact = compact[:action.start()]
    else:
        compact = compact.rstrip("。")
    compact = re.sub(r"^(?:(?:请|帮我|帮忙|如果|若|当|对|监控|盯住|盯着))+", "", compact)

    pools = {"持仓池": "holding_pool", "持仓股": "holding_pool", "持仓": "holding_pool",
             "观察池": "watch_pool", "自选池": "watch_pool", "关注池": "watch_pool"}
    pool_names = "|".join(pools)
    pool_match = re.match(rf"^((?:{pool_names})(?:(?:和|与|、|以及|及)(?:{pool_names}))*)", compact)
    codes = []
    warnings = []
    if pool_match:
        targets = list(dict.fromkeys(pools[item] for item in re.findall(pool_names, pool_match.group(1))))
        compact = compact[pool_match.end():]
        scope = "pools"
    else:
        code_match = re.match(r"^(?:股票|标的)?(\d{6}(?:(?:和|与|、|,|，|以及|及)\d{6})*)(?=\d+分钟|\D|$)", compact)
        if code_match:
            codes = list(dict.fromkeys(re.findall(r"\d{6}", code_match.group(1))))
            compact = compact[code_match.end():]
            targets = []
            scope = "explicit_symbols"
        else:
            targets, scope = ["holding_pool"], "pools"
            warnings.append("未指定标的或股票池，按持仓池执行")
    compact = re.sub(r"^(?:里的|中的|的)?(?:所有股票|所有标的|股票|标的)?(?:都|均)?[：:]?", "", compact)
    if re.search(r"(?<!\d)\d{6}(?!\d)", compact) or re.search(pool_names, compact):
        return reject("多个标的必须共用同一组条件，例如“510050、510300价格高于3元”；分别设置不同条件请分开解析，股票池与代码范围不能混用")
    if any(char in compact for char in ("；", ";")):
        return reject("多条独立策略请分开解析；不支持用分号合并不同条件或动作")

    connector_pattern = r"并且|而且|同时|以及|或者|\&\&|\|\||(?i:AND|OR)|且|并|和|与|或|[，,]"
    parts = re.split(f"({connector_pattern})", compact)
    clauses, connectors = parts[::2], parts[1::2]
    logic_set = {"any" if item.upper() in {"OR", "或", "或者", "||"} else "all" for item in connectors}
    if len(logic_set) > 1:
        return reject("同时使用“并且”和“或者”时逻辑范围有歧义，请拆分规则；不会把所有条件改成“或者”")
    if any(not clause for clause in clauses):
        return reject("条件连接词前后缺少完整条件")
    conditions = []
    previous_clause = None
    for clause in clauses:
        # A complete bare threshold can inherit only its immediately preceding
        # threshold subject. Reparse it so unit/range checks remain authoritative.
        if (
            previous_clause is not None
            and conditions[-1].get("type") in {"quote_threshold", "indicator_threshold"}
            and re.fullmatch(r"(?:>=|<=|>|<|==)-?(?:\d+(?:\.\d+)?|\.\d+)(?:%|元)?", clause)
        ):
            subject = re.split(r">=|<=|>|<|==", previous_clause, maxsplit=1)[0]
            clause = subject + clause
        condition, atom_warnings, error = _parse_strategy_atom(clause)
        if error:
            return reject(error)
        conditions.append(condition)
        warnings.extend(atom_warnings)
        previous_clause = clause
    if any(item.get("timeframe_minutes", 1) != 1 for item in conditions) and any("按1分钟" in item for item in warnings):
        return reject("组合条件的K线周期作用范围不明确，请分别写明每个指标的周期")
    rules = []
    for code in codes or [None]:
        rule = {"id": hashlib.sha1((raw + (":" + code if len(codes) > 1 else "")).encode("utf-8")).hexdigest()[:12],
                "code": code, "scope": scope, "side": side, "logic": next(iter(logic_set), "all"),
                "conditions": [dict(item) for item in conditions], "raw_text": raw}
        rules.append(rule)
    return {"ok": True, "schema_version": "strategy-dsl/v1", "operation": "replace", "rules": rules,
            "targets": targets, "unavailable_conditions": [], "warnings": list(dict.fromkeys(warnings)),
            "confirmation": [_strategy_rule_summary(rule, targets) for rule in rules]}

def _strategy_payload_from_parse(text, parsed):
    return {
        "schema_version": "strategy-dsl/v1",
        "raw_text": str(text).strip(),
        "rules": parsed.get("rules", []),
        "targets": parsed.get("targets", ["holding_pool"]),
        "unavailable_conditions": parsed.get("unavailable_conditions", []),
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _strategy_hash(strategy):
    stable = {
        "schema_version": strategy.get("schema_version", "strategy-dsl/v1"),
        "raw_text": strategy.get("raw_text", ""),
        "rules": strategy.get("rules", []),
        "targets": strategy.get("targets", []),
        "unavailable_conditions": strategy.get("unavailable_conditions", []),
    }
    encoded = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _strategy_runtime_status(strategy):
    cfg = load_watchlist_config()
    monitor = cfg.get("monitor", {}) or {}
    try:
        process = monitor_pid_status()
    except Exception:
        process = {"running": False, "identity_verified": False}
    configured = bool(strategy.get("rules"))
    active = bool(
        configured
        and monitor.get("enabled")
        and monitor.get("mode") == "strategy"
        and process.get("running")
        and process.get("identity_verified")
    )
    return {
        "configured": configured,
        "config_effective": True,
        "monitor_enabled": bool(monitor.get("enabled")),
        "monitor_mode": monitor.get("mode"),
        "process_running": bool(process.get("running")),
        "active_in_runtime": active,
        "note": (
            "策略配置已加载，策略盯盘运行中。"
            if active else
            "策略配置已加载；盯盘未运行时不会产生真实提醒。"
        ),
    }


def strategy_dry_run(text):
    parsed = parse_strategy_text(text)
    draft = _strategy_payload_from_parse(text, parsed) if parsed.get("ok") else None
    return {
        "ok": bool(parsed.get("ok")),
        "dry_run": True,
        "parse_result": parsed,
        "draft": draft,
        "confirmation": parsed.get("confirmation", []),
        "warnings": parsed.get("warnings", []),
        "write_result": {"ok": False, "skipped": True, "reason": "dry-run"},
        "reload_result": {"ok": False, "skipped": True, "reason": "dry-run"},
        "effective_status": {"effective": False, "reason": "尚未写入配置"},
        "verification_result": {"ok": False, "skipped": True, "reason": "dry-run"},
    }


def strategy_set(text):
    parsed = parse_strategy_text(text)
    if not parsed.get("ok"):
        return {
            "ok": False,
            "parse_result": parsed,
            "write_result": {"ok": False, "skipped": True, "reason": "parse failed"},
            "reload_result": {"ok": False, "skipped": True, "reason": "parse failed"},
            "effective_status": {"effective": False, "reason": "策略未写入"},
            "verification_result": {"ok": False, "reason": "策略解析失败"},
            "error": parsed.get("error"),
        }

    draft = _strategy_payload_from_parse(text, parsed)
    expected_hash = _strategy_hash(draft)
    try:
        with _strategy_config_lock():
            cfg = load_watchlist_config()
            cfg["strategy_monitor"] = draft
            save_watchlist_config(cfg)
            reloaded = load_watchlist_config().get("strategy_monitor", {}) or {}
        write_result = {"ok": True, "path": WATCHLIST_PATH, "hash": expected_hash}
    except Exception as exc:
        return {
            "ok": False,
            "parse_result": parsed,
            "write_result": {"ok": False, "error": str(exc)},
            "reload_result": {"ok": False, "skipped": True},
            "effective_status": {"effective": False, "reason": "配置写入失败"},
            "verification_result": {"ok": False, "reason": "未执行二次校验"},
            "error": f"策略配置写入失败：{exc}",
        }

    actual_hash = _strategy_hash(reloaded)
    verified = actual_hash == expected_hash
    reload_result = {
        "ok": bool(reloaded),
        "strategy": reloaded,
        "hash": actual_hash,
    }
    effective = _strategy_runtime_status(reloaded) if verified else {
        "configured": False,
        "config_effective": False,
        "active_in_runtime": False,
        "note": "磁盘配置与写入草案不一致。",
    }
    verification = {
        "ok": verified,
        "expected_hash": expected_hash,
        "actual_hash": actual_hash,
        "checks": ["config readable", "strategy hash matches", "runtime status inspected"],
    }
    return {
        "ok": verified,
        "message": (
            "策略已写入、重新加载并通过配置校验。"
            if verified else
            "策略写入后校验失败，不能确认已生效。"
        ),
        "parse_result": parsed,
        "write_result": write_result,
        "reload_result": reload_result,
        "effective_status": effective,
        "verification_result": verification,
        "strategy": reloaded,
        "confirmation": parsed.get("confirmation", []),
        "warnings": parsed.get("warnings", []),
        "capabilities": strategy_capabilities(),
    }


def strategy_get():
    cfg = load_watchlist_config()
    return {
        "ok": True,
        "strategy": cfg.get("strategy_monitor", {}),
        "capabilities": strategy_capabilities()
    }


def strategy_list():
    strategy = strategy_get().get("strategy", {}) or {}
    return {
        "ok": True,
        "schema_version": strategy.get("schema_version", "strategy-dsl/v1"),
        "count": len(strategy.get("rules", []) or []),
        "rules": strategy.get("rules", []) or [],
        "targets": strategy.get("targets", []),
        "updated_at": strategy.get("updated_at"),
    }


def strategy_status():
    strategy = strategy_get().get("strategy", {}) or {}
    return {
        "ok": True,
        "strategy_hash": _strategy_hash(strategy),
        "effective_status": _strategy_runtime_status(strategy),
        "strategy": strategy,
    }


def _find_strategy_rule(rule_id):
    strategy = strategy_get().get("strategy", {}) or {}
    for rule in strategy.get("rules", []) or []:
        if str(rule.get("id")) == str(rule_id):
            return strategy, rule
    return strategy, None


def strategy_explain(rule_id):
    strategy, rule = _find_strategy_rule(rule_id)
    if rule is None:
        return {"ok": False, "error": f"未找到策略：{rule_id}"}
    return {
        "ok": True,
        "id": rule.get("id"),
        "side": rule.get("side"),
        "logic": rule.get("logic"),
        "code": rule.get("code"),
        "targets": strategy.get("targets", []),
        "conditions": rule.get("conditions", []),
        "raw_text": rule.get("raw_text"),
        "explanation": _strategy_rule_summary(rule, strategy.get("targets")),
    }


def strategy_delete(rule_id):
    with _strategy_config_lock():
        strategy, rule = _find_strategy_rule(rule_id)
        if rule is None:
            return {"ok": False, "error": f"未找到策略：{rule_id}"}
        remaining = [item for item in strategy.get("rules", []) if item.get("id") != rule.get("id")]
        cfg = load_watchlist_config()
        updated = dict(strategy)
        updated["rules"] = remaining
        updated["raw_text"] = "" if not remaining else updated.get("raw_text", "")
        updated["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cfg["strategy_monitor"] = updated
        save_watchlist_config(cfg)
        reloaded = load_watchlist_config().get("strategy_monitor", {}) or {}
    absent = all(item.get("id") != rule.get("id") for item in reloaded.get("rules", []))
    return {
        "ok": absent,
        "parse_result": {"ok": True, "operation": "delete", "id": rule_id},
        "write_result": {"ok": True, "path": WATCHLIST_PATH},
        "reload_result": {"ok": True, "strategy": reloaded},
        "effective_status": _strategy_runtime_status(reloaded),
        "verification_result": {"ok": absent, "rule_absent": absent},
        "message": "策略已删除并通过二次校验。" if absent else "策略删除校验失败。",
    }


def strategy_clear():
    with _strategy_config_lock():
        cfg = load_watchlist_config()
        cfg["strategy_monitor"] = _default_watchlist_config()["strategy_monitor"]
        save_watchlist_config(cfg)
        reloaded = load_watchlist_config().get("strategy_monitor", {}) or {}
    verified = not bool(reloaded.get("rules"))
    return {
        "ok": verified,
        "parse_result": {"ok": True, "operation": "clear"},
        "write_result": {"ok": True, "path": WATCHLIST_PATH},
        "reload_result": {"ok": True, "strategy": reloaded},
        "effective_status": _strategy_runtime_status(reloaded),
        "verification_result": {"ok": verified, "rules_count": len(reloaded.get("rules", []))},
        "message": "当前策略已清空并通过二次校验。" if verified else "策略清空校验失败。",
    }


def backtest_status():
    from backtest.config import get_status
    return get_status()


def backtest_toggle(enabled):
    from backtest.config import set_enabled
    return set_enabled(enabled)


def backtest_set_strategy(text):
    from backtest.config import set_strategy
    from backtest.engine import parse_strategy
    parsed = parse_strategy(text)
    if not parsed.get("ok"):
        return parsed
    return set_strategy(parsed)


def backtest_show_strategy():
    return backtest_status()


def backtest_clear_strategy():
    from backtest.config import clear_strategy
    return clear_strategy()


def backtest_run_command(symbol, days=60, interval="1d", start=None, end=None):
    from backtest.engine import run_backtest
    return run_backtest(
        symbol=symbol,
        days=days,
        interval=interval,
        start=start,
        end=end
    )


def _parse_natural_backtest_request(text):
    raw = str(text or "").strip()
    code_match = re.search(r"(?<!\d)(\d{6})(?!\d)", raw)
    if not code_match:
        return {
            "ok": False,
            "error": "请在回测指令中提供六位股票或ETF代码，例如：回测510050最近60天。"
        }
    symbols = set(re.findall(r"(?<!\d)(\d{6})(?!\d)", raw))
    if len(symbols) != 1:
        return {"ok": False, "error": "每次回测请只指定一个证券代码。"}
    days_match = re.search(r"(?:最近|周期)\s*(\d+)\s*天", raw)
    if days_match and int(days_match.group(1)) <= 0:
        return {"ok": False, "error": "回测天数必须大于零。"}
    intervals = re.findall(r"(\d+|一|五|十五|三十|六十)\s*分钟", raw)
    intervals = [{"一": "1", "五": "5"}.get(item, item) for item in intervals]
    if any(item not in {"1", "5"} for item in intervals) or len(set(intervals)) > 1:
        return {"ok": False, "error": "回测仅支持 1 分钟、5 分钟或日线；不支持混合周期。"}
    interval = intervals[0] + "m" if intervals else "1d"
    return {
        "ok": True,
        "symbol": code_match.group(1),
        "days": int(days_match.group(1)) if days_match else 60,
        "interval": interval,
    }


def backtest_run_text_command(text):
    from backtest.config import set_strategy
    from backtest.engine import parse_strategy, run_backtest

    request = _parse_natural_backtest_request(text)
    if not request.get("ok"):
        return request
    parsed = parse_strategy(text)
    if not parsed.get("ok"):
        return parsed

    result = run_backtest(
        symbol=request["symbol"],
        days=request["days"],
        interval=request["interval"],
        strategy_override=parsed,
    )
    set_strategy(parsed)
    result["条件识别结果"] = {
        "买入条件": parsed["buy_conditions"],
        "卖出条件": parsed["sell_conditions"],
        "不支持字段": parsed.get("unsupported_fields", []),
    }
    result["strategy_saved"] = True
    result["execution_mode"] = "run-text"
    return result


def _format_backtest_number(value, digits=2):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value if value is not None else "-")
    text = f"{number:.{digits}f}"
    return text.rstrip("0").rstrip(".")


def _format_backtest_condition(condition):
    kind = condition.get("type")
    if kind in {"all", "any"}:
        separator = " 并且 " if kind == "all" else " 或者 "
        return separator.join(
            _format_backtest_condition(item)
            for item in condition.get("conditions", [])
        )
    if kind == "bar_return":
        return f"单日涨幅超过 {_format_backtest_number(condition.get('value'))}%"
    if kind == "volume_vs_average":
        return (
            f"成交量超过过去 {condition.get('lookback')} 日均量的 "
            f"{_format_backtest_number(condition.get('factor'))} 倍"
        )
    if kind == "rolling_high_breakout":
        return f"突破过去 {condition.get('lookback')} 日最高价"
    if kind == "entry_return":
        value = float(condition.get("value", 0))
        if condition.get("operator") in {"<", "<="} or value < 0:
            return f"买入价止损 {_format_backtest_number(abs(value))}%"
        return f"买入价止盈 {_format_backtest_number(abs(value))}%"
    if kind == "price_vs_ma":
        action = "跌破" if condition.get("operator") in {"<", "<="} else "站上"
        return f"{action} MA{condition.get('period')}"
    if kind == "macd_cross":
        return "MACD金叉" if condition.get("direction") == "golden_cross" else "MACD死叉"
    if kind == "kdj_cross":
        return "KDJ金叉" if condition.get("direction") == "golden_cross" else "KDJ死叉"
    if kind == "indicator_threshold":
        direction = "低于" if condition.get("operator") in {"<", "<="} else "高于"
        return (
            f"{str(condition.get('indicator', '')).upper()}{direction}"
            f"{_format_backtest_number(condition.get('value'))}"
        )
    if kind == "price_threshold":
        direction = "低于" if condition.get("operator") in {"<", "<="} else "高于"
        return f"收盘价{direction}{_format_backtest_number(condition.get('value'))}"
    return f"条件 {kind}"


def _format_backtest_condition_lines(conditions):
    return [
        f"- {_format_backtest_condition(condition)}：已识别"
        for condition in conditions or []
    ] or ["- 未提供"]


def _format_backtest_review(result):
    metrics = result.get("metrics", {})
    trades = result.get("trade_summary") or result.get("trades") or []
    total_return = float(metrics.get("total_return_percent", 0) or 0)
    win_rate = float(metrics.get("win_rate", 0) or 0)
    forced_count = sum(1 for trade in trades if trade.get("forced_exit"))
    best = max(trades, key=lambda item: item.get("return_percent", 0), default=None)
    worst = min(trades, key=lambda item: item.get("return_percent", 0), default=None)

    lines = [
        f"策略整体{'赚钱' if total_return > 0 else '亏钱' if total_return < 0 else '持平'}，"
        f"总收益率为 {_format_backtest_number(total_return)}%。",
        (
            f"胜率为 {_format_backtest_number(win_rate)}%，"
            f"处于{'较高' if win_rate >= 60 else '中等' if win_rate >= 40 else '较低'}水平。"
        ),
    ]
    if best and worst:
        lines.append(
            f"最好单笔为 {_format_backtest_number(best.get('return_percent'))}%，"
            f"最差单笔为 {_format_backtest_number(worst.get('return_percent'))}%。"
        )
    else:
        lines.append("本周期没有完成交易，暂时无法评价单笔盈亏来源。")
    lines.append(
        f"共有 {forced_count} 笔在回测结束时强制平仓。"
        if forced_count else
        "本次没有回测结束强制平仓。"
    )
    if result.get("open_position"):
        lines.append("期末仍有未平仓持仓，收益包含浮动估值；胜率只统计已完成交易。")
    return lines


def format_backtest_run_text_result(result, original_text):
    if not isinstance(result, dict):
        return str(result)
    if result.get("error"):
        unsupported = result.get("unsupported_fields") or []
        lines = ["历史回测未执行。", "", "一、策略识别", ""]
        if unsupported:
            lines.extend(
                f"- {field}：历史回测暂不支持"
                for field in unsupported
            )
        lines.extend(["", f"原因：{result['error']}"])
        return "\n".join(lines)

    request = _parse_natural_backtest_request(original_text)
    days = request.get("days", "-") if request.get("ok") else "-"
    recognized = result.get("条件识别结果", {})
    metrics = result.get("metrics", {})
    trades = result.get("trade_summary") or result.get("trades") or []
    lines = [
        f"已完成 {result.get('symbol', '-')} 回测，周期 {days} 天。",
        "",
        "一、策略识别",
        "",
        "买入条件：",
        *_format_backtest_condition_lines(recognized.get("买入条件")),
        "",
        "卖出条件：",
        *_format_backtest_condition_lines(recognized.get("卖出条件")),
        "",
        "二、回测指标",
        "",
        f"- 交易次数：{metrics.get('trade_count', 0)}",
        f"- 胜率：{_format_backtest_number(metrics.get('win_rate'))}%",
        f"- 总收益率：{_format_backtest_number(metrics.get('total_return_percent'))}%",
        f"- 最大回撤：{_format_backtest_number(metrics.get('max_drawdown_percent'))}%",
        f"- 平均单笔收益：{_format_backtest_number(metrics.get('average_trade_return_percent'))}%",
        f"- 最好单笔：{_format_backtest_number(metrics.get('best_trade_percent'))}%",
        f"- 最差单笔：{_format_backtest_number(metrics.get('worst_trade_percent'))}%",
        f"- 平均持仓K线数：{_format_backtest_number(metrics.get('average_holding_bars'))}",
        "",
        "三、交易明细摘要",
        "",
    ]
    if not trades:
        lines.append("本周期没有完成交易。")
    for index, trade in enumerate(trades, 1):
        sell_reason = trade.get("sell_reason") or "-"
        if trade.get("forced_exit"):
            sell_reason = "回测结束强制平仓"
        lines.extend([
            f"{index}. {trade.get('buy_time', '-')} → {trade.get('sell_time', '-')}",
            f"   - 单笔收益：{_format_backtest_number(trade.get('return_percent'))}%",
            f"   - 买入价：{_format_backtest_number(trade.get('buy_price'), 4)}",
            f"   - 卖出价：{_format_backtest_number(trade.get('sell_price'), 4)}",
            f"   - 买入理由：{trade.get('buy_reason') or '-'}",
            f"   - 卖出理由：{sell_reason}",
        ])
    lines.extend([
        "",
        "四、数据来源",
        "",
        f"- 数据源：{result.get('data_source') or '-'}",
        f"- 缓存时间：{result.get('cache_time') or '未使用缓存'}",
        f"- 结果文件：{result.get('result_path') or '-'}",
        "",
        "五、简评",
        "",
        *_format_backtest_review(result),
    ])
    assumptions = result.get("execution_assumptions", {})
    if assumptions:
        lines.extend(["", "六、成交与数据假设", assumptions.get("signal_timing", ""), assumptions.get("settlement", "")])
        lines.extend(f"- {item}" for item in assumptions.get("limitations", []))
    if result.get("cache_stale"):
        lines.append("- 当前使用过期缓存，请先核验数据时间。")
    return "\n".join(lines)


def _backtest_raw_output_requested(text):
    return bool(re.search(r"原始\s*JSON|调试信息", str(text or ""), re.I))


def backtest_run_saved_command(
    symbol,
    days=60,
    interval="1d",
    start=None,
    end=None,
    saved_strategy_confirmed=False,
):
    warning = (
        "backtest run 会使用已保存策略，不适合新的自然语言策略。"
        "新策略请使用 backtest run-text 或 nl。"
    )
    if not saved_strategy_confirmed:
        return {
            "ok": False,
            "error": (
                "未确认执行已保存策略，本次未运行回测。"
                "如需复跑已保存策略，请显式添加 --saved-strategy。"
            ),
            "warning": warning,
            "execution_mode": "saved-strategy-blocked",
        }
    try:
        result = backtest_run_command(
            symbol=symbol,
            days=days,
            interval=interval,
            start=start,
            end=end,
        )
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
            "warning": warning,
            "execution_mode": "saved-strategy",
        }
    result["warning"] = warning
    result["execution_mode"] = "saved-strategy"
    return result


def backtest_chart_command():
    from backtest.chart import render_last_chart
    return render_last_chart()


def backtest_trades_command():
    from backtest.config import load_last_result
    result = load_last_result()
    return {
        "ok": True,
        "result_id": result.get("result_id"),
        "symbol": result.get("symbol"),
        "interval": result.get("interval"),
        "trades": result.get("trades", [])
    }


def _parse_backtest_cli_args(args):
    options = {
        "days": 60,
        "interval": "1d",
        "start": None,
        "end": None,
        "saved_strategy_confirmed": False,
    }
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--days" and index + 1 < len(args):
            options["days"] = int(args[index + 1])
            index += 2
            continue
        if token == "--interval" and index + 1 < len(args):
            options["interval"] = args[index + 1]
            index += 2
            continue
        if token == "--start" and index + 1 < len(args):
            options["start"] = args[index + 1]
            index += 2
            continue
        if token == "--end" and index + 1 < len(args):
            options["end"] = args[index + 1]
            index += 2
            continue
        if token == "--saved-strategy":
            options["saved_strategy_confirmed"] = True
            index += 1
            continue
        raise ValueError(f"无法识别的回测参数：{token}")
    if options["interval"] not in {"1m", "5m", "1d"}:
        raise ValueError("回测 interval 仅支持 1m、5m、1d")
    if options["days"] <= 0:
        raise ValueError("回测天数必须为正整数")
    return options


MODEL_PRIMARY = os.environ.get("LOBSTER_QUANT_PRIMARY_MODEL", "").strip()
MODEL_FALLBACK_CANDIDATES = [
    item.strip()
    for item in os.environ.get("LOBSTER_QUANT_FALLBACK_MODELS", "").split(",")
    if item.strip()
]
MODEL_FALLBACK_LOG_PATH = _state_path("model_fallback.log")


def _load_model_traffic_controller():
    """The optional local controller is the sole owner of traffic-switch state."""
    try:
        return importlib.import_module("model_traffic_control")
    except ModuleNotFoundError as exc:
        if exc.name == "model_traffic_control":
            return None
        raise


def _model_route_snapshot():
    """Read once per request; a broken installed controller must never go legacy."""
    try:
        controller = _load_model_traffic_controller()
        if controller is None:
            return {"ok": True, "installed": False, "mode": "legacy"}
        result = controller.route_for_request()
        if not isinstance(result, dict) or not isinstance(result.get("installed"), bool):
            raise ValueError("invalid controller response")
        if result["installed"] is False and result.get("ok") is True:
            return {"ok": True, "installed": False, "mode": "legacy"}
        route = {key: result.get(key) for key in ("ok", "installed", "mode", "selected_model", "error", "config_path")}
        if route.get("ok") is not True:
            route["ok"] = False
            route["selected_model"] = None
            return route
        # The controller validates policy/provider consistency and owns model ids.
        # Do not duplicate its on/off mapping here; retain only a safe API shape.
        selected = route.get("selected_model")
        if route.get("mode") not in {"on", "off"} or not isinstance(selected, str) or not re.fullmatch(r"[^\s/]+/[^\s]+", selected):
            raise ValueError("invalid selected route")
        if not isinstance(route.get("config_path"), str) or not os.path.isabs(route["config_path"]):
            raise ValueError("missing controlled config path")
        try:
            _model_subprocess_options(route)
        except Exception:
            return {"ok": False, "installed": True, "mode": "blocked", "selected_model": None,
                    "error": "controlled_environment_unavailable"}
        return route
    except Exception:
        return {"ok": False, "installed": True, "mode": "blocked", "selected_model": None,
                "error": "controller_unavailable"}


def _model_subprocess_options(route=None):
    if not route or not route.get("installed"):
        return {}
    # Bind both discovery and inference to the same config the controller checked.
    # A separate environment leaves other requests and the parent process intact.
    adapter = importlib.import_module("model_traffic_adapter")
    return {"env": adapter.controlled_subprocess_env(route["config_path"])}


def _model_route_error(route):
    return {"ok": False, "error_type": "traffic_policy_blocked",
            "message": "模型流量开关状态不可用或与网关配置不一致，本次未发起模型请求；请检查 model codex status。",
            "routing": {key: value for key, value in route.items() if key != "config_path"},
            "attempts": [], "models": []}


def model_codex_control(action="status"):
    action = str(action).strip().lower()
    if action not in {"on", "off", "status"}:
        return {"ok": False, "error": "用法：model codex on|off|status"}
    try:
        controller = _load_model_traffic_controller()
        if controller is None:
            return {"ok": action == "status", "installed": False, "mode": "legacy",
                    "configured_primary_model": MODEL_PRIMARY or None,
                    "configured_fallback_models": list(MODEL_FALLBACK_CANDIDATES),
                    "message": "未安装 Codex 流量控制器；模型请求沿用原有 OpenClaw／插件配置。",
                    "model_request_sent": False}
        result = controller.get_status() if action == "status" else controller.set_mode(action)
        if not isinstance(result, dict) or not isinstance(result.get("installed"), bool):
            raise ValueError("invalid controller response")
        if result["installed"] is False:
            result = {**result, "routing_mode": "legacy",
                      "message": "Codex 流量开关尚未初始化；模型请求沿用原有 OpenClaw／插件配置。"}
        return {**result, "model_request_sent": False}
    except Exception:
        return {"ok": False, "installed": True, "mode": "blocked", "selected_model": None,
                "error": "controller_unavailable", "model_request_sent": False,
                "message": "Codex 流量控制器不可用；未切换到其他模型。"}


def _dispatch_model_command(args):
    action = str(args[0]).strip().lower() if args else "status"
    if action == "status" and len(args) <= 1:
        return model_codex_control("status")
    if action == "codex" and len(args) == 2:
        return model_codex_control(args[1])
    if action == "ping" and len(args) == 1:
        return model_ping()
    if action in {"ask", "run"}:
        prompt = " ".join(args[1:]).strip()
        return model_call_with_fallback(prompt) if prompt else {"ok": False, "error": "用法：model ask \"需要模型处理的文本\""}
    return {"ok": False, "error": "用法：model [status|codex on|codex off|codex status|ping|ask 文本]"}


def _classify_model_error(text):
    lowered = str(text or "").lower()
    patterns = [
        ("billing_error", ("billing error", "insufficient balance", "额度", "计费")),
        ("no_available_channel", ("no available channel", "无可用通道")),
        ("forbidden", ("403", "无权访问", "not allowed")),
        ("rate_limit", ("429", "rate limit")),
        ("timeout", ("timeout", "timed out", "超时")),
        ("connection_reset", ("connection reset", "remote disconnected", "connection aborted")),
        ("bad_gateway", ("502", "503", "504")),
        ("server_error", ("500",)),
        ("empty_response", ("no response generated", "empty response", "空响应")),
        ("session_conflict", ("sessiontakeover", "session file changed")),
        ("unsupported_cli", ("unknown command", "unknown option")),
    ]
    for error_type, needles in patterns:
        if any(needle in lowered for needle in needles):
            return error_type
    return "unknown_error"


def _model_request_id(text):
    matches = re.findall(
        r"(?:request[\s_-]*id\s*[:：]\s*|\b)([A-Za-z0-9:_-]{12,})",
        str(text or ""),
        re.IGNORECASE
    )
    return matches[-1] if matches else None


def _model_error_summary(error_type):
    return {
        "billing_error": "模型通道返回额度或计费异常",
        "no_available_channel": "当前模型没有可用上游通道",
        "forbidden": "模型通道拒绝访问或当前分组无权限",
        "rate_limit": "模型通道触发限流",
        "timeout": "模型请求超时",
        "connection_reset": "模型连接被重置",
        "bad_gateway": "模型上游网关暂时不可用",
        "server_error": "模型上游服务异常",
        "empty_response": "模型未生成有效响应",
        "session_conflict": "模型探测会话发生冲突",
        "json_parse_error": "模型响应格式无法解析",
        "not_configured": "模型未配置或不在允许列表",
        "unsupported_cli": "当前 OpenClaw 不支持 infer model run；请升级后使用辅助模型功能，其他研究功能不受影响",
        "discovery_failed": "无法读取当前 OpenClaw 模型列表；请检查 Gateway 状态后重试",
        "cli_unavailable": "无法启动 OpenClaw 命令，请检查安装和 PATH",
        "unknown_error": "模型调用失败",
    }.get(error_type, "模型调用失败")


def _configured_model_metadata(timeout_seconds=20, route=None):
    try:
        result = subprocess.run(
            ["openclaw", "models", "list", "--json"],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
            **_model_subprocess_options(route),
        )
        if result.returncode != 0:
            return {"keys": set(), "default": None, "error_type": "discovery_failed"}
        payload = json.loads(result.stdout or "{}")
        items = payload.get("models", [])
        if not isinstance(items, list):
            raise ValueError("invalid model list")
        available = [item for item in items if isinstance(item, dict)
                     and item.get("available") is True and isinstance(item.get("key"), str)]
        default = next((item["key"] for item in available if "default" in (item.get("tags") or [])), None)
        return {"keys": {item["key"] for item in available}, "default": default, "error_type": None}
    except subprocess.TimeoutExpired:
        return {"keys": set(), "default": None, "error_type": "timeout"}
    except Exception:
        return {"keys": set(), "default": None, "error_type": "discovery_failed"}


def _configured_model_keys():
    return _configured_model_metadata()["keys"]


def _model_candidates(metadata, route=None):
    route = _model_route_snapshot() if route is None else route
    if not route.get("ok"):
        return []
    if route.get("installed"):
        return [route["selected_model"]]
    primary = MODEL_PRIMARY or metadata.get("default")
    return list(dict.fromkeys(item for item in [primary] + MODEL_FALLBACK_CANDIDATES[:2] if item))


def _model_budget(timeout_seconds):
    budget = max(1.0, float(timeout_seconds))
    try:
        configured = float(os.environ.get("LOBSTER_QUANT_MODEL_TIMEOUT_SECONDS", "inf"))
        if configured > 0:
            budget = min(budget, configured)
    except ValueError:
        pass
    return budget


def _run_model_once(model, prompt="只回复pong", timeout_seconds=45, route=None):
    started = time.monotonic()
    controlled = bool(route and route.get("installed"))
    if controlled and model != route.get("selected_model"):
        return {"ok": False, "model": model, "latency_seconds": 0,
                "error_type": "traffic_policy_blocked", "request_id": None,
                "provider_summary": "请求模型与受控路由不一致，未发起请求。"}
    # One-shot inference has no agent tools, workspace bootstrap or delivery.
    # Older hosts fail explicitly; never retry via a full tool-enabled agent.
    command = [
        "openclaw", "infer", "model", "run", "--local",
        "--model", model,
        "--prompt", prompt,
        "--json"
    ]
    try:
        options = _model_subprocess_options(route)
    except Exception:
        return {"ok": False, "model": model, "latency_seconds": 0,
                "error_type": "traffic_policy_blocked", "request_id": None,
                "provider_summary": "受控模型执行环境不一致，未发起请求。"}
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
            **options,
        )
    except subprocess.TimeoutExpired as exc:
        latency = round(time.monotonic() - started, 3)
        return {
            "ok": False,
            "model": model,
            "latency_seconds": latency,
            "error_type": "timeout",
            "request_id": None,
            "provider_summary": "模型请求超时"
        }
    except OSError:
        return {"ok": False, "model": model,
                "latency_seconds": round(time.monotonic() - started, 3),
                "error_type": "cli_unavailable", "request_id": None,
                "provider_summary": _model_error_summary("cli_unavailable")}

    latency = round(time.monotonic() - started, 3)
    combined = "\n".join(x for x in (result.stdout, result.stderr) if x).strip()
    if result.returncode != 0:
        error_type = _classify_model_error(combined)
        return {
            "ok": False,
            "model": model,
            "latency_seconds": latency,
            "error_type": error_type,
            "request_id": _model_request_id(combined),
            "provider_summary": _model_error_summary(error_type)
        }

    try:
        payload = json.loads(result.stdout or "{}")
        evidence = {}
        if controlled:
            # OpenClaw local model.run reports the resolved provider and bare id,
            # after alias resolution. A requested --model alone is not evidence.
            provider = payload.get("provider") if isinstance(payload, dict) else None
            resolved_id = payload.get("model") if isinstance(payload, dict) else None
            expected_provider, expected_id = model.split("/", 1)
            complete = isinstance(provider, str) and bool(provider) and isinstance(resolved_id, str) and bool(resolved_id)
            if not complete or provider != expected_provider or resolved_id != expected_id:
                return {"ok": False, "model": model, "latency_seconds": latency,
                        "error_type": "model_routing_mismatch" if complete else "model_routing_unverified",
                        "request_id": None,
                        "provider_summary": "模型返回的路由证据缺失或与选定模型不一致；结果未采用，未尝试其他模型。"}
            evidence = {"routing_verified": True, "resolved_model": provider + "/" + resolved_id}
        texts = [
            str(item.get("text", "")).strip()
            for item in payload.get("outputs", [])
            if isinstance(item, dict) and str(item.get("text", "")).strip()
        ]
        if payload.get("ok") is not True or not texts:
            raise ValueError("empty response")
        return {
            "ok": True,
            "model": model,
            "latency_seconds": latency,
            "error_type": None,
            "request_id": None,
            "provider_summary": f"{payload.get('provider', 'unknown')} 返回成功",
            "text": "\n".join(texts),
            **evidence,
        }
    except Exception as exc:
        return {
            "ok": False,
            "model": model,
            "latency_seconds": latency,
            "error_type": "empty_response" if "empty" in str(exc).lower() else "json_parse_error",
            "request_id": _model_request_id(combined),
            "provider_summary": f"模型返回无法解析：{str(exc)[:120]}"
        }


def _write_model_fallback_log(event):
    try:
        os.makedirs(os.path.dirname(MODEL_FALLBACK_LOG_PATH), exist_ok=True)
        row = dict(event)
        row["time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(MODEL_FALLBACK_LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def model_call_with_fallback(prompt, timeout_seconds=90):
    started = time.monotonic()
    deadline = started + _model_budget(timeout_seconds)
    route = _model_route_snapshot()
    if not route.get("ok"):
        return _model_route_error(route)
    discovery_timeout = min(20, max(0.1, deadline - time.monotonic()))
    metadata = (_configured_model_metadata(discovery_timeout, route=route) if route.get("installed")
                else _configured_model_metadata(discovery_timeout))
    if metadata.get("error_type"):
        return {"ok": False, "message": _model_error_summary(metadata["error_type"]),
                "error_type": metadata["error_type"], "attempts": [],
                "latency_seconds": round(time.monotonic() - started, 3)}
    allowed = metadata["keys"]
    attempts = []
    candidates = _model_candidates(metadata, route)
    if not candidates:
        return {"ok": False, "message": "没有可用默认模型；请检查 OpenClaw 默认模型或显式配置 primaryModel。", "attempts": []}
    for index, model in enumerate(candidates):
        if model not in allowed:
            attempt = {
                "ok": False,
                "model": model,
                "latency_seconds": 0,
                "error_type": "not_configured",
                "request_id": None,
                "provider_summary": "模型未配置或不在允许列表"
            }
        else:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                attempts.append({"ok": False, "model": model, "latency_seconds": 0,
                                 "error_type": "timeout", "request_id": None,
                                 "provider_summary": "模型请求总时间预算已耗尽"})
                break
            attempt = (_run_model_once(model, prompt, remaining, route=route) if route.get("installed")
                       else _run_model_once(model, prompt, remaining))
        attempts.append(attempt)
        if attempt.get("ok"):
            if index > 0:
                _write_model_fallback_log({
                    "event": "fallback_model_used",
                    "primary_model": candidates[0],
                    "fallback_model": model,
                    "error_type": attempts[0].get("error_type"),
                    "request_id": attempts[0].get("request_id")
                })
            return {
                "ok": True,
                "model": model,
                "fallback_used": index > 0,
                "text": attempt.get("text"),
                "attempts": attempts,
                "latency_seconds": round(time.monotonic() - started, 3),
            }
        if index == 0:
            _write_model_fallback_log({
                "event": "primary_model_failed",
                "primary_model": model,
                "error_type": attempt.get("error_type"),
                "request_id": attempt.get("request_id")
            })
        if attempt.get("error_type") in {"unsupported_cli", "cli_unavailable", "traffic_policy_blocked", "model_routing_mismatch", "model_routing_unverified"}:
            return {"ok": False, "message": attempt["provider_summary"],
                    "attempts": attempts, "latency_seconds": round(time.monotonic() - started, 3)}

    billing = any(item.get("error_type") == "billing_error" for item in attempts)
    timed_out = time.monotonic() >= deadline
    message = (
        "模型请求总时间预算已耗尽；未继续启动其他模型。请稍后重试或检查上游响应速度。"
        if timed_out else
        "模型通道返回额度/计费异常，但不一定是账户总余额不足，可能是当前模型通道临时不可用。"
        "请检查所用模型通道。"
        if billing else
        "模型服务暂时不可用，配置的模型本次未成功响应。"
        "可能是上游通道、额度或网络波动，请稍后重试。"
    )
    return {"ok": False, "message": message, "attempts": attempts,
            "latency_seconds": round(time.monotonic() - started, 3)}


def model_ping():
    started = time.monotonic()
    deadline = started + _model_budget(90)
    route = _model_route_snapshot()
    if not route.get("ok"):
        return _model_route_error(route)
    discovery_timeout = min(20, max(0.1, deadline - time.monotonic()))
    metadata = (_configured_model_metadata(discovery_timeout, route=route) if route.get("installed")
                else _configured_model_metadata(discovery_timeout))
    if metadata.get("error_type"):
        return {"ok": False, "message": _model_error_summary(metadata["error_type"]), "models": []}
    allowed = metadata["keys"]
    candidates = _model_candidates(metadata, route)
    results = []
    for model in candidates:
        if model not in allowed:
            results.append({
                "ok": False,
                "model": model,
                "latency_seconds": 0,
                "error_type": "not_configured",
                "request_id": None,
                "provider_summary": "模型未配置或不在允许列表"
            })
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            results.append({"ok": False, "model": model, "latency_seconds": 0,
                            "error_type": "timeout", "request_id": None,
                            "provider_summary": "总时间预算已耗尽，此模型未开始探测"})
            break
        result = (_run_model_once(model, "只回复pong", min(45, remaining), route=route) if route.get("installed")
                  else _run_model_once(model, "只回复pong", min(45, remaining)))
        results.append(result)
        if result.get("error_type") in {"unsupported_cli", "cli_unavailable", "traffic_policy_blocked", "model_routing_mismatch", "model_routing_unverified"}:
            break
    return {
        "ok": any(item.get("ok") for item in results),
        "primary_model": candidates[0] if candidates else None,
        "fallback_candidates": candidates[1:],
        "gateway_fallback_config_changed": False,
        "scope_note": "只探测脚本侧配置的模型；没有修改 OpenClaw 默认模型或全局 fallback。",
        "latency_seconds": round(time.monotonic() - started, 3),
        "models": results
    }


def print_json(data):
    print(json.dumps(data, ensure_ascii=False, indent=2))


def get_sina_code(symbol):
    symbol = str(symbol).strip().lower()
    if symbol.startswith(("sh", "sz")):
        return symbol
    if symbol.startswith(("5", "6", "9")):
        return "sh" + symbol
    return "sz" + symbol


def get_secid(symbol):
    symbol = str(symbol).strip()
    if symbol.startswith(("5", "6", "9")):
        return f"1.{symbol}"
    return f"0.{symbol}"


def parse_sina_quote_text(text, symbol):
    if '="' not in text:
        return None
    content = text.split('="', 1)[1].rsplit('";', 1)[0]
    parts = content.split(",")
    if len(parts) < 32 or not parts[0]:
        return None

    def to_float(v):
        try:
            number = float(v) if v not in (None, "") else None
            return number if number is not None and math.isfinite(number) else None
        except Exception:
            return None

    latest = to_float(parts[3])
    prev_close = to_float(parts[2])
    high = to_float(parts[4])
    low = to_float(parts[5])
    volume_shares = to_float(parts[8])
    amount_yuan = to_float(parts[9])
    change = None
    pct = None
    if latest is not None and prev_close not in (None, 0):
        change = round(latest - prev_close, 4)
        pct = round((latest - prev_close) / prev_close * 100, 4)

    amplitude = None
    if high is not None and low is not None and prev_close not in (None, 0):
        amplitude = round((high - low) / prev_close * 100, 4)

    bids = []
    asks = []
    for level in range(5):
        bid_volume_index = 10 + level * 2
        bid_price_index = 11 + level * 2
        ask_volume_index = 20 + level * 2
        ask_price_index = 21 + level * 2
        bids.append({
            "level": level + 1,
            "price": to_float(parts[bid_price_index]),
            "volume": to_float(parts[bid_volume_index])
        })
        asks.append({
            "level": level + 1,
            "price": to_float(parts[ask_price_index]),
            "volume": to_float(parts[ask_volume_index])
        })

    order_book_complete = all(
        x["volume"] is not None
        for x in bids + asks
    )
    bid_volume = sum(x["volume"] or 0 for x in bids)
    ask_volume = sum(x["volume"] or 0 for x in asks)
    total_order_volume = bid_volume + ask_volume
    order_difference = (
        bid_volume - ask_volume
        if order_book_complete and total_order_volume > 0
        else None
    )
    order_imbalance = None
    if order_book_complete and total_order_volume > 0:
        order_imbalance = round(
            order_difference / total_order_volume * 100,
            4
        )

    quote_time = (
        f"{parts[30] if len(parts) > 30 else ''} "
        f"{parts[31] if len(parts) > 31 else ''}"
    ).strip()

    return {
        "代码": symbol,
        "名称": parts[0],
        "最新价": latest,
        "涨跌幅%": pct,
        "涨跌额": change,
        "今开": to_float(parts[1]),
        "最高": high,
        "最低": low,
        "昨收": prev_close,
        "振幅%": amplitude,
        "成交量": volume_shares,
        "成交量_股": volume_shares,
        "成交量_手": round(volume_shares / 100, 4) if volume_shares is not None else None,
        "成交量单位": "股",
        "成交额": amount_yuan,
        "成交额_元": amount_yuan,
        "成交额_万元": round(amount_yuan / 10000, 4) if amount_yuan is not None else None,
        "成交额_亿元": round(amount_yuan / 100000000, 4) if amount_yuan is not None else None,
        "成交额单位": "元",
        "买一到买五": bids,
        "卖一到卖五": asks,
        "五档买盘量": bid_volume,
        "五档卖盘量": ask_volume,
        "五档委差": order_difference,
        "五档委差_股": order_difference,
        "五档委差_手": round(order_difference / 100, 4) if order_difference is not None else None,
        "五档委差单位": "股",
        "盘口委比%": order_imbalance,
        "买卖盘强弱比": round(bid_volume / ask_volume, 4) if order_book_complete and ask_volume > 0 else None,
        "数据源": "sina",
        "数据来源": "新浪实时行情",
        "数据时间": quote_time,
        "数据获取时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "时间": quote_time,
    }


def quote_sina(symbol):
    code = get_sina_code(symbol)
    url = f"https://hq.sinajs.cn/list={code}"
    r = requests.get(url, headers=HEADERS, timeout=15)
    r.encoding = "gbk"
    r.raise_for_status()
    result = parse_sina_quote_text(r.text, symbol)
    if not result:
        raise RuntimeError("sina_parse_error")
    return result


def index_sina():
    items = [
        ("sh000001", "000001", "上证指数"),
        ("sz399001", "399001", "深证成指"),
        ("sz399006", "399006", "创业板指"),
        ("sh000688", "000688", "科创50"),
        ("sh000016", "000016", "上证50"),
        ("sh510050", "510050", "华夏上证50ETF"),
    ]
    out = []
    for sina_code, symbol, name in items:
        row = quote_sina_by_code(sina_code, symbol, name)
        out.append(row)
    return out


def quote_sina_by_code(sina_code, symbol, name=None):
    url = f"https://hq.sinajs.cn/list={sina_code}"
    r = requests.get(url, headers=HEADERS, timeout=15)
    r.encoding = "gbk"
    r.raise_for_status()
    result = parse_sina_quote_text(r.text, symbol)
    if not result:
        raise RuntimeError("sina_parse_error")
    if name:
        result["名称"] = name
    result["新浪代码"] = sina_code
    return result


def quote_us(symbol):
    """Fetch a delayed US-market snapshot with an actual prior-session reference."""
    import math
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    ticker = str(symbol).strip().upper()
    if not re.fullmatch(r"[A-Z^][A-Z0-9.^-]{0,14}", ticker):
        raise ValueError("invalid_us_symbol")
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
    response = requests.get(
        url,
        params={"interval": "1d", "range": "5d"},
        headers={"User-Agent": HEADERS["User-Agent"]},
        timeout=15,
    )
    response.raise_for_status()
    payload = response.json()
    result = ((payload.get("chart") or {}).get("result") or [None])[0]
    if not isinstance(result, dict):
        raise RuntimeError("us_quote_unavailable")
    meta = result.get("meta") or {}

    def finite(value):
        try:
            number = float(value)
            return number if math.isfinite(number) else None
        except (TypeError, ValueError):
            return None

    price = finite(meta.get("regularMarketPrice"))
    market_time = finite(meta.get("regularMarketTime"))
    previous = finite(meta.get("previousClose"))
    previous_source = "meta.previousClose" if previous is not None else None
    # chartPreviousClose belongs to the requested five-day range, not necessarily
    # the previous session. Derive the last earlier trading date when necessary.
    if previous is None and market_time is not None:
        try:
            market_zone = ZoneInfo(meta.get("exchangeTimezoneName") or "America/New_York")
            session_date = datetime.fromtimestamp(market_time, market_zone).date()
            quote_rows = (result.get("indicators") or {}).get("quote") or [{}]
            closes = quote_rows[0].get("close") or []
            candidates = []
            for timestamp, close in zip(result.get("timestamp") or [], closes):
                timestamp, close = finite(timestamp), finite(close)
                if timestamp is not None and close is not None and datetime.fromtimestamp(timestamp, market_zone).date() < session_date:
                    candidates.append((timestamp, close))
            if candidates:
                previous = max(candidates)[1]
                previous_source = "previous completed daily bar"
        except (ValueError, TypeError, OverflowError, KeyError):
            pass
    change = price - previous if price is not None and previous is not None and previous > 0 else None
    percent = change / previous * 100 if change is not None else None
    try:
        quote_time = datetime.fromtimestamp(market_time, timezone.utc).isoformat() if market_time is not None else None
    except (ValueError, OverflowError, OSError):
        quote_time = None
    return {
        "代码": ticker,
        "名称": meta.get("shortName") or meta.get("longName") or ticker,
        "最新价": price,
        "昨收": previous,
        "昨收来源": previous_source,
        "涨跌额": round(change, 4) if change is not None else None,
        "涨跌幅%": round(percent, 4) if percent is not None else None,
        "币种": meta.get("currency") or "USD",
        "交易所": meta.get("exchangeName"),
        "市场状态": meta.get("marketState"),
        "数据源": "Yahoo Finance public chart endpoint",
        "数据说明": "可能延迟，仅供研究参考；缺少可核验前收盘时不计算日涨跌幅",
        "数据时间": quote_time,
        "数据获取时间": datetime.now(timezone.utc).isoformat(),
    }



def us_index():
    return [quote_us(symbol) for symbol in ("^GSPC", "^DJI", "^IXIC")]


def quote(symbol):
    try:
        value = str(symbol).strip()
        result = (
            quote_us(value)
            if re.fullmatch(r"[A-Za-z^][A-Za-z0-9.^-]{0,14}", value)
            else _quote_with_realtime_metrics(value)
        )
        print_json(result)
    except Exception:
        print_json({"error": "暂缺（行情接口不可用）"})


def index(_symbol=None):
    try:
        scope = str(_symbol or "").strip().lower()
        print_json(us_index() if scope in {"us", "usa", "美股"} else index_sina())
    except Exception:
        print_json({"error": "暂缺（指数接口不可用）"})


def _is_index_or_etf(symbol):
    s = str(symbol).strip().lower()
    prefix = s[:2] if s.startswith(("sh", "sz")) else ""
    raw = s[2:] if prefix else s
    sh_indices = {"000001", "000688", "000016", "000300", "000905"}
    sz_indices = {"399001", "399006"}
    etf_codes = {"510050", "159915", "512000", "510300", "510500"}
    index_names = {"上证指数", "深证成指", "创业板指", "科创50", "上证50", "沪深300", "中证500"}
    etf_names = {"上证50etf", "沪深300etf", "创业板etf", "中证500etf"}
    return (prefix == "sh" and raw in sh_indices) or (prefix != "sh" and raw in sz_indices) or raw in etf_codes or s in index_names or s in etf_names


def lhb(date=None, symbol=None, emit=True):
    """
    龙虎榜查询：
    - 指数/ETF：不适用
    - 指定个股：查最近龙虎榜记录
    - 未指定个股：查指定日期或最近交易日龙虎榜概览
    """
    today = datetime.now().strftime("%Y%m%d")

    if symbol:
        symbol = str(symbol).strip()

    if symbol and _is_index_or_etf(symbol):
        result = {"error": "不适用（指数/ETF无龙虎榜）"}
        if emit:
            print_json(result)
        return result

    if symbol:
        symbol = re.sub(r"^(?:sh|sz)", "", symbol.lower())
        if not re.fullmatch(r"[0-9]{6}", symbol):
            result = {"error": "龙虎榜个股查询需要六位证券代码。"}
            if emit:
                print_json(result)
            return result
    try:
        import akshare as ak
    except ImportError:
        result = {"error": "暂缺（龙虎榜数据依赖 akshare 未安装）"}
        if emit:
            print_json(result)
        return result

    def _clean_row(row):
        d = {}
        for k, v in row.items():
            try:
                if hasattr(v, "item"):
                    v = v.item()
            except Exception:
                pass

            # pandas / python 日期对象转字符串，避免 JSON 序列化失败
            try:
                if hasattr(v, "strftime"):
                    v = v.strftime("%Y-%m-%d")
            except Exception:
                pass

            if str(v) == "nan":
                v = None

            # 兜底：如果不是基础 JSON 类型，就转字符串
            if v is not None and not isinstance(v, (str, int, float, bool, list, dict)):
                v = str(v)

            d[str(k)] = v
        return d

    def _find_code_col(df):
        for c in ["代码", "股票代码", "证券代码", "代码名称", "股票简称"]:
            if c in df.columns:
                return c
        return None

    # 指定日期时只查询该日；未指定日期才查询最近 60 天。
    if symbol:
        try:
            end_date = _normalize_report_date(date) if date else today
            start_date = end_date if date else (datetime.now() - timedelta(days=60)).strftime("%Y%m%d")
            df = ak.stock_lhb_detail_em(start_date=start_date, end_date=end_date)
        except Exception as e:
            result = {"error": "暂缺（东方财富接口不可用）", "details": str(e)}
            if emit:
                print_json(result)
            return result

        try:
            if df is None or df.empty:
                result = {"symbol": symbol, "error": "暂无近期龙虎榜数据"}
                if emit:
                    print_json(result)
                return result

            code_col = _find_code_col(df)
            if code_col:
                mask = df[code_col].astype(str).str.replace(r"\.0$", "", regex=True).str.zfill(6).eq(symbol)
                sub = df[mask].copy()
            else:
                # 找不到代码列时，退化为全文匹配
                sub = df[df.astype(str).apply(lambda r: r.str.contains(symbol, na=False).any(), axis=1)].copy()

            if sub.empty:
                result = {"symbol": symbol, "error": "暂无近期龙虎榜数据", "range": f"{start_date}-{end_date}"}
                if emit:
                    print_json(result)
                return result

            # 尽量按日期倒序
            for date_col in ["上榜日", "交易日期", "日期"]:
                if date_col in sub.columns:
                    sub = sub.sort_values(date_col, ascending=False)
                    break

            records = [_clean_row(r) for _, r in sub.iterrows()]
            result = {
                "symbol": symbol,
                "date": end_date if date else None,
                "mode": "指定日期龙虎榜" if date else "最近60日龙虎榜（非单日）",
                "range": f"{start_date}-{end_date}",
                "rows": int(len(sub)),
                "records": records,
                "数据源": "akshare.stock_lhb_detail_em / 东方财富"
            }
            if emit:
                print_json(result)
            return result
        except Exception as e:
            result = {"error": "暂缺（东方财富接口不可用）", "details": str(e)}
            if emit:
                print_json(result)
            return result

    # 未指定个股：自动查最近一个有龙虎榜数据的交易日
    query_dates = []
    if date:
        query_dates = [_normalize_report_date(date)]
    else:
        # 从今天往前倒查最多 10 天，自动跳过周末/无数据日
        for i in range(0, 11):
            query_dates.append((datetime.now() - timedelta(days=i)).strftime("%Y%m%d"))

    last_error = None
    for query_date in query_dates:
        try:
            df = ak.stock_lhb_detail_em(start_date=query_date, end_date=query_date)
        except Exception as e:
            last_error = str(e)
            continue

        try:
            if df is None or getattr(df, "empty", True):
                continue

            records = []
            for _, r in df.iterrows():
                try:
                    records.append(_clean_row(r))
                except Exception:
                    continue

            result = {
                "date": query_date,
                "mode": "最近有数据交易日龙虎榜" if not date else "指定日期龙虎榜",
                "rows": int(len(df)),
                "records": records if records else [],
                "数据源": "akshare.stock_lhb_detail_em / 东方财富"
            }
            if emit:
                print_json(result)
            return result
        except Exception as e:
            last_error = str(e)
            continue

    result = {
        "date_range_checked": query_dates,
        "error": "暂无近期龙虎榜数据"
    }
    if last_error:
        result["last_error"] = last_error
    if emit:
        print_json(result)
    return result


def _load_news_stock_map():
    import json as _json
    from pathlib import Path as _Path
    path = _Path(__file__).with_name("news_stock_map.json")
    try:
        if not path.exists():
            return {}
        return _json.loads(path.read_text())
    except Exception:
        return {}


def _fetch_json_or_text(url, timeout=10, headers=None):
    import http_client as _requests
    try:
        r = _requests.get(url, timeout=timeout, headers=headers or HEADERS)
        r.raise_for_status()
        return r.text
    except Exception:
        return None


def _extract_headlines_from_text(text, limit=5):
    import re
    if not text:
        return []
    patterns = [
        r'>([^<]{8,120}?(?:新闻|市场|政策|财经|A股|美股|港股|科技|半导体|芯片|黄金|油价|汇率|债券|降息|涨停|跌停)[^<]{0,40}?)<',
        r'"title"\s*:\s*"([^"]{8,120})"',
        r"'title'\s*:\s*'([^']{8,120})'",
    ]
    out = []
    for pat in patterns:
        for m in re.finditer(pat, text, re.I):
            title = m.group(1).strip()
            if 8 <= len(title) <= 120 and title not in out:
                out.append(title)
            if len(out) >= limit:
                return out[:limit]
    return out[:limit]


def news(limit=5):
    """
    盘前新闻标题抓取：优先使用财联社网页标题。
    新浪源保留备用，但新浪首页常见编码/动态结构问题，失败不影响报告。
    """
    sources = [
        "https://www.cls.cn/",
        "https://finance.sina.com.cn/",
        "https://news.sina.com.cn/",
    ]
    bad_titles = {
        "财联社-主流财经新闻集团和财经通讯社-cls.cn",
    }
    for url in sources:
        try:
            text = _fetch_json_or_text(
                url,
                timeout=8,
                headers={"User-Agent": HEADERS["User-Agent"], "Referer": url}
            )
            titles = _extract_headlines_from_text(text, limit=limit * 3)
            cleaned = []
            for t in titles:
                t = str(t).replace("&amp;", "&").strip()
                if not t or t in bad_titles:
                    continue
                if "财联社-主流财经新闻集团" in t:
                    continue
                if t not in cleaned:
                    cleaned.append(t)
                if len(cleaned) >= limit:
                    break
            if cleaned:
                return cleaned
        except Exception:
            continue
    return []


def _match_news_to_stocks(headlines):
    mapping = _load_news_stock_map()
    results = []
    if not headlines:
        return results
    for title in headlines:
        hit_topics = []
        hit_stocks = []
        title_l = title.lower()
        for topic, item in mapping.items():
            keywords = item.get("keywords", []) if isinstance(item, dict) else []
            stocks = item.get("stocks", []) if isinstance(item, dict) else []
            if any(k.lower() in title_l for k in keywords):
                hit_topics.append(topic)
                hit_stocks.extend(stocks)
        results.append({
            "新闻标题": title,
            "命中的题材": hit_topics if hit_topics else "暂无数据",
            "关联个股/ETF": list(dict.fromkeys(hit_stocks)) if hit_stocks else "暂无数据",
        })
    return results


def morning_report(limit=8, emit=True):
    """
    盘前播报：昨收后到开盘前的新闻热点 + 题材映射 + 相关个股。
    指数只作为背景，不作为主体。
    """
    try:
        headlines = news(limit=limit)
    except Exception:
        headlines = []

    try:
        matched_news = _match_news_to_stocks(headlines) if headlines else []
    except Exception:
        matched_news = []

    if not headlines:
        news_section = "暂缺（新闻接口不可用）"
    elif matched_news:
        news_section = matched_news
    else:
        news_section = [
            {
                "新闻标题": h,
                "命中题材": [],
                "关联个股/ETF": "暂无数据"
            }
            for h in headlines
        ]

    # 观察池 / 持仓池：盘前预案
    try:
        wl_cfg = load_watchlist_config()
    except Exception as e:
        wl_cfg = {
            "watch_pool": [],
            "holding_pool": [],
            "monitor": {"enabled": False},
            "_error": str(e)
        }

    watch_items = wl_cfg.get("watch_pool", []) or []
    holding_items = wl_cfg.get("holding_pool", []) or []

    watch_plan = []
    for item in watch_items:
        code = item.get("code") if isinstance(item, dict) else str(item)
        name = item.get("name", code) if isinstance(item, dict) else code
        note = item.get("note", "") if isinstance(item, dict) else ""
        watch_plan.append({
            "股票": f"{name} {code}",
            "备注": note,
            "盘前看点": "观察是否与盘前新闻、政策或当日主线形成共振。",
            "触发条件": [
                "高开后不快速回落，且板块同步走强",
                "放量突破或维持强势横盘",
                "所属题材进入盘前热点或开盘资金流入前列"
            ],
            "处理建议": "观察池不直接追高，优先等待板块确认和个股承接。"
        })

    holding_plan = []
    for item in holding_items:
        code = item.get("code") if isinstance(item, dict) else str(item)
        name = item.get("name", code) if isinstance(item, dict) else code
        note = item.get("note", "") if isinstance(item, dict) else ""
        plan = {
            "股票": f"{name} {code}",
            "备注": note,
            "盘前看点": "重点观察开盘承接、所属板块强弱，以及是否出现放量下跌或冲高回落。",
            "高开预案": "若高开后放量承接良好，可继续观察；若高开快速回落，注意兑现压力。",
            "平开预案": "重点看前30分钟方向选择，若强于板块可继续持有观察。",
            "低开预案": "若低开后快速拉回且板块不弱，属于正常分歧；若低开低走并放量，风险升高。",
            "处理建议": "持仓池以风险控制优先，重点看是否弱于板块、弱于指数或跌破关键位。"
        }
        for k in ("cost", "position", "stop_loss", "target"):
            if isinstance(item, dict) and k in item:
                plan[k] = item.get(k)
        holding_plan.append(plan)

    monitor = wl_cfg.get("monitor", {}) or {}
    monitor_section = {
        "enabled": bool(monitor.get("enabled", False)),
        "状态": "开启" if monitor.get("enabled", False) else "关闭",
        "刷新频率_秒": monitor.get("interval_seconds", 60),
        "盯盘范围": monitor.get("targets", ["watch_pool", "holding_pool"]),
        "提醒渠道": monitor.get("notify_channel", "telegram"),
        "说明": "实时盯盘默认关闭；可用 monitor on 开启，monitor off 关闭。"
    }

    result = {
        "report_type": "morning_report",
        "date": _normalize_report_date(),
        "title": "盘前播报",
        "说明": "盘前播报以昨收后到开盘前的新闻、政策、海外市场和产业消息为主，指数只作背景。",
        "sections": {
            "今日盘前核心结论": "关注隔夜消息、政策变化、海外市场与题材联动；重点观察新闻驱动方向的持续性，避免高开兑现。",
            "隔夜/盘前重要消息": news_section,
            "热点题材方向": _extract_hot_topics_from_matches(matched_news),
            "关联A股个股/ETF": _extract_related_stocks_from_matches(matched_news),
            "观察池盘前预案": watch_plan if watch_plan else "暂无数据（可用 watch add 代码 名称 添加）",
            "持仓池盘前预案": holding_plan if holding_plan else "暂无数据（可用 hold add 代码 名称 添加）",
            "实时盯盘状态": monitor_section,
            "需要回避的风险": [
                "消息落空",
                "高开低走",
                "题材一日游",
                "外围市场波动"
            ],
            "今日观察重点": [
                "新闻是否带来真实资金承接",
                "题材是否有板块联动",
                "高开后是否兑现",
                "指数环境是否配合",
                "观察池是否出现主线共振",
                "持仓池是否弱于所属板块或指数"
            ]
        }
    }
    if emit:
        print_json(result)
    return result


def _extract_hot_topics_from_matches(matches):
    topics = []
    if not isinstance(matches, list):
        return "暂无数据"
    for item in matches:
        for key in ("命中题材", "命中的题材", "topics", "题材"):
            vals = item.get(key) if isinstance(item, dict) else None
            if isinstance(vals, list):
                for v in vals:
                    if v and v != "暂无数据" and v not in topics:
                        topics.append(v)
            elif isinstance(vals, str) and vals and vals != "暂无数据" and vals not in topics:
                topics.append(vals)
    return topics if topics else "暂无数据"


def _extract_related_stocks_from_matches(matches):
    stocks = []
    if not isinstance(matches, list):
        return "暂无数据"
    for item in matches:
        for key in ("关联个股/ETF", "关联个股", "stocks", "个股"):
            vals = item.get(key) if isinstance(item, dict) else None
            if isinstance(vals, list):
                for v in vals:
                    if v not in stocks:
                        stocks.append(v)
            elif isinstance(vals, str) and vals and vals != "暂无数据" and vals not in stocks:
                stocks.append(vals)
    return stocks if stocks else "暂无数据"


def _fmt_yi(v):
    """金额转亿元显示。"""
    try:
        if v is None:
            return "暂无"
        return f"{float(v) / 100000000:.2f}亿"
    except Exception:
        return "暂无"


def summarize_lhb_records(lhb_result, limit=3):
    """
    把龙虎榜 records 压缩成人能读的摘要。
    保留原始 records，同时新增摘要，适合盘后总结展示。
    """
    if not isinstance(lhb_result, dict):
        return "暂无数据"

    if lhb_result.get("error"):
        return lhb_result.get("error")

    records = lhb_result.get("records") or []
    if not records:
        return "暂无数据"

    summaries = []
    for r in records[:limit]:
        if not isinstance(r, dict):
            continue

        name = r.get("名称") or r.get("股票简称") or ""
        code = r.get("代码") or r.get("股票代码") or ""
        date = r.get("上榜日") or r.get("交易日期") or r.get("日期") or ""
        reason = r.get("上榜原因") or "暂无"
        interp = r.get("解读") or "暂无"

        close = r.get("收盘价")
        pct = r.get("涨跌幅")
        net = r.get("龙虎榜净买额")
        buy = r.get("龙虎榜买入额")
        sell = r.get("龙虎榜卖出额")
        turnover = r.get("换手率")

        try:
            pct_text = f"{float(pct):.2f}%"
        except Exception:
            pct_text = "暂无"

        try:
            close_text = f"{float(close):.2f}"
        except Exception:
            close_text = "暂无"

        try:
            turnover_text = f"{float(turnover):.2f}%"
        except Exception:
            turnover_text = "暂无"

        # 简单资金判断
        try:
            net_val = float(net)
            if net_val > 0:
                money_view = "龙虎榜净买入，资金承接偏积极"
            elif net_val < 0:
                money_view = "龙虎榜净卖出，资金分歧偏大"
            else:
                money_view = "龙虎榜净额接近平衡"
        except Exception:
            money_view = "资金方向暂缺"

        summaries.append({
            "股票": f"{name} {code}".strip(),
            "上榜日": str(date),
            "上榜原因": reason,
            "解读": interp,
            "收盘价": close_text,
            "涨跌幅": pct_text,
            "龙虎榜净买额": _fmt_yi(net),
            "买入额": _fmt_yi(buy),
            "卖出额": _fmt_yi(sell),
            "换手率": turnover_text,
            "资金判断": money_view,
            "风险提示": "龙虎榜代表短线资金行为，次日需重点观察承接，谨防高开兑现。"
        })

    return summaries if summaries else "暂无数据"


def _safe_quote_for_report(symbol):
    try:
        r = quote_sina(symbol)
        if isinstance(r, dict) and "error" not in r:
            return r
        return {"symbol": symbol, "error": "暂缺（新浪接口不可用）"}
    except Exception as e:
        return {"symbol": symbol, "error": "暂缺（新浪接口不可用）", "details": str(e)}


def _rank_quotes(items, key="涨跌幅%", reverse=True, limit=8):
    rows = [x for x in items if isinstance(x, dict) and "error" not in x and x.get(key) is not None]
    try:
        rows.sort(key=lambda x: float(x.get(key) or 0), reverse=reverse)
    except Exception:
        pass
    return rows[:limit] if rows else "暂无数据"


def _after_market_judgement(index_rows):
    rows = [row for row in index_rows if isinstance(row, dict) and not row.get("error") and _to_float(row.get("涨跌幅%")) is not None]
    if not rows:
        return "指数数据暂缺，暂不判断市场强弱。"
    values = [_to_float(row["涨跌幅%"]) for row in rows[:5]]
    avg = sum(values) / len(values)
    view = "偏强" if avg >= 1.2 else "偏弱" if avg <= -1 else "震荡"
    return f"可用指数样本 {len(values)} 个，等权平均涨跌幅约{avg:.2f}%，指数表现{view}；该均值不是全A收益率。"


def _to_float(v, default=None):
    try:
        if v is None:
            return default
        if isinstance(v, str):
            v = v.replace("%", "").replace(",", "").strip()
            if v in ("", "-", "--", "None", "nan"):
                return default
        number = float(v)
        return number if math.isfinite(number) else default
    except Exception:
        return default


def _stock_row_to_dict(row):
    """把全A行情的一行转成稳定 JSON 字段，兼容新浪/东方财富字段。"""
    return {
        "代码": str(row.get("代码", "")),
        "名称": str(row.get("名称", "")),
        "最新价": _to_float(row.get("最新价")),
        "涨跌额": _to_float(row.get("涨跌额")),
        "涨跌幅": _to_float(row.get("涨跌幅")),
        "成交量": _to_float(row.get("成交量")),
        "成交额": _to_float(row.get("成交额")),
        "时间戳": str(row.get("时间戳", "")),
    }


def _normalize_spot_df(df):
    """统一新浪/东方财富全A行情字段。"""
    if df is None or getattr(df, "empty", True):
        return df

    rename_map = {
        "代码": "代码",
        "名称": "名称",
        "最新价": "最新价",
        "涨跌额": "涨跌额",
        "涨跌幅": "涨跌幅",
        "成交量": "成交量",
        "成交额": "成交额",
        "时间戳": "时间戳",
    }

    # 东方财富常见字段基本同名；这里保留兜底，避免不同版本 akshare 字段小差异
    alt_map = {
        "今开": "今开",
        "最高": "最高",
        "最低": "最低",
        "昨收": "昨收",
    }

    for src, dst in {**rename_map, **alt_map}.items():
        if src in df.columns and dst not in df.columns:
            df[dst] = df[src]

    # 确保必要字段存在
    for col in ["代码", "名称", "最新价", "涨跌额", "涨跌幅", "成交量", "成交额"]:
        if col not in df.columns:
            df[col] = None

    for col in ["最新价", "涨跌额", "涨跌幅", "成交量", "成交额"]:
        df[col] = df[col].apply(lambda x: _to_float(x))

    return df



MARKET_OVERVIEW_CACHE_PATH = _state_path("cache", "market_overview.json")


def save_market_overview_cache(data):
    """保存最近一次成功的全A市场概览缓存。"""
    try:
        if not isinstance(data, dict) or data.get("error"):
            return
        os.makedirs(os.path.dirname(MARKET_OVERVIEW_CACHE_PATH), exist_ok=True)
        cache_data = dict(data)
        cache_data["cached_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cache_data["缓存说明"] = "最近一次成功获取的全A市场概览"
        tmp = MARKET_OVERVIEW_CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache_data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, MARKET_OVERVIEW_CACHE_PATH)
    except Exception:
        pass


def load_market_overview_cache(max_age_seconds=900):
    """读取最近一次成功的全A市场概览缓存。"""
    try:
        if not os.path.exists(MARKET_OVERVIEW_CACHE_PATH):
            return None
        with open(MARKET_OVERVIEW_CACHE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or data.get("error"):
            return None
        cached_at = datetime.fromisoformat(data.get("cached_at", ""))
        age = (datetime.now() - cached_at).total_seconds()
        if age < 0 or age > max_age_seconds:
            return None
        data = dict(data)
        data["缓存年龄_秒"] = round(age)
        data["原始数据源"] = data.get("数据源")
        data["数据源"] = "cache / 最近一次成功全A市场概览"
        data["使用缓存"] = True
        return data
    except Exception:
        return None


def market_overview_sina(limit=10):
    """
    全A市场数据。
    优先使用新浪 ak.stock_zh_a_spot()；
    新浪失败时，自动兜底东方财富 ak.stock_zh_a_spot_em()。
    用于盘后总结：市场宽度、涨跌幅榜、成交额榜。
    """
    errors = []

    try:
        import akshare as ak
    except Exception as e:
        return {"error": "暂缺（akshare不可用）", "details": str(e)}

    df = None
    source = None

    # 1. 优先新浪
    try:
        df = ak.stock_zh_a_spot()
        df = _normalize_spot_df(df)
        if df is not None and not df.empty and not df["涨跌幅"].notna().any():
            df = None
            raise ValueError("全A涨跌幅没有有效样本")
        if df is not None and not df.empty:
            source = "akshare.stock_zh_a_spot / 新浪A股实时"
    except Exception as e:
        errors.append(f"新浪失败: {e}")

    # 2. 兜底东方财富
    if df is None or getattr(df, "empty", True):
        try:
            df = ak.stock_zh_a_spot_em()
            df = _normalize_spot_df(df)
            if df is not None and not df.empty and not df["涨跌幅"].notna().any():
                df = None
                raise ValueError("全A涨跌幅没有有效样本")
            if df is not None and not df.empty:
                source = "akshare.stock_zh_a_spot_em / 东方财富A股实时"
        except Exception as e:
            errors.append(f"东方财富失败: {e}")

    if df is None or getattr(df, "empty", True):
        cached = load_market_overview_cache()
        if cached:
            cached["fallback_errors"] = errors
            cached["缓存兜底原因"] = "实时全A行情接口失败，已使用最近一次成功缓存"
            return cached
        return {
            "error": "暂缺（全A行情接口不可用）",
            "details": "；".join(errors) if errors else "unknown"
        }

    up_count = int((df["涨跌幅"] > 0).sum()) if "涨跌幅" in df.columns else None
    down_count = int((df["涨跌幅"] < 0).sum()) if "涨跌幅" in df.columns else None
    flat_count = int((df["涨跌幅"] == 0).sum()) if "涨跌幅" in df.columns else None

    limit_up_count = int((df["涨跌幅"] >= 9.8).sum()) if "涨跌幅" in df.columns else None
    limit_down_count = int((df["涨跌幅"] <= -9.8).sum()) if "涨跌幅" in df.columns else None

    rise_top = df.dropna(subset=["涨跌幅"]).sort_values("涨跌幅", ascending=False).head(limit).apply(_stock_row_to_dict, axis=1).tolist()
    fall_top = df.dropna(subset=["涨跌幅"]).sort_values("涨跌幅", ascending=True).head(limit).apply(_stock_row_to_dict, axis=1).tolist()
    amount_top = df.dropna(subset=["成交额"]).sort_values("成交额", ascending=False).head(limit).apply(_stock_row_to_dict, axis=1).tolist()

    total_amount = _to_float(df["成交额"].sum(min_count=1)) if "成交额" in df.columns else None

    result = {
        "数据源": source,
        "股票数量": int(len(df)),
        "数据获取时间": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
        "时效说明": "全A接口不提供统一交易日期；仅能核对获取时间，不能保证为当日收盘。",
        "市场宽度": {
            "涨跌幅有效样本": int(df["涨跌幅"].notna().sum()),
            "涨跌幅缺失家数": int(df["涨跌幅"].isna().sum()),
            "成交额有效样本": int(df["成交额"].notna().sum()),
            "上涨家数": up_count,
            "下跌家数": down_count,
            "平盘家数": flat_count,
            "涨停数量_估算": limit_up_count,
            "跌停数量_估算": limit_down_count,
            "总成交额_元": total_amount,
        },
        "全市场涨幅榜": rise_top,
        "全市场跌幅榜": fall_top,
        "全市场成交额榜": amount_top,
        "fallback_errors": errors
    }
    save_market_overview_cache(result)
    return result




def analyze_lhb_enhanced(lhb_result, watch_items=None, holding_items=None, limit=10):
    """Deduplicate same-day disclosures; conflicting observation windows are not additive."""
    if not isinstance(lhb_result, dict) or lhb_result.get("error"):
        return {"error": (lhb_result or {}).get("error", "龙虎榜数据格式异常") if isinstance(lhb_result, dict) else "龙虎榜数据格式异常"}
    records = lhb_result.get("records") or []
    grouped = {}
    warnings = []

    def amount(row, keys):
        for key in keys:
            if key in row:
                value = row[key]
                mult = 1
                if isinstance(value, str) and value.endswith(("亿", "万")):
                    mult = 100000000 if value.endswith("亿") else 10000
                    value = value[:-1]
                parsed = _to_float(value)
                return parsed * mult if parsed is not None else None
        return None

    def code_of(row):
        return str(row.get("代码") or row.get("股票代码") or row.get("symbol") or "").removeprefix("sh").removeprefix("sz").zfill(6)

    for record in records:
        if not isinstance(record, dict):
            continue
        code = code_of(record)
        if code == "000000":
            warnings.append("存在无股票代码的记录，未纳入统计")
            continue
        date = str(record.get("上榜日") or record.get("交易日期") or record.get("日期") or lhb_result.get("date") or "未知日期")
        key = (date, code)
        buy = amount(record, ("买入额", "龙虎榜买入额", "买入金额"))
        sell = amount(record, ("卖出额", "龙虎榜卖出额", "卖出金额"))
        net = amount(record, ("龙虎榜净买额", "净买额", "净买入额", "净买入", "龙虎榜净买入额"))
        if net is None and buy is not None and sell is not None:
            net = buy - sell
        values = (buy, sell, net)
        reason = str(record.get("上榜原因") or record.get("解读") or "")
        if key not in grouped:
            grouped[key] = {"代码": code, "股票": record.get("名称") or record.get("股票简称") or record.get("股票") or code,
                            "日期": date, "涨跌幅": amount(record, ("涨跌幅", "涨跌幅%")),
                            "收盘价": _to_float(record.get("收盘价")), "买入额": buy,
                            "卖出额": sell, "龙虎榜净买额": net, "原始记录数": 0,
                            "上榜原因列表": [], "_values": values, "统计冲突": False}
        row = grouped[key]
        row["原始记录数"] += 1
        if reason and reason not in row["上榜原因列表"]:
            row["上榜原因列表"].append(reason)
        if values != row["_values"]:
            row["统计冲突"] = True
            row["买入额"] = row["卖出额"] = row["龙虎榜净买额"] = None
            warnings.append(f"{date} {code} 多原因上榜金额不一致，可能含不同统计区间，金额不合并")
    rows = list(grouped.values())
    for row in rows:
        row.pop("_values")
        row["上榜原因"] = "；".join(row.pop("上榜原因列表"))
    valid = [row for row in rows if row["龙虎榜净买额"] is not None]
    net = sum(row["龙虎榜净买额"] for row in valid) if valid else None
    complete = bool(rows) and len(valid) == len(rows)
    conclusion = ("本次有效龙虎榜样本净买入。" if net > 0 else "本次有效龙虎榜样本净卖出。" if net < 0 else "本次有效龙虎榜样本净额接近零。") if complete else "龙虎榜缺失或存在冲突，不作整体资金方向判断。"
    if lhb_result.get("rows", len(records)) > len(records):
        warnings.append("龙虎榜明细是截断样本，统计不代表全市场")
        conclusion = "龙虎榜仅返回部分明细，不作全市场资金方向判断。"

    def top(field, reverse):
        return sorted([row for row in rows if row.get(field) is not None], key=lambda row: row[field], reverse=reverse)[:limit]

    def hits(items):
        codes = {str(item.get("code") if isinstance(item, dict) else item).removeprefix("sh").removeprefix("sz") for item in items or []}
        return [row for row in rows if row["代码"] in codes]

    def total(field):
        values = [row[field] for row in rows if row[field] is not None]
        return sum(values) if values else None

    return {
        "龙虎榜完整统计": {"日期": lhb_result.get("date"), "模式": lhb_result.get("mode"),
            "原始记录数": len(records), "接口记录数": lhb_result.get("rows", len(records)),
            "去重后个股数": len({row["代码"] for row in rows}), "去重后日期个股数": len(rows),
            "重复上榜个股数": sum(row["原始记录数"] > 1 for row in rows),
            "净买入个股数": sum(row["龙虎榜净买额"] > 0 for row in valid),
            "净卖出个股数": sum(row["龙虎榜净买额"] < 0 for row in valid),
            "净额接近零个股数": sum(row["龙虎榜净买额"] == 0 for row in valid),
            "金额缺失或冲突数": len(rows) - len(valid), "合计买入额": total("买入额"),
            "合计卖出额": total("卖出额"), "合计净买额": net,
            "统计口径": "按日期和代码去重；重复金额只计一次，冲突金额剔除。合计仅含有效样本。"},
        "龙虎榜净买入前列": [row for row in top("龙虎榜净买额", True) if row["龙虎榜净买额"] > 0],
        "龙虎榜净卖出前列": [row for row in top("龙虎榜净买额", False) if row["龙虎榜净买额"] < 0],
        "龙虎榜涨幅前列": top("涨跌幅", True), "龙虎榜跌幅前列": top("涨跌幅", False),
        "观察池龙虎榜命中": hits(watch_items), "持仓池龙虎榜命中": hits(holding_items),
        "龙虎榜资金方向判断": {"结论": conclusion}, "数据提示": list(dict.fromkeys(warnings)),
    }


def build_after_close_brief_judgement(idx, market_overview=None, watch_rows=None, holding_rows=None, lhb_enhanced=None):
    """
    盘后增强简短判断：
    综合指数、全A市场温度、观察池、持仓池、龙虎榜资金方向。
    """
    base = _after_market_judgement(idx)

    market_overview = market_overview or {}
    watch_rows = watch_rows or []
    holding_rows = holding_rows or []
    lhb_enhanced = lhb_enhanced or {}

    parts = [base]

    to_float = _to_float

    # 1. 市场真实温度
    up_count = None
    down_count = None
    amount = None

    if isinstance(market_overview, dict):
        width = market_overview.get("市场宽度", {})
        if not isinstance(width, dict):
            width = {}

        # 优先读取 全A市场概览 -> 市场宽度 -> 上涨/下跌家数
        for k in ("上涨家数", "up_count", "上涨数"):
            if k in width:
                up_count = to_float(width.get(k))
                break
            if k in market_overview:
                up_count = to_float(market_overview.get(k))
                break

        for k in ("下跌家数", "down_count", "下跌数"):
            if k in width:
                down_count = to_float(width.get(k))
                break
            if k in market_overview:
                down_count = to_float(market_overview.get(k))
                break

        if _to_float(width.get("总成交额_元")) is not None:
            amount = _fmt_yi(width["总成交额_元"])

        # 成交额可能在概览内，也可能只有个股榜单里有，读不到就不强行写
        for k in ("成交额", "成交额_亿元", "total_amount", "amount"):
            if k in market_overview:
                amount = market_overview.get(k)
                break

    if up_count is not None and down_count is not None:
        if down_count > up_count:
            parts.append(f"但全A下跌家数多于上涨家数（上涨约{int(up_count)}家、下跌约{int(down_count)}家），说明真实赚钱效应弱于指数表现，属于结构性行情。")
        elif up_count > down_count:
            parts.append(f"全A上涨家数多于下跌家数（上涨约{int(up_count)}家、下跌约{int(down_count)}家），市场赚钱效应相对配合。")
        else:
            parts.append("全A涨跌家数接近，市场分歧仍然存在。")

    if amount:
        parts.append(f"有效行情样本成交额合计{amount}；缺少上一交易日对照，不判断量能增减。")

    # 2. 观察池 / 持仓池表现
    def top_stock(rows):
        valid = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            pct = to_float(r.get("涨跌幅%") if "涨跌幅%" in r else r.get("涨跌幅"))
            name = r.get("名称") or r.get("股票") or r.get("观察池名称") or r.get("持仓名称") or r.get("symbol") or r.get("代码")
            if pct is not None:
                valid.append((pct, name))
        if not valid:
            return None
        valid.sort(key=lambda item: item[0], reverse=True)
        return valid[0]

    w_top = top_stock(watch_rows)
    h_top = top_stock(holding_rows)

    if w_top:
        parts.append(f"观察池中表现较强的是{w_top[1]}，涨跌幅约{w_top[0]:.2f}%，可继续观察是否与当日主线共振。")
    if h_top:
        parts.append(f"持仓池中表现较强的是{h_top[1]}，涨跌幅约{h_top[0]:.2f}%，持仓侧重点看强势能否延续以及是否出现冲高回落。")

    # 3. 龙虎榜资金方向
    if isinstance(lhb_enhanced, dict):
        judgement = lhb_enhanced.get("龙虎榜资金方向判断", {})
        if isinstance(judgement, dict):
            conclusion = judgement.get("结论")
            if conclusion:
                parts.append(f"龙虎榜方面，{conclusion}")

        watch_hit = lhb_enhanced.get("观察池龙虎榜命中")
        holding_hit = lhb_enhanced.get("持仓池龙虎榜命中")

        if isinstance(watch_hit, list) and watch_hit:
            names = "、".join([str(x.get("股票") or x.get("代码")) for x in watch_hit[:3]])
            parts.append(f"观察池中有个股登上龙虎榜：{names}，需要重点看净买入还是净卖出。")

        if isinstance(holding_hit, list) and holding_hit:
            names = "、".join([str(x.get("股票") or x.get("代码")) for x in holding_hit[:3]])
            parts.append(f"持仓池中有个股登上龙虎榜：{names}，次日要重点关注资金承接和兑现压力。")

    # 4. 明日风险提示
    parts.append("明日重点观察核心主线高开后的承接强度；若指数继续强但个股分化扩大，非主线方向仍需控制追高风险。")

    return "".join(parts)


def _normalize_report_date(value=None):
    if value is None:
        return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d")
    raw = str(value).strip().replace("-", "").replace("/", "")
    if not re.fullmatch(r"\d{8}", raw):
        raise ValueError("复盘日期必须为 YYYYMMDD 或 YYYY-MM-DD")
    parsed = datetime.strptime(raw, "%Y%m%d")
    if parsed.date() > datetime.now(ZoneInfo("Asia/Shanghai")).date():
        raise ValueError("不能生成未来日期的复盘")
    return raw


def _historical_quote_for_report(symbol, date, *, index_code=None):
    """Fetch exactly the requested daily bar; never replace missing history with live data."""
    code = str(symbol).removeprefix("sh").removeprefix("sz")
    target = datetime.strptime(date, "%Y%m%d")
    try:
        # Bounded request using the same Eastmoney daily-bar schema as the installed provider.
        secid = ("1." if index_code.startswith("sh") else "0.") + code if index_code else get_secid(code)
        response = requests.get(
            "https://push2his.eastmoney.com/api/qt/stock/kline/get",
            params={"secid": secid, "fields1": "f1,f2,f3,f4,f5,f6",
                    "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
                    "klt": "101", "fqt": "0", "beg": date, "end": date},
            headers=HEADERS, timeout=10,
        )
        response.raise_for_status()
        payload = response.json().get("data") or {}
        for raw in payload.get("klines", []):
            fields = raw.split(",")
            if len(fields) >= 10 and fields[0] == target.strftime("%Y-%m-%d"):
                return {"代码": code, "名称": payload.get("name") or code,
                        "收盘价": _to_float(fields[2]), "涨跌幅%": _to_float(fields[8]),
                        "今开": _to_float(fields[1]), "最高": _to_float(fields[3]),
                        "最低": _to_float(fields[4]), "成交额": _to_float(fields[6]),
                        "数据时间": fields[0] + " 15:00:00", "数据源": "东方财富历史日线（不复权）"}
        return {"代码": code, "error": f"{date} 无日线数据（休市、停牌或接口缺失）"}
    except Exception:
        return {"代码": code, "error": f"{date} 历史日线不可用"}


def _dated_report_quote(row, date):
    if not isinstance(row, dict) or row.get("error"):
        return row
    row = dict(row)
    price = _to_float(row.get("最新价") if row.get("最新价") is not None else row.get("收盘价"))
    if price is None or price <= 0:
        row["error"] = "行情价格缺失或无效，未参与盘后判断"
        return row
    stamp = str(row.get("数据时间") or row.get("时间") or "")
    actual = stamp[:10].replace("-", "")
    if actual != date:
        row["error"] = f"行情日期 {stamp or '未知'} 与复盘日期 {date} 不一致，未参与判断"
    elif len(stamp) < 19 or stamp[11:19] < "15:00:00":
        row["error"] = f"行情尚未收盘（{stamp}），未参与盘后判断"
    return row


def after_close_report(date=None, symbol=None, include_lhb=False, emit=True):
    date = _normalize_report_date(date)
    historical = date != _normalize_report_date()
    sections = {}
    warnings = ["观察池与持仓池采用当前名单，不代表该历史日期的持仓。"] if historical else []
    quote_cache = {}

    def report_quote(code):
        if code not in quote_cache:
            row = _historical_quote_for_report(code, date) if historical else _safe_quote_for_report(code)
            quote_cache[code] = _dated_report_quote(row, date)
        return dict(quote_cache[code])


    try:
        if historical:
            idx = [_historical_quote_for_report(code, date, index_code=prefix + code)
                   for prefix, code in [("sh", "000001"), ("sz", "399001"), ("sz", "399006"), ("sh", "000688"), ("sh", "000016")]]
        else:
            idx = index_sina()
        idx = [_dated_report_quote(row, date) for row in idx]
        sections["指数概览"] = idx
    except Exception as e:
        idx = []
        sections["指数概览"] = {"error": "暂缺（新浪接口不可用）", "details": str(e)}

    # 全A市场数据：新浪全A接口，失败不拖垮报告
    try:
        sections["全A市场概览"] = ({"error": "历史全A市场宽度不可用；不会使用今日快照替代"}
                                  if historical else market_overview_sina(limit=10))
    except Exception as e:
        sections["全A市场概览"] = {"error": "暂缺（全A行情接口不可用）", "details": str(e)}

    # 指数ETF观察：用新浪行情，尽量稳定
    etf_symbols = ["510050", "510300", "510500", "159915", "512000"]
    etf_rows = []
    for s in etf_symbols:
        etf_rows.append(report_quote(s))
    sections["指数ETF观察"] = etf_rows

    # 观察池 / 持仓池：读取 ~/.openclaw/market_watchlist.json
    try:
        wl_cfg = load_watchlist_config()
    except Exception as e:
        wl_cfg = {
            "watch_pool": [],
            "holding_pool": [],
            "monitor": {"enabled": False},
            "_error": str(e)
        }

    watch_items = wl_cfg.get("watch_pool", []) or []
    holding_items = wl_cfg.get("holding_pool", []) or []

    watch_rows = []
    for item in watch_items:
        code = item.get("code") if isinstance(item, dict) else str(item)
        row = report_quote(code)
        if isinstance(row, dict) and isinstance(item, dict):
            row["观察池名称"] = item.get("name", code)
            row["备注"] = item.get("note", "")
        watch_rows.append(row)

    holding_rows = []
    for item in holding_items:
        code = item.get("code") if isinstance(item, dict) else str(item)
        row = report_quote(code)
        if isinstance(row, dict) and isinstance(item, dict):
            row["持仓名称"] = item.get("name", code)
            row["备注"] = item.get("note", "")
            for k in ("cost", "position", "stop_loss", "target"):
                if k in item:
                    row[k] = item.get(k)
        holding_rows.append(row)

    sections["观察池重点跟踪"] = watch_rows if watch_rows else "暂无数据（可用 watch add 代码 名称 添加）"
    sections["观察池涨幅榜"] = _rank_quotes(watch_rows, key="涨跌幅%", reverse=True, limit=8) if watch_rows else "暂无数据"
    sections["观察池跌幅榜"] = _rank_quotes(watch_rows, key="涨跌幅%", reverse=False, limit=5) if watch_rows else "暂无数据"

    sections["持仓池重点跟踪"] = holding_rows if holding_rows else "暂无数据（可用 hold add 代码 名称 添加）"
    sections["持仓池涨幅榜"] = _rank_quotes(holding_rows, key="涨跌幅%", reverse=True, limit=8) if holding_rows else "暂无数据"
    sections["持仓池跌幅榜"] = _rank_quotes(holding_rows, key="涨跌幅%", reverse=False, limit=5) if holding_rows else "暂无数据"

    monitor = wl_cfg.get("monitor", {}) or {}
    sections["实时盯盘状态"] = {
        "enabled": bool(monitor.get("enabled", False)),
        "状态": "开启" if monitor.get("enabled", False) else "关闭",
        "刷新频率_秒": monitor.get("interval_seconds", 60),
        "盯盘范围": monitor.get("targets", ["watch_pool", "holding_pool"]),
        "提醒渠道": monitor.get("notify_channel", "telegram"),
        "说明": "实时盯盘默认关闭；可用 monitor on 开启，monitor off 关闭。"
    }

    if symbol:
        sections["观察对象"] = {
            "symbol": symbol,
            "行情": report_quote(symbol)
        }
    else:
        sections["观察对象"] = {
            "symbol": "未指定",
            "note": "可使用 after 600519 --lhb 查看指定个股"
        }

    # 简短判断放到龙虎榜处理之后统一生成，便于综合龙虎榜、观察池、持仓池

    if include_lhb:
        try:
            lhb_result = lhb(date=date, symbol=symbol, emit=False)
            sections["龙虎榜摘要"] = summarize_lhb_records(lhb_result)
            sections["龙虎榜增强分析"] = analyze_lhb_enhanced(
                lhb_result,
                watch_items=watch_items,
                holding_items=holding_items,
                limit=10
            )

            # 盘后报告默认压缩龙虎榜原始明细，避免 TG 输出过长
            if isinstance(lhb_result, dict) and "records" in lhb_result:
                sections["龙虎榜"] = {
                    "date": lhb_result.get("date"),
                    "symbol": lhb_result.get("symbol"),
                    "mode": lhb_result.get("mode"),
                    "range": lhb_result.get("range"),
                    "rows": lhb_result.get("rows"),
                    "数据源": lhb_result.get("数据源"),
                    "提示": "完整明细已压缩，默认展示龙虎榜摘要"
                }
            else:
                sections["龙虎榜"] = lhb_result
        except Exception as e:
            sections["龙虎榜"] = {"error": "暂缺（东方财富接口不可用）", "details": str(e)}
            sections["龙虎榜摘要"] = "暂缺（东方财富接口不可用）"
    else:
        sections["龙虎榜"] = "已跳过（默认不启用）"
        sections["龙虎榜摘要"] = "已跳过（默认不启用）"

    sections["简短判断"] = build_after_close_brief_judgement(
        [row for row in idx if isinstance(row, dict) and not row.get("error")],
        market_overview=sections.get("全A市场概览"),
        watch_rows=[row for row in watch_rows if not row.get("error")],
        holding_rows=[row for row in holding_rows if not row.get("error")],
        lhb_enhanced=sections.get("龙虎榜增强分析")
    )

    result = {
        "report_type": "after_close_report",
        "date": date,
        "symbol": symbol,
        "warnings": warnings,
        "sections": sections
    }
    if emit:
        print_json(result)
    return result



MARKET_AFTER_CLOSE_PHRASES = (
    "盘后复盘",
    "大盘复盘",
    "整体盘后复盘",
    "整体的盘后复盘",
    "今天盘后",
    "今天大盘总结",
    "直接做一份整体的盘后复盘",
    "做完整盘后复盘",
    "完整版盘后复盘",
)
MARKET_SCOPE_KEYWORDS = ("整体", "大盘", "市场", "盘后", "收盘", "今天大盘")
STOCK_SCOPE_KEYWORDS = ("个股", "股票代码", "某只股票", "持仓")
HOLDING_SCOPE_KEYWORDS = ("持仓", "持仓池")
FULL_REPORT_KEYWORDS = ("完整版", "完整", "详细", "全量", "全部")
STOCK_CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")


def classify_after_close_intent(text):
    """Classify review requests before broad natural-language matching."""
    normalized = " ".join(str(text).strip().split())
    # Pool mutations carry stock codes too; leave them to their state-changing router.
    if any(pool in normalized for pool in ("观察池", "关注池", "自选池", "持仓池", "持仓")) and any(action in normalized for action in ("加入", "新增", "删除", "移除")):
        return {"intent": "other", "pool_mutation": True}
    code_match = STOCK_CODE_RE.search(normalized)
    stock_code = code_match.group(1) if code_match else None
    has_market_scope = any(k in normalized for k in ("整体", "大盘", "全市场", "市场整体"))
    has_holding_scope = any(k in normalized for k in HOLDING_SCOPE_KEYWORDS)
    has_stock_scope = any(k in normalized for k in STOCK_SCOPE_KEYWORDS)
    has_review_word = any(k in normalized for k in ("复盘", "盘后", "收盘总结", "盘后总结"))

    if stock_code:
        return {"intent": "stock_review" if has_review_word else "stock_quote", "stock_code": stock_code}

    if has_market_scope and (has_holding_scope or "个股" in normalized):
        return {"intent": "mixed_review"}

    if has_holding_scope and has_review_word:
        return {"intent": "holding_review"}

    if has_review_word and "个股" in normalized:
        return {"intent": "stock_review", "needs_stock_code": True}

    if "股票代码" in normalized or "某只股票" in normalized:
        return {"intent": "stock_review", "needs_stock_code": True}

    if any(phrase in normalized for phrase in MARKET_AFTER_CLOSE_PHRASES):
        return {"intent": "market_after_close"}

    if has_review_word and not has_stock_scope:
        return {"intent": "market_after_close"}

    if "帮我看一下" in normalized and "股票" in normalized:
        return {"intent": "stock_quote", "needs_stock_code": True}

    return {"intent": "other"}


def handle_natural_language_command(text):
    text = " ".join(str(text).strip().split())
    if not text:
        return {"error": "空命令"}
    codex_action = {
        "开启codex流量": "on", "关闭codex流量": "off", "codex流量状态": "status",
    }.get(re.sub(r"\s+", "", text).casefold())
    if codex_action:
        return model_codex_control(codex_action)

    name_code_map = {
        "中芯国际": "688981",
        "寒武纪": "688256",
        "兆易创新": "603986",
        "新易盛": "300502",
        "工业富联": "601138",
        "宁德时代": "300750",
        "贵州茅台": "600519",
        "中国平安": "601318",
        "中信证券": "600030",
        "科大讯飞": "002230",
        "立讯精密": "002475",
        "汇川技术": "300124",
        "山东黄金": "600547",
        "紫金矿业": "601899"
    }

    def parse_stock(raw):
        raw = raw.strip()
        parts = raw.split()
        if not parts:
            return None, None, None
        if parts[0].isdigit() and len(parts[0]) == 6:
            code = parts[0]
            name = parts[1] if len(parts) > 1 else code
            note = " ".join(parts[2:]) if len(parts) > 2 else ""
            return code, name, note
        name = parts[0]
        code = name_code_map.get(name)
        note = " ".join(parts[1:]) if len(parts) > 1 else ""
        if code:
            return code, name, note
        if name.isdigit() and len(name) == 6:
            return name, name, note
        return None, name, note

    if any(name in text for name in ("美股指数", "美国三大指数", "标普指数", "纳斯达克指数", "道琼斯指数")):
        try:
            return {"ok": True, "report_type": "us_indices", "indices": us_index()}
        except Exception as exc:
            return {"ok": False, "error": f"美股指数暂不可用：{exc}"}

    us_match = re.search(r"(?:美股\s*)?([A-Za-z][A-Za-z0-9.^-]{0,14})\s*(?:行情|股价|报价|走势)", text)
    if not us_match:
        us_match = re.search(r"(?:查询|查看|看一下)\s*(?:美股\s*)?([A-Za-z][A-Za-z0-9.^-]{0,14})", text)
    if us_match:
        try:
            return {"ok": True, "report_type": "us_quote", "行情": quote_us(us_match.group(1))}
        except Exception as exc:
            return {"ok": False, "error": f"美股行情暂不可用：{exc}"}

    if text in {"策略回测开启", "开启策略回测"}:
        return backtest_toggle(True)

    if text in {"策略回测关闭", "关闭策略回测"}:
        return backtest_toggle(False)

    if text in {"策略回测状态", "查看策略回测状态"}:
        return backtest_status()

    for prefix in ("设置回测策略：", "设置回测策略:", "设置回测策略 "):
        if text.startswith(prefix):
            return backtest_set_strategy(text[len(prefix):].strip())

    if text in {"查看回测策略", "查看当前回测策略"}:
        return backtest_show_strategy()

    if text in {"清空回测策略", "删除回测策略"}:
        return backtest_clear_strategy()

    if text in {"查看最近回测结果", "查看回测交易明细"}:
        return backtest_trades_command()

    if text in {"生成图形回测", "生成回测图形"}:
        return backtest_chart_command()

    if "回测" in text:
        status = backtest_status()
        if not status.get("enabled"):
            return {
                "ok": False,
                "needs_backtest_enable": True,
                "message": "策略回测当前关闭，如需自然语言回测请先发送“策略回测开启”。明确 backtest run 命令仍可直接执行。"
            }
        if re.search(r"(?:买入条件|卖出条件)\s*[:：]", text):
            return backtest_run_text_command(text)
        request = _parse_natural_backtest_request(text)
        if not request.get("ok"):
            return request
        saved_strategy_requested = bool(re.search(
            r"运行已保存策略|执行当前已保存回测配置|复跑(?:上次)?(?:已)?保存的策略",
            text,
        ))
        return backtest_run_saved_command(
            request["symbol"],
            days=request["days"],
            interval=request["interval"],
            saved_strategy_confirmed=saved_strategy_requested,
        )

    strategy_prefixes = ("设置策略盯盘：", "设置策略盯盘:", "设置策略盯盘 ")
    for prefix in strategy_prefixes:
        if text.startswith(prefix):
            return strategy_set(text[len(prefix):].strip())

    strategy_dry_run_match = re.match(
        r"^(?:策略\s*dry-run|策略草案|试解析策略)[：:\s]+(.+)$",
        text,
        re.IGNORECASE,
    )
    if strategy_dry_run_match:
        return strategy_dry_run(strategy_dry_run_match.group(1).strip().strip('"“”'))

    if text in {"查看当前策略", "查看策略盯盘", "当前策略", "strategy list", "策略列表"}:
        return strategy_list()

    if text in {"strategy status", "策略状态"}:
        return strategy_status()

    explain_match = re.match(r"^(?:strategy explain|解释策略)\s+([A-Za-z0-9_-]+)$", text, re.I)
    if explain_match:
        return strategy_explain(explain_match.group(1))

    test_match = re.match(r"^(?:strategy test|测试策略)\s+([A-Za-z0-9_-]+)$", text, re.I)
    if test_match:
        return strategy_test(test_match.group(1))

    delete_match = re.match(r"^(?:strategy delete|删除策略)\s+([A-Za-z0-9_-]+)$", text, re.I)
    if delete_match:
        return strategy_delete(delete_match.group(1))

    if text in {"查看策略详情"}:
        return strategy_get()

    if text in {"清空当前策略", "清空策略盯盘", "删除当前策略"}:
        return strategy_clear()

    if text in {
        "策略盯盘状态", "策略盯盘自检", "检查策略盯盘",
        "非交易时间验证策略盯盘",
    }:
        return strategy_check()

    if text in {
        "普通盯盘诊断", "为什么没提醒", "检查最近盯盘日志",
        "monitor diagnose", "检查普通盯盘", "检查普通盯盘策略",
    }:
        return monitor_diagnose()

    if text in {"测试盯盘提醒", "微信通知测试", "测试微信盯盘提醒"}:
        return monitor_notify_test("weixin")

    if text in {"模拟盯盘提醒", "模拟触发普通盯盘", "测试普通盯盘模拟提醒"}:
        return monitor_simulate_alert("weixin")

    simulate_match = re.search(
        r"(?:模拟触发策略盯盘|策略盯盘模拟验证)\s*([036]\d{5})?",
        text,
    )
    if simulate_match:
        return strategy_simulate(simulate_match.group(1) or "510050")

    if text in {"启动策略盯盘", "开启策略盯盘", "执行策略盯盘", "运行策略盯盘"}:
        set_result = monitor_set(
            True,
            mode="strategy",
            source_channel=_default_output_channel(),
            notify_channels=_normalize_notify_channels({}, include_fallback=True),
        )
        start_result = monitor_start()
        verify_result = monitor_verify(expected_running=True, wait_seconds=1.2)
        trading_status = trading_time_status()
        return {
            "ok": bool(verify_result.get("verified")),
            "action": "start_strategy_monitor",
            "message": _monitor_start_message(
                "策略盯盘", start_result, verify_result, trading_status
            ),
            "set": set_result,
            "strategy": strategy_get(),
            "start": start_result,
            "verify": verify_result,
            "trading_time": trading_status,
        }

    if text in {"停止策略盯盘", "关闭策略盯盘"}:
        stop_result = monitor_stop()
        verify_result = monitor_verify(expected_running=False, wait_seconds=1.2)
        return {
            "ok": bool(verify_result.get("verified")),
            "action": "stop_strategy_monitor",
            "stop": stop_result,
            "verify": verify_result
        }

    normal_monitor_commands = {
        "monitor on", "启动普通盯盘", "开启普通盯盘",
        "开启盯盘", "打开盯盘", "开始盯盘", "开启实时盯盘", "开始实时盯盘",
        "开启省流盯盘", "开启标准盯盘", "开启严密盯盘",
        "启动省流盯盘", "启动标准盯盘", "启动严密盯盘"
    }
    if text.lower() in normal_monitor_commands or text in normal_monitor_commands:
        set_result = monitor_set(
            True,
            mode="normal",
            source_channel=_default_output_channel(),
            notify_channels=_normalize_notify_channels({}, include_fallback=True),
        )
        start_result = monitor_start()
        verify_result = monitor_verify(expected_running=True, wait_seconds=1.2)
        return {
            "ok": bool(verify_result.get("verified")),
            "action": "start_normal_monitor",
            "set": set_result,
            "start": start_result,
            "verify": verify_result
        }

    if text in {"启动盯盘", "启动后台盯盘", "开始后台盯盘"}:
        monitor_set(
            True,
            mode="normal",
            source_channel=_default_output_channel(),
            notify_channels=_normalize_notify_channels({}, include_fallback=True),
        )
        start_result = monitor_start()
        verify_result = monitor_verify(expected_running=True, wait_seconds=1.2)
        return {"ok": bool(verify_result.get("verified")), "action": "start_monitor_current_mode", "start": start_result, "verify": verify_result}

    if text in {"停止后台盯盘", "关闭后台盯盘", "停止盯盘进程", "关闭盯盘进程"}:
        stop_result = monitor_stop()
        verify_result = monitor_verify(expected_running=False, wait_seconds=1.2)
        return {"ok": bool(verify_result.get("verified")), "action": "stop_monitor_process", "stop": stop_result, "verify": verify_result}

    if text in {"查看盯盘进程", "查看后台盯盘", "盯盘进程", "后台盯盘状态"}:
        return monitor_pid_status()

    if text.lower() == "monitor off" or text in {"关闭盯盘", "停止盯盘", "暂停盯盘", "关闭实时盯盘", "停止实时盯盘"}:
        # 关闭配置，同时尝试停止后台进程
        stop_result = monitor_stop()
        verify_result = monitor_verify(expected_running=False, wait_seconds=1.2)
        return {"ok": bool(verify_result.get("verified")), "action": "disable_monitor_and_stop_process", "stop": stop_result, "verify": verify_result}
    if _monitor_status_request(text):
        return monitor_status_output(text)

    if text in {"盘前报告", "盘前完整报告", "今日盘前", "今天盘前"}:
        return report_output("morning_report", variant="full", channel=_default_output_channel())

    if text in {"盘前简版报告", "盘前简报", "盘前简单版"}:
        return report_output("morning_report", variant="simple", channel=_default_output_channel())

    # 盘后复盘必须先做确定性分类，不能仅凭“复盘”追问股票代码。
    review_intent = classify_after_close_intent(text)
    if not review_intent.get("pool_mutation") and any(word in text for word in ("复盘", "盘后", "收盘总结")) and not any(word in text for word in ("整体", "大盘", "全市场")):
        named_codes = [code for name, code in name_code_map.items() if name in text]
        if len(set(named_codes)) == 1 and not review_intent.get("stock_code"):
            review_intent = {"intent": "stock_review", "stock_code": named_codes[0]}
    date_match = re.search(r"(?<!\d)(\d{4}[-/]?\d{2}[-/]?\d{2})(?!\d)", text)
    review_date = date_match.group(1) if date_match else None
    intent = review_intent.get("intent")

    if text in {"来个完整版", "来份完整版", "给我完整版", "完整版", "完整版本", "详细版"}:
        return report_output("after_close_report", variant="full", channel=_default_output_channel())

    if text in {"来个简单版", "来份简单版", "给我简单版", "简单版", "简洁版"}:
        return report_output("after_close_report", variant="simple", channel=_default_output_channel())

    if intent == "market_after_close":
        variant = "simple" if any(x in text for x in ("简版", "简洁", "简单")) else "full"
        return report_output("after_close_report", variant=variant, channel=_default_output_channel(), date=review_date)

    if intent == "mixed_review":
        return report_output("after_close_report", variant="full", channel=_default_output_channel(), date=review_date)

    if intent == "holding_review":
        holding_items = load_watchlist_config().get("holding_pool", []) or []
        if not holding_items:
            return {
                "intent": "holding_review",
                "needs_stock_code": True,
                "message": "当前没有持仓列表，请提供持仓股票代码。"
            }
        result = report_output("after_close_report", variant="full", channel=_default_output_channel(), date=review_date, scope="holding_pool")
        if isinstance(result, dict):
            result["review_scope"] = "holding_pool"
        return result

    if intent in {"stock_review", "stock_quote"}:
        stock_code = review_intent.get("stock_code")
        if not stock_code:
            for name, code in name_code_map.items():
                if name in text:
                    stock_code = code
                    break
        if not stock_code:
            return {
                "intent": intent,
                "needs_stock_code": True,
                "message": "请提供要分析的股票代码或明确股票名称。"
            }
        if intent == "stock_review":
            return report_output(
                "after_close_report", variant="full", channel=_default_output_channel(), symbol=stock_code, date=review_date
            )
        return {
            "report_type": "stock_quote",
            "symbol": stock_code,
            "行情": _safe_quote_for_report(stock_code)
        }

    if any(
        phrase in text
        for phrase in ("查看全部池行情快照", "查看持仓池和观察池行情")
    ):
        return pool_output("snapshot", "all", text)
    if "查看持仓池行情" in text:
        return pool_output("snapshot", "holding", text)
    if "查看观察池行情" in text:
        return pool_output("snapshot", "watch", text)
    if "查看持仓池名单" in text:
        return pool_output("list", "holding", text)
    if "查看观察池名单" in text:
        return pool_output("list", "watch", text)

    if text in {"查看观察池", "观察池", "查看关注池", "查看自选池"}:
        return watchlist_list("watch_pool")
    if text in {"查看持仓池", "持仓池", "查看持仓"}:
        return watchlist_list("holding_pool")

    for prefix in ("观察池", "关注池", "自选池"):
        if text.startswith(prefix + "加入") or text.startswith(prefix + "新增"):
            raw = text.replace(prefix + "加入", "", 1).replace(prefix + "新增", "", 1).strip()
            code, name, note = parse_stock(raw)
            if not code:
                return {"error": f"无法识别股票：{raw}，请用：观察池加入 688981 中芯国际"}
            return watchlist_add("watch_pool", code, name, note)

        if text.startswith(prefix + "删除") or text.startswith(prefix + "移除"):
            raw = text.replace(prefix + "删除", "", 1).replace(prefix + "移除", "", 1).strip()
            code, name, note = parse_stock(raw)
            return watchlist_remove("watch_pool", code or name or raw)

    for prefix in ("持仓池", "持仓"):
        if text.startswith(prefix + "加入") or text.startswith(prefix + "新增"):
            raw = text.replace(prefix + "加入", "", 1).replace(prefix + "新增", "", 1).strip()
            code, name, note = parse_stock(raw)
            if not code:
                return {"error": f"无法识别股票：{raw}，请用：持仓池加入 688256 寒武纪"}
            return watchlist_add("holding_pool", code, name, note)

        if text.startswith(prefix + "删除") or text.startswith(prefix + "移除"):
            raw = text.replace(prefix + "删除", "", 1).replace(prefix + "移除", "", 1).strip()
            code, name, note = parse_stock(raw)
            return watchlist_remove("holding_pool", code or name or raw)

    return {
        "error": "未识别的自然语言命令",
        "支持示例": [
            "观察池加入中芯国际",
            "观察池加入 688981 中芯国际",
            "观察池删除中芯国际",
            "持仓池加入寒武纪",
            "持仓池删除寒武纪",
            "开启盯盘",
            "启动普通盯盘",
            "启动策略盯盘",
            "设置策略盯盘：如果510050一分钟MACD金叉，就提醒我买入信号",
            "查看当前策略",
            "清空当前策略",
            "关闭盯盘",
            "查看盯盘状态"
        ]
    }


def after_close_report_full(date=None, symbol=None):
    """完整版盘后复盘：默认包含龙虎榜增强分析。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = after_close_report(date=date, symbol=symbol, include_lhb=True, emit=False)
    return result


def after_close_report_simple(date=None, symbol=None):
    """
    简洁版盘后复盘：
    复用完整版数据，但只保留适合 TG 快速阅读的关键模块。
    """
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        full = after_close_report(date=date, symbol=symbol, include_lhb=True, emit=False)

    if not isinstance(full, dict):
        return full

    sections = full.get("sections", {}) or {}

    simple_sections = {}

    if "简短判断" in sections:
        simple_sections["核心判断"] = sections.get("简短判断")

    if "指数概览" in sections:
        simple_sections["主要指数"] = sections.get("指数概览")
    elif "主要指数" in sections:
        simple_sections["主要指数"] = sections.get("主要指数")
    elif "指数表现" in sections:
        simple_sections["主要指数"] = sections.get("指数表现")

    overview = sections.get("全A市场概览")
    if isinstance(overview, dict):
        simple_sections["市场真实温度"] = {
            "数据源": overview.get("数据源"),
            "股票数量": overview.get("股票数量"),
            "市场宽度": overview.get("市场宽度"),
            "使用缓存": overview.get("使用缓存", False),
            "缓存时间": overview.get("cached_at")
        }
    elif overview:
        simple_sections["市场真实温度"] = overview

    if "观察池重点跟踪" in sections:
        simple_sections["观察池重点跟踪"] = sections.get("观察池重点跟踪")
    if "持仓池重点跟踪" in sections:
        simple_sections["持仓池重点跟踪"] = sections.get("持仓池重点跟踪")

    lhb_enhanced = sections.get("龙虎榜增强分析")
    if isinstance(lhb_enhanced, dict):
        lhb_brief = {}
        if "龙虎榜整体概况" in lhb_enhanced:
            lhb_brief["整体概况"] = lhb_enhanced.get("龙虎榜整体概况")
        if "龙虎榜资金方向判断" in lhb_enhanced:
            lhb_brief["资金方向判断"] = lhb_enhanced.get("龙虎榜资金方向判断")
        if "观察池龙虎榜命中" in lhb_enhanced:
            lhb_brief["观察池龙虎榜命中"] = lhb_enhanced.get("观察池龙虎榜命中")
        if "持仓池龙虎榜命中" in lhb_enhanced:
            lhb_brief["持仓池龙虎榜命中"] = lhb_enhanced.get("持仓池龙虎榜命中")
        simple_sections["龙虎榜简析"] = lhb_brief

    if "实时盯盘状态" in sections:
        simple_sections["实时盯盘状态"] = sections.get("实时盯盘状态")

    simple_sections["明日重点观察"] = [
        "核心主线高开后的承接强度",
        "指数强时个股赚钱效应能否修复",
        "观察池/持仓池个股是否强于所属板块",
        "龙虎榜净买入方向是否与主线一致",
        "若高位核心票冲高回落，注意主线分歧风险"
    ]

    return {
        "report_type": "after_close_report_simple",
        "date": full.get("date"),
        "title": "A股简洁盘后复盘",
        "说明": "默认复盘输出简洁版，仅保留核心判断、指数、市场温度、观察池/持仓池和龙虎榜简析；如需完整明细，可发送“来个完整版”。",
        "sections": simple_sections
    }


def report_output(report_type, variant="full", channel="telegram", date=None, symbol=None, scope=None):
    """Collect once, preserve diagnostics and distinguish incomplete data from success."""
    try:
        adapter = get_channel_adapter(channel)
    except ValueError:
        return {"ok": False, "error": "不支持的报告通道", "pipeline": {"channel_result": {"ok": False}}}
    try:
        if variant not in {"full", "simple"}:
            raise ValueError("报告版本必须为 full 或 simple")
        if report_type == "morning_report":
            if date and _normalize_report_date(date) != _normalize_report_date():
                raise ValueError("盘前新闻不支持历史日期回放，不能用当前新闻替代")
            data = morning_report(emit=False)
        elif report_type == "after_close_report":
            data = after_close_report(date=date, symbol=symbol, include_lhb=True, emit=False)
        else:
            raise ValueError("未知报告类型")
        if scope:
            data["review_scope"] = scope
        rendered = render_report(data, report_type, variant=variant, channel=channel)
        complete = rendered["analysis"]["status"] == "OK"
        return {"ok": True, "partial": not complete,
            "pipeline": {"collection_result": {"ok": complete, "report_type": report_type},
                         "analysis_result": {"ok": True, "schema_version": rendered["analysis"].get("schema_version")},
                         "template_result": {"ok": True, "variant": variant},
                         "channel_result": {"ok": True, "channel": channel}},
            "analysis": rendered["analysis"], "_output_format": "text", "text": rendered["text"]}
    except Exception as exc:
        # Exception payloads may contain URLs/tokens; expose a class and safe validation messages only.
        reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        analysis = fallback_analysis(report_type, variant, "报告流水线失败：" + reason)
        return {"ok": False, "error": reason,
            "pipeline": {"collection_result": {"ok": False}, "analysis_result": {"ok": False, "fallback_used": True},
                         "template_result": {"ok": True, "variant": variant}, "channel_result": {"ok": True, "channel": channel}},
            "analysis": analysis, "_output_format": "text", "text": adapter.render_report(analysis)}



MONITOR_STATE_PATH = _state_path("market_monitor_state.json")
MONITOR_STATE_LOCK_PATH = _state_path("market_monitor_state.lock")
STRATEGY_SELFCHECK_STATE_PATH = _state_path("strategy_selfcheck_state.json")


def _default_monitor_state():
    return {
        "last_alerts": {},
        "last_quotes": {},
        "strategy_active": {},
        "last_scan_at": None,
        "last_quote_at": None,
        "last_checked_symbols": [],
        "last_alert_candidates": [],
        "last_suppressed": [],
        "last_notify_result": None,
        "updated_at": None
    }


@contextlib.contextmanager
def _locked_file(path, exclusive=True, blocking=True):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lock_file = open(path, "a+", encoding="utf-8")
    operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    if not blocking:
        operation |= fcntl.LOCK_NB
    try:
        fcntl.flock(lock_file.fileno(), operation)
        yield lock_file
    finally:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            lock_file.close()


def _read_monitor_state_unlocked():
    if not os.path.exists(MONITOR_STATE_PATH):
        return _default_monitor_state()
    try:
        with open(MONITOR_STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return _default_monitor_state()
    if not isinstance(data, dict):
        return _default_monitor_state()
    data.setdefault("last_alerts", {})
    data.setdefault("last_quotes", {})
    data.setdefault("strategy_active", {})
    data.setdefault("last_checked_symbols", [])
    data.setdefault("last_alert_candidates", [])
    data.setdefault("last_suppressed", [])
    return data


def _write_monitor_state_unlocked(state):
    state["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    tmp = f"{MONITOR_STATE_PATH}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, MONITOR_STATE_PATH)


def _merge_alert_timestamps(current, incoming):
    merged = dict(current or {})
    for key, value in (incoming or {}).items():
        try:
            current_ts = float(merged.get(key, 0))
            incoming_ts = float(value)
            merged[key] = max(current_ts, incoming_ts)
        except Exception:
            if key not in merged:
                merged[key] = value
    return merged


def _merge_last_quotes(current, incoming):
    merged = dict(current or {})
    for code, quote in (incoming or {}).items():
        existing = merged.get(code)
        if not isinstance(existing, dict) or not isinstance(quote, dict):
            merged[code] = quote
            continue
        try:
            if float(quote.get("ts", 0)) >= float(existing.get("ts", 0)):
                merged[code] = quote
        except Exception:
            merged[code] = quote
    return merged


def load_monitor_state():
    try:
        with _locked_file(MONITOR_STATE_LOCK_PATH, exclusive=False):
            return _read_monitor_state_unlocked()
    except Exception:
        return _default_monitor_state()


def save_monitor_state(state):
    try:
        with _locked_file(MONITOR_STATE_LOCK_PATH, exclusive=True):
            current = _read_monitor_state_unlocked()
            merged = dict(current)
            merged.update({
                k: v for k, v in state.items()
                if k not in {"last_alerts", "last_quotes", "updated_at"}
            })
            merged["last_alerts"] = _merge_alert_timestamps(
                current.get("last_alerts", {}),
                state.get("last_alerts", {})
            )
            merged["last_quotes"] = _merge_last_quotes(
                current.get("last_quotes", {}),
                state.get("last_quotes", {})
            )
            _write_monitor_state_unlocked(merged)
            return True
    except Exception:
        return False


def _monitor_to_float(x, default=None):
    try:
        if x is None:
            return default
        if isinstance(x, str):
            x = x.replace("%", "").replace(",", "").strip()
            if x in {"", "-", "None", "nan"}:
                return default
        value = float(x)
        return value if math.isfinite(value) else default
    except Exception:
        return default


def _monitor_quote(code):
    try:
        row = _safe_quote_for_report(code)
        if not isinstance(row, dict):
            return {"代码": code, "error": str(row)}
        return row
    except Exception as e:
        return {"代码": code, "error": str(e)}


def _monitor_row_pct(row):
    for k in ("涨跌幅%", "涨跌幅", "change_pct", "pct_chg"):
        if k in row:
            return _monitor_to_float(row.get(k))
    return None


def _monitor_row_price(row):
    for k in ("最新价", "当前价", "price", "收盘价"):
        if k in row:
            return _monitor_to_float(row.get(k))
    return None


def _eastmoney_quote_supplement(code):
    url = "https://push2.eastmoney.com/api/qt/stock/get"
    params = {
        "secid": get_secid(code),
        "fields": "f8,f10,f86",
        "fltt": "2",
        "invt": "2"
    }
    response = requests.get(url, params=params, headers=HEADERS, timeout=5)
    response.raise_for_status()
    data = (response.json() or {}).get("data") or {}
    fetched_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    result = {
        "turnover_rate_percent": _monitor_to_float(data.get("f8")),
        "volume_ratio": _monitor_to_float(data.get("f10")),
        "source": "东方财富实时行情",
        "data_time": (datetime.fromtimestamp(float(data["f86"]), timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S") if _monitor_to_float(data.get("f86")) else None),
        "fetched_at": fetched_at
    }
    result["turnover_rate"] = result["turnover_rate_percent"]
    if result["turnover_rate_percent"] is None and result["volume_ratio"] is None:
        raise RuntimeError("接口未返回量比或换手率字段")
    return result


def _quote_with_realtime_metrics(code, quote_row=None):
    row = dict(quote_row or quote_sina(code))
    amplitude_percent = _monitor_to_float(row.get("振幅%"))
    field_sources = {
        "amplitude_percent": "新浪实时行情/本地计算",
        "振幅%": "新浪实时行情/本地计算",
        "volume_ratio": "东方财富实时行情",
        "量比": "东方财富实时行情",
        "turnover_rate_percent": "东方财富实时行情",
        "换手率%": "东方财富实时行情"
    }

    supplement = {}
    supplement_error = None
    supplement_fetched_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        supplement = _eastmoney_quote_supplement(code)
        supplement_fetched_at = supplement.get("fetched_at") or supplement_fetched_at
    except Exception as exc:
        supplement_error = str(exc)

    volume_ratio = supplement.get("volume_ratio")
    turnover_rate_percent = supplement.get("turnover_rate_percent")
    row.update({
        "量比": volume_ratio,
        "换手率%": turnover_rate_percent,
        "振幅%": amplitude_percent,
        "volume_ratio": volume_ratio,
        "turnover_rate_percent": turnover_rate_percent,
        "turnover_rate": turnover_rate_percent,
        "amplitude_percent": amplitude_percent,
        "字段来源": field_sources,
        "数据时间": row.get("数据时间") or row.get("时间"),
        "补充数据获取时间": supplement_fetched_at
    })
    if supplement_error:
        row["补充数据错误"] = f"东方财富补充行情暂不可用：{supplement_error}"
    return row


def _pool_scope_keys(scope):
    normalized = str(scope or "").strip().lower()
    if normalized in {"holding", "hold", "holding_pool", "持仓", "持仓池"}:
        return ["holding_pool"]
    if normalized in {"watch", "watch_pool", "观察", "观察池", "关注池", "自选池"}:
        return ["watch_pool"]
    if normalized in {"all", "全部", "全部池", "持仓池和观察池"}:
        return ["holding_pool", "watch_pool"]
    raise ValueError("池范围仅支持 holding、watch 或 all")


def _pool_display_name(pool_key):
    return {
        "holding_pool": "持仓池",
        "watch_pool": "观察池",
    }.get(pool_key, pool_key)


def pool_list_command(scope):
    cfg = load_watchlist_config()
    pools = {}
    for pool_key in _pool_scope_keys(scope):
        pools[pool_key] = list(cfg.get(pool_key, []) or [])
    return {
        "ok": True,
        "scope": str(scope),
        "total_count": sum(len(items) for items in pools.values()),
        "pools": pools,
    }


def pool_snapshot_command(scope):
    cfg = load_watchlist_config()
    snapshots = []
    pools = {}
    for pool_key in _pool_scope_keys(scope):
        items = list(cfg.get(pool_key, []) or [])
        pools[pool_key] = len(items)
        for item in items:
            code = str(item.get("code") if isinstance(item, dict) else item).strip()
            name = item.get("name", code) if isinstance(item, dict) else code
            quote_row = _monitor_quote(code)
            if quote_row.get("error"):
                snapshots.append({
                    "pool": pool_key,
                    "code": code,
                    "name": name,
                    "error": quote_row.get("error"),
                })
                continue
            row = _quote_with_realtime_metrics(code, quote_row)
            snapshots.append({
                "pool": pool_key,
                "code": code,
                "name": name,
                "price": _monitor_row_price(row),
                "change_percent": _monitor_row_pct(row),
                "amplitude_percent": _monitor_to_float(row.get("amplitude_percent")),
                "volume_ratio": _monitor_to_float(row.get("volume_ratio")),
                "turnover_rate_percent": _monitor_to_float(
                    row.get("turnover_rate_percent")
                ),
                "data_time": row.get("数据时间") or row.get("时间"),
                "data_source": row.get("数据来源") or row.get("数据源"),
                "field_sources": row.get("字段来源", {}),
                "supplement_error": row.get("补充数据错误"),
            })
    return {
        "ok": True,
        "scope": str(scope),
        "total_count": len(snapshots),
        "pool_counts": pools,
        "snapshots": snapshots,
        "monitor_independent": True,
        "note": "只读查询，不依赖 monitor.enabled，不启动盯盘或触发提醒。",
    }


def _pool_number(value, digits=2, suffix=""):
    if value is None:
        return "暂缺"
    try:
        text = f"{float(value):.{digits}f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        text = str(value)
    return f"{text}{suffix}"


def format_pool_list_summary(result):
    lines = [f"池子名单，共 {result.get('total_count', 0)} 只："]
    for pool_key, items in (result.get("pools") or {}).items():
        lines.extend(["", f"{_pool_display_name(pool_key)}："])
        if not items:
            lines.append("- 暂无标的")
            continue
        for index, item in enumerate(items, 1):
            lines.append(f"{index}. {item.get('code', '-')} {item.get('name', '-')}")
    return "\n".join(lines)


def format_pool_snapshot_summary(result):
    snapshots = result.get("snapshots") or []
    lines = [f"当前池子行情快照，共 {result.get('total_count', 0)} 只。"]
    current_pool = None
    for index, row in enumerate(snapshots, 1):
        pool_key = row.get("pool")
        if pool_key != current_pool:
            current_pool = pool_key
            lines.extend(["", f"{_pool_display_name(pool_key)}："])
        lines.append(f"{index}. {row.get('code', '-')} {row.get('name', '-')}")
        if row.get("error"):
            lines.append(f"   - 行情：暂不可用（{row.get('error')}）")
            continue
        lines.extend([
            f"   - 最新价：{_pool_number(row.get('price'), 4)}",
            f"   - 涨跌幅：{_pool_number(row.get('change_percent'), 2, '%')}",
            f"   - 振幅：{_pool_number(row.get('amplitude_percent'), 2, '%')}",
            f"   - 量比：{_pool_number(row.get('volume_ratio'), 2)}",
            f"   - 换手率：{_pool_number(row.get('turnover_rate_percent'), 2, '%')}",
            f"   - 数据时间：{row.get('data_time') or '暂缺'}",
            f"   - 主行情来源：{row.get('data_source') or '暂缺'}",
        ])
        field_sources = row.get("field_sources") or {}
        lines.append(
            "   - 字段来源："
            f"振幅={field_sources.get('振幅%', '新浪实时行情/本地计算')}；"
            f"量比={field_sources.get('量比', '东方财富实时行情')}；"
            f"换手率={field_sources.get('换手率%', '东方财富实时行情')}"
        )
        if row.get("supplement_error"):
            lines.append(
                "   - 补充字段：量比/换手率暂缺；"
                "东方财富补充接口失败，新浪主行情仍正常。"
            )
    if not snapshots:
        lines.extend(["", "当前所选池子没有标的。"])
    lines.extend(["", "说明：本次为只读快照，未启动盯盘、未触发提醒。"])
    return "\n".join(lines)


def _pool_raw_output_requested(text):
    return bool(re.search(r"原始\s*JSON|调试信息", str(text or ""), re.I))


def pool_output(action, scope, request_text=""):
    result = (
        pool_list_command(scope)
        if action == "list"
        else pool_snapshot_command(scope)
    )
    if _pool_raw_output_requested(request_text):
        return result
    formatter = format_pool_list_summary if action == "list" else format_pool_snapshot_summary
    return {
        "_output_format": "text",
        "text": formatter(result),
    }


def _eastmoney_minute_bars(code):
    url = "https://push2his.eastmoney.com/api/qt/stock/trends2/get"
    params = {
        "secid": get_secid(code),
        "fields1": "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
        "iscr": "0",
        "ndays": "1"
    }
    response = requests.get(url, params=params, headers=HEADERS, timeout=5)
    response.raise_for_status()
    trends = ((response.json() or {}).get("data") or {}).get("trends") or []
    bars = []
    for row in trends:
        parts = str(row).split(",")
        if len(parts) < 7:
            continue
        bars.append({
            "time": parts[0],
            "open": _monitor_to_float(parts[1]),
            "close": _monitor_to_float(parts[2]),
            "high": _monitor_to_float(parts[3]),
            "low": _monitor_to_float(parts[4]),
            "volume": _monitor_to_float(parts[5]),
            "amount": _monitor_to_float(parts[6]),
            "average_price": _monitor_to_float(parts[7]) if len(parts) > 7 else None
        })
    return bars


# Only completed daily history is cached. Today's bar always comes from a fresh quote.
_STRATEGY_DAILY_CACHE = {}
_STRATEGY_DAILY_CACHE_SECONDS = 300


def _market_now():
    return datetime.now(timezone(timedelta(hours=8))).replace(tzinfo=None)


def _market_data_error(value, now=None, max_age_seconds=120):
    if not value:
        return "行情未提供数据时间，无法确认实时性"
    try:
        stamp = datetime.fromisoformat(str(value).strip())
        if stamp.tzinfo is not None:
            stamp = stamp.astimezone(timezone(timedelta(hours=8))).replace(tzinfo=None)
    except (TypeError, ValueError):
        return "行情数据时间格式无效"
    age = ((now or _market_now()) - stamp).total_seconds()
    if age < -30:
        return "行情数据时间晚于当前时间"
    if age > max_age_seconds:
        return f"行情已过期（约 {int(age)} 秒）"
    return None


def _strategy_daily_bars(code, current_snapshot):
    from backtest.data_provider import load_bars

    end_date = _market_now()
    today = end_date.strftime("%Y-%m-%d")
    cache_key = (str(code), today)
    cached = _STRATEGY_DAILY_CACHE.get(cache_key)
    if cached and time.monotonic() - cached["fetched_at"] < _STRATEGY_DAILY_CACHE_SECONDS:
        history = cached["bars"]
    else:
        start_date = end_date - timedelta(days=120)
        data = load_bars(code, "1d", start_date.strftime("%Y-%m-%d"), today, timeout_seconds=3)
        if data.get("cache_stale"):
            raise RuntimeError("日线缓存已过期，暂不用于实时策略")
        # New providers expose normalized shares. Old deployments need explicit
        # source-based conversion rather than mixing Sina shares and Eastmoney lots.
        volume_unit = data.get("volume_unit")
        source = data.get("original_source") if data.get("used_cache") else data.get("source")
        if volume_unit in {"share", "shares", "股"}:
            volume_factor = 1
        elif volume_unit in {"lot", "lots", "手"} or (not volume_unit and source in {"东方财富", "akshare兜底"}):
            volume_factor = 100
        elif not volume_unit and source == "新浪日K兜底":
            volume_factor = 1
        else:
            raise RuntimeError("日线成交量单位未知，暂不用于实时策略")
        history = []
        for item in data.get("bars", []):
            if str(item.get("time", ""))[:10] >= today:
                continue
            bar = dict(item)
            volume = _monitor_to_float(bar.get("volume"))
            if volume is None or volume < 0:
                raise RuntimeError("日线成交量缺失或无效")
            bar["volume"] = volume * volume_factor
            history.append(bar)
        if not history:
            raise RuntimeError("日线历史为空")
        if len(_STRATEGY_DAILY_CACHE) > 512:
            _STRATEGY_DAILY_CACHE.clear()
        _STRATEGY_DAILY_CACHE[cache_key] = {"bars": history, "fetched_at": time.monotonic()}
    bars = [dict(item) for item in history]
    if current_snapshot.get("volume") is not None and current_snapshot.get("price") is not None:
        bars.append({
            "time": today,
            "open": current_snapshot.get("open", current_snapshot["price"]),
            "close": current_snapshot["price"],
            "high": current_snapshot.get("high", current_snapshot["price"]),
            "low": current_snapshot.get("low", current_snapshot["price"]),
            "volume": current_snapshot["volume"],
            "amount": current_snapshot.get("amount"),
        })
    return bars


def _aggregate_minute_bars(bars, minutes):
    if minutes == 1:
        return list(bars)
    if minutes <= 0:
        raise ValueError("分钟周期必须为正整数")
    buckets = {}
    # Quote trend timestamps label completed minutes. 09:30 / 13:00 are
    # session-opening points, not a completed continuous-auction minute.
    for bar in bars:
        try:
            stamp = datetime.fromisoformat(str(bar.get("time", "")))
        except ValueError:
            continue
        if stamp.tzinfo is not None:
            stamp = stamp.astimezone(timezone(timedelta(hours=8))).replace(tzinfo=None)
        minute = stamp.hour * 60 + stamp.minute
        if stamp.second or stamp.microsecond:
            continue
        if 9 * 60 + 30 < minute <= 11 * 60 + 30:
            session_start = stamp.replace(hour=9, minute=30)
        elif 13 * 60 < minute <= 15 * 60:
            session_start = stamp.replace(hour=13, minute=0)
        else:
            continue
        offset = int((stamp - session_start).total_seconds() // 60)
        bucket_number = (offset - 1) // minutes
        key = (session_start, bucket_number)
        buckets.setdefault(key, []).append((stamp, bar))
    output = []
    for (session_start, bucket_number), items in sorted(buckets.items()):
        expected = [session_start + timedelta(minutes=bucket_number * minutes + index + 1) for index in range(minutes)]
        # Missing, duplicate, out-of-order and partial periods stay unavailable.
        if [stamp for stamp, _ in items] != expected:
            continue
        bucket = [bar for _, bar in items]
        if any(_monitor_to_float(bar.get(field)) is None for bar in bucket for field in ("open", "close", "high", "low", "volume", "amount")):
            continue
        output.append({
            "time": expected[-1].strftime("%Y-%m-%d %H:%M:%S"),
            "open": bucket[0]["open"], "close": bucket[-1]["close"],
            "high": max(bar["high"] for bar in bucket), "low": min(bar["low"] for bar in bucket),
            "volume": sum(bar["volume"] for bar in bucket), "amount": sum(bar["amount"] for bar in bucket),
        })
    return output


def _ema(values, period):
    if not values:
        return []
    alpha = 2.0 / (period + 1)
    output = [values[0]]
    for value in values[1:]:
        output.append(alpha * value + (1 - alpha) * output[-1])
    return output


def _indicator_values(bars):
    closes = [x["close"] for x in bars if x.get("close") is not None]
    if not closes:
        return {}

    ema12 = _ema(closes, 12)
    ema26 = _ema(closes, 26)
    dif = [a - b for a, b in zip(ema12, ema26)]
    dea = _ema(dif, 9)
    macd = [2 * (a - b) for a, b in zip(dif, dea)]

    rsi_values = []
    period = 14
    for index in range(len(closes)):
        if index < period:
            rsi_values.append(None)
            continue
        changes = [closes[i] - closes[i - 1] for i in range(index - period + 1, index + 1)]
        gains = sum(max(x, 0) for x in changes) / period
        losses = sum(max(-x, 0) for x in changes) / period
        rsi_values.append((50.0 if gains == 0 else 100.0) if losses == 0 else 100 - 100 / (1 + gains / losses))

    k_values = []
    d_values = []
    k = 50.0
    d = 50.0
    for index, bar in enumerate(bars):
        window = bars[max(0, index - 8):index + 1]
        highs = [x["high"] for x in window if x.get("high") is not None]
        lows = [x["low"] for x in window if x.get("low") is not None]
        close = bar.get("close")
        if not highs or not lows or close is None or max(highs) == min(lows):
            rsv = 50.0
        else:
            rsv = (close - min(lows)) / (max(highs) - min(lows)) * 100
        k = 2 / 3 * k + 1 / 3 * rsv
        d = 2 / 3 * d + 1 / 3 * k
        k_values.append(k)
        d_values.append(d)

    ma = {}
    for period_value in (5, 10, 20, 30, 60):
        values = []
        for index in range(len(closes)):
            if index + 1 < period_value:
                values.append(None)
            else:
                window = closes[index - period_value + 1:index + 1]
                values.append(sum(window) / period_value)
        ma[str(period_value)] = values

    return {
        "dif": [value if index >= 33 else None for index, value in enumerate(dif)],
        "dea": [value if index >= 33 else None for index, value in enumerate(dea)],
        "macd": [value if index >= 33 else None for index, value in enumerate(macd)],
        "rsi": rsi_values,
        "kdj_k": [value if index >= 8 else None for index, value in enumerate(k_values)],
        "kdj_d": [value if index >= 8 else None for index, value in enumerate(d_values)],
        "ma": ma
    }


def _strategy_condition_source(condition):
    kind = condition.get("type")
    if kind in {"macd_cross", "kdj_cross", "volume_vs_average", "indicator_threshold", "price_vs_ma"}:
        return "daily" if int(condition.get("timeframe_minutes", 1)) == 1440 else "minute"
    if kind == "quote_threshold" and condition.get("field") in {"volume_ratio", "turnover_rate", "turnover_rate_percent"}:
        return "supplement"
    return "quote"


def _strategy_required_sources(conditions=None):
    if conditions is None:
        return {"quote", "supplement", "minute", "daily"}
    return {"quote"} | {_strategy_condition_source(item) for item in conditions}


def _fetch_strategy_inputs(requirements):
    """One bounded pool per scan; independent symbols/sources cannot queue serially."""
    inputs = {code: {} for code in requirements}
    def fetch(code, source):
        started = time.monotonic()
        try:
            if source == "quote":
                value = _monitor_quote(code)
            elif source == "supplement":
                value = _eastmoney_quote_supplement(code)
            elif source == "minute":
                value = _eastmoney_minute_bars(code)
            else:
                value = _strategy_daily_bars(code, {})
            return {"value": value, "elapsed_ms": round((time.monotonic() - started) * 1000, 2)}
        except Exception as exc:
            return {"error": str(exc), "elapsed_ms": round((time.monotonic() - started) * 1000, 2)}
    jobs = [(code, source) for code, sources in requirements.items() for source in sorted(sources)]
    if not jobs:
        return inputs
    with ThreadPoolExecutor(max_workers=min(4, len(jobs))) as executor:
        futures = {executor.submit(fetch, code, source): (code, source) for code, source in jobs}
        for future in as_completed(futures):
            code, source = futures[future]
            inputs[code][source] = future.result()
    return inputs


def build_strategy_snapshot(code, conditions=None, _inputs=None):
    sources = _strategy_required_sources(conditions)
    inputs = _inputs if _inputs is not None else _fetch_strategy_inputs({code: sources})[code]
    source_names = {"quote": "新浪实时行情", "supplement": "量比/换手率行情", "minute": "分钟行情", "daily": "日线历史"}
    source_errors = {source: f"{source_names[source]}：{entry['error']}" for source, entry in inputs.items() if entry.get("error")}
    quote_row = inputs.get("quote", {}).get("value") or {}
    data_time = quote_row.get("数据时间") or quote_row.get("时间")
    quote_error = quote_row.get("error") or _market_data_error(data_time)
    if _monitor_row_price(quote_row) is None or (_monitor_row_price(quote_row) or 0) <= 0:
        quote_error = quote_error or "实时价格缺失或无效"
    if quote_error:
        source_errors["quote"] = str(quote_error)
        quote_row = {}

    supplement = inputs.get("supplement", {}).get("value") or {}
    if "supplement" in sources:
        error = _market_data_error(supplement.get("data_time"))
        if error:
            source_errors.setdefault("supplement", error)
            supplement = {}

    bars_1m = inputs.get("minute", {}).get("value") or []
    if "minute" in sources:
        error = _market_data_error(bars_1m[-1].get("time") if bars_1m else None)
        previous_time = ""
        for bar in bars_1m:
            stamp = str(bar.get("time", ""))
            if stamp <= previous_time or any(_monitor_to_float(bar.get(field)) is None for field in ("open", "high", "low", "close", "volume", "amount")):
                error = "分钟行情缺字段或时间顺序无效"
                break
            previous_time = stamp
        if error:
            source_errors.setdefault("minute", error)
            bars_1m = []

    current_snapshot = {
        "price": _monitor_row_price(quote_row),
        "volume": _monitor_to_float(quote_row.get("成交量")),
        "amount": _monitor_to_float(quote_row.get("成交额")),
    }
    bars_daily = [dict(item) for item in inputs.get("daily", {}).get("value", [])]
    if "daily" in sources:
        if "quote" in source_errors:
            source_errors["daily"] = "当日实时行情不可用，不能拼接日线策略数据"
            bars_daily = []
        elif not source_errors.get("daily") and current_snapshot["volume"] is not None:
            bars_daily.append({
                "time": _market_now().strftime("%Y-%m-%d"),
                "open": _monitor_to_float(quote_row.get("今开"), current_snapshot["price"]),
                "close": current_snapshot["price"],
                "high": _monitor_to_float(quote_row.get("最高"), current_snapshot["price"]),
                "low": _monitor_to_float(quote_row.get("最低"), current_snapshot["price"]),
                "volume": current_snapshot["volume"], "amount": current_snapshot["amount"],
            })
        elif not source_errors.get("daily"):
            source_errors["daily"] = "当日成交量缺失，不能拼接日线策略数据"
            bars_daily = []

    bars = {1: bars_1m, 3: _aggregate_minute_bars(bars_1m, 3), 5: _aggregate_minute_bars(bars_1m, 5), 1440: bars_daily}
    return {
        "code": code, "time": _market_now().strftime("%Y-%m-%d %H:%M:%S"), "data_time": data_time,
        **current_snapshot,
        "change_percent": _monitor_row_pct(quote_row),
        "volume_ratio": supplement.get("volume_ratio"),
        "turnover_rate_percent": supplement.get("turnover_rate_percent"),
        "turnover_rate": supplement.get("turnover_rate_percent"),
        "amplitude_percent": _monitor_to_float(quote_row.get("振幅%")),
        "bids": quote_row.get("买一到买五"), "asks": quote_row.get("卖一到卖五"),
        "order_imbalance": _monitor_to_float(quote_row.get("盘口委比%")),
        "buy_sell_ratio": _monitor_to_float(quote_row.get("买卖盘强弱比")),
        "field_sources": {"amplitude_percent": "新浪实时行情/本地计算", "volume_ratio": "东方财富实时行情", "turnover_rate_percent": "东方财富实时行情"},
        "bars": bars, "indicators": {minutes: _indicator_values(rows) for minutes, rows in bars.items()},
        "source_errors": source_errors, "errors": list(source_errors.values()),
        "sources": sorted(sources), "timings_ms": {source: entry.get("elapsed_ms") for source, entry in inputs.items()},
    }


def _compare(left, operator, right):
    if left is None:
        return None
    if operator == ">":
        return left > right
    if operator == ">=":
        return left >= right
    if operator == "<":
        return left < right
    if operator == "<=":
        return left <= right
    return left == right


def _last_two(values):
    usable = [x for x in values if x is not None]
    if len(usable) < 2:
        return None, None
    return usable[-2], usable[-1]


def evaluate_strategy_condition(condition, snapshot):
    condition_type = condition.get("type")
    source_error = snapshot.get("source_errors", {}).get(_strategy_condition_source(condition))
    if source_error:
        return None, source_error
    timeframe = int(condition.get("timeframe_minutes", 1))
    bars = snapshot.get("bars", {}).get(timeframe, [])
    indicators = snapshot.get("indicators", {}).get(timeframe, {})

    if condition_type == "macd_cross":
        prev_dif, current_dif = _last_two(indicators.get("dif", []))
        prev_dea, current_dea = _last_two(indicators.get("dea", []))
        if None in (prev_dif, current_dif, prev_dea, current_dea):
            return None, f"{timeframe}分钟 MACD 历史数据不足"
        if condition.get("direction") == "golden_cross":
            matched = prev_dif <= prev_dea and current_dif > current_dea
            return matched, f"{timeframe}分钟 MACD {'已金叉' if matched else '未金叉'}"
        matched = prev_dif >= prev_dea and current_dif < current_dea
        return matched, f"{timeframe}分钟 MACD {'已死叉' if matched else '未死叉'}"

    if condition_type == "kdj_cross":
        prev_k, current_k = _last_two(indicators.get("kdj_k", []))
        prev_d, current_d = _last_two(indicators.get("kdj_d", []))
        if None in (prev_k, current_k, prev_d, current_d):
            return None, f"{timeframe}分钟 KDJ 历史数据不足"
        if condition.get("direction") == "golden_cross":
            matched = prev_k <= prev_d and current_k > current_d
            return matched, f"{timeframe}分钟 KDJ {'已金叉' if matched else '未金叉'}"
        matched = prev_k >= prev_d and current_k < current_d
        return matched, f"{timeframe}分钟 KDJ {'已死叉' if matched else '未死叉'}"

    if condition_type == "volume_vs_average":
        lookback = int(condition.get("lookback", 5))
        volumes = [x.get("volume") for x in bars if x.get("volume") is not None]
        timeframe_name = "日线" if timeframe == 1440 else f"{timeframe}分钟"
        if len(volumes) < lookback + 1:
            return None, f"{timeframe_name}成交量历史不足 {lookback + 1} 根"
        average = sum(volumes[-lookback - 1:-1]) / lookback
        ratio = volumes[-1] / average if average > 0 else None
        if ratio is None:
            return None, "历史均量为零，成交量条件暂不可用"
        factor = float(condition.get("factor", 1.5))
        matched = _compare(ratio, condition.get("operator", ">="), factor)
        unit = "日" if timeframe == 1440 else "根"
        return matched, f"当前量/过去{lookback}{unit}均量={ratio:.2f}倍，要求{factor:.2f}倍"

    if condition_type == "order_book_strength":
        ratio = snapshot.get("buy_sell_ratio")
        if ratio is None or ratio <= 0:
            return None, "当前接口暂未返回有效五档盘口，该条件暂不可用"
        minimum = float(condition.get("min_ratio", 1.2))
        if condition.get("direction") == "sell":
            sell_ratio = 1 / ratio
            return _compare(sell_ratio, condition.get("operator", ">="), minimum), f"卖盘/买盘={sell_ratio:.2f}倍，要求{minimum:.2f}倍"
        return _compare(ratio, condition.get("operator", ">="), minimum), f"买盘/卖盘={ratio:.2f}倍，要求{minimum:.2f}倍"

    if condition_type == "indicator_threshold":
        values = indicators.get(condition.get("indicator"), [])
        current = next((x for x in reversed(values) if x is not None), None)
        if current is None:
            return None, f"{timeframe}分钟 {condition.get('indicator', '').upper()} 数据不足"
        target = float(condition.get("value"))
        matched = _compare(current, condition.get("operator"), target)
        return matched, f"{condition.get('indicator', '').upper()}={current:.2f}，阈值{target:.2f}"

    if condition_type == "price_vs_ma":
        period = str(int(condition.get("period", 5)))
        values = indicators.get("ma", {}).get(period, [])
        current_ma = next((x for x in reversed(values) if x is not None), None)
        price = bars[-1].get("close") if bars else snapshot.get("price")
        if current_ma is None or price is None:
            return None, f"{timeframe}分钟 MA{period} 数据不足"
        matched = _compare(price, condition.get("operator"), current_ma)
        return matched, f"价格={price:.3f}，MA{period}={current_ma:.3f}"

    if condition_type == "quote_threshold":
        field = condition.get("field")
        field_aliases = {
            "turnover_rate": "turnover_rate_percent",
            "turnover_rate_percent": "turnover_rate",
            "amplitude": "amplitude_percent"
        }
        field_names = {
            "volume_ratio": "实时量比",
            "turnover_rate": "换手率",
            "turnover_rate_percent": "换手率",
            "amplitude": "振幅",
            "amplitude_percent": "振幅",
            "change_percent": "涨跌幅",
            "price": "实时价格"
        }
        current = snapshot.get(field)
        if current is None and field in field_aliases:
            current = snapshot.get(field_aliases[field])
        if current is None:
            return None, f"当前接口暂不支持或未返回{field_names.get(field, field)}，该条件暂不可用"
        target = float(condition.get("value"))
        matched = _compare(current, condition.get("operator"), target)
        return matched, f"{field_names.get(field, field)}={current:.3f}，阈值{target:.3f}"

    return None, f"不支持的策略条件：{condition_type}"


def evaluate_strategy_rule(rule, snapshot):
    evaluations = []
    for condition in rule.get("conditions", []) or []:
        matched, reason = evaluate_strategy_condition(condition, snapshot)
        evaluations.append({
            "condition": condition,
            "matched": matched,
            "reason": reason,
        })
    values = [item.get("matched") for item in evaluations]
    logic = rule.get("logic", "all")
    if not values:
        matched = False
    elif logic == "any":
        matched = True if True in values else None if None in values else False
    else:
        matched = False if False in values else None if None in values else True
    return matched, evaluations


def _strategy_selfcheck_state():
    try:
        with open(STRATEGY_SELFCHECK_STATE_PATH, "r", encoding="utf-8") as handle:
            state = json.load(handle)
        return state if isinstance(state, dict) else {}
    except Exception:
        return {}


def _save_strategy_selfcheck_state(state):
    os.makedirs(os.path.dirname(STRATEGY_SELFCHECK_STATE_PATH), exist_ok=True)
    tmp_path = f"{STRATEGY_SELFCHECK_STATE_PATH}.tmp.{os.getpid()}"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2)
    os.replace(tmp_path, STRATEGY_SELFCHECK_STATE_PATH)


def strategy_check():
    cfg = load_watchlist_config()
    monitor = cfg.get("monitor", {}) or {}
    strategy = cfg.get("strategy_monitor", {}) or {}
    process = monitor_pid_status()
    trading = trading_time_status()
    runtime_state = load_monitor_state()
    selfcheck_state = _strategy_selfcheck_state()
    holding_pool = list(cfg.get("holding_pool", []) or [])
    watch_pool = list(cfg.get("watch_pool", []) or [])
    targets = list(strategy.get("targets") or ["holding_pool"])

    probe_item = holding_pool[0] if holding_pool else (watch_pool[0] if watch_pool else None)
    probe_code = None
    quote_status = "WARN"
    quote_note = "持仓池和观察池均为空，未执行行情连接探测。"
    if probe_item:
        probe_code = str(
            probe_item.get("code") if isinstance(probe_item, dict) else probe_item
        ).strip()
        quote_row = _monitor_quote(probe_code)
        if quote_row.get("error"):
            quote_note = f"{probe_code} 行情探测失败：{quote_row.get('error')}"
        else:
            quote_status = "OK"
            quote_note = (
                f"{probe_code} 行情可读，数据时间 "
                f"{quote_row.get('数据时间') or quote_row.get('时间') or '未知'}。"
            )

    configured = bool(strategy.get("rules"))
    unavailable = list(strategy.get("unavailable_conditions") or [])
    status = "OK"
    notes = []
    if not configured:
        status = "WARN"
        notes.append("尚未配置策略规则。")
    if not holding_pool:
        status = "WARN"
        notes.append("holding_pool 为空。")
    if quote_status != "OK" or unavailable:
        status = "WARN"
    if not trading.get("is_trading_time"):
        notes.append("非交易时间，真实策略盯盘休眠中；配置与策略引擎仍可自检。")
    elif not monitor.get("enabled") or monitor.get("mode") != "strategy":
        notes.append("当前策略盯盘未启动，本次仅执行只读自检。")

    last_alerts = runtime_state.get("last_alerts", {}) or {}
    strategy_alert_times = [
        value for key, value in last_alerts.items() if ":strategy:" in str(key)
    ]
    last_trigger = None
    if strategy_alert_times:
        try:
            last_trigger = datetime.fromtimestamp(
                max(float(value) for value in strategy_alert_times)
            ).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            last_trigger = None

    checked_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    selfcheck_state["last_check_at"] = checked_at
    _save_strategy_selfcheck_state(selfcheck_state)
    return {
        "ok": status != "FAIL",
        "status": status,
        "checked_at": checked_at,
        "trading_time": trading,
        "monitor": {
            "enabled": bool(monitor.get("enabled")),
            "mode": monitor.get("mode"),
            "running": bool(process.get("running")),
            "pid": process.get("pid") if process.get("running") else None,
        },
        "strategy": {
            "configured": configured,
            "rules_count": len(strategy.get("rules") or []),
            "targets": targets,
            "raw_text": strategy.get("raw_text", ""),
            "unavailable_conditions": unavailable,
        },
        "holding_pool": holding_pool,
        "watch_pool": watch_pool,
        "actual_targets": targets,
        "quote_connection": {
            "status": quote_status,
            "symbol": probe_code,
            "note": quote_note,
        },
        "telegram_channel": {
            "configured": bool((cfg.get("telegram") or {}).get("enabled")),
            "note": "仅检查本地通道配置，未发送测试消息。",
        },
        "last_strategy_check_at": runtime_state.get("updated_at"),
        "last_real_trigger_at": last_trigger,
        "last_simulation_at": selfcheck_state.get("last_simulation_at"),
        "cooldown": {
            "minutes": monitor.get("cooldown_minutes"),
            "tracked_alerts": len(last_alerts),
        },
        "notes": notes,
        "conclusion": (
            "；".join(notes)
            if notes
            else "策略配置、行情连接与策略引擎自检可用。"
        ),
    }


def _parse_strategy_mock_text(mock_text):
    aliases = {
        "change_pct": "change_percent",
        "涨跌幅": "change_percent",
        "量比": "volume_ratio",
        "换手率": "turnover_rate_percent",
        "振幅": "amplitude_percent",
    }
    values = {}
    for key, value in re.findall(
        r"([A-Za-z_\u4e00-\u9fff]+)\s*=\s*(-?\d+(?:\.\d+)?)",
        str(mock_text or ""),
    ):
        values[aliases.get(key, key)] = float(value)
    return values


def _strategy_mock_snapshot(symbol, preset="breakout", mock_text=""):
    values = {
        "price": 2.5,
        "change_percent": 2.5,
        "volume_ratio": 2.1,
        "turnover_rate_percent": 1.5,
        "amplitude_percent": 3.2,
        "order_imbalance": 35.0,
        "buy_sell_ratio": 1.5,
    }
    if preset == "volume_spike":
        values.update({"change_percent": 1.2, "volume_ratio": 2.5})
    values.update(_parse_strategy_mock_text(mock_text))

    daily_volumes = [100.0] * 5 + [values.get("volume_ratio", 2.1) * 100.0]
    daily_bars = [
        {
            "time": f"mock-{index + 1}",
            "open": values["price"] * 0.99,
            "high": values["price"] * 1.01,
            "low": values["price"] * 0.98,
            "close": values["price"],
            "volume": volume,
            "amount": volume * values["price"],
        }
        for index, volume in enumerate(daily_volumes)
    ]
    minute_bars = [dict(row) for row in daily_bars]
    indicators = {
        "dif": [-0.02, 0.03],
        "dea": [0.0, 0.01],
        "macd": [-0.04, 0.04],
        "kdj_k": [45.0, 58.0],
        "kdj_d": [50.0, 53.0],
        "rsi": [52.0, 61.0],
        "ma": {
            "5": [values["price"] * 0.98],
            "10": [values["price"] * 0.97],
            "20": [values["price"] * 0.96],
            "30": [values["price"] * 0.95],
            "60": [values["price"] * 0.94],
        },
    }
    return {
        "code": symbol,
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        **values,
        "bars": {1: minute_bars, 3: minute_bars, 5: minute_bars, 1440: daily_bars},
        "indicators": {1: indicators, 3: indicators, 5: indicators, 1440: indicators},
        "errors": [],
        "sources": ["mock"],
    }


def strategy_simulate(symbol, preset="breakout", mock_text="", send_test=False):
    symbol = str(symbol or "510050").strip()
    cfg = load_watchlist_config()
    strategy = cfg.get("strategy_monitor", {}) or {}
    snapshot = _strategy_mock_snapshot(symbol, preset, mock_text)
    rule_results = []
    would_alert = False
    for rule in strategy.get("rules", []) or []:
        if rule.get("code") and str(rule.get("code")) != symbol:
            continue
        matched, evaluations = evaluate_strategy_rule(rule, snapshot)
        would_alert = would_alert or matched is True
        rule_results.append({
            "rule_id": rule.get("id"),
            "rule": rule.get("raw_text"),
            "matched": matched is True,
            "matched_conditions": [
                item["reason"] for item in evaluations if item["matched"] is True
            ],
            "unmatched_conditions": [
                item["reason"] for item in evaluations if item["matched"] is not True
            ],
            "evaluations": evaluations,
        })

    reminder = (
        f"【策略盯盘模拟验证】\n标的：{symbol}\n"
        "这是一条测试消息，不代表真实行情，不构成交易建议。\n"
        f"判断结果：{'会触发提醒' if would_alert else '不会触发提醒'}"
    )
    send_result = None
    if send_test:
        send_result = send_telegram_message(reminder)

    state = _strategy_selfcheck_state()
    simulated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    state["last_simulation_at"] = simulated_at
    state["last_simulation_symbol"] = symbol
    state["last_simulation_result"] = bool(would_alert)
    _save_strategy_selfcheck_state(state)
    return {
        "ok": bool(rule_results),
        "simulation": True,
        "disclaimer": "【模拟验证，不代表真实行情，不构成交易建议】",
        "simulated_at": simulated_at,
        "symbol": symbol,
        "preset": preset,
        "mock_fields": {
            key: snapshot.get(key)
            for key in (
                "price", "change_percent", "volume_ratio",
                "turnover_rate_percent", "amplitude_percent",
                "order_imbalance", "buy_sell_ratio",
            )
        },
        "rules": rule_results,
        "would_alert": would_alert,
        "reminder_text": reminder,
        "send_test_requested": bool(send_test),
        "send_result": send_result,
        "note": (
            "未找到适用于该标的的已保存策略规则。"
            if not rule_results else
            "仅运行本地模拟判断；未读取真实行情、未启动盯盘。"
        ),
    }


def _gateway_probe_for_strategy_test():
    try:
        result = subprocess.run(
            ["openclaw", "health", "--json"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=8,
            check=False,
        )
        return {
            "ok": result.returncode == 0,
            "returncode": result.returncode,
            "note": "仅探测本地 Gateway 健康接口，未读取或输出认证信息。",
        }
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": type(exc).__name__}


def strategy_test(rule_id, symbol=None, live_probe=True):
    strategy, rule = _find_strategy_rule(rule_id)
    if rule is None:
        return {"ok": False, "error": f"未找到策略：{rule_id}"}
    symbol = str(symbol or rule.get("code") or "510050")
    snapshot = _strategy_mock_snapshot(symbol, preset="breakout")
    simulated_result, evaluations = evaluate_strategy_rule(rule, snapshot)
    simulated_match = simulated_result is True
    loaded_hash = _strategy_hash(strategy)
    reloaded = load_watchlist_config().get("strategy_monitor", {}) or {}
    reload_hash = _strategy_hash(reloaded)
    gateway = _gateway_probe_for_strategy_test() if live_probe else {
        "ok": True,
        "skipped": True,
        "note": "离线测试模式跳过 Gateway 探测。",
    }
    verification_ok = loaded_hash == reload_hash and bool(evaluations)
    return {
        "ok": verification_ok and gateway.get("ok", False),
        "test_mode": "simulation_only",
        "disclaimer": "模拟行情验收，不代表真实行情，不产生真实交易信号。",
        "server_connection": gateway,
        "parse_result": {"ok": True, "rule": rule},
        "write_result": {
            "ok": True,
            "skipped": True,
            "reason": "验收已保存配置，不重复写入",
        },
        "reload_result": {"ok": loaded_hash == reload_hash, "hash": reload_hash},
        "effective_status": _strategy_runtime_status(reloaded),
        "verification_result": {
            "ok": verification_ok,
            "simulated_match": simulated_match,
            "evaluations": evaluations,
            "real_signal_emitted": False,
        },
    }


def build_strategy_alerts_once():
    cfg = load_watchlist_config()
    monitor = cfg.get("monitor", {}) or {}
    strategy = cfg.get("strategy_monitor", {}) or {}
    holdings = cfg.get("holding_pool", []) or []
    watch_items = cfg.get("watch_pool", []) or []
    rules = strategy.get("rules", []) or []
    strategy_targets = strategy.get("targets") or ["holding_pool"]

    if not monitor.get("enabled") or monitor.get("mode") != "strategy":
        return {"ok": True, "monitor_enabled": False, "mode": monitor.get("mode"), "alerts": [], "alerts_count": 0}
    t_status = trading_time_status()
    if monitor.get("market_hours_only", True) and not t_status.get("is_trading_time"):
        next_open = t_status.get("next_open")
        return {
            "ok": True,
            "monitor_enabled": True,
            "mode": "strategy",
            "trading_time": t_status,
            "checked_count": 0,
            "alerts": [],
            "alerts_count": 0,
            "message": (
                f"{t_status.get('message', '当前不在A股交易时间。')}"
                "策略盯盘当前不扫描行情、不触发提醒；"
                + (
                    f"按工作日规则将在 {next_open} 后恢复扫描。"
                    if next_open else
                    "将在下一个交易时段恢复扫描。"
                )
            )
        }
    if not rules:
        return {
            "ok": True,
            "monitor_enabled": True,
            "mode": "strategy",
            "alerts": [],
            "alerts_count": 0,
            "message": "策略盯盘已启动，但当前没有策略。请先设置策略盯盘。"
        }

    started = time.monotonic()
    state = load_monitor_state()
    active = state.setdefault("strategy_active", {})
    alerts, checked, suppressed, runtime_unavailable = [], [], [], []
    _, profile = _get_monitor_profile(cfg)
    cooldown_minutes = float(profile.get("cooldown_minutes", 1))
    rule_targets = []
    conditions_by_code = {}
    pools = []
    if "holding_pool" in strategy_targets:
        pools.extend(("holding_pool", item) for item in holdings)
    if "watch_pool" in strategy_targets:
        pools.extend(("watch_pool", item) for item in watch_items)
    for rule in rules:
        targets = []
        seen_codes = set()
        for pool_key, item in pools:
            code = str(item.get("code") if isinstance(item, dict) else item).strip()
            if code and code not in seen_codes and (not rule.get("code") or rule.get("code") == code):
                seen_codes.add(code)
                targets.append((code, item.get("name", code) if isinstance(item, dict) else code, pool_key))
        # Explicit symbols do not silently depend on membership in a watch/holding pool.
        if rule.get("code") and str(rule["code"]) not in seen_codes:
            code = str(rule["code"])
            targets.append((code, code, "explicit_symbols"))
        rule_targets.append((rule, targets))
        for code, _, _ in targets:
            conditions_by_code.setdefault(code, []).extend(rule.get("conditions", []))

    inputs = _fetch_strategy_inputs({code: _strategy_required_sources(conditions) for code, conditions in conditions_by_code.items()})
    snapshot_cache = {code: build_strategy_snapshot(code, conditions, _inputs=inputs[code]) for code, conditions in conditions_by_code.items()}
    scan_day = _market_now().strftime("%Y%m%d")
    for rule, targets in rule_targets:
        for code, name, pool_key in targets:
            snapshot = snapshot_cache[code]
            try:
                matched_result, evaluations = evaluate_strategy_rule(rule, snapshot)
            except (TypeError, ValueError, KeyError) as exc:
                matched_result, evaluations = None, [{"matched": None, "reason": f"策略配置或行情无效：{exc}"}]
            matched = matched_result is True
            for evaluation in evaluations:
                if evaluation.get("matched") is None:
                    runtime_unavailable.append(f"{code}: {evaluation.get('reason')}")
            active_key = f"{scan_day}:{rule.get('id')}:{code}"
            was_active = bool(active.get(active_key))
            # Unknown data is not a false condition; preserve a delivered activation.
            # True is committed only after a successful notification, so failure retries.
            if matched_result is False:
                active[active_key] = False
            checked.append({
                "code": code, "name": name, "rule_id": rule.get("id"), "matched": matched,
                "available": matched_result is not None,
                "amplitude_percent": snapshot.get("amplitude_percent"), "volume_ratio": snapshot.get("volume_ratio"),
                "turnover_rate_percent": snapshot.get("turnover_rate_percent"), "data_time": snapshot.get("data_time"),
                "field_sources": snapshot.get("field_sources", {}), "evaluations": evaluations,
                "data_errors": snapshot.get("errors", []), "timings_ms": snapshot.get("timings_ms", {}),
            })
            if matched and not was_active:
                reason = _cooldown_suppressed_reason(state, code, f"strategy:{rule.get('id')}", cooldown_minutes)
                if reason:
                    suppressed.append({"code": code, "rule_id": rule.get("id"), "suppressed_reason": reason})
                    continue
                side = rule.get("side", "alert")
                label = {"buy": "买入", "sell": "卖出"}.get(side, "条件")
                alerts.append({
                    "_cooldown_key": _alert_key(code, f"strategy:{rule.get('id')}"),
                    "_strategy_active_key": active_key,
                    "level": "important", "mode": "strategy", "profile_name": "策略盯盘",
                    "pool": "指定标的" if pool_key == "explicit_symbols" else _monitor_target_name(pool_key),
                    "code": code, "name": name, "side": side, "rule": rule.get("raw_text"),
                    "message": f"{name}({code}) 满足{label}策略。",
                    "reasons": [x["reason"] for x in evaluations if x.get("matched") is True],
                })
    state["strategy_last_unavailable"] = sorted(set(runtime_unavailable))
    state["last_scan_at"] = _market_now().strftime("%Y-%m-%d %H:%M:%S")
    state["last_quote_at"] = max((item["data_time"] for item in checked if item.get("data_time")), default=None)
    state["last_checked_symbols"] = [{"code": item["code"], "name": item["name"]} for item in checked]
    state["last_suppressed"] = suppressed[-50:]
    state["last_alert_candidates"] = alerts[-50:]
    saved = save_monitor_state(state)
    return {
        "ok": saved, "monitor_enabled": True, "mode": "strategy", "profile_name": "策略盯盘",
        "interval_seconds": profile.get("interval_seconds", 1), "cooldown_minutes": cooldown_minutes,
        "targets": list(strategy_targets), "checked_count": len(checked), "checked": checked,
        "alerts_count": len(alerts), "alerts": alerts, "suppressed": suppressed,
        "unavailable_conditions": strategy.get("unavailable_conditions", []),
        "runtime_unavailable": state["strategy_last_unavailable"],
        "last_scan_at": state["last_scan_at"], "last_quote_at": state["last_quote_at"],
        "scan_duration_ms": round((time.monotonic() - started) * 1000, 2),
    }


def _alert_key(code, rule):
    today = _market_now().strftime("%Y%m%d")
    return f"{today}:{code}:{rule}"


def _should_emit_alert(state, code, rule, cooldown_minutes=20):
    key = _alert_key(code, rule)
    last = state.get("last_alerts", {}).get(key)
    now_ts = time.time()

    if last:
        try:
            if now_ts - float(last) < cooldown_minutes * 60:
                return False
        except Exception:
            pass

    return True


def _cooldown_suppressed_reason(state, code, rule, cooldown_minutes=20):
    key = _alert_key(code, rule)
    last = state.get("last_alerts", {}).get(key)
    if not last:
        return None
    try:
        elapsed = time.time() - float(last)
    except Exception:
        return None
    remaining = cooldown_minutes * 60 - elapsed
    if remaining > 0:
        return f"cooldown 压制，剩余约 {int(remaining // 60) + 1} 分钟"
    return None


def _monitor_symbol_kind(code):
    code = str(code or "")
    if code.startswith(("5", "15", "16", "18")):
        return "etf"
    return "stock"


def _normal_monitor_thresholds(pool_name, code, profile):
    kind = _monitor_symbol_kind(code)
    if pool_name == "holding_pool":
        up = float(profile.get("hold_up_percent", 1))
        down = float(profile.get("hold_down_percent", -1))
        amplitude = 1.5 if kind == "etf" else 3.0
        volume_ratio = 1.8 if kind == "etf" else 2.0
    else:
        up = float(profile.get("watch_up_percent", 2))
        down = float(profile.get("watch_down_percent", -2))
        amplitude = 2.0 if kind == "etf" else 4.0
        volume_ratio = 2.0 if kind == "etf" else 2.5
    return {
        "kind": kind,
        "up": up,
        "down": down,
        "amplitude": amplitude,
        "volume_ratio": volume_ratio,
    }


def _append_normal_alert(alerts, suppressed, state, alert, cooldown_minutes):
    code = alert.get("code")
    rule = alert.get("_rule_key")
    reason = _cooldown_suppressed_reason(state, code, rule, cooldown_minutes)
    if reason:
        suppressed.append({
            "code": code,
            "name": alert.get("name"),
            "pool": alert.get("pool"),
            "rule": alert.get("rule"),
            "suppressed_reason": reason,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
        return False
    alert["_cooldown_key"] = _alert_key(code, rule)
    alert.pop("_rule_key", None)
    alerts.append(alert)
    return True


def _commit_alert_cooldowns(alerts, sent_at=None):
    keys = {
        alert.get("_cooldown_key")
        for alert in (alerts or [])
        if isinstance(alert, dict) and alert.get("_cooldown_key")
    }
    if not keys:
        return True

    try:
        with _locked_file(MONITOR_STATE_LOCK_PATH, exclusive=True):
            state = _read_monitor_state_unlocked()
            last_alerts = state.setdefault("last_alerts", {})
            timestamp = float(sent_at if sent_at is not None else time.time())
            for key in keys:
                last_alerts[key] = timestamp
            active = state.setdefault("strategy_active", {})
            for alert in alerts or []:
                if isinstance(alert, dict) and alert.get("_strategy_active_key"):
                    active[alert["_strategy_active_key"]] = True
            _write_monitor_state_unlocked(state)
        return True
    except Exception:
        return False


def _get_monitor_profile(cfg):
    monitor = cfg.get("monitor", {}) or {}
    profiles = cfg.get("monitor_profiles", {}) or {}
    mode = monitor.get("mode", "normal")

    profile = profiles.get(mode) or profiles.get("normal") or {
        "name": "普通盯盘",
        "interval_seconds": 20,
        "targets": ["holding_pool", "watch_pool"],
        "cooldown_minutes": 8,
        "watch_up_percent": 2,
        "watch_down_percent": -2,
        "hold_up_percent": 1,
        "hold_down_percent": -1,
        "fast_move_percent": 1,
        "use_llm": False
    }

    return mode, profile



def _next_weekday_open(now):
    morning_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
    afternoon_open = now.replace(hour=13, minute=0, second=0, microsecond=0)
    if now.weekday() < 5:
        if now < morning_open:
            return morning_open
        if now < afternoon_open and now.hour * 60 + now.minute > 11 * 60 + 30:
            return afternoon_open
    candidate = morning_open
    candidate += timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate


def is_a_share_trading_time(now=None):
    """
    A股连续竞价交易时间判断：
    周一至周五 09:30-11:30、13:00-15:00。
    暂不判断法定节假日。
    """
    now = now or _market_now()

    if now.weekday() >= 5:
        return False

    hm = now.hour * 60 + now.minute
    morning_start = 9 * 60 + 30
    morning_end = 11 * 60 + 30
    afternoon_start = 13 * 60
    afternoon_end = 15 * 60

    return (morning_start <= hm <= morning_end) or (afternoon_start <= hm <= afternoon_end)


def trading_time_status(now=None):
    now = now or _market_now()
    weekday_names = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
    weekday_name = weekday_names[now.weekday()]
    hm = now.hour * 60 + now.minute
    is_trading_day = now.weekday() < 5
    is_trading_time = is_a_share_trading_time(now)

    if not is_trading_day:
        session = "non_trading_day"
        message = f"今天是{weekday_name}，属于非交易日。"
    elif hm < 9 * 60 + 30:
        session = "pre_open"
        message = "今天是交易日，当前尚未开盘。"
    elif 11 * 60 + 30 < hm < 13 * 60:
        session = "lunch_break"
        message = "今天是交易日，当前处于午间休市。"
    elif hm > 15 * 60:
        session = "after_close"
        message = "今天是交易日，当前已经收盘。"
    else:
        session = "trading"
        message = "当前处于A股连续竞价交易时段。"

    next_open = None if is_trading_time else _next_weekday_open(now)
    return {
        "now": now.strftime("%Y-%m-%d %H:%M:%S"),
        "weekday": weekday_name,
        "is_trading_day": is_trading_day,
        "is_trading_time": is_trading_time,
        "session": session,
        "message": message,
        "next_open": next_open.strftime("%Y-%m-%d %H:%M:%S") if next_open else None,
        "rule": "A股交易时间：周一至周五 09:30-11:30、13:00-15:00；暂不判断法定节假日。"
    }


def _monitor_start_message(label, start_result, verify_result, trading_status):
    if not verify_result.get("verified"):
        return f"{label}启动后未通过后台进程验证，请查看 start/verify 详情。"

    pid = (verify_result.get("pid_status") or {}).get("pid") or start_result.get("pid")
    prefix = f"{label}已在后台启动" + (f"（PID {pid}）" if pid else "") + "。"
    if trading_status.get("is_trading_time"):
        return prefix + "当前处于交易时段，将按已保存策略扫描并触发提醒。"

    detail = trading_status.get("message", "当前不在A股交易时间。")
    next_open = trading_status.get("next_open")
    resume = (
        f"按工作日规则将在 {next_open} 后恢复扫描"
        if next_open else
        "将在下一个交易时段恢复扫描"
    )
    return (
        f"{prefix}{detail}当前不会扫描行情或触发提醒，后台进程保持等待；"
        f"{resume}。法定节假日暂未纳入日历判断。"
    )


def build_monitor_alerts_once():
    """
    单次盯盘检查。
    第一版只按涨跌幅阈值判断，不调用大模型。
    """
    cfg = load_watchlist_config()
    monitor = cfg.get("monitor", {}) or {}
    enabled = bool(monitor.get("enabled", False))

    mode, profile = _get_monitor_profile(cfg)
    if mode == "strategy":
        return build_strategy_alerts_once()
    market_hours_only = bool(monitor.get("market_hours_only", True))
    t_status = trading_time_status()

    if not enabled:
        return {
            "ok": True,
            "monitor_enabled": False,
            "mode": mode,
            "profile_name": profile.get("name", mode),
            "trading_time": t_status,
            "message": "实时盯盘当前关闭；可用 monitor on 或“启动普通盯盘”打开。",
            "alerts": []
        }

    if market_hours_only and not t_status.get("is_trading_time"):
        next_open = t_status.get("next_open")
        return {
            "ok": True,
            "monitor_enabled": True,
            "mode": mode,
            "profile_name": profile.get("name", mode),
            "market_hours_only": True,
            "trading_time": t_status,
            "checked_count": 0,
            "alerts_count": 0,
            "alerts": [],
            "message": (
                f"{t_status.get('message', '当前不在A股交易时间。')}"
                "已跳过实时盯盘提醒，不会根据收盘或非实时行情触发 alerts；"
                + (
                    f"按工作日规则将在 {next_open} 后恢复扫描。"
                    if next_open else
                    "将在下一个交易时段恢复扫描。"
                )
            )
        }

    state = load_monitor_state()

    watch_items = cfg.get("watch_pool", []) or []
    holding_items = cfg.get("holding_pool", []) or []

    cooldown_minutes = int(profile.get("cooldown_minutes", 20) or 20)

    targets = set(profile.get("targets", ["holding_pool", "watch_pool"]))

    alerts = []
    checked = []
    candidates = []
    suppressed = []
    last_quote_at = None
    codes = set()
    for pool_name, items in (("holding_pool", holding_items), ("watch_pool", watch_items)):
        if pool_name in targets:
            codes.update(str(item.get("code") if isinstance(item, dict) else item).strip() for item in items)
    quote_inputs = _fetch_strategy_inputs({code: {"quote", "supplement"} for code in codes if code})
    checked_codes = set()

    def check_item(item, pool_name):
        nonlocal last_quote_at
        code = item.get("code") if isinstance(item, dict) else str(item)
        code = str(code).strip()
        if not code:
            return

        if code in checked_codes:
            return
        checked_codes.add(code)
        name = item.get("name", code) if isinstance(item, dict) else code
        row = quote_inputs[code].get("quote", {}).get("value") or {"error": quote_inputs[code].get("quote", {}).get("error", "行情缺失")}
        supplement = quote_inputs[code].get("supplement", {}).get("value") or {}
        supplement_error = _market_data_error(supplement.get("data_time"))
        display_row = dict(row)
        display_row.update({
            "amplitude_percent": _monitor_to_float(row.get("振幅%")),
            "volume_ratio": None if supplement_error else supplement.get("volume_ratio"),
            "turnover_rate_percent": None if supplement_error else supplement.get("turnover_rate_percent"),
        })

        pct = _monitor_row_pct(row)
        price = _monitor_row_price(row)
        amplitude = _monitor_to_float(display_row.get("amplitude_percent"))
        volume_ratio = _monitor_to_float(display_row.get("volume_ratio"))
        turnover_rate = _monitor_to_float(display_row.get("turnover_rate_percent"))
        data_time = display_row.get("数据时间") or display_row.get("时间")
        if data_time:
            last_quote_at = data_time

        checked.append({
            "pool": pool_name,
            "code": code,
            "name": name,
            "pct": pct,
            "price": price,
            "amplitude_percent": amplitude,
            "volume_ratio": volume_ratio,
            "turnover_rate_percent": turnover_rate,
            "data_time": data_time,
            "field_sources": display_row.get("字段来源", {}),
            "error": row.get("error")
        })

        quote_error = row.get("error") or _market_data_error(data_time)
        if price is None or price <= 0:
            quote_error = quote_error or "实时价格缺失或无效"
        if quote_error or pct is None:
            suppressed.append({
                "code": code,
                "name": name,
                "pool": _monitor_target_name(pool_name),
                "rule": "行情检查",
                "suppressed_reason": quote_error or "行情数据缺失",
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })
            return

        threshold = _normal_monitor_thresholds(pool_name, code, profile)
        pool_text = _monitor_target_name(pool_name)
        priority = "important" if pool_name == "holding_pool" else "normal"
        if pct >= threshold["up"]:
            candidates.append({
                "code": code, "name": name, "pool": pool_text,
                "rule": "单标的快速上涨提醒",
                "reason": f"涨跌幅 {pct:.2f}% >= 阈值 {threshold['up']:.2f}%",
            })
            _append_normal_alert(alerts, suppressed, state, {
                "_rule_key": f"{pool_name}:fast_up",
                "level": priority,
                "mode": mode,
                "profile_name": profile.get("name", mode),
                "pool": pool_text,
                "code": code,
                "name": name,
                "rule": "单标的快速上涨提醒",
                "message": f"{pool_text}上涨异动：{name}({code}) 当前涨跌幅约 {pct:.2f}%，触发上涨阈值 {threshold['up']:.2f}%。",
                "suggestion": "持仓池优先看是否需要止盈/风控；观察池先确认板块共振，避免脉冲追高。",
                "reasons": [f"涨跌幅 {pct:.2f}% >= {threshold['up']:.2f}%"],
            }, cooldown_minutes)

        if pct <= threshold["down"]:
            candidates.append({
                "code": code, "name": name, "pool": pool_text,
                "rule": "单标的快速下跌提醒",
                "reason": f"涨跌幅 {pct:.2f}% <= 阈值 {threshold['down']:.2f}%",
            })
            _append_normal_alert(alerts, suppressed, state, {
                "_rule_key": f"{pool_name}:fast_down",
                "level": "important",
                "mode": mode,
                "profile_name": profile.get("name", mode),
                "pool": pool_text,
                "code": code,
                "name": name,
                "rule": "单标的快速下跌提醒",
                "message": f"{pool_text}下跌风险：{name}({code}) 当前涨跌幅约 {pct:.2f}%，触发下跌阈值 {threshold['down']:.2f}%。",
                "suggestion": "持仓池优先检查是否弱于板块、是否放量下跌；观察池暂缓追踪买点。",
                "reasons": [f"涨跌幅 {pct:.2f}% <= {threshold['down']:.2f}%"],
            }, cooldown_minutes)

        if amplitude is not None and amplitude >= threshold["amplitude"]:
            candidates.append({
                "code": code, "name": name, "pool": pool_text,
                "rule": "振幅异常提醒",
                "reason": f"振幅 {amplitude:.2f}% >= 阈值 {threshold['amplitude']:.2f}%",
            })
            _append_normal_alert(alerts, suppressed, state, {
                "_rule_key": f"{pool_name}:amplitude",
                "level": priority,
                "mode": mode,
                "profile_name": profile.get("name", mode),
                "pool": pool_text,
                "code": code,
                "name": name,
                "rule": "振幅异常提醒",
                "message": f"{pool_text}振幅异常：{name}({code}) 当前振幅约 {amplitude:.2f}%，超过阈值 {threshold['amplitude']:.2f}%。",
                "suggestion": "检查是否冲高回落或急跌反抽，注意盘口承接变化。",
                "reasons": [f"振幅 {amplitude:.2f}% >= {threshold['amplitude']:.2f}%"],
            }, cooldown_minutes)

        if volume_ratio is not None and volume_ratio >= threshold["volume_ratio"]:
            candidates.append({
                "code": code, "name": name, "pool": pool_text,
                "rule": "放量异常提醒",
                "reason": f"量比 {volume_ratio:.2f} >= 阈值 {threshold['volume_ratio']:.2f}",
            })
            _append_normal_alert(alerts, suppressed, state, {
                "_rule_key": f"{pool_name}:volume_ratio",
                "level": priority,
                "mode": mode,
                "profile_name": profile.get("name", mode),
                "pool": pool_text,
                "code": code,
                "name": name,
                "rule": "放量异常提醒",
                "message": f"{pool_text}放量异常：{name}({code}) 当前量比约 {volume_ratio:.2f}，超过阈值 {threshold['volume_ratio']:.2f}。",
                "suggestion": "结合涨跌方向判断是资金攻击还是放量出逃。",
                "reasons": [f"量比 {volume_ratio:.2f} >= {threshold['volume_ratio']:.2f}"],
            }, cooldown_minutes)

        state.setdefault("last_quotes", {})[code] = {
            "name": name,
            "pct": pct,
            "price": price,
            "pool": pool_name,
            "mode": mode,
            "amplitude_percent": amplitude,
            "volume_ratio": volume_ratio,
            "turnover_rate_percent": turnover_rate,
            "data_time": data_time,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "ts": time.time()
        }

    if "holding_pool" in targets:
        for item in holding_items:
            check_item(item, "holding_pool")

    if "watch_pool" in targets:
        for item in watch_items:
            check_item(item, "watch_pool")

    now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    state["last_scan_at"] = now_text
    state["last_quote_at"] = last_quote_at
    state["last_checked_symbols"] = [
        {"code": item.get("code"), "name": item.get("name"), "pool": item.get("pool")}
        for item in checked
    ]
    state["last_alert_candidates"] = candidates[-50:]
    state["last_suppressed"] = suppressed[-50:]
    saved = save_monitor_state(state)

    return {
        "ok": saved,
        "monitor_enabled": True,
        "mode": mode,
        "profile_name": profile.get("name", mode),
        "interval_seconds": profile.get("interval_seconds"),
        "cooldown_minutes": cooldown_minutes,
        "targets": list(targets),
        "checked_count": len(checked),
        "checked": checked,
        "last_scan_at": now_text,
        "last_quote_at": last_quote_at,
        "alert_candidates_count": len(candidates),
        "alert_candidates": candidates,
        "suppressed_count": len(suppressed),
        "suppressed": suppressed,
        "alerts_count": len(alerts),
        "alerts": alerts,
        "note": "普通盯盘沿用原严密盯盘阈值；本地判断，不调用大模型。"
    }



def _safe_error_summary(error):
    text = str(error or "")
    text = re.sub(r"bot[0-9A-Za-z:_-]+", "bot***", text)
    text = re.sub(r"Bearer\s+[0-9A-Za-z._-]+", "Bearer ***", text, flags=re.I)
    text = re.sub(r"token[=:]\s*[^\\s,]+", "token=***", text, flags=re.I)
    return text[:240] if text else ""


def _notification_targets():
    """Read non-secret delivery targets injected by the OpenClaw plugin config."""
    raw = os.environ.get("LOBSTER_QUANT_NOTIFY_TARGETS", "{}").strip() or "{}"
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def send_openclaw_message(channel, text):
    """Delegate delivery to an already configured OpenClaw channel extension."""
    normalized = _normalize_channel_name(channel)
    delivery = _notification_targets().get(normalized)
    if isinstance(delivery, str):
        delivery = {"target": delivery}
    if not isinstance(delivery, dict) or not str(delivery.get("target") or "").strip():
        return {
            "ok": False,
            "skipped": True,
            "channel": normalized,
            "error": "未配置通知目标；为防止误发，本次已跳过。",
        }

    provider_channel = {
        "weixin": "openclaw-weixin",
        "telegram": "telegram",
        "qq": "qqbot",
    }.get(normalized, normalized)
    cmd = [
        "openclaw", "message", "send",
        "--channel", provider_channel,
        "--target", str(delivery["target"]),
        "--message", str(text),
        "--json",
    ]
    if delivery.get("account"):
        cmd[3:3] = ["--account", str(delivery["account"])]
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=25,
        )
        raw = (proc.stdout or "").strip()
        payload = {}
        if raw:
            try:
                payload = json.loads(raw)
            except Exception:
                payload = {"raw": raw[:500]}
        if proc.returncode != 0:
            return {
                "ok": False,
                "channel": normalized,
                "error": _safe_error_summary(proc.stderr or proc.stdout),
                "returncode": proc.returncode,
            }
        return {
            "ok": True,
            "channel": normalized,
            "provider_channel": provider_channel,
            "target_masked": str(delivery["target"])[:4] + "***",
            "response": payload,
        }
    except Exception as exc:
        return {"ok": False, "channel": normalized, "error": _safe_error_summary(exc)}


def send_telegram_message(text):
    return send_openclaw_message("telegram", text)


def send_weixin_message(text):
    return send_openclaw_message("weixin", text)


def send_monitor_notification(text, channels=None, source_channel=None):
    channels = _normalize_notify_channels(
        {"notify_channels": channels or []},
        include_fallback=not bool(channels),
    )
    results = []
    for channel in channels:
        result = send_openclaw_message(channel, text)
        results.append(result)

    ok = any(item.get("ok") for item in results)
    payload = {
        "ok": ok,
        "partial": ok and any(not item.get("ok") for item in results),
        "successful_channels": [item.get("channel") for item in results if item.get("ok")],
        "failed_channels": [item.get("channel") for item in results if not item.get("ok")],
        "delivery_policy": "any_channel_success",
        "source_channel": _normalize_channel_name(source_channel),
        "notify_channels": channels,
        "results": results,
        "sent_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        state = load_monitor_state()
        state["last_notify_result"] = payload
        save_monitor_state(state)
        cfg = load_watchlist_config()
        cfg.setdefault("monitor", {})["last_notify_result"] = payload
        save_watchlist_config(cfg)
    except Exception:
        pass
    return payload


def format_monitor_alerts_for_telegram(result):
    """
    把 monitor_loop 的 alerts 转成适合 Telegram 的简短文本。
    """
    alerts = result.get("alerts", []) or []
    if not alerts:
        return ""

    mode = result.get("profile_name") or result.get("mode") or "盯盘"
    now = datetime.now().strftime("%H:%M:%S")

    lines = [
        f"🚨【{mode}提醒】{now}",
        f"触发数量：{len(alerts)} 条",
        ""
    ]

    for i, a in enumerate(alerts[:10], 1):
        lines.append(f"{i}. {a.get('pool', '')}｜{a.get('name', '')} {a.get('code', '')}")
        lines.append(f"   {a.get('message', '')}")
        suggestion = a.get("suggestion")
        if suggestion:
            lines.append(f"   建议：{suggestion}")
        reasons = a.get("reasons") or []
        for reason in reasons:
            lines.append(f"   条件：{reason}")
        lines.append("")

    if len(alerts) > 10:
        lines.append(f"还有 {len(alerts) - 10} 条提醒未展示。")

    return "\n".join(lines).strip()


def _monitor_channel_arg(args, default="weixin"):
    channel = default
    index = 0
    while index < len(args):
        value = args[index]
        if value in {"--channel", "-c"} and index + 1 < len(args):
            channel = args[index + 1]
            index += 2
        else:
            index += 1
    return _normalize_channel_name(channel)


def _format_monitor_diagnose(result):
    status = result.get("status", {}) or {}
    monitor = status.get("monitor", {}) or {}
    process = status.get("process", {}) or {}
    trading = status.get("trading_time", {}) or {}
    scan = result.get("scan_result", {}) or {}
    checked = scan.get("checked") or result.get("last_checked") or []
    candidates = scan.get("alert_candidates") or result.get("last_alert_candidates") or []
    suppressed = scan.get("suppressed") or result.get("last_suppressed") or []
    notify_channels = _normalize_notify_channels(monitor)
    channel_display = {"weixin": "微信", "telegram": "Telegram", "qq": "QQ"}
    delivery = result.get("last_notify_result") or {}
    channel_results = delivery.get("results") or []
    succeeded = [channel_display.get(item.get("channel"), item.get("channel")) for item in channel_results if item.get("ok")]
    failed = [channel_display.get(item.get("channel"), item.get("channel")) for item in channel_results if not item.get("ok")]
    delivery_summary = "暂无记录"
    if succeeded and failed:
        delivery_summary = f"部分成功：{', '.join(succeeded)} 已送达；{', '.join(failed)} 失败。当前按至少一个通道成功确认提醒，不单独补发失败通道。"
    elif channel_results:
        delivery_summary = "全部通道成功" if not failed else "全部通道失败"


    lines = [
        "普通盯盘诊断：",
        "",
        "一、运行状态",
        f"- 盯盘开关：{'开启' if monitor.get('enabled') else '关闭'}",
        f"- 后台进程：{'运行中' if process.get('running') else '未运行'}",
        f"- PID：{process.get('pid') if process.get('running') else '无'}",
        f"- 当前是否交易时间：{'是' if trading.get('is_trading_time') else '否'}",
        f"- last_scan_at：{result.get('last_scan_at') or '-'}",
        f"- last_quote_at：{result.get('last_quote_at') or '-'}",
        "",
        "二、通知出口",
        f"- source_channel：{channel_display.get(_normalize_channel_name(monitor.get('source_channel')), monitor.get('source_channel') or '-')}",
        f"- notify_channels：{', '.join(channel_display.get(x, x) for x in notify_channels)}",
        f"- 通知结果：{delivery_summary}",
        f"- last_notify_result：{json.dumps(result.get('last_notify_result'), ensure_ascii=False)[:600] if result.get('last_notify_result') else '暂无记录'}",
        "",
        "三、最近扫描标的",
    ]
    if checked:
        for item in checked[:20]:
            lines.append(
                f"- {item.get('pool')}｜{item.get('name')} {item.get('code')}："
                f"价 {item.get('price')}，涨跌幅 {item.get('pct')}%，"
                f"振幅 {item.get('amplitude_percent')}%，量比 {item.get('volume_ratio')}，"
                f"换手 {item.get('turnover_rate_percent')}%，时间 {item.get('data_time') or item.get('time') or '-'}"
            )
    else:
        lines.append("- 暂无扫描记录。")

    lines.extend(["", "四、alert candidate"])
    if candidates:
        for item in candidates[:20]:
            lines.append(
                f"- {item.get('pool')}｜{item.get('name')} {item.get('code')}："
                f"{item.get('rule')}，{item.get('reason')}"
            )
    else:
        reason = "非交易时间" if not trading.get("is_trading_time") else "未满足阈值"
        if not monitor.get("enabled"):
            reason = "盯盘未开启"
        lines.append(f"- 暂无候选提醒；原因：{reason}。")

    lines.extend(["", "五、未提醒原因"])
    if suppressed:
        for item in suppressed[:20]:
            lines.append(
                f"- {item.get('pool')}｜{item.get('name')} {item.get('code')}："
                f"{item.get('rule')}，{item.get('suppressed_reason')}"
            )
    elif not candidates:
        lines.append("- 未满足阈值 / 非交易时间 / 盯盘未开启，见上方运行状态。")
    else:
        lines.append("- 有候选提醒且未被 cooldown 压制；若未收到，请看 last_notify_result。")

    lines.extend(["", "六、结论", f"- {result.get('conclusion') or '-'}"])
    return "\n".join(lines)


def monitor_diagnose(raw_json=False):
    status = _monitor_status_with_runtime()
    state = load_monitor_state()
    monitor = status.get("monitor", {}) or {}
    scan_result = None
    if monitor.get("enabled") and monitor.get("mode") == "normal":
        scan_result = build_monitor_alerts_once()
        state = load_monitor_state()

    conclusion = "普通盯盘未开启。"
    if monitor.get("enabled"):
        if not (status.get("trading_time") or {}).get("is_trading_time"):
            conclusion = "当前不在交易时间，后台若运行也只等待，不扫描实时行情。"
        elif scan_result and scan_result.get("alerts_count"):
            conclusion = "本次诊断发现候选提醒，真实后台会按 notify_channels 推送。"
        elif scan_result:
            conclusion = "本次诊断完成扫描，但未满足提醒阈值或被 cooldown 压制。"

    result = {
        "ok": True,
        "status": status,
        "scan_result": scan_result,
        "last_scan_at": state.get("last_scan_at"),
        "last_quote_at": state.get("last_quote_at"),
        "last_checked": state.get("last_checked_symbols", []),
        "last_quotes": state.get("last_quotes", {}),
        "last_alert_candidates": state.get("last_alert_candidates", []),
        "last_suppressed": state.get("last_suppressed", []),
        "last_notify_result": state.get("last_notify_result"),
        "conclusion": conclusion,
    }
    if raw_json:
        return result
    return {"_output_format": "text", "text": _format_monitor_diagnose(result)}


def monitor_notify_test(channel="weixin"):
    channel = _normalize_channel_name(channel)
    text = (
        "【龙虾盯盘测试】\n"
        "这是一条微信通知链路测试。\n"
        "来源：monitor notify-test\n"
        f"通道：{channel}\n"
        f"时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        "说明：模拟/测试消息，不是真实交易信号。"
    )
    result = send_monitor_notification(
        text,
        channels=[channel],
        source_channel=channel,
    )
    return {"ok": bool(result.get("ok")), "test": True, "notify_result": result}


def monitor_simulate_alert(channel="weixin"):
    channel = _normalize_channel_name(channel)
    result = {
        "mode": "normal",
        "profile_name": "普通盯盘",
        "alerts": [{
            "pool": "模拟池",
            "code": "510050",
            "name": "模拟标的",
            "message": "模拟触发：价格快速波动，测试普通盯盘通知链路。",
            "suggestion": "这是一条模拟提醒，不构成交易建议。",
            "reasons": ["模拟涨跌幅达到阈值", "模拟放量达到阈值"],
        }],
    }
    text = "【龙虾盯盘模拟提醒】\n模拟/测试消息，不是真实交易信号。\n\n"
    text += format_monitor_alerts_for_telegram(result)
    notify_result = send_monitor_notification(
        text,
        channels=[channel],
        source_channel=channel,
    )
    return {
        "ok": bool(notify_result.get("ok")),
        "simulated": True,
        "channel": channel,
        "notify_result": notify_result,
    }


def monitor_once():
    result = build_monitor_alerts_once()
    print_json(result)
    return result


def _monitor_wait(seconds, mode=None):
    """Reload toggles during sleep rather than sleeping through five minutes."""
    deadline = time.monotonic() + max(0, seconds)
    while time.monotonic() < deadline:
        monitor = load_watchlist_config().get("monitor", {}) or {}
        if not monitor.get("enabled") or (mode and monitor.get("mode") != mode):
            return
        time.sleep(min(1, max(0, deadline - time.monotonic())))


def monitor_loop():
    lock_file = _acquire_monitor_instance_lock()
    if lock_file is None:
        print_json({"ok": False, "message": "已有 monitor_loop 实例持有运行锁，本进程退出。", "lock_file": MONITOR_LOCK_PATH})
        return
    own_pid = os.getpid()
    last_idle_log = 0.0
    notify_failures = 0
    retry_notification_at = 0.0
    pending_delivery_commit = None
    try:
        _write_monitor_pid(own_pid)
        print_json({"ok": True, "message": "monitor_loop 已启动；关闭配置后会自动退出。", "pid": own_pid})
        while True:
            cfg = load_watchlist_config()
            monitor = cfg.get("monitor", {}) or {}
            mode, profile = _get_monitor_profile(cfg)
            if not monitor.get("enabled"):
                print_json({"ok": True, "message": "实时盯盘已关闭，monitor_loop 退出。"})
                return
            if pending_delivery_commit is not None:
                # Delivery already succeeded. Retry only its acknowledgement while
                # storage is unavailable; sending again would duplicate the message.
                if _commit_alert_cooldowns(pending_delivery_commit["alerts"], sent_at=pending_delivery_commit["sent_at"]):
                    pending_delivery_commit = None
                    notify_failures = 0
                    retry_notification_at = 0.0
                else:
                    print_json({"ok": False, "event": "monitor_delivery_commit_pending", "message": "提醒已送达，但状态保存失败；暂停新提醒并重试保存。"})
                    _monitor_wait(5, mode)
                    continue
            if monitor.get("market_hours_only", True) and not trading_time_status().get("is_trading_time"):
                if time.monotonic() - last_idle_log >= 60:
                    print_json({"ok": True, "event": "market_closed", "trading_time": trading_time_status(), "sleep_seconds": 30})
                    last_idle_log = time.monotonic()
                _monitor_wait(30, mode)
                continue
            scan_started = time.monotonic()
            try:
                result = build_monitor_alerts_once()
                if result.get("ok") is False:
                    print_json({"ok": False, "event": "monitor_scan_not_committed", "message": "扫描状态保存失败，本轮不发送提醒。"})
                    _monitor_wait(5, mode)
                    continue
                # The user can turn monitoring off/change rules while I/O is in flight.
                latest = load_watchlist_config()
                latest_monitor = latest.get("monitor", {}) or {}
                if not latest_monitor.get("enabled") or latest_monitor.get("mode") != mode or latest.get("strategy_monitor") != cfg.get("strategy_monitor"):
                    continue
                if result.get("alerts_count", 0) > 0:
                    print_json(result)
                    if time.monotonic() >= retry_notification_at:
                        # The formatter displays ten alerts; acknowledge only that batch.
                        # Remaining candidates stay pending for the next iteration.
                        batch = dict(result, alerts=result.get("alerts", [])[:10])
                        alert_text = format_monitor_alerts_for_telegram(batch)
                        if alert_text:
                            notify_started = time.monotonic()
                            notify_result = send_monitor_notification(alert_text, channels=latest_monitor.get("notify_channels"), source_channel=latest_monitor.get("source_channel"))
                            committed = False
                            if notify_result.get("ok") is True:
                                sent_at = time.time()
                                committed = _commit_alert_cooldowns(batch["alerts"], sent_at=sent_at)
                                if committed:
                                    notify_failures = 0
                                    retry_notification_at = 0.0
                                else:
                                    pending_delivery_commit = {"alerts": batch["alerts"], "sent_at": sent_at}
                            else:
                                notify_failures += 1
                                retry_notification_at = time.monotonic() + min(60, 5 * (2 ** min(notify_failures - 1, 4)))
                            print_json({"ok": bool(notify_result.get("ok")) and committed, "event": "monitor_alert_sent" if committed else "monitor_delivery_uncommitted" if notify_result.get("ok") else "monitor_delivery_failed", "notify_result": notify_result, "cooldown_committed": committed, "notification_duration_ms": round((time.monotonic() - notify_started) * 1000, 2)})
                elif mode != "strategy" or time.monotonic() - last_idle_log >= 60:
                    print_json({"ok": result.get("ok", True), "monitor_enabled": True, "mode": mode, "checked_count": result.get("checked_count"), "alerts_count": 0, "runtime_unavailable": result.get("runtime_unavailable", []), "scan_duration_ms": result.get("scan_duration_ms")})
                    last_idle_log = time.monotonic()
            except Exception as exc:
                print_json({"ok": False, "event": "monitor_iteration_failed", "error": str(exc)})
                _monitor_wait(5, mode)
                continue
            minimum = 1 if mode == "strategy" else 15
            interval = max(minimum, float(profile.get("interval_seconds", minimum) or minimum))
            # interval is start-to-start, not another full sleep after slow requests.
            _monitor_wait(max(5 if pending_delivery_commit is not None else 0.1, interval - (time.monotonic() - scan_started)), mode)
    finally:
        _cleanup_monitor_runtime(own_pid, lock_file)



MONITOR_PID_PATH = _state_path("market_monitor.pid")
MONITOR_LOG_PATH = _state_path("market_monitor.log")
MONITOR_LOCK_PATH = _state_path("market_monitor.lock")


def _write_monitor_pid(pid):
    os.makedirs(os.path.dirname(MONITOR_PID_PATH), exist_ok=True)
    tmp = f"{MONITOR_PID_PATH}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(str(int(pid)))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, MONITOR_PID_PATH)


def _acquire_monitor_instance_lock():
    os.makedirs(os.path.dirname(MONITOR_LOCK_PATH), exist_ok=True)
    lock_file = open(MONITOR_LOCK_PATH, "a+", encoding="utf-8")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock_file.seek(0)
        lock_file.truncate()
        lock_file.write(str(os.getpid()))
        lock_file.flush()
        os.fsync(lock_file.fileno())
        return lock_file
    except BlockingIOError:
        lock_file.close()
        return None
    except Exception:
        lock_file.close()
        raise


def _is_monitor_lock_held():
    try:
        lock_file = open(MONITOR_LOCK_PATH, "a+", encoding="utf-8")
    except Exception:
        return False
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        return False
    except BlockingIOError:
        return True
    finally:
        lock_file.close()


def _read_monitor_lock_pid():
    try:
        with open(MONITOR_LOCK_PATH, "r", encoding="utf-8") as f:
            value = f.read().strip()
        return int(value) if value else None
    except Exception:
        return None


def _read_monitor_pid():
    try:
        if not os.path.exists(MONITOR_PID_PATH):
            return None
        with open(MONITOR_PID_PATH, "r", encoding="utf-8") as f:
            pid = f.read().strip()
        if not pid:
            return None
        return int(pid)
    except Exception:
        return None


def _is_pid_running(pid):
    try:
        if not pid:
            return False
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def _process_command(pid):
    if not _is_pid_running(pid):
        return None

    proc_cmdline = f"/proc/{int(pid)}/cmdline"
    if os.path.exists(proc_cmdline):
        try:
            with open(proc_cmdline, "rb") as f:
                return [part.decode("utf-8", errors="replace") for part in f.read().split(b"\0") if part]
        except Exception:
            pass

    try:
        result = subprocess.run(
            ["/bin/ps", "-p", str(int(pid)), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=3,
            check=False
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


def _is_monitor_process(pid):
    command = _process_command(pid)
    if not command:
        return False
    script_path = os.path.realpath(__file__)
    allowed_flags = {"-u", "-B", "-s", "-S", "-E", "-I", "-O", "-OO"}
    def matches(args):
        if len(args) < 3 or not re.fullmatch(r"python(?:[0-9.]+)?", os.path.basename(args[0]), re.I):
            return False
        script_index = len(args) - 2
        return (
            args[-1] == "monitor_loop"
            and os.path.isabs(args[script_index])
            and os.path.realpath(args[script_index]) == script_path
            and all(arg in allowed_flags for arg in args[1:script_index])
        )
    # Linux preserves the true NUL-separated argv, including spaces in paths.
    if isinstance(command, (list, tuple)):
        return matches(command)
    try:
        if matches(shlex.split(command)):
            return True
    except ValueError:
        return False
    # macOS ps emits an unquoted command string. Match our exact known script
    # boundary, then separately validate the interpreter, flags and subcommand.
    for known_path in {script_path, os.path.abspath(__file__)}:
        marker = " " + known_path + " "
        if marker not in command:
            continue
        prefix, suffix = command.rsplit(marker, 1)
        if suffix.strip() != "monitor_loop":
            continue
        if prefix in {sys.executable, sys.executable + " -u"}:
            args = [sys.executable] + (["-u"] if prefix.endswith(" -u") else [])
        else:
            try:
                args = shlex.split(prefix)
            except ValueError:
                continue
        if matches(args + [known_path, "monitor_loop"]):
            return True
    return False



def _cleanup_monitor_runtime(own_pid, lock_file=None):
    try:
        if _read_monitor_pid() == int(own_pid):
            os.remove(MONITOR_PID_PATH)
    except Exception:
        pass

    if lock_file is not None:
        try:
            lock_file.seek(0)
            lock_file.truncate()
            lock_file.flush()
        except Exception:
            pass
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            lock_file.close()
        except Exception:
            pass


def monitor_pid_status():
    pid_file_pid = _read_monitor_pid()
    lock_pid = _read_monitor_lock_pid()
    lock_held = _is_monitor_lock_held()

    pid = pid_file_pid
    pid_source = "pid_file" if pid is not None else None
    pid_running = _is_pid_running(pid)
    identity_verified = _is_monitor_process(pid) if pid_running else False

    if lock_held and lock_pid and (not pid_running or not identity_verified):
        lock_pid_running = _is_pid_running(lock_pid)
        lock_identity_verified = _is_monitor_process(lock_pid) if lock_pid_running else False
        if lock_pid_running and lock_identity_verified:
            pid = lock_pid
            pid_source = "lock_file"
            pid_running = True
            identity_verified = True

    return {
        "ok": True,
        "pid": pid,
        "pid_source": pid_source,
        "pid_file_pid": pid_file_pid,
        "lock_pid": lock_pid,
        "running": bool(pid_running and identity_verified and lock_held),
        "pid_running": pid_running,
        "identity_verified": identity_verified,
        "lock_held": lock_held,
        "pid_file": MONITOR_PID_PATH,
        "lock_file": MONITOR_LOCK_PATH,
        "log_file": MONITOR_LOG_PATH
    }


def monitor_verify(expected_running=None, wait_seconds=1.0):
    """
    执行开/关盯盘后做二次验收，避免 TG 只回复“已执行”但后台实际没变。
    expected_running=True  表示期望进程正在运行
    expected_running=False 表示期望进程已经停止
    """
    try:
        time.sleep(wait_seconds)
    except Exception:
        pass

    status = monitor_pid_status()
    running = bool(status.get("running"))

    verified = True
    if expected_running is not None:
        verified = (running == bool(expected_running))

    return {
        "ok": verified,
        "verified": verified,
        "expected_running": expected_running,
        "pid_status": status
    }


def monitor_start():
    """
    后台启动 monitor_loop。
    前提：monitor.enabled 应为 true。
    """
    cfg = load_watchlist_config()
    monitor = cfg.get("monitor", {}) or {}

    if not bool(monitor.get("enabled", False)):
        return {
            "ok": False,
            "message": "实时盯盘当前是关闭状态。请先执行 monitor on、启动普通盯盘或启动策略盯盘。",
            "monitor": monitor
        }

    current_status = monitor_pid_status()
    if current_status.get("running"):
        return {
            "ok": True,
            "message": "monitor_loop 已经在后台运行，无需重复启动。",
            "pid": current_status.get("pid"),
            "source_channel": monitor.get("source_channel"),
            "notify_channels": _normalize_notify_channels(monitor),
            "log_file": MONITOR_LOG_PATH
        }

    os.makedirs(os.path.dirname(MONITOR_PID_PATH), exist_ok=True)

    script_path = os.path.abspath(__file__)
    log_f = open(MONITOR_LOG_PATH, "a", encoding="utf-8")

    try:
        proc = subprocess.Popen(
            [sys.executable, "-u", script_path, "monitor_loop"],
            stdout=log_f,
            stderr=log_f,
            stdin=subprocess.DEVNULL,
            start_new_session=True
        )
    finally:
        log_f.close()

    status = None
    deadline = time.time() + 3
    while time.time() < deadline:
        time.sleep(0.1)
        status = monitor_pid_status()
        if status.get("running"):
            break
        if proc.poll() is not None:
            break

    if not status or not status.get("running"):
        return {
            "ok": False,
            "message": "monitor_loop 启动后未能完成运行锁和 PID 身份验证。",
            "spawned_pid": proc.pid,
            "status": status or monitor_pid_status(),
            "log_file": MONITOR_LOG_PATH
        }

    return {
        "ok": True,
        "message": "monitor_loop 已后台启动。",
        "pid": status.get("pid"),
        "mode": monitor.get("mode"),
        "profile_name": monitor.get("profile_name"),
        "interval_seconds": monitor.get("interval_seconds"),
        "source_channel": monitor.get("source_channel"),
        "notify_channels": _normalize_notify_channels(monitor),
        "log_file": MONITOR_LOG_PATH
    }


def monitor_stop():
    """
    停止后台 monitor_loop，并关闭 monitor.enabled。
    """
    # 先把配置关闭，循环自己也会退出
    try:
        monitor_set(False)
    except Exception:
        pass

    status = monitor_pid_status()
    pid = status.get("pid")
    stopped = False
    message = ""

    if status.get("running"):
        try:
            os.kill(pid, 15)
            stopped = True
            message = "已发送停止信号，后台盯盘进程将退出。"
        except Exception as e:
            message = f"尝试停止进程失败：{e}"
    elif status.get("pid_running") and not status.get("identity_verified"):
        message = "PID 文件指向的进程身份不匹配，已拒绝发送停止信号。"
    else:
        message = "未发现正在运行的后台盯盘进程。"

    if stopped:
        deadline = time.time() + 5
        while time.time() < deadline and _is_monitor_process(pid):
            time.sleep(0.1)

    final_status = monitor_pid_status()
    if not final_status.get("pid_running") and not final_status.get("lock_held"):
        try:
            if _read_monitor_pid() == pid and os.path.exists(MONITOR_PID_PATH):
                os.remove(MONITOR_PID_PATH)
        except Exception:
            pass

    return {
        "ok": not final_status.get("running"),
        "stopped": stopped,
        "pid": pid,
        "message": message,
        "pid_file": MONITOR_PID_PATH,
        "lock_file": MONITOR_LOCK_PATH,
        "final_status": final_status,
        "log_file": MONITOR_LOG_PATH
    }


def _report_cli_options(args, default_variant="full"):
    options = {"channel": _default_output_channel(), "variant": default_variant, "raw_json": False,
               "remaining": [], "date": None, "symbol": None}
    index = 0
    while index < len(args):
        value = args[index]
        if value in {"--channel", "--date", "date", "--symbol"}:
            if index + 1 >= len(args) or args[index + 1].startswith("--"):
                raise ValueError(f"{value} 需要参数")
            key = {"--channel": "channel", "--date": "date", "date": "date", "--symbol": "symbol"}[value]
            options[key] = args[index + 1]
            index += 2
        elif value in {"--simple", "--full"}:
            options["variant"] = value[2:]
            index += 1
        elif value in {"--json", "原始JSON"}:
            options["raw_json"] = True
            index += 1
        elif value == "--lhb":
            index += 1  # Compatibility: reports already include date-scoped LHB.
        else:
            options["remaining"].append(value)
            index += 1
    for value in options["remaining"]:
        if re.fullmatch(r"\d{4}[-/]?\d{2}[-/]?\d{2}", value):
            if options["date"] is not None:
                raise ValueError("请只指定一个复盘日期")
            options["date"] = value
        elif re.fullmatch(r"(?:sh|sz)?\d{6}", value, re.I):
            if options["symbol"] is not None:
                raise ValueError("请只指定一只复盘标的")
            options["symbol"] = value
        else:
            raise ValueError("无法识别报告参数")
    if options["date"] is not None:
        options["date"] = _normalize_report_date(options["date"])
    return options



def _parse_monitor_cli_args(args):
    mode = None
    channel = None
    index = 0
    while index < len(args):
        value = args[index]
        if value in {"--channel", "-c"} and index + 1 < len(args):
            channel = args[index + 1]
            index += 2
        elif value.startswith("--"):
            index += 1
        elif mode is None:
            mode = value
            index += 1
        else:
            index += 1
    return mode, _normalize_channel_name(channel or _default_output_channel())


def _print_report_result(result, raw_json=False):
    if raw_json:
        print_json({key: value for key, value in result.items() if key != "text"})
    else:
        print(result.get("text", ""))


def _dispatch_auxiliary_market_command(command, args):
    """Return a result for documented market commands, or None for other routes."""
    supported = {"kline", "news_map", "morning_news", "us_quote", "us_index", "lhb"}
    if command not in supported:
        return None
    args = list(args)

    if command == "lhb":
        if len(args) > 1:
            raise ValueError("用法：lhb [YYYYMMDD 或六位证券代码]")
        if not args:
            return lhb(emit=False)
        value = str(args[0]).strip()
        if re.fullmatch(r"[0-9]{8}", value):
            datetime.strptime(value, "%Y%m%d")
            return lhb(date=value, emit=False)
        if re.fullmatch(r"(?:(?:sh|sz))?[0-9]{6}", value, re.I) or _is_index_or_etf(value):
            return lhb(symbol=value, emit=False)
        raise ValueError("龙虎榜参数应为有效 YYYYMMDD 日期或六位证券代码")

    if command == "kline":
        from backtest.data_provider import load_bars, resolve_range
        from zoneinfo import ZoneInfo
        if len(args) != 2 or not re.fullmatch(r"[0-9]{6}", str(args[0])):
            raise ValueError("用法：kline 六位证券代码 天数（1 至 1000）")
        if not re.fullmatch(r"[0-9]{1,4}", str(args[1])) or not 1 <= int(args[1]) <= 1000:
            raise ValueError("K线天数必须是 1 至 1000 的整数")
        symbol, count = args[0], int(args[1])
        today = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
        start, end = resolve_range(count, None, today)
        data = load_bars(symbol, "1d", start, end, timeout_seconds=5)
        bars = [row for row in data.get("bars", []) if start <= str(row.get("time", ""))[:10] <= end][-count:]
        if not bars:
            return {"ok": False, "command": command, "error": "请求区间暂无可用K线", "source": data.get("source")}
        warnings = []
        if len(bars) < count:
            warnings.append(f"仅有 {len(bars)} 根可用K线，少于请求的 {count} 根；未补造缺失行情")
        if bars[-1]["time"][:10] == today:
            warnings.append("包含当日日K；交易结束前该根K线可能尚未完成")
        if data.get("cache_stale"):
            warnings.append("当前使用过期缓存，请核验行情时间")
        return {
            "ok": True, "command": command, "symbol": symbol, "interval": "1d",
            "requested_bars": count, "count": len(bars), "bars": bars,
            "available_start": bars[0]["time"], "available_end": bars[-1]["time"],
            "source": data.get("source"), "original_source": data.get("original_source"),
            "volume_unit": data.get("volume_unit"), "amount_unit": data.get("amount_unit"),
            "price_adjustment": data.get("price_adjustment"),
            "used_cache": bool(data.get("used_cache")), "cache_stale": bool(data.get("cache_stale")),
            "cache_time": data.get("cache_time"), "fallback_errors": data.get("fallback_errors", []),
            "warnings": warnings,
        }

    if command == "news_map":
        text = " ".join(str(value) for value in args).strip()
        if not text or len(text) > 12000:
            raise ValueError("用法：news_map 新闻内容（1 至 12000 字符）")
        mapping = _load_news_stock_map()
        if not isinstance(mapping, dict) or not mapping:
            return {"ok": False, "command": command, "error": "新闻题材映射表暂缺或格式错误"}
        matches = _match_news_to_stocks([text])
        keywords = []
        for item in mapping.values():
            if isinstance(item, dict):
                keywords.extend(word for word in item.get("keywords", []) if isinstance(word, str) and word.lower() in text.lower())
        return {
            "ok": True, "command": command, "matches": matches,
            "matched_keywords": list(dict.fromkeys(keywords)), "source": "用户提供文本 + 本地关键词映射表",
            "note": "仅为关键词题材关联；泛泛关键词相关性较弱，未核验催化强度，不等于买卖建议",
        }

    if command == "morning_news":
        if len(args) > 1 or (args and not re.fullmatch(r"[0-9]{1,2}", str(args[0]))):
            raise ValueError("用法：morning_news [条数，1 至 50]")
        limit = int(args[0]) if args else 10
        if not 1 <= limit <= 50:
            raise ValueError("新闻条数必须为 1 至 50 的整数")
        from datetime import timezone
        headlines = news(limit=limit)
        if not headlines:
            return {"ok": False, "command": command, "error": "新闻源暂未返回可用标题"}
        mapping = _load_news_stock_map()
        mapping_available = isinstance(mapping, dict) and bool(mapping)
        return {
            "ok": True, "command": command, "headlines": headlines,
            "matches": _match_news_to_stocks(headlines) if mapping_available else [],
            "mapping_available": mapping_available,
            "source": "现有网页标题抓取链（财联社／新浪备用；逐条来源未返回）",
            "fetched_at": datetime.now(timezone.utc).isoformat(), "published_at": None,
            "warnings": ["抓取时间不等于新闻发布时间；未核验全部标题均为当日新闻", "仅为关键词题材映射，不等于买卖建议"] + ([] if mapping_available else ["题材映射表暂缺"]),
        }

    if command == "us_quote":
        if len(args) != 1 or not re.fullmatch(r"[A-Za-z^][A-Za-z0-9.^-]{0,14}", str(args[0])):
            raise ValueError("用法：us_quote 美股代码，例如 AAPL 或 NVDA")
        return quote_us(args[0])
    if args:
        raise ValueError("用法：us_index（不接受额外参数）")
    return us_index()


def main():
    if len(sys.argv) < 2:
        print("用法：")
        print("python3 a_stock_query.py quote 600519")
        print("python3 a_stock_query.py index 000001")
        print("python3 a_stock_query.py lhb 510050")
        print("python3 a_stock_query.py morning_report")
        print("python3 a_stock_query.py after_close_report")
        print("python3 a_stock_query.py demo")
        return

    cmd = sys.argv[1]
    try:
        auxiliary = _dispatch_auxiliary_market_command(cmd, sys.argv[2:])
        if auxiliary is not None:
            print_json(auxiliary)
            return
        if cmd in {"demo", "synthetic-demo", "合成演示"}:
            print_json(synthetic_demo())
        elif cmd in {"nl", "自然语言"}:
            text = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else ""
            result = handle_natural_language_command(text)
            if isinstance(result, dict) and result.get("_output_format") == "text":
                print(result.get("text", ""))
            else:
                print_json(result)
        elif cmd in {"watch", "hold"}:
            pool = "watch_pool" if cmd == "watch" else "holding_pool"
            action = sys.argv[2] if len(sys.argv) > 2 else "list"
            if action in {"add", "加入", "新增"}:
                if len(sys.argv) < 4:
                    print_json({"error": "用法：watch add 代码 名称；或 hold add 代码 名称"})
                else:
                    code = sys.argv[3]
                    name = sys.argv[4] if len(sys.argv) > 4 else None
                    note = " ".join(sys.argv[5:]) if len(sys.argv) > 5 else None
                    print_json(watchlist_add(pool, code, name, note))
            elif action in {"remove", "rm", "delete", "del", "删除", "移除"}:
                if len(sys.argv) < 4:
                    print_json({"error": "用法：watch remove 代码/名称；或 hold remove 代码/名称"})
                else:
                    print_json(watchlist_remove(pool, sys.argv[3]))
            elif action in {"list", "ls", "查看"}:
                print_json(watchlist_list(pool))
            else:
                print_json({"error": f"未知{cmd}操作：{action}"})
        elif cmd == "pool":
            action = sys.argv[2] if len(sys.argv) > 2 else "list"
            scope = sys.argv[3] if len(sys.argv) > 3 else "all"
            request_text = " ".join(sys.argv[4:]).strip()
            if action not in {"list", "snapshot"}:
                print_json({
                    "error": "用法：pool list|snapshot holding|watch|all"
                })
            else:
                result = pool_output(action, scope, request_text)
                if result.get("_output_format") == "text":
                    print(result.get("text", ""))
                else:
                    print_json(result)
        elif cmd == "monitor":
            action = sys.argv[2] if len(sys.argv) > 2 else "status"
            if action in {"on", "open", "start", "开启", "打开", "开始"}:
                mode, channel = _parse_monitor_cli_args(sys.argv[3:])
                set_result = monitor_set(
                    True,
                    mode=mode,
                    source_channel=channel,
                    notify_channels=[channel],
                )
                start_result = monitor_start()
                verify_result = monitor_verify(expected_running=True, wait_seconds=1.2)
                print_json({
                    "ok": bool(verify_result.get("verified")),
                    "action": "monitor_on_start_verified",
                    "set": set_result,
                    "start": start_result,
                    "verify": verify_result
                })
            elif action in {"off", "close", "stop", "关闭", "停止", "暂停"}:
                set_result = monitor_set(False)
                stop_result = monitor_stop()
                verify_result = monitor_verify(expected_running=False, wait_seconds=1.2)
                print_json({
                    "ok": bool(verify_result.get("verified")),
                    "action": "monitor_off_stop_verified",
                    "set": set_result,
                    "stop": stop_result,
                    "verify": verify_result
                })
            elif action in {"status", "查看", "状态"}:
                text = " ".join(sys.argv[3:]).strip()
                result = monitor_status_output(text)
                if result.get("_output_format") == "text":
                    print(result.get("text", ""))
                else:
                    print_json(result)
            elif action in {"diagnose", "诊断"}:
                text = " ".join(sys.argv[3:]).strip()
                result = monitor_diagnose(raw_json=_monitor_raw_output_requested(text))
                if isinstance(result, dict) and result.get("_output_format") == "text":
                    print(result.get("text", ""))
                else:
                    print_json(result)
            elif action in {"notify-test", "通知测试"}:
                channel = _monitor_channel_arg(sys.argv[3:], default="weixin")
                print_json(monitor_notify_test(channel))
            elif action in {"simulate-alert", "模拟提醒"}:
                channel = _monitor_channel_arg(sys.argv[3:], default="weixin")
                print_json(monitor_simulate_alert(channel))
            else:
                print_json({"error": f"未知盯盘操作：{action}"})
        elif cmd == "strategy":
            action = sys.argv[2] if len(sys.argv) > 2 else "show"
            if action in {"set", "设置"}:
                text = " ".join(sys.argv[3:]) if len(sys.argv) > 3 else ""
                print_json(strategy_set(text))
            elif action in {"dry-run", "draft", "草案"}:
                text = " ".join(sys.argv[3:]) if len(sys.argv) > 3 else ""
                print_json(strategy_dry_run(text))
            elif action in {"list", "ls", "列表"}:
                print_json(strategy_list())
            elif action in {"status", "状态"}:
                print_json(strategy_status())
            elif action in {"show", "get", "查看"}:
                print_json(strategy_get())
            elif action in {"explain", "解释"}:
                rule_id = sys.argv[3] if len(sys.argv) > 3 else ""
                print_json(strategy_explain(rule_id))
            elif action in {"test", "测试"}:
                rule_id = sys.argv[3] if len(sys.argv) > 3 else ""
                offline = "--offline" in sys.argv[4:]
                print_json(strategy_test(rule_id, live_probe=not offline))
            elif action in {"delete", "remove", "删除指定"}:
                rule_id = sys.argv[3] if len(sys.argv) > 3 else ""
                print_json(strategy_delete(rule_id))
            elif action in {"clear", "清空", "删除"}:
                print_json(strategy_clear())
            elif action in {"capabilities", "fields", "字段"}:
                print_json(strategy_capabilities())
            elif action in {"check", "selfcheck", "自检"}:
                print_json(strategy_check())
            elif action in {"simulate", "模拟"}:
                symbol = "510050"
                preset = "breakout"
                mock_text = ""
                send_test = False
                args = sys.argv[3:]
                index = 0
                while index < len(args):
                    value = args[index]
                    if value == "--symbol" and index + 1 < len(args):
                        symbol = args[index + 1]
                        index += 2
                    elif value == "--preset" and index + 1 < len(args):
                        preset = args[index + 1]
                        index += 2
                    elif value == "--mock" and index + 1 < len(args):
                        mock_text = args[index + 1]
                        index += 2
                    elif value == "--send-test":
                        send_test = True
                        index += 1
                    else:
                        index += 1
                print_json(strategy_simulate(
                    symbol,
                    preset=preset,
                    mock_text=mock_text,
                    send_test=send_test,
                ))
            else:
                print_json({"error": f"未知策略操作：{action}"})
        elif cmd == "backtest":
            action = sys.argv[2] if len(sys.argv) > 2 else "status"
            if action in {"on", "开启"}:
                print_json(backtest_toggle(True))
            elif action in {"off", "关闭"}:
                print_json(backtest_toggle(False))
            elif action in {"status", "状态"}:
                print_json(backtest_status())
            elif action in {"set", "设置"}:
                text = " ".join(sys.argv[3:]) if len(sys.argv) > 3 else ""
                print_json(backtest_set_strategy(text))
            elif action in {"show", "查看"}:
                print_json(backtest_show_strategy())
            elif action in {"clear", "清空"}:
                print_json(backtest_clear_strategy())
            elif action in {"trades", "交易"}:
                print_json(backtest_trades_command())
            elif action in {"chart", "图形"}:
                print_json(backtest_chart_command())
            elif action in {"run-text", "执行文本"}:
                text = " ".join(sys.argv[3:]).strip()
                if not text:
                    print_json({
                        "error": "用法：backtest run-text \"完整自然语言回测文本\""
                    })
                else:
                    result = backtest_run_text_command(text)
                    if _backtest_raw_output_requested(text):
                        print_json(result)
                    else:
                        print(format_backtest_run_text_result(result, text))
            elif action in {"run", "执行"}:
                if len(sys.argv) < 4:
                    print_json({
                        "error": (
                            "用法：backtest run 510050 --days 60 --interval 1d "
                            "--saved-strategy"
                        )
                    })
                else:
                    symbol = sys.argv[3]
                    options = _parse_backtest_cli_args(sys.argv[4:])
                    print_json(backtest_run_saved_command(symbol, **options))
            else:
                print_json({"error": f"未知回测操作：{action}"})
        elif cmd == "model":
            print_json(_dispatch_model_command(sys.argv[2:]))
        elif cmd in {"monitor_once", "盯盘一次"}:
            monitor_once()
        elif cmd in {"monitor_loop", "盯盘循环"}:
            monitor_loop()
        elif cmd in {"monitor_start", "盯盘启动", "后台盯盘启动"}:
            print_json(monitor_start())
        elif cmd in {"monitor_stop", "盯盘停止", "后台盯盘停止"}:
            print_json(monitor_stop())
        elif cmd in {"monitor_pid", "monitor_status", "盯盘进程"}:
            print_json(monitor_pid_status())
        elif cmd == "quote":
            quote(sys.argv[2])
        elif cmd == "index":
            index(sys.argv[2] if len(sys.argv) > 2 else None)
        elif cmd == "lhb":
            if len(sys.argv) < 3:
                lhb()
            else:
                lhb(symbol=sys.argv[2], emit=True)
        elif cmd in {"morning_report", "morning", "盘前", "盘前报告"}:
            options = _report_cli_options(sys.argv[2:], default_variant="full")
            result = report_output("morning_report", variant=options["variant"], channel=options["channel"], date=options["date"])
            _print_report_result(result, options["raw_json"])
        elif cmd in {"after_simple", "simple_after", "复盘简洁版", "简单复盘", "after_full", "full_after", "复盘完整版", "完整复盘", "after_close_report", "after"}:
            default_variant = "simple" if cmd in {"after_simple", "simple_after", "复盘简洁版", "简单复盘"} else "full"
            options = _report_cli_options(sys.argv[2:], default_variant=default_variant)
            result = report_output("after_close_report", variant=options["variant"], channel=options["channel"], date=options["date"], symbol=options["symbol"])
            _print_report_result(result, options["raw_json"])
        else:
            print_json({"error": f"未知命令：{cmd}"})
    except Exception as e:
        print_json({"error": str(e)})


if __name__ == "__main__":
    main()
