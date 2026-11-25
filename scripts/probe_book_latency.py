"""Time anonymous read-only CLOB samples across fixtures, outside the paper ledger."""

import argparse
import json
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from math import ceil
from pathlib import Path

from polymarket861 import market_archive as markets
from polymarket861 import market_provider as provider
from polymarket861 import store
from polymarket861.execution_books import native_rows


def probe(root, fixtures, rounds):
    tokens = {}
    for slug in fixtures:
        event, _, _ = markets._fetch(
            root, "event", slug, provider.GAMMA + "/events/slug/" + slug
        )
        tokens[slug] = [
            t["token_id"]
            for m in provider.normalized_markets(event)
            if not m["mapping_error"] and m["market_open"] is True
            for t in m["tokens"]
        ][:3]
        if not tokens[slug]:
            raise ValueError(f"no open mapped outcome for {slug}")

    def sample(args):
        slug, label, limit = args
        start = time.monotonic()
        try:
            rows, reply, sha = markets._fetch(
                root,
                "books_research",
                slug,
                provider.CLOB + "/books",
                {"token_ids": tokens[slug]},
                attempts=1,
                timeout=limit,
                metadata={"capture_scope": "transport_probe", "sample": label},
            )
            matched = native_rows(rows, tokens[slug])
            result = {
                "status": "ok",
                "returned_tokens": len(matched),
                "sha256": sha,
                "requested_at": reply.requested_at,
                "received_at": reply.received_at,
                "http_date": reply.headers.get("date"),
                "encoding": reply.headers.get("content-encoding"),
                "http_fresh": provider.fresh_observation(reply),
                "native_latency_ms": round(
                    (
                        store.instant(reply.received_at)
                        - store.instant(reply.requested_at)
                    ).total_seconds()
                    * 1000,
                    3,
                ),
            }
        except (OSError, ValueError) as exc:
            result = {
                "status": type(exc).__name__,
                "error": str(exc),
                "retry_at": getattr(exc, "retry_at", None),
            }
        result.update(
            fixture=slug,
            phase=label,
            timeout_seconds=limit,
            requested_tokens=len(tokens[slug]),
            total_ms=round(1000 * (time.monotonic() - start), 3),
        )
        return result

    first = fixtures[0]
    samples = [
        sample((first, "cold_short", 1.5)),
        sample((first, "decision_warmup", 12)),
    ]
    with ThreadPoolExecutor(max_workers=len(fixtures)) as pool:
        samples.extend(
            pool.map(
                sample, [(slug, "single_warm_concurrent", 1.5) for slug in fixtures]
            )
        )
        samples.extend(
            pool.map(sample, [(slug, "per_fixture_warmup", 12) for slug in fixtures])
        )
        for _ in range(rounds):
            samples.extend(
                pool.map(
                    sample,
                    [(slug, "per_fixture_warm_concurrent", 1.5) for slug in fixtures],
                )
            )
    summary = {}
    for phase in dict.fromkeys(s["phase"] for s in samples):
        selected = [s for s in samples if s["phase"] == phase]
        ok = [s for s in selected if s["status"] == "ok"]
        latency = sorted(s["native_latency_ms"] for s in ok)
        summary[phase] = {
            "attempts": len(selected),
            "complete": len(ok),
            "fresh_complete": sum(
                s["http_fresh"] and s["returned_tokens"] == s["requested_tokens"]
                for s in ok
            ),
            "native_p50_ms": round(statistics.median(latency), 3) if latency else None,
            "native_p90_ms": latency[ceil(0.9 * len(latency)) - 1] if latency else None,
        }
    with closing(markets.database(root)) as db:
        book_rows = db.execute("SELECT count(*) FROM ma_books").fetchone()[0]
    return {
        "observed_at": store.now_utc(),
        "scope": "Pre-match public transport timing only. Tokens were sampled without any frozen orders.",
        "gamma": [provider.GAMMA + "/events/slug/" + s for s in fixtures],
        "clob_post": provider.CLOB + "/books",
        "forecast_or_fill_count": 0,
        "book_rows_in_probe_archive": book_rows,
        "summary": summary,
        "tokens": tokens,
        "samples": samples,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--rounds", type=int, default=4, choices=range(1, 11))
    parser.add_argument("fixtures", nargs="+")
    args = parser.parse_args()
    if len(args.fixtures) > 4:
        parser.error("use up to four concurrent fixtures")
    print(
        json.dumps(
            probe(args.data_dir, args.fixtures, args.rounds),
            ensure_ascii=False,
            indent=2,
        )
    )
