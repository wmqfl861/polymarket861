"""Physical encodings of immutable evidence; SHA-256 always identifies original bytes."""

import gzip
import zlib
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


def read_blob(root: str | PathLike[str], sha: str) -> bytes:
    path = blob_path(root, sha)
    return decoded(path, path.read_bytes())
