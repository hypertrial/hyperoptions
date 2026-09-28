"""Qualified pooled training is offline, causal, and pickle-free."""

from __future__ import annotations

import json
from datetime import date
from math import log

import numpy as np
import polars as pl
import pytest

from stocksweeper.forecast import pooled_ngboost
from stocksweeper.forecast.calendar import SessionCalendar
from stocksweeper.forecast.provenance import SourceRightsUnverified


def _bars(count: int, last: date) -> pl.DataFrame:
    sessions = SessionCalendar().sessions(date(2020, 1, 1), last)[-count:]
    returns = np.random.default_rng(17).standard_t(5, count) * 0.012
    closes = (100 * np.exp(np.cumsum(returns))).tolist()
    return pl.DataFrame(
        {
            "ts": sessions,
            "open": closes,
            "high": [price * 1.01 for price in closes],
            "low": [price * 0.99 for price in closes],
            "close": closes,
            "volume": [1000.0] * count,
            "dividends": [0.0] * count,
            "stock_splits": [0.0] * count,
        }
    )


def test_current_unqualified_cohort_cannot_train(tmp_path, monkeypatch):
    def denied(*_args, **_kwargs):
        raise SourceRightsUnverified("training_rights_unverified")

    monkeypatch.setattr(pooled_ngboost, "load_training_cohort", denied)
    with pytest.raises(SourceRightsUnverified, match="training_rights_unverified"):
        pooled_ngboost.train_pooled_model(tmp_path)
    assert not (tmp_path / "forecast" / "training" / "ngboost").exists()


def test_qualified_synthetic_cohort_trains_and_roundtrips_without_pickle(tmp_path, monkeypatch):
    session = date(2026, 9, 25)
    frame = _bars(520, session)
    cohort = tuple((f"FIXTURE{i}", frame, f"{i:064x}") for i in range(20))
    monkeypatch.setattr(pooled_ngboost, "load_training_cohort", lambda *_args, **_kwargs: cohort)
    manifest = tmp_path / "forecast" / "training" / "cohort.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps(
            {
                "rights_status": "approved_for_training",
                "source_name": "Licensed synthetic fixture",
                "license_reference": "test-license",
                "manifest_hash": "f" * 64,
            }
        )
    )
    fitted = {}
    original_export = pooled_ngboost._export_model

    def capture(model, *args):
        fitted["model"] = model
        return original_export(model, *args)

    monkeypatch.setattr(pooled_ngboost, "_export_model", capture)
    path = pooled_ngboost.train_pooled_model(tmp_path)
    assert path.suffix == ".json"
    assert path.stat().st_size < pooled_ngboost.MAX_ARTIFACT_BYTES
    artifact = pooled_ngboost.load_pooled_model(tmp_path)
    mean, scale = pooled_ngboost.predict_pooled_params(artifact, frame, 5, session)
    assert np.isfinite(mean) and np.isfinite(scale) and scale > 0
    features = pooled_ngboost._features(frame, frame.height - 1, 5)
    direct = fitted["model"].pred_param(np.asarray([features]))[0]
    np.testing.assert_allclose((mean, log(scale)), direct, rtol=1e-12, atol=1e-12)
    assert (mean, scale) == pooled_ngboost.predict_pooled_params(artifact, frame, 5, session)
    with pytest.raises(ValueError, match="pooled_artifact_future_leak"):
        pooled_ngboost.predict_pooled_params(artifact, frame, 5, date(2026, 9, 24))
    with pytest.raises(ValueError, match="pooled_inputs_invalid"):
        pooled_ngboost.predict_pooled_params(artifact, frame, 26, session)

    def denied(*_args, **_kwargs):
        raise SourceRightsUnverified("training_rights_unverified")

    monkeypatch.setattr(pooled_ngboost, "load_training_cohort", denied)
    with pytest.raises(SourceRightsUnverified, match="training_rights_unverified"):
        pooled_ngboost.load_pooled_model(tmp_path)
    monkeypatch.setattr(pooled_ngboost, "load_training_cohort", lambda *_args, **_kwargs: cohort)

    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="pooled_artifact_invalid"):
        pooled_ngboost.load_pooled_model(tmp_path)
