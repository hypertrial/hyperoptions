"""Offline NGBoost training and safe, bounded live inference.

The current public Yahoo cohort is deliberately not licensed for this training
path. A qualified frozen cohort is required before an artifact can be built.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from math import exp, isfinite, log
from pathlib import Path
from uuid import uuid4

import numpy as np
import polars as pl

from stocksweeper.forecast.provenance import load_training_cohort

VERSION = "ngboost-pooled-v1"
FEATURES = (
    "horizon",
    "last_return",
    "mean_return_5",
    "mean_return_22",
    "volatility_5",
    "volatility_22",
    "range_variance_5",
    "range_variance_22",
    "volume_ratio_log",
)
MAX_ARTIFACT_BYTES = 2_000_000
MAX_STAGES = 100
MIN_TICKERS = 20


def _features(frame: pl.DataFrame, index: int, horizon: int) -> tuple[float, ...] | None:
    if index < 22 or not 1 <= horizon <= 25:
        return None
    tail = frame.slice(index - 22, 23)
    if tail.height != 23 or any(float(value) > 0 for value in tail["stock_splits"]):
        return None
    close = np.asarray(tail["close"].to_list(), dtype=float)
    high = np.asarray(tail["high"].to_list(), dtype=float)
    low = np.asarray(tail["low"].to_list(), dtype=float)
    volume = np.asarray(tail["volume"].to_list(), dtype=float)
    if (
        not all(np.isfinite(values).all() for values in (close, high, low, volume))
        or np.any(close <= 0)
        or np.any(low <= 0)
        or np.any(volume <= 0)
    ):
        return None
    returns = np.diff(np.log(close))
    range_variance = np.log(high[1:] / low[1:]) ** 2 / (4 * log(2))
    features = (
        float(horizon),
        float(returns[-1]),
        float(returns[-5:].mean()),
        float(returns.mean()),
        float(returns[-5:].std()),
        float(returns.std()),
        float(range_variance[-5:].mean()),
        float(range_variance.mean()),
        float(log(volume[-1] / volume[1:].mean())),
    )
    return features if all(map(isfinite, features)) else None


def _cohort_digest(cohort: tuple[tuple[str, pl.DataFrame, str], ...]) -> str:
    return hashlib.sha256(
        "|".join(f"{ticker}:{digest}" for ticker, _, digest in sorted(cohort)).encode()
    ).hexdigest()


def _training_rows(
    cohort: tuple[tuple[str, pl.DataFrame, str], ...],
) -> tuple[np.ndarray, np.ndarray, date, str]:
    if len(cohort) < MIN_TICKERS or len(cohort) > 100:
        raise ValueError("pooled_training_cohort_size_invalid")
    if len({ticker for ticker, _, _ in cohort}) != len(cohort):
        raise ValueError("pooled_training_cohort_duplicate")
    through = {frame["ts"][-1] for _, frame, _ in cohort}
    if len(through) != 1:
        raise ValueError("pooled_training_vintage_mismatch")
    inputs: list[tuple[float, ...]] = []
    targets: list[float] = []
    for _, frame, _ in cohort:
        if frame.height < 500 or frame.height > 756:
            raise ValueError("pooled_training_history_invalid")
        close = np.asarray(frame["close"].to_list(), dtype=float)
        splits = np.asarray(frame["stock_splits"].to_list(), dtype=float)
        # Fixed, evenly spaced origins keep training size and ticker weight bounded.
        for origin in sorted(set(np.linspace(60, frame.height - 26, 12, dtype=int))):
            features = _features(frame, int(origin), 1)
            if features is None:
                continue
            for horizon in range(1, 26):
                if np.any(splits[origin + 1 : origin + horizon + 1] > 0):
                    continue
                outcome = float(log(close[origin + horizon] / close[origin]))
                if isfinite(outcome):
                    inputs.append((float(horizon), *features[1:]))
                    targets.append(outcome)
    if len(inputs) < 5_000:
        raise ValueError("pooled_training_support_insufficient")
    return np.asarray(inputs), np.asarray(targets), through.pop(), _cohort_digest(cohort)


def _tree_payload(tree: object) -> dict[str, list[float] | list[int]]:
    raw = tree.tree_
    return {
        "left": raw.children_left.tolist(),
        "right": raw.children_right.tolist(),
        "feature": raw.feature.tolist(),
        "threshold": raw.threshold.tolist(),
        "value": raw.value[:, 0, 0].tolist(),
    }


def _export_model(
    fitted: object,
    through: date,
    cohort_hash: str,
    bounds: tuple[np.ndarray, np.ndarray],
    rights: dict[str, str],
) -> dict[str, object]:
    return {
        "version": VERSION,
        "features": FEATURES,
        "training_through": through.isoformat(),
        "cohort_hash": cohort_hash,
        "cohort_manifest_hash": rights["manifest_hash"],
        "rights_status": "approved_for_training",
        "source_name": rights["source_name"],
        "license_reference": rights["license_reference"],
        "initial": np.asarray(fitted.init_params, dtype=float).tolist(),
        "learning_rate": float(fitted.learning_rate),
        "lower": bounds[0].tolist(),
        "upper": bounds[1].tolist(),
        "stages": [
            {
                "scaling": float(scale),
                "columns": np.asarray(columns, dtype=int).tolist(),
                "trees": [_tree_payload(tree) for tree in models],
            }
            for models, scale, columns in zip(
                fitted.base_models, fitted.scalings, fitted.col_idxs, strict=True
            )
        ],
    }


def _validate_model(payload: dict[str, object]) -> None:
    if (
        payload.get("version") != VERSION
        or tuple(payload.get("features", ())) != FEATURES
        or payload.get("rights_status") != "approved_for_training"
        or not isinstance(payload.get("source_name"), str)
        or not payload["source_name"]
        or not isinstance(payload.get("license_reference"), str)
        or not payload["license_reference"]
        or not isinstance(payload.get("cohort_hash"), str)
        or len(payload["cohort_hash"]) != 64
        or not isinstance(payload.get("cohort_manifest_hash"), str)
        or len(payload["cohort_manifest_hash"]) != 64
        or any(character not in "0123456789abcdef" for character in payload["cohort_hash"])
        or any(character not in "0123456789abcdef" for character in payload["cohort_manifest_hash"])
    ):
        raise ValueError("pooled_artifact_invalid")
    date.fromisoformat(str(payload["training_through"]))
    initial = payload["initial"]
    lower, upper = payload["lower"], payload["upper"]
    rate = float(payload["learning_rate"])
    stages = payload["stages"]
    if (
        len(initial) != 2
        or len(lower) != len(FEATURES)
        or len(upper) != len(FEATURES)
        or not 0 < rate <= 1
        or not 1 <= len(stages) <= MAX_STAGES
        or not all(isfinite(float(value)) for value in (*initial, *lower, *upper))
        or any(float(low) > float(high) for low, high in zip(lower, upper, strict=True))
    ):
        raise ValueError("pooled_artifact_invalid")
    for stage in stages:
        columns = stage["columns"]
        if (
            not isfinite(float(stage["scaling"]))
            or not columns
            or any(
                not isinstance(column, int) or not 0 <= column < len(FEATURES) for column in columns
            )
            or len(stage["trees"]) != 2
        ):
            raise ValueError("pooled_artifact_invalid")
        for tree in stage["trees"]:
            left, right, feature, threshold, value = (
                tree[name] for name in ("left", "right", "feature", "threshold", "value")
            )
            size = len(left)
            if (
                not 1 <= size <= 63
                or any(len(items) != size for items in (right, feature, threshold, value))
                or not all(isfinite(float(item)) for item in (*threshold, *value))
            ):
                raise ValueError("pooled_artifact_invalid")
            for node in range(size):
                if left[node] == -1 and right[node] == -1:
                    continue
                if (
                    not isinstance(left[node], int)
                    or not isinstance(right[node], int)
                    or not node < left[node] < size
                    or not node < right[node] < size
                    or not isinstance(feature[node], int)
                    or not 0 <= feature[node] < len(columns)
                ):
                    raise ValueError("pooled_artifact_invalid")


def _artifact_dir(data_dir: Path) -> Path:
    return data_dir / "forecast" / "training" / "ngboost"


def train_pooled_model(data_dir: Path) -> Path:
    """Train only from a rights-qualified immutable cohort; never from live requests."""
    cohort = load_training_cohort(data_dir, require_training_rights=True)
    metadata = json.loads((data_dir / "forecast" / "training" / "cohort.json").read_text())
    if (
        metadata.get("rights_status") != "approved_for_training"
        or not metadata.get("source_name")
        or not metadata.get("license_reference")
        or not metadata.get("manifest_hash")
    ):
        raise ValueError("training_source_rights_unverified")
    from ngboost import NGBRegressor
    from ngboost.distns import Normal
    from sklearn.tree import DecisionTreeRegressor

    inputs, targets, through, cohort_hash = _training_rows(cohort)
    fitted = NGBRegressor(
        Dist=Normal,
        Base=DecisionTreeRegressor(max_depth=2, min_samples_leaf=40, random_state=0),
        n_estimators=60,
        learning_rate=0.05,
        random_state=0,
        verbose=False,
    ).fit(inputs, targets)
    margins = np.maximum(np.ptp(inputs, axis=0) * 0.25, 1e-12)
    payload = _export_model(
        fitted,
        through,
        cohort_hash,
        (inputs.min(axis=0) - margins, inputs.max(axis=0) + margins),
        metadata,
    )
    _validate_model(payload)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if len(encoded) > MAX_ARTIFACT_BYTES:
        raise ValueError("pooled_artifact_too_large")
    digest = hashlib.sha256(encoded).hexdigest()
    root = _artifact_dir(data_dir)
    root.mkdir(parents=True, exist_ok=True)
    output = root / f"model-{digest}.json"
    if output.exists():
        if output.read_bytes() != encoded:
            raise ValueError("pooled_artifact_hash_conflict")
    else:
        temporary = root / f".{uuid4().hex}.tmp"
        temporary.write_bytes(encoded)
        temporary.replace(output)
    pointer = root / "latest.json"
    temporary = root / f".{uuid4().hex}.tmp"
    temporary.write_text(json.dumps({"sha256": digest}, sort_keys=True))
    temporary.replace(pointer)
    return output


def load_pooled_model(data_dir: Path) -> dict[str, object]:
    root = _artifact_dir(data_dir)
    pointer = root / "latest.json"
    if not pointer.exists():
        raise FileNotFoundError("pooled_training_artifact_unavailable")
    if pointer.stat().st_size > 1024:
        raise ValueError("pooled_artifact_invalid")
    digest = json.loads(pointer.read_text())["sha256"]
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise ValueError("pooled_artifact_invalid")
    path = root / f"model-{digest}.json"
    if not path.is_file():
        raise ValueError("pooled_artifact_invalid")
    if path.stat().st_size > MAX_ARTIFACT_BYTES:
        raise ValueError("pooled_artifact_invalid")
    encoded = path.read_bytes()
    if hashlib.sha256(encoded).hexdigest() != digest:
        raise ValueError("pooled_artifact_invalid")
    payload = json.loads(encoded)
    if not isinstance(payload, dict):
        raise ValueError("pooled_artifact_invalid")
    _validate_model(payload)
    # An artifact's self-described license is insufficient: verify the frozen
    # cohort with the qualified loader before making its predictions live.
    cohort = load_training_cohort(data_dir, require_training_rights=True)
    manifest = json.loads((data_dir / "forecast" / "training" / "cohort.json").read_text())
    if (
        _cohort_digest(cohort) != payload["cohort_hash"]
        or manifest.get("manifest_hash") != payload["cohort_manifest_hash"]
        or manifest.get("source_name") != payload["source_name"]
        or manifest.get("license_reference") != payload["license_reference"]
    ):
        raise ValueError("pooled_artifact_provenance_mismatch")
    return payload


def _tree_predict(
    tree: dict[str, object], columns: list[int], features: tuple[float, ...]
) -> float:
    node = 0
    for _ in range(len(tree["left"])):
        if tree["left"][node] == -1:
            return float(tree["value"][node])
        feature = features[columns[tree["feature"][node]]]
        node = tree["left"][node] if feature <= tree["threshold"][node] else tree["right"][node]
    raise ValueError("pooled_artifact_invalid")


def predict_pooled_params(
    payload: dict[str, object], frame: pl.DataFrame, horizon: int, as_of: date
) -> tuple[float, float]:
    if date.fromisoformat(payload["training_through"]) > as_of:
        raise ValueError("pooled_artifact_future_leak")
    features = _features(frame, frame.height - 1, horizon)
    if features is None:
        raise ValueError("pooled_inputs_invalid")
    if any(
        value < low or value > high
        for value, low, high in zip(features, payload["lower"], payload["upper"], strict=True)
    ):
        raise ValueError("pooled_inputs_out_of_domain")
    parameters = [float(value) for value in payload["initial"]]
    for stage in payload["stages"]:
        factor = payload["learning_rate"] * stage["scaling"]
        for index, tree in enumerate(stage["trees"]):
            parameters[index] -= factor * _tree_predict(tree, stage["columns"], features)
    mean, log_scale = parameters
    if not all(map(isfinite, parameters)) or not -20 < log_scale < 5:
        raise ValueError("pooled_parameters_invalid")
    return mean, exp(log_scale)


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m stocksweeper.forecast.pooled_ngboost DATA_DIR")
    print(train_pooled_model(Path(sys.argv[1])))
