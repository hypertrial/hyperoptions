import pytest

import stocksweeper.forecast.ledger as ledger_module
import stocksweeper.forecast.physical_evaluation as evaluation
from stocksweeper.forecast.ledger import ForecastLedger
from stocksweeper.storage.db import DB_LOCK

from .test_forecast_ledger import T0


def test_crps_computes_outside_database_lock_and_keeps_batches(tmp_path, monkeypatch):
    ledger = ForecastLedger(tmp_path)
    digests = [ledger.record_distribution((40.0 + i, 44.0 + i), (0.4, 0.6), T0) for i in range(65)]
    original_rows, original_crps = ledger_module.rows, evaluation.crps
    batches = []

    def read(db, sql, params=None):
        assert DB_LOCK.locked()
        if "FROM forecast_distributions" in sql:
            batches.append(len(params))
        return original_rows(db, sql, params)

    def score(*args):
        assert not DB_LOCK.locked()
        return original_crps(*args)

    monkeypatch.setattr(ledger_module, "rows", read)
    monkeypatch.setattr(evaluation, "crps", score)
    scored = ledger.crps_scores((digest, "42") for digest in digests)
    assert batches == [64, 1]
    assert len(scored) == 65
    assert scored[(digests[0], "42")] == pytest.approx(1.04)

