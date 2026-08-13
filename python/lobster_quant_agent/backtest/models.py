from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class TradeRecord:
    buy_time: str
    buy_price: float
    sell_time: str
    sell_price: float
    quantity: int
    net_profit: float
    return_percent: float
    holding_bars: int
    buy_reason: str
    sell_reason: str
    forced_exit: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class PerformanceMetrics:
    trade_count: int = 0
    win_rate: float = 0.0
    total_return_percent: float = 0.0
    max_drawdown_percent: float = 0.0
    average_trade_return_percent: float = 0.0
    best_trade_percent: float = 0.0
    worst_trade_percent: float = 0.0
    average_holding_bars: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class BacktestResult:
    result_id: str
    symbol: str
    interval: str
    start: str
    end: str
    strategy_text: str
    initial_capital: float
    final_equity: float
    metrics: PerformanceMetrics
    trades: List[TradeRecord] = field(default_factory=list)
    bars: List[Dict[str, Any]] = field(default_factory=list)
    equity_curve: List[Dict[str, Any]] = field(default_factory=list)
    data_source: str = ""
    cache_path: Optional[str] = None
    cache_time: Optional[str] = None
    cache_stale: bool = False
    completed_at: str = ""
    chart_path: Optional[str] = None

    def to_dict(self, include_bars: bool = True) -> Dict[str, Any]:
        data = asdict(self)
        if not include_bars:
            data.pop("bars", None)
            data.pop("equity_curve", None)
        return data
