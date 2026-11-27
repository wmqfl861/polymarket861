"""Process-local reuse of verified domestic Wikipedia scores; SQLite keeps every original."""

from __future__ import annotations

import hashlib
import sqlite3
import zlib
from collections import OrderedDict, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from functools import lru_cache
from pathlib import Path
from threading import Lock
from types import MappingProxyType

from . import football, league_registry


# At most one observed page per hour, or 8,784 revisions in a leap year.
_MAX_PAGES = 9000
_MAX_CHANGES = 32768
_UNKNOWN = 255
MATCH_DURATION = timedelta(minutes=90)


@lru_cache(maxsize=12)
def _positions(league, season):
    clubs = league_registry.members(league, season)
    return MappingProxyType({fid: i * 2 for i, fid in enumerate(
        football.fixture_id(season, h, a, league=league) for h in clubs for a in clubs if h != a
    )})


@dataclass(frozen=True, slots=True, eq=False)
class _Scores(Mapping):
    positions: Mapping
    values_bytes: bytes

    @classmethod
    def pack(cls, league, season, scores):
        positions = _positions(league, season)
        packed = bytearray([_UNKNOWN]) * (len(positions) * 2)
        for fid, score in scores.items():
            if score is not None:
                i = positions[fid]
                packed[i], packed[i + 1] = score
        return cls(positions, bytes(packed))

    def __len__(self):
        return len(self.positions)

    def __iter__(self):
        return iter(self.positions)

    def __getitem__(self, fid):
        i = self.positions[fid]
        home = self.values_bytes[i]
        return None if home == _UNKNOWN else (home, self.values_bytes[i + 1])


def _file_stamp(path):
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_ctime_ns, stat.st_mtime_ns


class _ReceiptConnection(sqlite3.Connection):
    archive = None


def connect(path, *, timeout=8):
    path = Path(path).resolve()
    try:
        before = _file_stamp(path)[:2]
    except FileNotFoundError:
        before = None
    db = sqlite3.connect(path, timeout=timeout, factory=_ReceiptConnection)
    try:
        if _file_stamp(path)[:2] == before:
            db.archive = (str(path), before)
    except OSError:
        pass
    return db


def _archive_stamp(archive):
    if archive is None:
        return None
    path, opened_inode = archive
    try:
        main = _file_stamp(Path(path))
        if main[:2] != opened_inode or Path(path + "-journal").exists():
            return None
        try:
            wal = _file_stamp(Path(path + "-wal"))
        except FileNotFoundError:
            wal = None
    except OSError:
        return None
    return path, main, wal


class _SeasonCache:
    def __init__(self):
        self.lock = Lock()
        self.parsed = OrderedDict()
        self.stamp = None
        self.rows = {}
        self.clock = None
        self.headers = ()
        self.changes = {}
        self.change_count = 0

    def verified(self, packed, sha, league, season, parse, *, observed=None):
        # Bind the uncompressed evidence hash to the exact archived representation.
        # Even a manual edit retaining the old SHA must pass original verification.
        key = sha, hashlib.sha256(packed).digest()
        result = self.parsed.get(key)
        if result is not None:
            self.parsed.move_to_end(key)
            return result
        raw = observed
        if raw is None:
            try:
                raw = zlib.decompress(packed)
            except zlib.error as exc:
                raise ValueError("unreadable compressed Wikimedia receipt") from exc
            if hashlib.sha256(raw).hexdigest() != sha:
                raise ValueError("Wikimedia receipt hash mismatch")
        revision, published, scores = parse(raw, league, season)
        result = revision, published, _Scores.pack(league, season, scores)
        self.parsed[key] = result
        if len(self.parsed) > _MAX_PAGES:
            self.parsed.popitem(last=False)
        return result


@lru_cache(maxsize=12)
def _cache(league, season, parse):
    # A parser change or process restart starts with the archived originals.
    return _SeasonCache()


def prepare_receipt(raw, league, season, parse):
    sha = hashlib.sha256(raw).hexdigest()
    packed = zlib.compress(raw)
    scope = _cache(league, season, parse)
    with scope.lock:
        rev, published, _ = scope.verified(packed, sha, league, season, parse, observed=raw)
    return rev, published, sha, packed


def _read_snapshot(db, league, season, title, at, parse, scope, archive):
    own = not db.in_transaction
    before = _archive_stamp(archive)
    if own:
        db.execute("BEGIN")
    try:
        generation = db.execute("SELECT token FROM wiki_archive_generation").fetchone()
        stamp = (before, generation[0] if generation else None) if before else None
        same = stamp is not None and scope.stamp == stamp
        projection = "revision,published,seen,sha" + ("" if same else ",body")
        rows = db.execute(f"SELECT {projection} FROM wiki_receipts WHERE league=? AND season=? "
                          "AND title=? AND seen<=? ORDER BY seen,revision", (league, season, title, at))
        result, additions = [], {}
        for row in rows:
            rev, published, seen, sha = row[:4]
            item = scope.rows.get((rev, sha)) if same else None
            if item is None:
                packed = db.execute("SELECT body FROM wiki_receipts WHERE league=? AND season=? AND revision=?",
                                    (league, season, rev)).fetchone()[0] if same else row[4]
                item = scope.verified(packed, sha, league, season, parse)
                if stamp is not None and len(additions) < _MAX_PAGES:
                    additions[rev, sha] = item
            if item[:2] != (rev, published) or published > seen:
                raise ValueError("Wikimedia receipt has inconsistent revision or time")
            result.append((rev, published, seen, sha, item[2]))
    finally:
        if own:
            db.rollback()
    if before != _archive_stamp(archive):
        # Retry a metadata-only read against original bytes if the physical
        # file moved or changed during the read, even outside SQLite.
        return result, same
    if stamp is not None:
        if same:
            if len(scope.rows) + len(additions) <= _MAX_PAGES:
                scope.rows.update(additions)
        else:
            scope.stamp, scope.rows = stamp, additions
    return result, False


def verified_rows(db, league, season, title, at, parse):
    """Return only visible revisions from one SQLite read snapshot, in receipt order."""
    scope = _cache(league, season, parse)
    archive = None if db.in_transaction else getattr(db, "archive", None)
    with scope.lock:
        rows, retry = _read_snapshot(db, league, season, title, at, parse, scope, archive)
        if retry:
            rows, _ = _read_snapshot(db, league, season, title, at, parse, scope, None)
        return rows


def score_observations(league, season, parse, history, fixtures, schedules, pin_seen):
    """Keep the first receipt for each effective score, correction, withdrawal or reinstatement."""
    if not history:
        return {}
    scope = _cache(league, season, parse)
    members = history[0][4]
    clocks = {fid: (f.kickoff + MATCH_DURATION, tuple((stamp, kick + MATCH_DURATION)
                                                for stamp, kick in schedules.get(fid, ())))
              for fid, f in fixtures.items() if fid in members}
    clock = (pin_seen, clocks)
    headers = tuple((r[0], r[2], r[3]) for r in history)
    with scope.lock:
        reuse = clock == scope.clock and headers[:len(scope.headers)] == scope.headers
        start = len(scope.headers) if reuse else 0
        if reuse and start == len(headers):
            return scope.changes
        old = scope.changes if reuse else {}
        latest = {fid: events[-1][-1] for fid, events in old.items()}
        # A schedule observation may move a game away and back between two
        # Wikipedia receipts. The first receipt in each new schedule epoch
        # must reach reconciliation, including an empty or repeated score.
        next_move = {}
        for fid, (initial, schedule) in clocks.items():
            if schedule and fid in old:
                observed = football.utc(old[fid][-1][0])
                next_move[fid] = sum(stamp <= observed for stamp, _ in schedule)
        additions = defaultdict(list)
        for seen, source, locator, published, scores in history[start:]:
            known = max(pin_seen, seen)
            received = football.utc(known)
            by = min(received, football.utc(published))
            for fid, (initial, schedule) in clocks.items():
                epoch = next_move.get(fid, 0)
                first = fid not in latest
                while epoch < len(schedule) and schedule[epoch][0] <= received:
                    if schedule[epoch][1] != (schedule[epoch - 1][1] if epoch else initial):
                        first = True
                    epoch += 1
                if schedule:
                    next_move[fid] = epoch
                score = scores[fid]
                if score is not None:
                    eligible_after = schedule[epoch - 1][1] if epoch else initial
                    if by < eligible_after:
                        score = None
                if first or latest.get(fid) != score:
                    additions[fid].append((known, source, locator, published, score))
                    latest[fid] = score
        changes = dict(old)
        for fid, events in additions.items():
            changes[fid] = changes.get(fid, ()) + tuple(events)
        count = (scope.change_count if reuse else 0) + sum(map(len, additions.values()))
        if len(headers) <= _MAX_PAGES and count <= _MAX_CHANGES:
            scope.clock, scope.headers, scope.changes, scope.change_count = clock, headers, changes, count
        return changes
