"""TOML settings with STOCKSWEEPER_* environment overrides."""

from __future__ import annotations

import os
import tomllib
from datetime import date
from importlib.resources import files
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, Field, model_validator

TICKER_PATTERN = r"^[A-Z0-9][A-Z0-9.\-]{0,11}$"
Ticker = Annotated[str, Field(pattern=TICKER_PATTERN)]


def repo_root() -> Path:
    override = os.environ.get("STOCKSWEEPER_ROOT")
    if override:
        return Path(override)
    source_root = Path(__file__).resolve().parents[3]
    if (source_root / "backend" / "pyproject.toml").is_file():
        return source_root
    for candidate in (Path.cwd(), Path.cwd().parent):
        if (candidate / "backend" / "pyproject.toml").is_file():
            return candidate
    return Path.cwd()


class MarketSettings(BaseModel):
    tickers: list[Ticker] = Field(default_factory=lambda: ["IREN", "CIFR", "WULF", "NBIS"])
    interval: str = "1d"
    overlap_bars: int = 5
    start_dates: dict[Ticker, date] = Field(default_factory=dict)


class BacktestSettings(BaseModel):
    initial_capital: float = 100_000
    fees: float = 0.001
    slippage: float = 0.001
    periods_per_year: int = 252


class ValidationSettings(BaseModel):
    train: float = 0.6
    validation: float = 0.2
    test: float = 0.2
    limited_history_bars: int = 750
    limited_train: float = 0.5
    limited_validation: float = 0.25
    limited_test: float = 0.25
    wf_folds: int = 5


class GeneratorSettings(BaseModel):
    max_strategies: int = 20000
    seed: int = 7


class GateSettings(BaseModel):
    min_trades: int = 20
    min_val_trades: int = 5
    max_drawdown: float = 0.60
    min_degradation: float = 0.4
    min_stability: float = 0.5


class RobustnessWeights(BaseModel):
    sharpe: float = 0.25
    cagr: float = 0.15
    max_drawdown: float = 0.15
    profit_factor: float = 0.10
    trade_sufficiency: float = 0.10
    walk_forward: float = 0.15
    stability: float = 0.10

    @model_validator(mode="after")
    def weights_sum_to_one(self) -> RobustnessWeights:
        total = (
            self.sharpe
            + self.cagr
            + self.max_drawdown
            + self.profit_factor
            + self.trade_sufficiency
            + self.walk_forward
            + self.stability
        )
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"robustness weights must sum to 1, got {total}")
        return self


class CrossTickerSettings(BaseModel):
    penalty_k: float = 0.5
    min_tickers: int = 4


class Settings(BaseModel):
    data_dir: Path = Path(".local/research")
    market: MarketSettings = Field(default_factory=MarketSettings)
    backtest: BacktestSettings = Field(default_factory=BacktestSettings)
    validation: ValidationSettings = Field(default_factory=ValidationSettings)
    generator: GeneratorSettings = Field(default_factory=GeneratorSettings)
    gates: GateSettings = Field(default_factory=GateSettings)
    robustness: RobustnessWeights = Field(default_factory=RobustnessWeights)
    cross_ticker: CrossTickerSettings = Field(default_factory=CrossTickerSettings)

    def resolved_data_dir(self) -> Path:
        if self.data_dir.is_absolute():
            return self.data_dir
        return repo_root() / self.data_dir


def load_settings(path: Path | None = None) -> Settings:
    config_text = (
        path.read_text() if path else files("stocksweeper").joinpath("defaults.toml").read_text()
    )
    raw = tomllib.loads(config_text)
    weights = raw.get("robustness", {}).pop("weights", None)
    if weights is not None:
        raw["robustness"] = weights
    elif "robustness" in raw and not raw["robustness"]:
        raw.pop("robustness")
    env_dir = os.environ.get("STOCKSWEEPER_DATA_DIR")
    if env_dir:
        raw["data_dir"] = env_dir
    return Settings.model_validate(raw)
