from typing import Dict, List

from .models import PerformanceMetrics, TradeRecord


def calculate_metrics(
    initial_capital: float,
    final_equity: float,
    trades: List[TradeRecord],
    equity_curve: List[Dict],
) -> PerformanceMetrics:
    returns = [trade.return_percent for trade in trades]
    wins = [value for value in returns if value > 0]
    peak = initial_capital
    max_drawdown = 0.0
    for point in equity_curve:
        equity = float(point["equity"])
        peak = max(peak, equity)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - equity) / peak * 100)
    return PerformanceMetrics(
        trade_count=len(trades),
        win_rate=round(len(wins) / len(trades) * 100, 4) if trades else 0.0,
        total_return_percent=round((final_equity / initial_capital - 1) * 100, 4),
        max_drawdown_percent=round(max_drawdown, 4),
        average_trade_return_percent=round(sum(returns) / len(returns), 4) if returns else 0.0,
        best_trade_percent=round(max(returns), 4) if returns else 0.0,
        worst_trade_percent=round(min(returns), 4) if returns else 0.0,
        average_holding_bars=round(
            sum(trade.holding_bars for trade in trades) / len(trades), 2
        ) if trades else 0.0,
    )
