"""Small immutable-artifact primitives shared by freeze and run.

Hashes cover analytical files and settings. The caller places measured resource
use and wall-clock dates under ``runtime``; those are deliberately not analytical.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import ctypes
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

import pyarrow.parquet as pq

_NAME = re.compile(r"[a-z][a-z0-9_-]*\.(?:parquet|json|md)\Z")
_VOLATILE = {"runtime", "generated_at", "created_at", "completed_at", "duration_seconds"}


def _analytical(metadata: dict) -> dict:
    result = {
        key: value for key, value in metadata.items()
        if key not in _VOLATILE and key != "canonical_hash"
    }
    if isinstance(result.get("files"), dict):
        result["files"] = {
            name: value for name, value in result["files"].items() if name != "resource.json"
        }
    return result


def _json_default(value: object) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"not canonical JSON: {type(value).__name__}")


def canonical_hash(value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False, default=_json_default
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_path(path: Path, *, must_exist: bool = False) -> Path:
    """Reject symlinks before resolution, including symlinked ancestors."""
    absolute = path.expanduser().absolute()
    for component in (absolute, *absolute.parents):
        if component.is_symlink():
            raise ValueError("research paths cannot contain symlinks")
    resolved = absolute.resolve(strict=must_exist)
    if ".git" in resolved.parts:
        raise ValueError("research artifacts cannot be placed in Git metadata")
    return resolved


def _publish_exclusive(stage: Path, output: Path) -> None:
    """The native exclusive rename also rejects a noncooperating mkdir race."""
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        rename = libc.renamex_np
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        status = rename(os.fsencode(stage), os.fsencode(output), 0x4)  # RENAME_EXCL
    elif sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
        rename = libc.renameat2
        rename.argtypes = [
            ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint,
        ]
        status = rename(-100, os.fsencode(stage), -100, os.fsencode(output), 1)
        # AT_FDCWD, RENAME_NOREPLACE
    else:
        raise RuntimeError("exclusive atomic publication requires macOS or Linux rename support")
    if status:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


@contextmanager
def atomic_directory(output: Path) -> Iterator[Path]:
    """Publish only a complete private staging directory to an absent target.

The adjacent lock serializes cooperating writers. A failed operation removes
its temporary files and leaves no destination that could look complete.
"""
    output = safe_path(output)
    if output.exists():
        raise FileExistsError("research destination already exists; choose a fresh destination")
    output.parent.mkdir(parents=True, exist_ok=True)
    lock = output.parent / f".{output.name}.publish.lock"
    descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    stage = None
    try:
        stage = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
        yield stage
        if not (stage / "manifest.json").is_file():
            raise ValueError("cannot publish an incomplete research artifact")
        verify_artifact(stage)
        if output.exists():
            raise FileExistsError("research destination appeared during publication")
        _publish_exclusive(stage, output)
    finally:
        if stage is not None and stage.exists():
            shutil.rmtree(stage)
        lock.unlink(missing_ok=True)


def finalize_manifest(stage: Path, metadata: dict) -> dict:
    """Hash the files in a staging directory and write the manifest last."""
    if (stage / "manifest.json").exists():
        raise FileExistsError("manifest is immutable")
    files = {}
    for path in sorted(stage.iterdir()):
        if not path.is_file() or not _NAME.fullmatch(path.name):
            raise ValueError("unexpected research artifact name")
        entry: dict[str, object] = {"sha256": hash_file(path), "bytes": path.stat().st_size}
        if path.suffix == ".parquet":
            parquet = pq.ParquetFile(path)
            entry.update(rows=parquet.metadata.num_rows, schema=str(parquet.schema_arrow))
        files[path.name] = entry
    result = {**metadata, "files": files}
    result["canonical_hash"] = canonical_hash(_analytical(result))
    (stage / "manifest.json").write_text(
        json.dumps(result, sort_keys=True, indent=2, allow_nan=False, default=_json_default) + "\n"
    )
    return result


def verify_artifact(path: Path) -> dict:
    path = safe_path(path, must_exist=True)
    manifest_path = path / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("incomplete research artifact: manifest missing")
    metadata = json.loads(manifest_path.read_text())
    if not isinstance(metadata, dict) or not isinstance(metadata.get("files"), dict):
        raise ValueError("invalid research manifest")
    expected = metadata.get("canonical_hash")
    if expected != canonical_hash(_analytical(metadata)):
        raise ValueError("research manifest hash mismatch")
    actual_names = {item.name for item in path.iterdir()}
    if actual_names != {*metadata["files"], "manifest.json"}:
        raise ValueError("research artifact has missing or unexpected files")
    for name, entry in metadata["files"].items():
        if not _NAME.fullmatch(name) or not isinstance(entry, dict):
            raise ValueError("unsafe research manifest path")
        artifact = path / name
        if artifact.is_symlink() or not artifact.is_file():
            raise ValueError("research artifact file is absent or unsafe")
        if (
            entry.get("sha256") != hash_file(artifact)
            or entry.get("bytes") != artifact.stat().st_size
        ):
            raise ValueError(f"research artifact hash mismatch: {name}")
        if artifact.suffix == ".parquet":
            parquet = pq.ParquetFile(artifact)
            if (
                entry.get("rows") != parquet.metadata.num_rows
                or entry.get("schema") != str(parquet.schema_arrow)
            ):
                raise ValueError(f"research artifact schema/count mismatch: {name}")
    return metadata
