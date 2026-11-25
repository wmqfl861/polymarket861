"""Reviewed international fee policies and their original, time-stamped evidence."""

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from . import dbscope, market_provider, store
from .ledger_rules import ROUNDINGS, UNIT, number


@dataclass(frozen=True)
class Document:
    url: str
    sha256: str
    published_at: str


@dataclass(frozen=True)
class FeePolicy:
    key: str
    protocol: str
    families: tuple[str, ...]
    effective_from: str
    effective_until: str
    documents: tuple[Document, ...]
    rate: str
    exponent: int
    model: str
    asset: str
    collateral_asset: str
    round_in: str
    quantum: str
    rounding: str
    aggregation: str
    share_conversion: str


# A change here requires version-controlled review of the exact official document
# bytes. No locally supplied JSON, CLI flag or live Gamma field can add a policy.
# Currently observed sports Gamma profiles do not establish the charged asset,
# aggregation, precision and rounding throughout a documented effective interval.
REVIEWED: tuple[FeePolicy, ...] = ()


def _checked(policy):
    if (
        not re.fullmatch(r"[A-Za-z0-9._-]+", policy.key)
        or policy.protocol not in ("v1", "v2")
        or not policy.families
        or not set(policy.families) <= set(market_provider.FAMILIES)
        or store.instant(policy.effective_from) >= store.instant(policy.effective_until)
    ):
        raise ValueError("invalid reviewed fee policy scope")
    if (
        policy.model != "symmetric"
        or not 0 < number(policy.rate) <= 1
        or type(policy.exponent) is not int
        or policy.exponent not in (1, 2)
        or policy.asset not in ("cash", "shares")
        or policy.round_in not in ("cash", "charged")
        or policy.rounding not in ROUNDINGS
        or not UNIT <= number(policy.quantum) <= 1
        or number(policy.quantum) % UNIT != 0
        or policy.aggregation != "per_execution_price_level"
        or not re.fullmatch(
            r"eip155:[1-9][0-9]*/erc20:0x[0-9a-fA-F]{40}", policy.collateral_asset
        )
    ):
        raise ValueError("fee formula or charging conventions are unsupported")
    conversion = (
        "ceil_at_0.000001_shares"
        if policy.asset == "shares" and policy.round_in == "cash"
        else "none"
    )
    if policy.share_conversion != conversion or (
        policy.asset == "cash" and policy.round_in != "cash"
    ):
        raise ValueError("fee conversion does not match the ledger implementation")
    if not policy.documents or len({d.sha256 for d in policy.documents}) != len(
        policy.documents
    ):
        raise ValueError(
            "a reviewed fee policy needs distinct, pinned original documents"
        )
    for doc in policy.documents:
        origin = urlsplit(doc.url)
        if (
            origin.scheme != "https"
            or origin.netloc
            not in {
                "docs.polymarket.com",
                "clob.polymarket.com",
                "polymarket.com",
                "help.polymarket.com",
            }
            or not origin.path.strip("/")
            or origin.fragment
            or not re.fullmatch(r"[0-9a-f]{64}", doc.sha256)
        ):
            raise ValueError("unapproved fee-document origin or fingerprint")
        store.instant(doc.published_at)
    return store.digest(asdict(policy))


def list_policies():
    return {
        "venue": "polymarket.com",
        "policies": [
            {
                "id": p.key,
                "version": _checked(p),
                "effective_from": p.effective_from,
                "effective_until": p.effective_until,
                "source_sha256": [d.sha256 for d in p.documents],
            }
            for p in REVIEWED
        ],
    }


def _read_document(doc):
    start = store.now_utc()
    request = Request(
        doc.url,
        headers={"User-Agent": "polymarket861/0.1", "Cache-Control": "no-cache"},
    )
    with urlopen(request, timeout=25) as response:
        if response.geturl() != doc.url:
            raise ValueError("fee policy source redirected away from its reviewed URL")
        body = response.read(4 * 1024 * 1024 + 1)
        if len(body) > 4 * 1024 * 1024:
            raise ValueError("fee document exceeds size limit")
        return market_provider.Reply(
            body,
            start,
            store.now_utc(),
            {k.lower(): v for k, v in response.headers.items()},
            response.geturl(),
        )


def capture(data_dir, key):
    """The caller chooses a reviewed ID. The source URL and digest come from code."""
    from .market_archive import _log

    matches = [p for p in REVIEWED if p.key == key]
    if len(matches) != 1:
        raise ValueError("unknown or ambiguous reviewed fee policy")
    policy = matches[0]
    version = _checked(policy)
    receipts = []
    for doc in policy.documents:
        reply = _read_document(doc)
        if reply.url != doc.url or hashlib.sha256(reply.body).hexdigest() != doc.sha256:
            raise ValueError(
                "official fee policy content differs from its reviewed bytes"
            )
        if store.instant(reply.requested_at) > store.instant(
            reply.received_at
        ) or store.instant(doc.published_at) > store.instant(reply.received_at):
            raise ValueError(
                "fee policy source publication and receipt times are inconsistent"
            )
        meta = {
            "operation": "fee_policy",
            "url": doc.url,
            "requested_at": reply.requested_at,
            "http_date": reply.headers.get("date"),
            "policy_version": version,
        }
        stored = store.archive_bytes(
            data_dir,
            "polymarket.com",
            reply.body,
            observed_at=reply.received_at,
            media_type=reply.headers.get("content-type") or "application/octet-stream",
            metadata=meta,
        )
        _log(
            data_dir,
            "fee_policy",
            version,
            reply.requested_at,
            reply.received_at,
            "ok",
            stored["sha256"],
        )
        receipts.append(
            {"sha256": stored["sha256"], "observed_at": stored["observed_at"]}
        )
    return {"policy": key, "version": version, "receipts": receipts}


def _proof(db, root, doc, version, at):
    rows = db.execute(
        "SELECT o.observed_at,o.metadata,r.requested_at FROM observations o JOIN ma_requests r "
        "ON r.received_at=o.observed_at AND r.sha=o.sha256 WHERE o.source='polymarket.com' AND o.sha256=? "
        "AND o.observed_at<=? AND r.status='ok' AND r.operation='fee_policy' AND r.selector=? ORDER BY o.observed_at",
        (doc.sha256, store.iso(at), version),
    )
    for row in rows:
        meta = json.loads(row["metadata"])
        if (
            meta.get("url") == doc.url
            and meta.get("policy_version") == version
            and meta.get("operation") == "fee_policy"
            and meta.get("requested_at") == row["requested_at"]
            and store.instant(row["requested_at"]) <= store.instant(row["observed_at"])
            and store.instant(doc.published_at) <= store.instant(row["observed_at"])
        ):
            dbscope.verified_bytes(
                root, store.blob_path(root, doc.sha256), doc.sha256,
                "reviewed fee policy bytes failed integrity check",
            )
            return {
                "url": doc.url,
                "sha256": doc.sha256,
                "published_at": doc.published_at,
                "received_at": row["observed_at"],
            }
    raise ValueError("independently captured fee documents unavailable at cutoff")


def resolve(data_dir, market, at):
    observation, cutoff = store.instant(market["recorded_at"]), store.instant(at)
    if (
        market["venue"] != "polymarket.com"
        or market.get("fees_enabled") is not True
        or observation > cutoff
    ):
        raise ValueError("fee authority needs a timely international Gamma quote")
    schedule = market.get("fee_schedule")
    if not isinstance(schedule, dict) or schedule.get("takerOnly") is not True:
        raise ValueError(
            "fee-enabled Gamma rate, exponent and taker scope are unverified"
        )
    rate, exponent = number(schedule.get("rate")), number(schedule.get("exponent"))
    matches = [
        p
        for p in REVIEWED
        if p.protocol == market["protocol"]
        and market["family"] in p.families
        and store.instant(p.effective_from)
        <= observation
        <= cutoff
        < store.instant(p.effective_until)
        and number(p.rate) == rate
        and p.exponent == exponent
    ]
    if len(matches) != 1:
        raise ValueError(
            "fee-enabled market lacks one reviewed effective charging asset and rounding policy"
        )
    policy = matches[0]
    version = _checked(policy)
    def documents():
        with dbscope.reading(data_dir) as db:
            return [_proof(db, data_dir, doc, version, cutoff) for doc in policy.documents]

    key = ("fee_policy_documents", version, store.iso(cutoff))
    proofs = dbscope.checked(data_dir, key, documents)
    return {
        "enabled": True,
        "version": version,
        "policy": policy.key,
        "effective_at": store.iso(policy.effective_from),
        "expires_at": store.iso(policy.effective_until),
        "received_at": max(
            [store.iso(observation)] + [x["received_at"] for x in proofs]
        ),
        "source_sha256": proofs[0]["sha256"],
        "documents": proofs,
        "gamma_source_sha256": market["source_sha256"],
        "model": policy.model,
        "rate": policy.rate,
        "exponent": policy.exponent,
        "asset": policy.asset,
        "round_in": policy.round_in,
        "quantum": policy.quantum,
        "rounding": policy.rounding,
        "aggregation": policy.aggregation,
        "collateral_asset": policy.collateral_asset,
        "share_conversion": policy.share_conversion,
    }


def register(subparsers):
    subparsers.add_parser("ledger-fee-policies").set_defaults(
        func=lambda args: list_policies()
    )
    capture_parser = subparsers.add_parser("ledger-capture-fee-policy")
    capture_parser.add_argument("--policy", required=True)
    capture_parser.set_defaults(func=lambda args: capture(args.data_dir, args.policy))
