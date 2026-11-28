"""Archive and verify the original inputs of the 2026-11-27 manual paper slate."""

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile

SOURCE = Path("/tmp/pm861-weekly-value-20261127")
DESTINATION = Path(__file__).resolve().parent / "source-evidence" / SOURCE.name
MANIFEST = "_pm861_sha256.json"


def inventory(root, *, archived=False):
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"missing regular input directory: {root}")
    files = {}
    for entry in sorted(root.rglob("*")):
        name = entry.relative_to(root).as_posix()
        if entry.is_symlink() or not (entry.is_dir() or entry.is_file()):
            raise ValueError(f"unsupported source entry: {name}")
        if name == MANIFEST:
            if archived and entry.is_file():
                continue
            raise ValueError(f"input contains reserved name: {name}")
        if entry.is_file():
            with entry.open("rb") as stream:
                files[name] = hashlib.file_digest(stream, "sha256").hexdigest()
    if not {"forecasts.json", "markets.json"}.issubset(files):
        raise ValueError("missing historic slate forecasts.json or markets.json")
    return files


def verify(destination):
    expected = inventory(destination, archived=True)
    if json.loads((destination / MANIFEST).read_bytes()) != expected:
        raise ValueError("archive manifest differs from its contents")
    return expected


def archive(source, destination):
    files = inventory(source)
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (destination.parent / ".slate-evidence.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if destination.exists():
            if files != verify(destination):
                raise ValueError("existing archive differs; retained both originals")
            return
        with tempfile.TemporaryDirectory(dir=destination.parent) as tmp:
            staging = Path(tmp) / "archive"
            shutil.copytree(source, staging, symlinks=True)
            if inventory(staging) != files or inventory(source) != files:
                raise ValueError("originals changed during archival")
            (staging / MANIFEST).write_text(json.dumps(files, ensure_ascii=False, indent=2) + "\n")
            os.rename(staging, destination)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--destination", type=Path, default=DESTINATION)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--verify", action="store_true")
    mode.add_argument("--archive-or-verify", action="store_true",
                      help="if the temporary originals are gone, verify a previously archived copy")
    args = parser.parse_args()
    if args.archive_or_verify and not args.source.exists() and not args.source.is_symlink():
        print(f"Originals absent at {args.source}; checking existing archive")
    elif not args.verify:
        archive(args.source, args.destination)
    print(f"Verified {len(verify(args.destination))} archived inputs at {args.destination}")


if __name__ == "__main__":
    main()
