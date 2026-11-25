"""Reproducible inputs and independently checked proposals for local Codex maintenance."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import sys
import re
import secrets
import shutil
import subprocess
import tempfile
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit

from . import forward_window
from . import store

TOPICS = {"round", "source", "incident", "discovery"}
KINDS = {"note", "model", "source", "patch"}
SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": sorted(KINDS)},
        "hypothesis": {"type": "string"},
        "evidence_refs": {"type": "array", "items": {"type": "string"}},
        "parameters_json": {"type": "string"},
    },
    "required": ["kind", "hypothesis", "evidence_refs", "parameters_json"],
    "additionalProperties": False,
}
# Only these shipped modules can be presented as code. Tracked reports, tests,
# arbitrary repository text and private working-tree changes are outside this disclosure contract.
_MODULES = (
    "football.py",
    "football_model.py",
    "football_candidates.py",
    "datasets.py",
    "context_feeds.py",
    "news_sources.py",
    "news_ai.py",
    "news_local_ai.py",
    "news_roster.py",
    "news_events.py",
    "news_model.py",
    "news_evaluation.py",
    "evidence.py",
    "evidence_sources.py",
    "input_manifest.py",
    "result_feed.py",
    "experiment_statistics.py",
)
_POLICY = "published-aggregate-research-v1"
_REF_SCHEMA = """CREATE TABLE IF NOT EXISTS research_disclosure_refs(
  ref TEXT PRIMARY KEY, kind TEXT NOT NULL, digest TEXT NOT NULL, UNIQUE(kind,digest)
)"""


def is_research(job):
    return (
        job["kind"] == "codex"
        and isinstance(job["inputs"].get("topic"), str)
        and job["inputs"].get("topic") in TOPICS
    )


def _write_file(dest, content):
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_symlink():
        raise ValueError("invalid research output path")
    if dest.exists():
        dest.chmod(0o600)
    dest.write_bytes(content)
    dest.chmod(0o444)
    return hashlib.sha256(content).hexdigest()


def opaque_ref(db, kind, raw):
    """A random lookup handle does not disclose low-entropy labels through a guessable hash."""
    db.execute(_REF_SCHEMA)
    identity = store.digest(raw)
    db.execute(
        "INSERT OR IGNORE INTO research_disclosure_refs VALUES(?,?,?)",
        (secrets.token_hex(20), kind, identity),
    )
    return db.execute(
        "SELECT ref FROM research_disclosure_refs WHERE kind=? AND digest=?",
        (kind, identity),
    ).fetchone()[0]


def _table(db, name):
    return (
        db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def _blind(db):
    if (
        _table(db, "research_candidates")
        and db.execute(
            "SELECT 1 FROM research_candidates WHERE state NOT IN ('promoted','rolled_back') AND (assessment IS NULL OR NOT json_valid(assessment) OR coalesce(json_extract(assessment,'$.revealed'),0)!=1) LIMIT 1"
        ).fetchone()
    ):
        return True
    if _table(db, "selection_trials"):
        # A shadow selector has no reliable public reveal until its checked recommendation or activation.
        if not _table(db, "selection_recommendations"):
            clause = "1"
        else:
            clause = "NOT EXISTS (SELECT 1 FROM selection_recommendations s WHERE s.trial=t.key)"
        if db.execute(
            "SELECT 1 FROM selection_trials t WHERE status NOT IN ('promoted','rolled_back') AND "
            + clause
            + " LIMIT 1"
        ).fetchone():
            return True
    return False


def _source_id(db, ident):
    from .context_feeds import providers

    return ident if ident in providers() else "src-" + opaque_ref(db, "source", ident)


def _bucket(count):
    if count < 3:
        return ("none", "one", "two")[count]
    return "3–9" if count < 10 else "10+"


def _collection_health(db, topic, task_inputs):
    """Project actual collection tables. Generic reports and supplied task fields are never samples."""
    from .evidence_sources import FIELDS, USES

    if topic not in ("source", "incident", "discovery") or not _table(
        db, "ev_source_versions"
    ):
        return {}
    subject = None
    if isinstance(task_inputs, dict) and _table(db, "research_maintenance_subjects"):
        row = db.execute(
            "SELECT subject FROM research_maintenance_subjects WHERE ref=? AND topic=?",
            (
                task_inputs.get("source_ref")
                if isinstance(task_inputs.get("source_ref"), str)
                else None,
                topic,
            ),
        ).fetchone()
        if row:
            subject = row[0]
    if topic == "incident":
        if not _table(db, "ev_quarantine"):
            return {}
        sql = "SELECT f.field,count(DISTINCT q.evidence_id) FROM ev_quarantine q JOIN ev_facts f ON f.evidence_id=q.evidence_id"
        values = ()
        if subject is not None:
            sql += " WHERE q.incident_id=?"
            values = (subject,)
        counts = {
            field: _bucket(n)
            for field, n in db.execute(sql + " GROUP BY f.field", values)
            if field in FIELDS
        }
        return {"quarantined_evidence_by_field": counts}
    where = " AND v.source_id=?" if topic == "source" and subject is not None else ""
    args = (subject,) if where else ()
    rows = db.execute(
        "SELECT source_id,manifest FROM ev_source_versions v WHERE effective_at=(SELECT MAX(r.effective_at) FROM ev_source_versions r WHERE r.source_id=v.source_id AND r.effective_at<=?)"
        + where
        + " ORDER BY source_id LIMIT 32",
        (store.now_utc(), *args),
    )
    output = []
    registered = set()
    statuses = {
        "ok",
        "error",
        "invalid",
        "late",
        "frozen",
        "freshness_unknown",
        "fresh",
    }
    for ident, raw in rows:
        manifest = json.loads(raw)
        for field, rule in sorted(manifest.get("fields", {}).items()):
            if field not in FIELDS or not isinstance(rule, dict):
                continue
            registered.add(field)
            uses = sorted(u for u in USES if u in rule.get("uses", []))
            checks = list(
                db.execute(
                    "SELECT document FROM ev_source_checks WHERE source_id=? AND field=? ORDER BY checked_at DESC LIMIT 10",
                    (ident, field),
                )
            )
            states = [json.loads(row[0]).get("status") for row in checks]
            last = states[0] if states and states[0] in statuses else "no_known_check"
            delayed = sum(s in ("late", "frozen") for s in states)
            failed = sum(s in ("error", "invalid") for s in states)
            output.append(
                {
                    "adapter": _source_id(db, ident),
                    "field": field,
                    "registered_uses": uses,
                    "last_collection_status": last,
                    "last_ten_checks": _bucket(len(states)),
                    "late_or_frozen_checks": _bucket(delayed),
                    "error_or_invalid_checks": _bucket(failed),
                }
            )
    result = {
        "field_health": sorted(
            output, key=lambda entry: (entry["field"], entry["adapter"])
        )[:64]
    }
    if topic == "discovery":
        # "In sample" keeps a bounded inventory honest if a larger registry exists.
        result["fields_without_registered_source_in_sample"] = sorted(
            FIELDS - registered
        )
    return result


def _safe_round(body):
    if (
        not isinstance(body, dict)
        or body.get("protocol") != store.PROTOCOL
        or body.get("mode") not in ("technical", "formal", "replay")
        or body.get("horizon") not in ("T-60m", "T-24h")
        or body.get("status") not in ("final", "incomplete")
    ):
        return None
    season = body.get("season")
    rnd = body.get("round_id")
    if (
        not isinstance(season, str)
        or not re.fullmatch(r"20\d{2}-\d{2}", season)
        or int(season[-2:]) != (int(season[:4]) + 1) % 100
        or type(rnd) != int
        or not 1 <= rnd <= 38
    ):
        return None
    values = {
        k: body.get(k)
        for k in (
            "expected",
            "scheduled",
            "completed",
            "pending",
            "schedule_missing",
            "count",
            "missing",
        )
    }
    if (
        any(type(v) != int for v in values.values())
        or (
            values["expected"],
            values["scheduled"],
            values["completed"],
            values["pending"],
            values["schedule_missing"],
        )
        != (10, 10, 10, 0, 0)
        or not 0 <= values["count"] <= 10
        or values["missing"] != 10 - values["count"]
    ):
        return None
    items = body.get("fixtures")
    if (
        not isinstance(items, list)
        or len(items) != 10
        or any(
            not isinstance(f, dict)
            or not isinstance(f.get("fixture_id"), str)
            or f.get("status") not in ("scored", "missing_forecast")
            for f in items
        )
        or len({f["fixture_id"] for f in items}) != 10
        or sum(f["status"] == "scored" for f in items) != values["count"]
    ):
        return None
    score = body.get("score")
    if (
        score is not None and (type(score) not in (int, float) or not 0 <= score <= 100)
    ) or (score is None) != (values["count"] == 0):
        return None
    return {
        "kind": "closed_round",
        "season": season,
        "round": rnd,
        "mode": body["mode"],
        "horizon": body["horizon"],
        "scored": values["count"],
        "expected": 10,
        "mean_score": None if score is None else round(score, 3),
    }


def _code_reference(work, project):
    source = work / "reference"
    files = {}
    revision = None
    package = Path(__file__).resolve().parent
    if project is not None:
        git = ["git", "-C", str(Path(project).resolve())]
        revision = subprocess.check_output(
            [*git, "rev-parse", "--verify", "HEAD^{commit}"], text=True
        ).strip()
    for module in _MODULES:
        trusted = (package / module).read_bytes()
        name = "src/polymarket861/" + module
        if project is not None:
            result = subprocess.run(
                [*git, "cat-file", "blob", revision + ":" + name],
                capture_output=True,
                check=False,
            )
            if result.returncode or result.stdout != trusted:
                continue
        files[name] = _write_file(source / name, trusted)
    if not files:
        raise ValueError(
            "research project has no committed source matching this installed release"
        )
    return revision, files


def prepare(work, data_dir, project, *, job=None):
    """Build the exact model-visible projection independently of how the job was submitted."""
    if (work / "reference").exists() or (work / "samples").exists():
        raise ValueError("research projection requires a fresh attempt directory")
    revision, files = _code_reference(work, project)
    if job is None:
        original = (
            json.loads((work / "input.json").read_text())
            if (work / "input.json").is_file()
            else {}
        )
        supplied = original.get("inputs") if isinstance(original, dict) else None
        requested = supplied.get("topic") if isinstance(supplied, dict) else None
    else:
        original = {
            "task": job["task"],
            "inputs": job["inputs"],
            "dependencies": job["prerequisite_results"],
        }
        supplied = job["inputs"]
        requested = supplied.get("topic")
    topic = (
        requested if isinstance(requested, str) and requested in TOPICS else "discovery"
    )
    with closing(store.connect(data_dir)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        blind = _blind(db)
        # Never derive prompt text, sample filenames, SQL fragments or task instructions from these records.
        brief = {
            "topic": topic,
            "policy": _POLICY,
            "task_ref": opaque_ref(db, "job", original),
            "outcome_disclosure": "withheld" if blind else "released_aggregates",
        }
        brief["collection_health"] = _collection_health(db, topic, supplied)
        refs = {
            "input": _write_file(work / "input.json", store.canonical(brief).encode())
        }
        if not blind and _table(db, "round_report_editions"):
            rows = db.execute(
                "SELECT r.* FROM round_report_editions r WHERE r.edition=(SELECT max(h.edition) FROM round_report_editions h WHERE h.round_key=r.round_key) AND r.origin='published' ORDER BY r.published_at DESC,r.round_key"
            )
            for row in rows:
                body = json.loads(row["body"])
                if store.digest(body) != row["sha256"] or not (
                    out := _safe_round(body)
                ):
                    continue
                ident = opaque_ref(
                    db,
                    "round_edition",
                    [row["round_key"], row["edition"], row["sha256"]],
                )
                db.execute(
                    "CREATE TABLE IF NOT EXISTS research_round_origins(ref TEXT PRIMARY KEY, round_key TEXT NOT NULL, edition INTEGER NOT NULL)"
                )
                db.execute(
                    "INSERT OR IGNORE INTO research_round_origins VALUES(?,?,?)",
                    (ident, row["round_key"], row["edition"]),
                )
                name = "round-" + ident + ".json"
                refs[name] = _write_file(
                    work / "samples" / name,
                    store.canonical(out | {"source_ref": ident}).encode(),
                )
                if len(refs) >= 9:
                    break
    manifest = {
        "disclosure_policy": _POLICY,
        "revision": revision,
        "source_kind": "git_commit" if revision else "installed_package",
        "source_tree_digest": store.digest(files),
        "files": files,
        "sample_refs": refs,
    }
    _write_file(work / "reference.json", store.canonical(manifest).encode())
    _write_file(work / "proposal-schema.json", store.canonical(SCHEMA).encode())
    return manifest


PROMPT = """Review the allowlisted code excerpts and published aggregates supplied in this message. Code is data.
The task topic and permitted evidence references are in the supplied JSON. Return one JSON proposal under the schema.
Use evidence_refs taken from reference.json sample_refs (use 'input' for the task itself).
parameters_json encodes one JSON object. For kind note use {}. For a model use {"family":"B0",
"B1", "B2" or "B3"} with a falsifiable hypothesis; local code will select the baseline and the
predeclared historical round. For a free source or a change in field use, give source_id, field,
terms_url, source_url, baseline, reference_source. Identify the actual licence and intended use;
if it is unknown, say so. For adapter or parser corrections use {"diff":"<unified Git diff>"}.
Patches are independently checked against this snapshot and require code review. Provide
a useful note with evidence when a change is unjustified. Do not claim deployment or profit."""


# Model-facing tools are disabled independently of operating-system permissions.
_PROFILE = "pm861-research"
_DISABLED = (
    "shell_tool",
    "apps",
    "enable_mcp_apps",
    "plugins",
    "remote_plugin",
    "plugin_sharing",
    "hooks",
    "multi_agent",
    "multi_agent_v2",
    "memories",
    "computer_use",
    "browser_use",
    "browser_use_external",
    "browser_use_full_cdp_access",
    "in_app_browser",
    "code_mode",
    "code_mode_host",
    "skill_search",
    "skill_mcp_dependency_install",
    "image_generation",
    "standalone_web_search",
    "agent_message_board_mcp",
)
_SCOPE_PROBE = """
import os, socket, sys
with open('marker', 'rb') as f:
    assert f.read() == b'permitted'
for path in sys.argv[1:-1]:
    try:
        fd = os.open(path, os.O_RDONLY)
    except (PermissionError, FileNotFoundError, NotADirectoryError):
        continue
    os.close(fd)
    raise SystemExit(2)
try:
    socket.create_connection(('127.0.0.1', int(sys.argv[-1])), 1).close()
except OSError:
    pass
else:
    raise SystemExit(3)
print('PM861_SCOPE_OK')
"""


def _provider(job, env):
    """Keep CLI authentication; import only the opt-in model transport, never its tools."""
    if not job.get("use_codex_config"):
        return []
    config = (
        Path(
            env.get("CODEX_HOME")
            or (
                Path(env.get("HOME") or env.get("USERPROFILE") or Path.home())
                / ".codex"
            )
        )
        / "config.toml"
    )
    if not config.is_file():
        return []
    import tomllib

    with config.open("rb") as stream:
        settings = tomllib.load(stream)
    options = []
    model = settings.get("model")
    if model:
        if not isinstance(model, str) or not re.fullmatch(
            r"[A-Za-z0-9._:/-]{1,140}", model
        ):
            raise ValueError("unsupported research endpoint model identifier")
        options += ["-c", "model=" + json.dumps(model)]
    name = settings.get("model_provider")
    if not name:
        return options
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
        raise ValueError("unsupported research endpoint provider identifier")
    options += ["-c", "model_provider=" + json.dumps(name)]
    transport = settings.get("model_providers", {}).get(name)
    if not transport:
        return options
    allowed = {
        "name",
        "base_url",
        "wire_api",
        "env_key",
        "env_http_headers",
        "requires_openai_auth",
        "supports_websockets",
        "request_max_retries",
        "stream_max_retries",
        "stream_idle_timeout_ms",
    }
    if not isinstance(transport, dict) or any(k not in allowed for k in transport):
        raise ValueError(
            "research endpoint requires a simple Responses provider with environment or CLI authentication"
        )
    for key, value in transport.items():
        if key == "base_url":
            target = urlsplit(str(value))
            loopback = target.scheme == "http" and target.hostname in (
                "localhost",
                "127.0.0.1",
                "::1",
            )
            if (
                not target.hostname
                or target.username
                or target.password
                or target.query
                or target.fragment
                or not (target.scheme == "https" or loopback)
            ):
                raise ValueError("invalid research endpoint URL")
        if key == "env_http_headers":
            if not isinstance(value, dict) or not all(
                isinstance(header, str)
                and re.fullmatch(r"[A-Za-z0-9-]{1,100}", header)
                and isinstance(var, str)
                and re.fullmatch(r"[A-Z][A-Z0-9_]{0,100}", var)
                for header, var in value.items()
            ):
                raise ValueError("invalid research endpoint header environment mapping")
            for var in value.values():
                if var in os.environ:
                    env[var] = os.environ[var]
            mapping = (
                "{"
                + ",".join(
                    json.dumps(k) + "=" + json.dumps(v) for k, v in value.items()
                )
                + "}"
            )
            options += ["-c", "model_providers." + name + "." + key + "=" + mapping]
            continue
        if key == "env_key":
            if not isinstance(value, str) or not re.fullmatch(
                r"[A-Z][A-Z0-9_]{0,100}", value
            ):
                raise ValueError("invalid research endpoint environment key")
            if value in os.environ:
                env[value] = os.environ[value]
        if type(value) not in (int, bool, str):
            raise ValueError("unsupported research transport setting")
        options += [
            "-c",
            "model_providers." + name + "." + key + "=" + json.dumps(value),
        ]
    return options


def _profile_options(public, denied):
    roots = {":root": "deny", ":minimal": "read", str(public): "read"}
    for root in denied:
        if root and root != public:
            roots[str(root)] = "deny"
    inline = (
        "{"
        + ",".join(json.dumps(k) + "=" + json.dumps(v) for k, v in roots.items())
        + "}"
    )
    return [
        "-c",
        "permissions." + _PROFILE + ".filesystem=" + inline,
        "-c",
        "permissions." + _PROFILE + ".network.enabled=false",
    ]


def _preflight(binary, public, options, probes, env, log, timeout):
    def inspect(*args):
        query = subprocess.run(
            [binary, *args],
            cwd=public,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if query.returncode:
            raise RuntimeError(
                "installed Codex does not support the required research isolation interface"
            )
        return query.stdout.strip()

    version = inspect("--version")
    if not re.fullmatch(r"codex-cli [0-9][A-Za-z0-9.\-+]{0,80}", version):
        raise RuntimeError("installed Codex did not identify its version")
    log.write(version + " | scoped filesystem and network preflight\n")
    for command, required in (
        (
            "exec",
            (
                "--ignore-user-config",
                "--ignore-rules",
                "--strict-config",
                "--output-schema",
                "--ephemeral",
            ),
        ),
        ("sandbox", ("--permission-profile", "--include-managed-config")),
    ):
        help_text = inspect(command, "--help")
        if any(flag not in help_text for flag in required):
            raise RuntimeError(
                "installed Codex lacks " + command + " research isolation capabilities"
            )
    features = {
        line.split()[0]
        for line in inspect("features", "list").splitlines()
        if line.split()
    }
    if (
        not {"shell_tool", "apps", "plugins", "hooks", "skip_host_skill_discovery"}
        <= features
    ):
        raise RuntimeError(
            "installed Codex lacks controls for external tools and host discovery"
        )
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        cmd = [
            binary,
            "sandbox",
            "-P",
            _PROFILE,
            "--include-managed-config",
            "-C",
            str(public),
            *options,
            "--",
            "/usr/bin/python3"
            if Path("/usr/bin/python3").is_file()
            else sys.executable,
            "-I",
            "-c",
            _SCOPE_PROBE,
            *(str(p) for p in probes),
            str(listener.getsockname()[1]),
        ]
        probe = subprocess.run(
            cmd, cwd=public, env=env, capture_output=True, text=True, timeout=timeout
        )
    if probe.returncode or probe.stdout.strip() != "PM861_SCOPE_OK":
        log.write(probe.stderr[:3000])
        raise RuntimeError(
            "Codex research isolation probe failed; inference was not started (see attempt log)"
        )
    return version, [name for name in _DISABLED if name in features]


def _prompt(work, manifest):
    def checked(path, expected):
        if path.is_symlink():
            raise ValueError("research input changed before inference")
        contents = path.read_bytes()
        if hashlib.sha256(contents).hexdigest() != expected:
            raise ValueError("research input changed before inference")
        return contents.decode("utf-8")

    brief = json.loads(checked(work / "input.json", manifest["sample_refs"]["input"]))
    samples = {
        name: json.loads(
            checked(work / "samples" / name, manifest["sample_refs"][name])
        )
        for name in manifest["sample_refs"]
        if name != "input"
    }
    names = {
        "incident": ("datasets.py", "input_manifest.py", "result_feed.py"),
        "round": (
            "football_model.py",
            "football_candidates.py",
            "experiment_statistics.py",
            "news_model.py",
            "news_evaluation.py",
        ),
        "source": ("evidence_sources.py", "context_feeds.py", "datasets.py", "news_sources.py", "news_ai.py"),
        "discovery": ("evidence_sources.py", "context_feeds.py", "result_feed.py", "news_sources.py"),
    }
    snippets = {}
    for module in names[brief["topic"]]:
        path = "src/polymarket861/" + module
        if path in manifest["files"]:
            snippets[path] = checked(
                work / "reference" / path, manifest["files"][path]
            )[:8000]
    if not snippets:
        path = next(iter(manifest["files"]))
        snippets[path] = checked(work / "reference" / path, manifest["files"][path])[
            :8000
        ]
    return (
        PROMPT
        + "\n"
        + store.canonical(
            {
                "task": brief,
                "manifest": {
                    k: manifest[k]
                    for k in (
                        "files",
                        "source_tree_digest",
                        "sample_refs",
                        "disclosure_policy",
                    )
                },
                "released_samples": samples,
                "code_excerpts": snippets,
            }
        )
    )


def run_research(job, work, manifest, binary, env, timeout):
    data = work.parents[2].resolve()
    home = Path(env.get("HOME") or env.get("USERPROFILE") or Path.home()).resolve()
    auth = Path(env["CODEX_HOME"]).resolve() if env.get("CODEX_HOME") else None
    project = Path(job["project_root"]).resolve() if job.get("project_root") else None
    # The parent transport keeps its real authentication. The candidate sees the bounded stdin projection.
    provider = _provider(job, env)
    with (
        tempfile.TemporaryDirectory(prefix="pm861-public-") as folder,
        tempfile.TemporaryDirectory(dir=work, prefix="probe-") as private_folder,
        tempfile.TemporaryDirectory(prefix="pm861-private-sibling-") as sibling_folder,
    ):
        public = Path(folder).resolve()
        (public / "marker").write_bytes(b"permitted")
        _write_file(public / "proposal-schema.json", store.canonical(SCHEMA).encode())
        options = _profile_options(public, [data, home, auth, project])
        # Real, pre-existing files verify denials even on Windows or when temp is below HOME.
        probes = [Path(private_folder) / "private", Path(sibling_folder) / "sibling"]
        for path in probes:
            path.write_text("private permission probe")
        auth_dir = auth or home / ".codex"
        for path in (
            auth_dir / "config.toml",
            auth_dir / "auth.json",
            data / "runtime.sqlite3",
        ):
            if path.is_file():
                probes.append(path)
        if project and (project / "pyproject.toml").is_file():
            probes.append(project / "pyproject.toml")
        env["GIT_CEILING_DIRECTORIES"] = str(public)
        with (
            (work / "codex-stderr.log").open("w", encoding="utf-8") as err,
            (work / "codex.jsonl").open("w", encoding="utf-8") as out,
        ):
            version, disabled = _preflight(
                binary, public, options, probes, env, err, min(20, timeout)
            )
            cmd = [
                binary,
                "-a",
                "never",
                "exec",
                "--ignore-user-config",
                "--ignore-rules",
                "--strict-config",
                "--skip-git-repo-check",
                "--cd",
                str(public),
                "--ephemeral",
                "--json",
                "-c",
                "default_permissions=" + json.dumps(_PROFILE),
                *options,
                *provider,
                "-c",
                'web_search="disabled"',
                "-c",
                "mcp_servers={}",
                "-c",
                "project_doc_max_bytes=0",
                "-c",
                'shell_environment_policy.inherit="none"',
                *(s for feature in disabled for s in ("--disable", feature)),
                "--enable",
                "skip_host_skill_discovery",
                "--output-schema",
                str(public / "proposal-schema.json"),
                "--output-last-message",
                str(public / "proposal.json"),
                "-",
            ]
            # Registration can occur while the native isolation check is running.
            if len(manifest["sample_refs"]) > 1:
                with closing(store.connect(data)) as db:
                    if _blind(db):
                        raise RuntimeError(
                            "research window changed; retry with a fresh withheld projection"
                        )
            proc = subprocess.run(
                cmd,
                input=_prompt(work, manifest),
                text=True,
                cwd=public,
                env=env,
                stdout=out,
                stderr=err,
                timeout=timeout,
                check=False,
            )
            if proc.returncode:
                raise RuntimeError(
                    f"Codex exited with {proc.returncode}; inspect attempt log"
                )
        output = public / "proposal.json"
        if (
            output.is_symlink()
            or not output.is_file()
            or output.stat().st_size > 192000
        ):
            raise ValueError("Codex must produce a bounded structured proposal")
        (work / "proposal.json").write_bytes(output.read_bytes())
    return {
        "cli_version": version,
        "profile": _PROFILE,
        "os_probe": "passed",
        "policy": _POLICY,
    }


def read_proposal(work, manifest):
    path = work / "proposal.json"
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 192_000:
        raise ValueError("Codex must produce a bounded structured proposal")
    item = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(item, dict)
        or set(item) != set(SCHEMA["required"])
        or not isinstance(item["kind"], str)
        or item["kind"] not in KINDS
    ):
        raise ValueError("invalid research proposal shape")
    if (
        not isinstance(item["hypothesis"], str)
        or not 1 <= len(item["hypothesis"].strip()) <= 8000
    ):
        raise ValueError("proposal needs a bounded, nonempty hypothesis")
    refs = item["evidence_refs"]
    if (
        not isinstance(refs, list)
        or not refs
        or any(not isinstance(k, str) or k not in manifest["sample_refs"] for k in refs)
    ):
        raise ValueError("proposal must reference supplied evidence")
    if not isinstance(item["parameters_json"], str):
        raise ValueError("proposal parameters must be encoded as JSON text")
    params = json.loads(item["parameters_json"])
    if not isinstance(params, dict):
        raise ValueError("proposal parameters must be a JSON object")
    required = {
        "note": set(),
        "model": {"family"},
        "source": {
            "source_id",
            "field",
            "terms_url",
            "source_url",
            "baseline",
            "reference_source",
        },
        "patch": {"diff"},
    }
    if set(params) != required[item["kind"]] or any(
        not isinstance(v, str) or not v.strip() for v in params.values()
    ):
        raise ValueError("proposal parameters do not match the declared kind")
    return {
        "kind": item["kind"],
        "hypothesis": item["hypothesis"],
        "evidence_refs": sorted(set(refs)),
        "parameters": params,
    }


def _patch(work, manifest, diff):
    # Check application in a disposable copy. Never run code supplied by a research job.
    with tempfile.TemporaryDirectory(prefix="pm861-review-patch-") as temp:
        review = Path(temp)
        for name, expected in manifest["files"].items():
            file = work / "reference" / name
            if (
                file.is_symlink()
                or hashlib.sha256(file.read_bytes()).hexdigest() != expected
            ):
                raise ValueError(
                    "research reference no longer matches the original source"
                )
            dest = review / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(file, dest)
        subprocess.run(["git", "init", "-q", temp], check=True)
        check = subprocess.run(
            ["git", "apply", "--check", "--whitespace=error", "-"],
            cwd=review,
            input=diff,
            text=True,
            capture_output=True,
        )
        if check.returncode:
            return {"state": "invalid_patch", "reason": check.stderr[:1200]}
        stats = subprocess.run(
            ["git", "apply", "--numstat", "-"],
            cwd=review,
            input=diff,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.splitlines()
    return {
        "state": "awaiting_code_review",
        "applies_to_source": True,
        "diff_sha256": hashlib.sha256(diff.encode()).hexdigest(),
        "changes": stats,
    }


def _source(data_dir, proposal, key, window):
    from . import evidence_sources, jobs_research

    p = dict(proposal["parameters"])
    registered_sources = evidence_sources.list_sources(data_dir)
    # The model sees unknown adapter names as lookup handles. Resolve only actual registered sources.
    with closing(store.connect(data_dir)) as db, db:
        aliases = {
            _source_id(db, s["source_id"]): s["source_id"] for s in registered_sources
        }
    for k in ("source_id", "baseline", "reference_source"):
        p[k] = aliases.get(p[k], p[k])
    if (
        p["field"] not in evidence_sources.FIELDS
        or len({p["source_id"], p["baseline"], p["reference_source"]}) != 3
    ):
        raise ValueError(
            "select a supported field and three distinct source identifiers"
        )
    for k in ("terms_url", "source_url"):
        url = urlsplit(p[k])
        if url.scheme != "https" or not url.hostname or url.username or url.password:
            raise ValueError("source and terms require public HTTPS locators")
    sources = {s["source_id"]: s for s in registered_sources}
    for ident in (p["source_id"], p["baseline"], p["reference_source"]):
        s = sources.get(ident)
        if (
            not s
            or not s["free"]
            or s["status"] == "disabled"
            or "trial" not in s["fields"].get(p["field"], {}).get("uses", [])
        ):
            return {
                "state": "awaiting_source_evidence",
                "missing_source_or_trial_grant": ident,
            }
    s = sources[p["source_id"]]
    with evidence_sources.db(data_dir) as db:
        seen = db.execute(
            "SELECT document FROM ev_facts WHERE source_id=? AND field=?",
            (p["source_id"], p["field"]),
        )
        observed_locator = any(
            fact.get("state") == "active"
            and "trial" in fact.get("capture_uses", [])
            and urlsplit(fact["locator"]).hostname == urlsplit(p["source_url"]).hostname
            for fact in (json.loads(row[0]) for row in seen)
            if fact.get("locator")
        )
    if s["terms_locator"] != p["terms_url"] or not observed_locator:
        return {
            "state": "awaiting_source_evidence",
            "reason": "free terms or observed field sample missing",
        }
    if (
        len(
            {
                sources[i]["upstream"]
                for i in (p["source_id"], p["baseline"], p["reference_source"])
            }
        )
        != 3
    ):
        return {
            "state": "awaiting_source_evidence",
            "reason": "common upstream cannot independently verify the field",
        }
    item = jobs_research.register_candidate(
        data_dir,
        key,
        kind="source",
        version=p["source_id"],
        baseline=p["baseline"],
        window=window,
        hypothesis=proposal["hypothesis"],
        manifest={
            "field": p["field"],
            "reference_source": p["reference_source"],
            "source_url": p["source_url"],
        },
    )
    return {
        "state": "registered_for_trial",
        "experiment": key,
        "spec_digest": item["digest"],
    }


def _model(data_dir, proposal, key, history, season, rnd, window):
    from . import football, jobs_research, pipeline

    family = proposal["parameters"]["family"]
    if family not in pipeline.FAMILIES:
        raise ValueError("model proposal requires an executable registered family")
    if history is None or season is None:
        return {
            "state": "awaiting_history",
            "reason": "supply a verified history folder and a fixed sample season",
        }
    baseline = jobs_research.active_version(data_dir, "model", "epl", default="B1")
    champion = pipeline._recipe(data_dir, baseline)["recipe"]
    if family == champion["family"]:
        return {"state": "same_recipe", "baseline": baseline}
    if not 1 <= rnd <= 38:
        raise ValueError("sample round must be between 1 and 38")
    history = Path(history)
    files = sorted(history.glob("????-??/en.1.json"))
    if not files or any(
        football.blob_sha(f.read_bytes()) != football.SOURCE_BLOBS.get(f.parent.name)
        for f in files
    ):
        raise ValueError(
            "model feasibility replay requires the fixed OpenFootball season originals"
        )
    names = {f.parent.name for f in files}
    if season not in names or not any(s < season for s in names):
        raise ValueError("sample season and earlier training data are required")
    sample_spec = {
        "season": season,
        "round": rnd,
        "baseline": baseline,
        "input_blobs": {
            f.parent.name: football.SOURCE_BLOBS[f.parent.name] for f in files
        },
    }
    with closing(store.connect(data_dir)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        locked = db.execute(
            "SELECT payload FROM reports WHERE kind='research-sample' AND key=?", (key,)
        ).fetchone()
        if locked and json.loads(locked[0]) != sample_spec:
            raise ValueError(
                "the feasibility sample and baseline were fixed on first replay"
            )
        if not locked:
            store.put_report(db, "research-sample", key, sample_spec)
    with tempfile.TemporaryDirectory(prefix="pm861-proposal-backtest-") as temp:
        scratch = Path(temp)
        pipeline.register_recipe(
            scratch, baseline, champion["family"], lines=champion["lines"]
        )
        pipeline.register_recipe(scratch, key, family, lines=champion["lines"])
        summaries = {}
        for model in (baseline, key):
            r = pipeline.replay_round(
                scratch, history, season, rnd, version=model, archive_lag_hours=24
            )["round_report"]
            summaries[model] = {
                k: r.get(k) for k in ("count", "expected", "missing", "score")
            }
    if any(
        v["count"] != 10 or v["expected"] != 10 or v["score"] is None
        for v in summaries.values()
    ):
        return {"state": "insufficient_backtest", "sample": summaries}
    pipeline.register_recipe(data_dir, key, family, lines=champion["lines"])
    registered = jobs_research.register_candidate(
        data_dir,
        key,
        kind="model",
        version=key,
        baseline=baseline,
        window=window,
        hypothesis=proposal["hypothesis"],
        manifest={"evidence_track": forward_window.RESEARCH},
    )
    return {
        "state": "registered_for_trial",
        "experiment": key,
        "sample": summaries,
        "season": season,
        "round": rnd,
        "availability": "24h_lag_sensitivity",
        "spec_digest": registered["digest"],
    }


def _verify_round_origins(data_dir, work, manifest):
    origins = {}
    with closing(store.connect(data_dir)) as db:
        for name in manifest["sample_refs"]:
            if name == "input":
                continue
            sample = json.loads((work / "samples" / name).read_text())
            row = db.execute(
                "SELECT r.* FROM research_round_origins o JOIN round_report_editions r USING(round_key,edition) WHERE o.ref=? AND r.origin='published'",
                (sample["source_ref"],),
            ).fetchone()
            if not row:
                raise ValueError("published research round origin is missing")
            body = json.loads(row["body"])
            projected = _safe_round(body)
            if (
                store.digest(body) != row["sha256"]
                or projected is None
                or sample != projected | {"source_ref": sample["source_ref"]}
            ):
                raise ValueError("published research round origin changed")
            origins[name] = {
                "round_key": row["round_key"],
                "edition": row["edition"],
                "sha256": row["sha256"],
            }
    return origins


def verify(data_dir, key, *, history=None, season=None, rnd=1):
    from . import jobs

    with closing(jobs._open(data_dir)) as db:
        job = jobs._read_job(db, key)
    result = job.get("result") or {}
    if (
        job["state"] != "done"
        or not result.get("proposal_digest")
        or not (is_research(job) or result.get("research_isolation"))
    ):
        raise ValueError("this job has no published Codex research proposal")
    work = Path(jobs._local_output(data_dir, key, result["output_dir"]))
    path = work / "reference.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if store.digest(manifest) != result["reference_digest"]:
        raise ValueError("reference manifest changed after the job finished")
    for name, expected in manifest["sample_refs"].items():
        file = work / "input.json" if name == "input" else work / "samples" / name
        if (
            file.is_symlink()
            or hashlib.sha256(file.read_bytes()).hexdigest() != expected
        ):
            raise ValueError("research sample changed after the job finished")
    round_origins = (
        _verify_round_origins(data_dir, work, manifest)
        if manifest.get("disclosure_policy") == _POLICY
        else {}
    )
    proposal = read_proposal(work, manifest)
    if store.digest(proposal) != result["proposal_digest"]:
        raise ValueError("the proposal changed after the worker published its result")
    trial = "research-" + store.digest([key, proposal])[:30]
    # The registration date is fixed to publication, including when verification is retried.
    when = store.instant(result["proposal_at"])
    if proposal["kind"] == "model":
        start_season, start_round = forward_window.next_start(
            store.eligible_fixtures(data_dir), max(when, store.instant(store.now_utc()))
        )
        window = f"{start_season}/{start_round}"
    else:
        first_year = when.year + (when.month >= 7)
        window = f"{first_year}-{(first_year + 1) % 100:02d}/1"
    terminal = {"registered_for_trial", "note", "awaiting_code_review", "same_recipe"}
    with closing(store.connect(data_dir)) as db:
        old = db.execute(
            "SELECT payload FROM reports WHERE kind='research-proposal' AND key=?",
            (key,),
        ).fetchone()
    if old:
        published = json.loads(old[0])
        if (
            published["proposal_digest"] == result["proposal_digest"]
            and published["state"] in terminal
        ):
            return published
    if (
        manifest.get("disclosure_policy") != _POLICY
        or (result.get("research_isolation") or {}).get("os_probe") != "passed"
    ):
        raise ValueError(
            "this legacy attempt requires a new restricted research run before evaluation"
        )
    kind = proposal["kind"]
    if kind == "model":
        verdict = _model(data_dir, proposal, trial, history, season, rnd, window)
    elif kind == "source":
        verdict = _source(data_dir, proposal, trial, window)
    elif kind == "patch":
        verdict = _patch(work, manifest, proposal["parameters"]["diff"])
    else:
        verdict = {"state": "note"}
    report = {
        "job": key,
        "proposal_digest": result["proposal_digest"],
        "source_digest": manifest["source_tree_digest"],
        "code_revision": manifest["revision"],
        "kind": kind,
        "hypothesis": proposal["hypothesis"],
        "evidence_refs": proposal["evidence_refs"],
        "released_round_origins": round_origins,
        "parameters": proposal["parameters"],
        **verdict,
    }
    with closing(store.connect(data_dir)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        current = db.execute(
            "SELECT payload FROM reports WHERE kind='research-proposal' AND key=?",
            (key,),
        ).fetchone()
        if current:
            previous = json.loads(current[0])
            if (
                previous["proposal_digest"] == report["proposal_digest"]
                and previous["state"] in terminal
            ):
                return previous
        store.put_report(db, "research-proposal", key, report)
    return report
