"""Physical encodings of immutable evidence; SHA-256 always identifies original bytes."""

import gzip
import hashlib
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from io import BufferedReader
from os import PathLike
from pathlib import Path


def blob_name(sha: str) -> Path:
    if not isinstance(sha, str) or len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
        raise ValueError("invalid SHA-256")
    return Path("archive") / sha[:2] / sha


def blob_path(root: str | PathLike[str], sha: str) -> Path:
    raw = Path(root) / blob_name(sha)
    if raw.is_file():
        return raw
    packed = raw.with_suffix(".gz")
    return packed if packed.is_file() else raw


def blob_size(root: str | PathLike[str], sha: str) -> tuple[Path, int]:
    path = blob_path(root, sha)
    try:
        return path, path.stat().st_size
    except FileNotFoundError:
        current = blob_path(root, sha)
        if current == path:
            raise
        return current, current.stat().st_size


def representation(path: Path, payload: bytes) -> tuple[Path, bytes]:
    if len(payload) >= 8192:
        packed = gzip.compress(payload, compresslevel=3, mtime=0)
        if len(packed) <= len(payload) * 7 // 8:
            return path.with_suffix(".gz"), packed
    return path, payload


def decoded(path: Path, payload: bytes) -> bytes:
    if path.suffix == ".gz":
        try:
            return gzip.decompress(payload)
        except (EOFError, OSError, zlib.error) as exc:
            raise ValueError("unreadable gzip evidence original") from exc
    return payload


@contextmanager
def open_blob(root: str | PathLike[str], sha: str) -> Iterator[BufferedReader | gzip.GzipFile]:
    """Open a stable file descriptor while a retired raw encoding may move to gzip."""
    path = blob_path(root, sha)
    try:
        handle = path.open("rb")
    except FileNotFoundError:
        current = blob_path(root, sha)
        if path == current:
            raise
        path, handle = current, current.open("rb")
    with handle:
        if path.suffix == ".gz":
            with gzip.GzipFile(fileobj=handle, mode="rb") as data:
                yield data
        else:
            yield handle


def read_blob(root: str | PathLike[str], sha: str) -> bytes:
    try:
        with open_blob(root, sha) as data:
            payload = data.read()
    except (EOFError, gzip.BadGzipFile, zlib.error) as exc:
        raise ValueError("unreadable gzip evidence original") from exc
    if hashlib.sha256(payload).hexdigest() != sha:
        raise ValueError(f'archived original SHA-256 mismatch: {sha}')
    return payload
