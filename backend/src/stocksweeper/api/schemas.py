"""API request and response models."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from stocksweeper.config import Ticker
from stocksweeper.strategy.model import ConditionGroup


class GateView(BaseModel):
    min_trades: int
    min_val_trades: int
    max_drawdown: float
    min_degradation: float
    min_stability: float


class ConfigView(BaseModel):
    tickers: list[str]
    interval: str
    initial_capital: float
    fees: float
    slippage: float
    max_strategies: int
    gates: GateView
    robustness_weights: dict[str, float]
    cross_ticker_min: int


class UpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    full_refresh: bool = False
    tickers: list[Ticker] | None = Field(default=None, min_length=1, max_length=20)


class BacktestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_strategies: int | None = Field(default=None, ge=1, le=20000)
    tickers: list[Ticker] | None = Field(default=None, min_length=1, max_length=20)


class JobView(BaseModel):
    id: str
    kind: str
    state: Literal["queued", "running", "succeeded", "failed"]
    progress: float
    message: str
    error: str | None = None
    run_id: str | None = None


class TickerStatusView(BaseModel):
    ticker: str
    bars: int
    first: date | None
    last: date | None
    has_indicators: bool
    limited_history: bool


class RunView(BaseModel):
    id: str
    created_at: datetime
    status: str
    strategy_count: int
    ticker_count: int


class RunTickerView(BaseModel):
    ticker: str
    n_bars: int
    first_ts: date | None = None
    last_ts: date | None = None
    limited_history: bool
    survivors: int


class OverviewCard(BaseModel):
    ticker: str
    bars: int
    first: date | None
    last: date | None
    limited_history: bool
    strategy_id: str | None = None
    strategy_name: str | None = None
    signals: str | None = None
    robustness: float | None = None
    oos_cagr: float | None = None
    sharpe: float | None = None
    max_drawdown: float | None = None
    trades: int | None = None
    buy_hold_cagr: float | None = None
    buy_hold_max_drawdown: float | None = None
    test_cagr: float | None = None
    test_max_drawdown: float | None = None
    test_trades: int | None = None
    test_buy_hold_cagr: float | None = None
    entry_signals: list[str] = Field(default_factory=list)
    filter_signals: list[str] = Field(default_factory=list)
    exit_signals: list[str] = Field(default_factory=list)
    exit_kind: str = "mirror"


class LeaderboardItem(BaseModel):
    rank: int | None = None
    ticker: str
    strategy_id: str
    strategy: str
    family: str
    signals: str
    parameters: str
    robustness: float
    rejected: bool
    flags: list[str]
    oos_cagr: float | None = None
    sharpe: float | None = None
    max_drawdown: float | None = None
    win_rate: float | None = None
    trades: int | None = None
    test_cagr: float | None = None
    variants: int = 1
    entry_signals: list[str] = Field(default_factory=list)
    filter_signals: list[str] = Field(default_factory=list)
    exit_signals: list[str] = Field(default_factory=list)
    exit_kind: str = "mirror"


class Leaderboard(BaseModel):
    run_id: str
    total: int
    families: list[str]
    items: list[LeaderboardItem]


class CrossTickerPeer(BaseModel):
    score: float
    rejected: bool
    sharpe: float | None
    limited: bool


class CrossTickerItem(BaseModel):
    strategy_id: str
    strategy: str
    family: str
    signals: str
    parameters: str
    cross_score: float
    per_ticker: dict[str, CrossTickerPeer]
    entry_signals: list[str] = Field(default_factory=list)
    filter_signals: list[str] = Field(default_factory=list)
    exit_signals: list[str] = Field(default_factory=list)
    exit_kind: str = "mirror"


class SegmentBound(BaseModel):
    start: str | None
    end: str | None


class SegmentView(BaseModel):
    segment: str
    cagr: float | None
    total_return: float | None
    sharpe: float | None
    sortino: float | None
    max_drawdown: float | None
    calmar: float | None
    win_rate: float | None
    profit_factor: float | None
    avg_trade: float | None
    median_trade: float | None
    n_trades: int | None
    exposure: float | None
    avg_holding_period: float | None
    buy_hold_cagr: float | None
    buy_hold_max_drawdown: float | None


class FoldView(BaseModel):
    fold: int
    is_sharpe: float | None
    oos_sharpe: float | None
    oos_return: float | None


class ReoptView(BaseModel):
    fold: int
    strategy_id: str
    oos_sharpe: float | None
    oos_return: float | None


class TradeView(BaseModel):
    entry_date: str
    exit_date: str
    entry_price: float
    exit_price: float
    return_: float = Field(alias="return")
    pnl: float
    holding_bars: int


class StrategyDetail(BaseModel):
    run_id: str
    ticker: str
    strategy_id: str
    name: str
    family: str
    signals: str
    entry_signals: list[str] = Field(default_factory=list)
    filter_signals: list[str] = Field(default_factory=list)
    exit_signals: list[str] = Field(default_factory=list)
    exit_kind: str = "mirror"
    parameters: dict[str, int | float]
    entry: ConditionGroup
    exit: ConditionGroup
    robustness: float | None = None
    degradation: float | None = None
    stability: float | None = None
    walk_forward_consistency: float | None = None
    rejected: bool
    flags: list[str]
    rank: int | None = None
    data_snapshot: Literal["exact", "changed"] = "exact"
    segment_bounds: dict[str, SegmentBound] = Field(default_factory=dict)
    segments: list[SegmentView]
    folds: list[FoldView]
    reopt: list[ReoptView]
    dates: list[str]
    close: list[float]
    entry_marks: list[float | None]
    exit_marks: list[float | None]
    equity: list[float]
    buy_hold: list[float]
    drawdown: list[float]
    trades: list[TradeView]
