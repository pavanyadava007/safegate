"""
safegate.core.storage
=====================

Where evidence bytes live. The evidence store (`core/cas.py`) owns the
integrity logic (content addressing, the chained manifest, signatures); a
backend only has to provide four properties:

  1. Blobs are written once and never replaced.
  2. A manifest entry is created at a sequence number exactly once. A second
     writer that races for the same number fails instead of overwriting.
  3. Entries read back in sequence order.
  4. The newest entry can be read without reading the whole log.

Two backends:

  FilesystemBackend  <root>/blobs/ab/cdef..., <root>/manifest.log (JSONL).
                     Appends take an exclusive `flock` and re-read the last
                     line under the lock, so concurrent writers cannot
                     both claim a sequence number.
  S3Backend          s3://bucket/prefix. Blobs at prefix/blobs/ab/cdef...,
                     one object per entry at prefix/manifest/<seq>.json.
                     Every write is a conditional create (If-None-Match: *),
                     so storage refuses both overwrites and seq collisions.
                     Works with AWS S3 and S3-compatible stores that honour
                     conditional writes (MinIO does; tested). Endpoint and
                     credentials come from the standard AWS environment
                     (AWS_ENDPOINT_URL, AWS_ACCESS_KEY_ID, ...).

For deletion resistance beyond detection, create the bucket with S3 Object
Lock in compliance mode: then not even the account owner can delete an entry
inside the retention period. Detection (the chain) does not depend on it.
"""

from __future__ import annotations

import fcntl
import os
import random
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Protocol


class ConcurrentAppend(RuntimeError):
    """Another writer created the entry at this sequence number first."""


class StorageBackend(Protocol):
    location: str

    def has_blob(self, dg: str) -> bool: ...
    def put_blob(self, dg: str, data: bytes) -> None: ...
    def get_blob(self, dg: str) -> bytes: ...
    def append(self, build: Callable[[str | None], tuple[int, str]]) -> str: ...
    def last_entry(self) -> str | None: ...
    def entries(self) -> Iterator[str]: ...
    def read_file(self, name: str) -> bytes | None: ...
    def write_file(self, name: str, data: bytes) -> None: ...


# --------------------------------------------------------------------------


class FilesystemBackend:
    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root)
        self.location = str(self.root)
        (self.root / "blobs").mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.root / "manifest.log"
        self.manifest_path.touch(exist_ok=True)

    def blob_path(self, dg: str) -> Path:
        return self.root / "blobs" / dg[:2] / dg[2:]

    def has_blob(self, dg: str) -> bool:
        return self.blob_path(dg).exists()

    def put_blob(self, dg: str, data: bytes) -> None:
        p = self.blob_path(dg)
        if p.exists():
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        # Unique temp name: parallel campaign workers may write the same blob
        # at the same moment.
        tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
        tmp.write_bytes(data)
        tmp.replace(p)  # atomic
        p.chmod(0o444)  # read-only: mutation must be conscious

    def get_blob(self, dg: str) -> bytes:
        p = self.blob_path(dg)
        if not p.exists():
            raise KeyError(f"blob {dg} not found")
        return p.read_bytes()

    def append(self, build: Callable[[str | None], tuple[int, str]]) -> str:
        """Read the head, build the entry and write it under one exclusive lock.

        `build(last_line)` returns (seq, line). Holding the lock across all
        three steps means concurrent writers queue instead of racing.
        """
        with self.manifest_path.open("a", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                _, line = build(_last_line(self.manifest_path))
                fh.write(line + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        return line

    def last_entry(self) -> str | None:
        return _last_line(self.manifest_path)

    def entries(self) -> Iterator[str]:
        with self.manifest_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    yield line

    def read_file(self, name: str) -> bytes | None:
        p = self.root / name
        return p.read_bytes() if p.exists() else None

    def write_file(self, name: str, data: bytes) -> None:
        p = self.root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)


def _last_line(path: Path) -> str | None:
    """Last non-empty line of a file, reading backwards from the end."""
    with path.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        end = fh.tell()
        buf = b""
        pos = end
        while pos > 0:
            step = min(1 << 16, pos)
            pos -= step
            fh.seek(pos)
            buf = fh.read(step) + buf
            stripped = buf.rstrip(b"\n")
            if b"\n" in stripped:
                return stripped.rsplit(b"\n", 1)[1].decode("utf-8")
        stripped = buf.strip()
        return stripped.decode("utf-8") if stripped else None


# --------------------------------------------------------------------------


class S3Backend:
    SEQ_WIDTH = 12

    def __init__(self, uri: str, client: Any = None) -> None:
        if not uri.startswith("s3://"):
            raise ValueError(f"not an s3:// URI: {uri}")
        bucket, _, prefix = uri[len("s3://") :].partition("/")
        if not bucket:
            raise ValueError(f"no bucket in {uri}")
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.location = uri
        self._head: str | None = None
        if client is None:
            import boto3  # optional dependency: pip install safegate[s3]

            client = boto3.client("s3")
        self.s3 = client

    def _key(self, *parts: str) -> str:
        return "/".join(p for p in (self.prefix, *parts) if p)

    def _entry_key(self, seq: int) -> str:
        return self._key("manifest", f"{seq:0{self.SEQ_WIDTH}d}.json")

    @staticmethod
    def _code(exc: Exception) -> str:
        return str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))

    def _create(self, key: str, data: bytes) -> bool:
        """Create an object that must not exist. False if it already does."""
        try:
            self.s3.put_object(Bucket=self.bucket, Key=key, Body=data, IfNoneMatch="*")
        except Exception as exc:
            if self._code(exc) in ("PreconditionFailed", "412", "ConditionalRequestConflict"):
                return False
            raise
        return True

    def has_blob(self, dg: str) -> bool:
        try:
            self.s3.head_object(Bucket=self.bucket, Key=self._key("blobs", dg[:2], dg[2:]))
        except Exception as exc:
            if self._code(exc) in ("404", "NoSuchKey", "NotFound"):
                return False
            raise
        return True

    def put_blob(self, dg: str, data: bytes) -> None:
        self._create(self._key("blobs", dg[:2], dg[2:]), data)  # existing is fine

    def get_blob(self, dg: str) -> bytes:
        try:
            obj = self.s3.get_object(Bucket=self.bucket, Key=self._key("blobs", dg[:2], dg[2:]))
        except Exception as exc:
            if self._code(exc) in ("404", "NoSuchKey", "NotFound"):
                raise KeyError(f"blob {dg} not found") from exc
            raise
        return obj["Body"].read()

    def append_entry(self, seq: int, line: str) -> None:
        if not self._create(self._entry_key(seq), line.encode("utf-8")):
            raise ConcurrentAppend(f"manifest entry {seq} already exists")

    def append(
        self, build: Callable[[str | None], tuple[int, str]], retries: int = 200
    ) -> str:
        """Optimistic append: build on the cached head, create conditionally,
        and on a collision refresh the head and try again with back-off."""
        for attempt in range(retries):
            if self._head is None or attempt:
                self._head = self.last_entry()
            seq, line = build(self._head)
            try:
                self.append_entry(seq, line)
            except ConcurrentAppend:
                time.sleep(random.uniform(0, min(0.2, 0.005 * (attempt + 1))))
                continue
            self._head = line
            return line
        raise ConcurrentAppend(f"could not append after {retries} attempts")

    def _entry_keys(self) -> list[str]:
        keys: list[str] = []
        paginator = self.s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=self._key("manifest") + "/"):
            keys += [o["Key"] for o in page.get("Contents", [])]
        return sorted(keys)

    def _get_text(self, key: str) -> str:
        return self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read().decode("utf-8")

    def last_entry(self) -> str | None:
        keys = self._entry_keys()
        return self._get_text(keys[-1]) if keys else None

    def entries(self) -> Iterator[str]:
        keys = self._entry_keys()
        with ThreadPoolExecutor(max_workers=16) as pool:
            yield from pool.map(self._get_text, keys)

    def read_file(self, name: str) -> bytes | None:
        try:
            return self.s3.get_object(Bucket=self.bucket, Key=self._key(name))["Body"].read()
        except Exception as exc:
            if self._code(exc) in ("404", "NoSuchKey", "NotFound"):
                return None
            raise

    def write_file(self, name: str, data: bytes) -> None:
        self.s3.put_object(Bucket=self.bucket, Key=self._key(name), Body=data)


def open_backend(location: str | os.PathLike[str]) -> FilesystemBackend | S3Backend:
    loc = str(location)
    if loc.startswith("s3://"):
        return S3Backend(loc)
    return FilesystemBackend(loc)


__all__ = [
    "ConcurrentAppend",
    "FilesystemBackend",
    "S3Backend",
    "StorageBackend",
    "open_backend",
]
