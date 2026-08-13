import json
import os
from datetime import datetime
from typing import Any, Dict


STATE_DIR = os.path.abspath(os.path.expanduser(
    os.environ.get("LOBSTER_QUANT_HOME", "~/.openclaw/lobster-quant-agent")
))
CONFIG_PATH = os.path.join(STATE_DIR, "backtest_config.json")
RESULTS_DIR = os.path.join(STATE_DIR, "backtests", "results")
CHARTS_DIR = os.path.join(STATE_DIR, "backtests", "charts")


def default_config() -> Dict[str, Any]:
    return {
        "enabled": False,
        "strategy": {
            "raw_text": "",
            "buy_conditions": [],
            "sell_conditions": [],
            "unsupported_fields": [],
            "updated_at": None,
        },
        "defaults": {
            "initial_capital": 100000.0,
            "commission_rate": 0.0003,
            "slippage_rate": 0.0002,
        },
        "last_result": {
            "result_id": None,
            "result_path": None,
            "chart_path": None,
            "completed_at": None,
        },
    }


def load_config() -> Dict[str, Any]:
    if not os.path.exists(CONFIG_PATH):
        return default_config()
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
            config = json.load(handle)
    except Exception:
        return default_config()
    defaults = default_config()
    for key, value in defaults.items():
        config.setdefault(key, value)
    config.setdefault("strategy", defaults["strategy"])
    config.setdefault("defaults", defaults["defaults"])
    config.setdefault("last_result", defaults["last_result"])
    return config


def save_config(config: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    tmp_path = f"{CONFIG_PATH}.tmp.{os.getpid()}"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(config, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, CONFIG_PATH)


def set_enabled(enabled: bool) -> Dict[str, Any]:
    config = load_config()
    config["enabled"] = bool(enabled)
    save_config(config)
    return get_status()


def set_strategy(parsed: Dict[str, Any]) -> Dict[str, Any]:
    config = load_config()
    config["strategy"] = {
        "raw_text": parsed["raw_text"],
        "buy_conditions": parsed["buy_conditions"],
        "sell_conditions": parsed["sell_conditions"],
        "unsupported_fields": parsed.get("unsupported_fields", []),
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    save_config(config)
    return {"ok": True, "strategy": config["strategy"]}


def clear_strategy() -> Dict[str, Any]:
    config = load_config()
    config["strategy"] = default_config()["strategy"]
    save_config(config)
    return {"ok": True, "message": "回测策略已清空。"}


def save_last_result(result: Dict[str, Any]) -> str:
    os.makedirs(RESULTS_DIR, exist_ok=True)
    result_path = os.path.join(RESULTS_DIR, f"{result['result_id']}.json")
    with open(result_path, "w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
    config = load_config()
    config["last_result"] = {
        "result_id": result["result_id"],
        "result_path": result_path,
        "chart_path": result.get("chart_path"),
        "completed_at": result.get("completed_at"),
    }
    save_config(config)
    return result_path


def update_chart_path(chart_path: str) -> None:
    config = load_config()
    config.setdefault("last_result", {})["chart_path"] = chart_path
    save_config(config)


def load_last_result() -> Dict[str, Any]:
    config = load_config()
    result_path = config.get("last_result", {}).get("result_path")
    if not result_path or not os.path.exists(result_path):
        raise RuntimeError("尚无可用的回测结果，请先执行 backtest run。")
    with open(result_path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def get_status() -> Dict[str, Any]:
    config = load_config()
    strategy = config.get("strategy", {})
    return {
        "ok": True,
        "enabled": bool(config.get("enabled", False)),
        "note": "enabled 只控制自然语言自动回测，不启动后台进程。",
        "strategy_configured": bool(strategy.get("buy_conditions")),
        "strategy": strategy,
        "defaults": config.get("defaults", {}),
        "last_result": config.get("last_result", {}),
    }
