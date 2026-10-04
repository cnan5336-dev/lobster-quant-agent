import hashlib
import math
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from .config import load_config, save_last_result
from .data_provider import load_bars, resolve_range, _validate_bars
from .metrics import calculate_metrics
from .models import BacktestResult, TradeRecord


UNSUPPORTED_TERMS = (
    "买一到买五", "卖一到卖五", "五档盘口", "实时委比",
    "实时买卖盘强弱", "主动买入", "主动卖出", "盘口委比", "委比", "量比",
)


def _condition(kind: str, **kwargs) -> Dict[str, Any]:
    data = {"type": kind}
    data.update(kwargs)
    return data


_COMPARISON_WORDS = {
    "超过": ">", "高于": ">", "大于": ">",
    "达到": ">=", "不少于": ">=", "不低于": ">=",
    "低于": "<", "小于": "<", "跌破": "<",
    "不超过": "<=", "不高于": "<=", "至多": "<=",
}
_COMPARISON_PATTERN = "|".join(_COMPARISON_WORDS)
_NUMBER_PATTERN = r"([0-9]+(?:\.[0-9]+)?)"


def _parse_atom(text: str, side: str) -> Optional[Dict[str, Any]]:
    atom = re.sub(r"^(如果|当|若)", "", str(text).strip())
    atom = re.sub(r"(时|的时候)$", "", atom).strip()
    atom = atom.strip("，,；;。 ")
    compact = re.sub(r"\s+", "", atom)
    upper = compact.upper()

    if upper == "MACD金叉":
        return _condition("macd_cross", direction="golden_cross")
    if upper == "MACD死叉":
        return _condition("macd_cross", direction="death_cross")
    if upper == "KDJ金叉":
        return _condition("kdj_cross", direction="golden_cross")
    if upper == "KDJ死叉":
        return _condition("kdj_cross", direction="death_cross")

    rolling_high = re.fullmatch(
        r"(?:收盘价)?(?:突破|超过)过去(\d+)(?:天|日|根K线?)最高价",
        compact,
        re.I
    )
    if rolling_high and 1 <= int(rolling_high.group(1)) <= 10000:
        return _condition(
            "rolling_high_breakout",
            lookback=int(rolling_high.group(1))
        )

    volume = re.fullmatch(
        rf"成交量({_COMPARISON_PATTERN})(?:过去|近)(\d+)(?:天|日|根|根K线?)均量(?:的)?{_NUMBER_PATTERN}倍",
        compact
    )
    if volume and 1 <= int(volume.group(2)) <= 10000 and float(volume.group(3)) > 0:
        return _condition(
            "volume_vs_average", lookback=int(volume.group(2)),
            factor=float(volume.group(3)), operator=_COMPARISON_WORDS[volume.group(1)]
        )
    # Relative volume must name its window and multiplier; do not invent thresholds.

    bar_return = re.fullmatch(
        rf"(?:收盘价|单日|当日|本K线)?(?:上涨|涨幅)({_COMPARISON_PATTERN}){_NUMBER_PATTERN}%",
        compact
    )
    if bar_return:
        return _condition(
            "bar_return", operator=_COMPARISON_WORDS[bar_return.group(1)],
            value=float(bar_return.group(2))
        )

    stop_patterns = (
        r"买入价止损([0-9]+(?:\.[0-9]+)?)%",
        r"止损([0-9]+(?:\.[0-9]+)?)%",
        r"跌破买入价([0-9]+(?:\.[0-9]+)?)%",
        r"亏损(?:超过|达到|大于)([0-9]+(?:\.[0-9]+)?)%",
        r"相对买入价(?:下跌|跌幅)(?:超过|达到|大于)?([0-9]+(?:\.[0-9]+)?)%",
        r"回撤到买入价以下([0-9]+(?:\.[0-9]+)?)%",
    )
    for pattern in stop_patterns:
        match = re.fullmatch(pattern, compact)
        if match:
            return _condition(
                "entry_return",
                operator="<" if any(word in compact for word in ("超过", "大于", "跌破")) else "<=",
                value=-float(match.group(1))
            )

    profit_patterns = (
        r"买入价止盈([0-9]+(?:\.[0-9]+)?)%",
        r"上涨(?:超过|达到|大于)([0-9]+(?:\.[0-9]+)?)%",
        r"盈利(?:超过|达到|大于)([0-9]+(?:\.[0-9]+)?)%",
        r"涨到买入价上方([0-9]+(?:\.[0-9]+)?)%",
        r"(?:超过|高于)买入价([0-9]+(?:\.[0-9]+)?)%",
    )
    if side == "sell":
        for pattern in profit_patterns:
            match = re.fullmatch(pattern, compact)
            if match:
                return _condition(
                    "entry_return",
                    operator=">" if any(word in compact for word in ("超过", "大于", "高于")) else ">=",
                    value=float(match.group(1))
                )

    rsi = re.fullmatch(rf"RSI({_COMPARISON_PATTERN}){_NUMBER_PATTERN}", compact, re.I)
    if rsi:
        return _condition(
            "indicator_threshold", indicator="rsi",
            operator=_COMPARISON_WORDS[rsi.group(1)], value=float(rsi.group(2))
        )

    ma = re.fullmatch(
        r"(?:收盘价)?(站上|突破|高于|跌破|低于)MA?(\d+)(?:(?:日|分钟)?均线)?",
        compact,
        re.I
    )
    if ma and 1 <= int(ma.group(2)) <= 10000:
        return _condition(
            "price_vs_ma",
            period=int(ma.group(2)),
            operator="<" if ma.group(1) in ("跌破", "低于") else ">"
        )

    price = re.fullmatch(
        rf"(?:价格|股价|收盘价)({_COMPARISON_PATTERN}){_NUMBER_PATTERN}(?:元)?", compact
    )
    if price:
        return _condition("price_threshold", operator=_COMPARISON_WORDS[price.group(1)], value=float(price.group(2)))
    return None


def _parse_expression(text: str, side: str) -> Tuple[List[Dict[str, Any]], List[str]]:
    expression = str(text or "").strip()
    if not expression:
        return [], []

    if re.search(r"(?:或者|或是|或|并且|而且|同时|且)\s*$", expression):
        return [], [expression]
    or_parts = [
        item.strip()
        for item in re.split(r"或者|或是|或", expression)
        if item.strip()
    ]
    parsed_or = []
    unsupported = []
    for or_part in or_parts:
        and_parts = [
            item.strip()
            for item in re.split(r"并且|而且|同时|且", or_part)
            if item.strip()
        ]
        parsed_and = []
        for atom_text in and_parts:
            condition = _parse_atom(atom_text, side)
            if condition is None:
                unsupported.append(atom_text)
            else:
                parsed_and.append(condition)
        if len(parsed_and) == 1:
            parsed_or.append(parsed_and[0])
        elif len(parsed_and) > 1:
            parsed_or.append(_condition("all", conditions=parsed_and))

    if len(parsed_or) == 1:
        only = parsed_or[0]
        if only.get("type") == "all":
            return only["conditions"], unsupported
        return [only], unsupported
    if len(parsed_or) > 1:
        return [_condition("any", conditions=parsed_or)], unsupported
    return [], unsupported


def parse_strategy(text: str) -> Dict[str, Any]:
    raw = " ".join(str(text or "").strip().split())
    unsupported = [term for term in UNSUPPORTED_TERMS if term in raw]
    unsupported = [
        term for term in unsupported
        if not any(term != other and term in other for other in unsupported)
    ]
    if unsupported:
        return {
            "ok": False,
            "error": f"该策略包含历史回测暂不支持字段：{'、'.join(unsupported)}。请修改策略后再回测。",
            "unsupported_fields": unsupported,
        }
    labeled_buy = re.search(
        r"买入条件\s*[:：]\s*(.*?)(?=卖出条件\s*[:：]|$)",
        raw
    )
    labeled_sell = re.search(
        r"卖出条件\s*[:：]\s*(.*?)(?=(?:。|\.)\s*(?:请|输出|返回|展示|说明)|$)",
        raw
    )
    if labeled_buy:
        buy_text = labeled_buy.group(1).strip("，,；;。 ")
        sell_text = (
            labeled_sell.group(1).strip("，,；;。 ")
            if labeled_sell else ""
        )
    else:
        clauses = re.findall(
            r"如果([^，,；;]*?)(?:就)?(买入|卖出)(?=$|[，,；;])",
            raw
        )
        buy_parts = [clause for clause, action in clauses if action == "买入"]
        sell_parts = [clause for clause, action in clauses if action == "卖出"]
        buy_text = "；".join(buy_parts) if buy_parts else raw
        sell_text = "；".join(sell_parts)
        if not sell_text:
            split = re.split(r"(?:；|;|，|,)\s*(?=卖出|止损|止盈)", raw, maxsplit=1)
            if len(split) == 2:
                buy_text, sell_text = split
    buy_conditions, unsupported_buy = _parse_expression(buy_text, "buy")
    sell_conditions, unsupported_sell = _parse_expression(sell_text, "sell")
    unsupported_conditions = unsupported_buy + unsupported_sell
    if unsupported_conditions:
        return {
            "ok": False,
            "error": (
                "该策略包含历史回测暂不支持或无法识别的条件："
                f"{'、'.join(unsupported_conditions)}。请修改策略后再回测。"
            ),
            "unsupported_fields": unsupported_conditions,
        }
    if not buy_conditions:
        return {"ok": False, "error": "未识别到可执行的买入条件。"}
    if not sell_conditions:
        return {"ok": False, "error": "请明确卖出条件；回测不会自动补入止损比例。"}
    try:
        _validate_conditions(buy_conditions, "buy")
        _validate_conditions(sell_conditions, "sell")
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "raw_text": raw,
        "buy_conditions": buy_conditions,
        "sell_conditions": sell_conditions,
        "unsupported_fields": [],
    }


def _ema(values: List[float], period: int) -> List[float]:
    alpha = 2.0 / (period + 1)
    output = [values[0]]
    for value in values[1:]:
        output.append(alpha * value + (1 - alpha) * output[-1])
    return output


def _validate_conditions(conditions, side):
    if not isinstance(conditions, list) or not conditions:
        raise ValueError(f"回测{side}条件不能为空")
    for condition in conditions:
        if not isinstance(condition, dict):
            raise ValueError("回测条件格式错误")
        kind = condition.get("type")
        if kind in {"all", "any"}:
            _validate_conditions(condition.get("conditions"), side)
            continue
        if kind not in {"macd_cross", "kdj_cross", "indicator_threshold", "price_vs_ma", "volume_vs_average", "bar_return", "rolling_high_breakout", "entry_return", "price_threshold"}:
            raise ValueError(f"不支持回测条件：{kind}")
        if kind.endswith("_cross") and condition.get("direction") not in {"golden_cross", "death_cross"}:
            raise ValueError("不支持的交叉方向")
        if kind == "entry_return" and side == "buy":
            raise ValueError("买入条件不能依赖尚不存在的持仓收益")
        if kind == "indicator_threshold" and condition.get("indicator") != "rsi":
            raise ValueError("不支持的回测指标")
        if kind in {"price_vs_ma", "volume_vs_average", "rolling_high_breakout"}:
            key = "period" if kind == "price_vs_ma" else "lookback"
            number = condition.get(key)
            if not isinstance(number, int) or isinstance(number, bool) or not 1 <= number <= 10000:
                raise ValueError("历史窗口必须为 1 至 10000 的整数")
        if kind in {"indicator_threshold", "price_vs_ma", "volume_vs_average", "bar_return", "entry_return", "price_threshold"}:
            if condition.get("operator") not in {">", ">=", "<", "<="}:
                raise ValueError("不支持的回测比较符")
        if kind in {"indicator_threshold", "volume_vs_average", "bar_return", "entry_return", "price_threshold"}:
            key = "factor" if kind == "volume_vs_average" else "value"
            number = condition.get(key)
            if not isinstance(number, (int, float)) or isinstance(number, bool) or not math.isfinite(number):
                raise ValueError("回测阈值必须是有限数值")
            if kind in {"volume_vs_average", "price_threshold"} and number <= 0:
                raise ValueError("成交量倍数和价格阈值必须大于零")
            if kind == "indicator_threshold" and not 0 <= number <= 100:
                raise ValueError("RSI 阈值必须在 0 至 100 之间")


def _ma_periods(conditions):
    periods = set()
    for condition in conditions:
        if condition["type"] == "price_vs_ma":
            periods.add(condition["period"])
        elif condition["type"] in {"all", "any"}:
            periods.update(_ma_periods(condition["conditions"]))
    return periods


def _prepare_bars(bars: List[Dict[str, Any]], ma_periods=None) -> None:
    closes = [bar["close"] for bar in bars]
    ema12 = _ema(closes, 12)
    ema26 = _ema(closes, 26)
    dif = [a - b for a, b in zip(ema12, ema26)]
    dea = _ema(dif, 9)
    k = d = 50.0
    for index, bar in enumerate(bars):
        bar["dif"] = dif[index]
        bar["dea"] = dea[index]
        bar["macd"] = 2 * (dif[index] - dea[index])
        for period in ({5, 10, 20, 30, 60} | set(ma_periods or [])):
            bar[f"ma{period}"] = (
                sum(closes[index - period + 1:index + 1]) / period
                if index + 1 >= period else None
            )
        if index >= 14:
            changes = [closes[i] - closes[i - 1] for i in range(index - 13, index + 1)]
            gain = sum(max(value, 0) for value in changes) / 14
            loss = sum(max(-value, 0) for value in changes) / 14
            bar["rsi"] = 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)
        else:
            bar["rsi"] = None
        window = bars[max(0, index - 8):index + 1]
        high = max(item["high"] for item in window)
        low = min(item["low"] for item in window)
        rsv = 50.0 if high == low else (bar["close"] - low) / (high - low) * 100
        k = 2 / 3 * k + 1 / 3 * rsv
        d = 2 / 3 * d + 1 / 3 * k
        bar["kdj_k"] = k
        bar["kdj_d"] = d


def _compare(left: Optional[float], operator: str, right: float) -> bool:
    if left is None or right is None:
        return False
    if math.isclose(left, right, rel_tol=1e-12, abs_tol=1e-12):
        left = right
    return {
        ">": left > right,
        ">=": left >= right,
        "<": left < right,
        "<=": left <= right,
    }.get(operator, False)


def _evaluate(
    condition: Dict[str, Any],
    bars: List[Dict[str, Any]],
    index: int,
    entry_price: Optional[float],
) -> Tuple[bool, str]:
    bar = bars[index]
    previous = bars[index - 1] if index > 0 else None
    kind = condition["type"]
    if kind in {"all", "any"}:
        results = [
            _evaluate(item, bars, index, entry_price)
            for item in condition.get("conditions", [])
        ]
        if kind == "all":
            matched = bool(results) and all(item[0] for item in results)
            reasons = [item[1] for item in results if item[0]]
            return matched, "并且".join(reasons) if reasons else "组合条件未满足"
        matched_results = [item for item in results if item[0]]
        return (
            bool(matched_results),
            "或者".join(item[1] for item in matched_results)
            if matched_results else "任一条件均未满足"
        )
    if kind == "macd_cross" and previous:
        golden = previous["dif"] <= previous["dea"] and bar["dif"] > bar["dea"]
        death = previous["dif"] >= previous["dea"] and bar["dif"] < bar["dea"]
        matched = golden if condition["direction"] == "golden_cross" else death
        return matched, f"MACD{'金叉' if golden else '死叉' if death else '未交叉'}"
    if kind == "kdj_cross" and previous:
        golden = previous["kdj_k"] <= previous["kdj_d"] and bar["kdj_k"] > bar["kdj_d"]
        death = previous["kdj_k"] >= previous["kdj_d"] and bar["kdj_k"] < bar["kdj_d"]
        matched = golden if condition["direction"] == "golden_cross" else death
        return matched, f"KDJ{'金叉' if golden else '死叉' if death else '未交叉'}"
    if kind == "indicator_threshold":
        value = bar.get(condition["indicator"])
        return _compare(value, condition["operator"], condition["value"]), f"RSI={value}"
    if kind == "price_vs_ma":
        value = bar.get(f"ma{condition['period']}")
        return _compare(bar["close"], condition["operator"], value), f"收盘价与MA{condition['period']}"
    if kind == "volume_vs_average":
        lookback = condition["lookback"]
        if index < lookback:
            return False, "均量历史不足"
        average = sum(item["volume"] for item in bars[index - lookback:index]) / lookback
        if average <= 0:
            return False, "历史均量为零，无法比较倍数"
        ratio = bar["volume"] / average
        return (
            _compare(ratio, condition["operator"], condition["factor"]),
            (
                f"当日成交量={bar['volume']:.2f}，"
                f"过去{lookback}日均量={average:.2f}，"
                f"实际倍数={ratio:.4f}倍"
            )
        )
    if kind == "bar_return":
        if not previous or not previous.get("close"):
            return False, "前一根K线数据不足"
        value = (bar["close"] / previous["close"] - 1) * 100
        return (
            _compare(value, condition["operator"], condition["value"]),
            f"单日涨幅={value:.2f}%"
        )
    if kind == "rolling_high_breakout":
        lookback = int(condition["lookback"])
        if index < lookback:
            return False, f"过去{lookback}根K线历史不足"
        previous_high = max(item["high"] for item in bars[index - lookback:index])
        return (
            bar["close"] > previous_high,
            f"收盘价={bar['close']:.4f}，过去{lookback}根最高价={previous_high:.4f}"
        )
    if kind == "entry_return":
        if not entry_price:
            return False, "尚无买入价"
        value = (bar["close"] / entry_price - 1) * 100
        return _compare(value, condition["operator"], condition["value"]), f"持仓收益={value:.2f}%"
    if kind == "price_threshold":
        return _compare(bar["close"], condition["operator"], condition["value"]), f"收盘价={bar['close']}"
    return False, f"不支持条件 {kind}"


def _all_match(conditions, bars, index, entry_price):
    reasons = []
    for condition in conditions:
        matched, reason = _evaluate(condition, bars, index, entry_price)
        if not matched:
            return False, reasons
        reasons.append(reason)
    return True, reasons


def run_backtest(
    symbol: str,
    days: int = 60,
    interval: str = "1d",
    start: Optional[str] = None,
    end: Optional[str] = None,
    strategy_override: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    config = load_config()
    strategy = strategy_override or config.get("strategy", {})
    unsupported = strategy.get("unsupported_fields", [])
    if unsupported:
        raise RuntimeError(
            f"该策略包含历史回测暂不支持字段：{'、'.join(unsupported)}。请修改策略后再回测。"
        )
    buy_conditions = strategy.get("buy_conditions", [])
    sell_conditions = strategy.get("sell_conditions", [])
    if not buy_conditions:
        raise RuntimeError("尚未设置回测策略，请先执行 backtest set。")
    _validate_conditions(buy_conditions, "buy")
    _validate_conditions(sell_conditions, "sell")
    start_date, end_date = resolve_range(days, start, end)
    data = load_bars(symbol, interval, start_date, end_date)
    bars = _validate_bars(data["bars"], data.get("source", "历史行情"))
    bars = [bar for bar in bars if start_date <= bar["time"][:10] <= end_date]
    if interval == "1d" and days and not start:
        bars = bars[-days:]
    if interval in {"1m", "5m"} and days and not start:
        unique_dates = sorted({bar["time"][:10] for bar in bars})
        if len(unique_dates) < days:
            raise RuntimeError(
                f"{interval} 历史接口仅返回 {len(unique_dates)} 个交易日，少于请求的 {days} 天；"
                "请缩短回测天数或使用 1d。"
            )
        selected = set(unique_dates[-days:])
        bars = [bar for bar in bars if bar["time"][:10] in selected]
    if len(bars) < 3:
        raise RuntimeError(f"历史K线数量不足：仅 {len(bars)} 根")
    _prepare_bars(bars, _ma_periods(buy_conditions + sell_conditions))

    defaults = config.get("defaults", {})
    initial = float(defaults.get("initial_capital", 100000))
    commission = float(defaults.get("commission_rate", 0.0003))
    slippage = float(defaults.get("slippage_rate", 0.0002))
    if not math.isfinite(initial) or initial <= 0 or not 0 <= commission < 1 or not 0 <= slippage < 1:
        raise ValueError("初始资金必须为正数，佣金与滑点必须为 [0, 1) 内有限比例")
    cash = initial
    quantity = 0
    entry_price = None
    entry_time = None
    entry_index = None
    entry_equity = None
    buy_reason = ""
    pending = None
    trades = []
    equity_curve = []

    for index, bar in enumerate(bars):
        if pending and pending["execute_index"] <= index:
            # Conservative T+1 model for the supported domestic shares/ETFs.
            sell_locked = pending["side"] == "sell" and entry_time and bar["time"][:10] <= entry_time[:10]
            if bar["volume"] <= 0 or sell_locked:
                pending["execute_index"] = index + 1
                equity_curve.append({"time": bar["time"], "equity": round(cash + quantity * bar["close"], 2)})
                continue
            if pending["side"] == "buy" and quantity == 0:
                entry_equity = cash
                fill = bar["open"] * (1 + slippage)
                quantity = int(cash / (fill * (1 + commission)) / 100) * 100
                if quantity > 0:
                    cost = quantity * fill
                    cash -= cost + cost * commission
                    entry_price = fill
                    entry_time = bar["time"]
                    entry_index = index
                    buy_reason = pending["reason"]
            elif pending["side"] == "sell" and quantity > 0:
                fill = bar["open"] * (1 - slippage)
                proceeds = quantity * fill
                cash += proceeds - proceeds * commission
                basis = quantity * entry_price * (1 + commission)
                net_profit = cash - entry_equity
                return_percent = net_profit / basis * 100 if basis else 0
                trades.append(TradeRecord(
                    buy_time=entry_time,
                    buy_price=round(entry_price, 4),
                    sell_time=bar["time"],
                    sell_price=round(fill, 4),
                    quantity=quantity,
                    net_profit=round(net_profit, 2),
                    return_percent=round(return_percent, 4),
                    holding_bars=index - entry_index,
                    buy_reason=buy_reason,
                    sell_reason=pending["reason"],
                ))
                quantity = 0
                entry_price = entry_time = entry_index = entry_equity = None
            pending = None

        equity_curve.append({
            "time": bar["time"],
            "equity": round(cash + quantity * bar["close"], 2),
        })
        if index >= len(bars) - 1 or pending:
            continue
        if quantity == 0:
            matched, reasons = _all_match(buy_conditions, bars, index, None)
            if matched:
                pending = {
                    "side": "buy",
                    "execute_index": index + 1,
                    "reason": "；".join(reasons),
                }
        else:
            matched, reasons = _all_match(sell_conditions, bars, index, entry_price)
            if matched:
                pending = {
                    "side": "sell",
                    "execute_index": index + 1,
                    "reason": "；".join(reasons),
                }

    if quantity > 0 and bars[-1]["time"][:10] > entry_time[:10] and bars[-1]["volume"] > 0:
        bar = bars[-1]
        fill = bar["close"] * (1 - slippage)
        proceeds = quantity * fill
        cash += proceeds - proceeds * commission
        basis = quantity * entry_price * (1 + commission)
        net_profit = cash - entry_equity
        return_percent = (fill * (1 - commission) / (entry_price * (1 + commission)) - 1) * 100
        trades.append(TradeRecord(
            buy_time=entry_time,
            buy_price=round(entry_price, 4),
            sell_time=bar["time"],
            sell_price=round(fill, 4),
            quantity=quantity,
            net_profit=round(net_profit, 2),
            return_percent=round(return_percent, 4),
            holding_bars=len(bars) - 1 - entry_index,
            buy_reason=buy_reason,
            sell_reason="回测结束按最后收盘价强制平仓",
            forced_exit=True,
        ))
        quantity = 0
        equity_curve[-1]["equity"] = round(cash, 2)

    final_equity = cash + quantity * bars[-1]["close"]
    metrics = calculate_metrics(initial, final_equity, trades, equity_curve)
    assumptions = {
        "signal_timing": "收盘确认信号，最早下一根开盘成交",
        "settlement": "保守 T+1：买入当日不卖出；不适用于需要 T+0 的策略",
        "lot_size": 100,
        "limitations": ["未模拟涨跌停封单成交、逐笔流动性、印花税、分红送转权益及不同证券最小申报数量"],
        "price_adjustment": data.get("price_adjustment", "unknown"),
        "volume_unit": data.get("volume_unit", "unknown"),
    }
    if data.get("price_adjustment") != "qfq":
        assumptions["limitations"].append("历史价格未确认前复权，除权除息可能影响收益与信号")
    open_position = None if quantity == 0 else {
        "quantity": quantity, "entry_time": entry_time, "entry_price": entry_price,
        "mark_price": bars[-1]["close"], "unrealized": True,
        "reason": "期末无法满足 T+1 或有效成交量条件，保留持仓按收盘价估值",
    }
    result_id = hashlib.sha1(
        f"{symbol}:{interval}:{start_date}:{end_date}:{datetime.now().isoformat()}".encode()
    ).hexdigest()[:16]
    result = BacktestResult(
        result_id=result_id,
        symbol=symbol,
        interval=interval,
        start=bars[0]["time"],
        end=bars[-1]["time"],
        strategy_text=strategy.get("raw_text", ""),
        initial_capital=initial,
        final_equity=round(final_equity, 2),
        metrics=metrics,
        trades=trades,
        bars=bars,
        equity_curve=equity_curve,
        data_source=data["source"],
        cache_path=data.get("cache_path"),
        cache_time=data.get("cache_time"),
        cache_stale=bool(data.get("cache_stale")),
        completed_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )
    result_dict = result.to_dict()
    result_dict.update(execution_assumptions=assumptions, open_position=open_position, cash=round(cash, 2))
    result_path = save_last_result(result_dict)
    summary = result.to_dict(include_bars=False)
    summary.update(execution_assumptions=assumptions, open_position=open_position, cash=round(cash, 2))
    summary["result_path"] = result_path
    summary["trade_summary"] = [trade.to_dict() for trade in trades[:20]]
    summary["回测指标"] = {
        "交易次数": metrics.trade_count,
        "胜率%": metrics.win_rate,
        "总收益率%": metrics.total_return_percent,
        "最大回撤%": metrics.max_drawdown_percent,
        "平均单笔收益%": metrics.average_trade_return_percent,
        "最好单笔%": metrics.best_trade_percent,
        "最差单笔%": metrics.worst_trade_percent,
        "平均持仓K线数": metrics.average_holding_bars,
    }
    summary["交易明细摘要"] = summary["trade_summary"]
    return summary
