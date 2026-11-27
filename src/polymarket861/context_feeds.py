"""Free field-scoped adapters for GDELT index metadata and individual GFS forecast runs."""

import json
import math
import hashlib
import io
import re
import zipfile
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

from . import evidence as ev
from . import store
from .market_provider import Reply, get


GDELT = "gdelt-doc"
GKG = "gdelt-gkg"
WEATHER = "open-meteo-gfs"
NEWS_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
WEATHER_URL = "https://single-runs-api.open-meteo.com/v1/forecast"
GKG_BASE = "https://data.gdeltproject.org/gdeltv2/"
VARIABLES = {"temperature_2m": "°C", "precipitation": "mm", "wind_speed_10m": "m/s", "wind_gusts_10m": "m/s"}


def providers():
    return {
        GDELT: {
            "source_id": GDELT, "name": "GDELT DOC 2.0 article index", "upstream": "GDELT",
            "terms_locator": "https://www.gdeltproject.org/data.html",
            "docs_locator": "https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/",
            "endpoint": NEWS_URL, "attribution": "The GDELT Project, https://www.gdeltproject.org/",
            "scope": "Article index metadata only. Index first-seen is distinct from reporter publication.",
            "fields": {"news": {"uses": ["trial"], "methods": ["auto"], "unit": "article link"}},
            "raw_fields": ["url", "domain", "seendate", "language", "sourcecountry", "headline_fingerprint"],
        },
        GKG: {
            "source_id":GKG, "name":"GDELT Global Knowledge Graph 15-minute index", "upstream":"GDELT",
            "terms_locator":"https://www.gdeltproject.org/data.html",
            "docs_locator":"https://data.gdeltproject.org/documentation/GDELT-Global_Knowledge_Graph_Codebook-V2.pdf",
            "endpoint":GKG_BASE + "lastupdate.txt", "attribution":"The GDELT Project, https://www.gdeltproject.org/",
            "scope":"Web record links matched on indexed organization or URL, shadow leads only.",
            "fields":{"news":{"uses":["trial"],"methods":["auto"],"unit":"article link"}},
            "raw_fields":["gkg_record_id","url","domain","seendate","matched_via"],
        },
        WEATHER: {
            "source_id": WEATHER, "name": "Open-Meteo Single Runs, NOAA GFS Global", "upstream": "NOAA-GFS",
            "terms_locator": "https://open-meteo.com/en/terms",
            "docs_locator": "https://open-meteo.com/en/docs/single-runs-api", "endpoint": WEATHER_URL,
            "attribution": "Weather data by Open-Meteo (open-meteo.com), CC BY 4.0. Model: NOAA GFS.",
            "scope": "Free hosted access requires noncommercial use. Data attribution is required.",
            "fields": {"weather": {"uses": ["trial"], "methods": ["auto"], "unit": VARIABLES}},
            "raw_fields": ["latitude", "longitude", "elevation", "utc_offset_seconds", "hourly_units", "hourly"],
        },
    }


def enable(data_dir, provider, *, noncommercial=False):
    spec = providers()[provider]
    if provider == WEATHER and not noncommercial:
        raise ValueError("Open-Meteo hosted free access requires explicit --noncommercial for this use")
    with ev.db(data_dir) as conn:
        known = ev.source_at(conn, provider)
    if known:
        return ev.source_report(data_dir, provider)
    return ev.register_source(data_dir, dict(spec, free=True, status="candidate", adapter_version=1,
                                             noncommercial_use_confirmed=bool(noncommercial)), only_new=True)


def _authorize(data_dir, provider, field):
    with ev.db(data_dir) as conn:
        manifest = ev.source_at(conn, provider, ev.utc(store.now_utc()))
    if not manifest or not ev.granted(manifest, field, "auto"):
        raise ValueError(f"first enable or register a free {provider}/{field} automatic collection grant")
    if provider == WEATHER and manifest.get("noncommercial_use_confirmed") is not True:
        raise ValueError("this source manifest has no noncommercial free API access declaration")
    if provider in {GDELT, GKG} and "trial" not in ev.granted(manifest, field, "auto"):
        raise ValueError("GDELT metadata harvesting requires a trial grant for news")


def _remember(data_dir, provider, rows, archive, reply):
    """A repeated poll is harmless; a changed run keeps its previous observation."""
    ids = []
    with ev.db(data_dir, write=True) as conn:
        for record, sequence in rows:
            low, high = sequence * 1_000_000, (sequence + 1) * 1_000_000
            previous = conn.execute(
                """SELECT document FROM ev_facts WHERE source_id=? AND fixture_id=? AND team_key=?
                   AND field=? AND origin_id=? AND claim_key=? AND revision>=? AND revision<?
                   ORDER BY revision DESC LIMIT 1""",
                (provider, record["fixture_id"], ev.name_key(record["team"]), record["field"],
                 record["origin_id"], record["claim_key"], low, high),
            ).fetchone()
            old = json.loads(previous[0]) if previous else None
            content = record["value"]
            semantic_hash = ev.digest(content)
            if old and old["value"].get("observation_hash") == semantic_hash:
                ids.append(old["evidence_id"])
                continue
            revision = old["revision"] + 1 if old else low
            if revision >= high:
                raise ValueError("feed exhausted observation revision range")
            value = dict(content, observation_hash=semantic_hash, snapshot_sha256=archive["sha256"],
                         request_url=reply.url, requested_at=reply.requested_at,
                         local_first_response_at=reply.received_at)
            fact = ev._ingest(conn, dict(record, value=value, source_id=provider, method="auto",
                                          received_at=reply.received_at, revision=revision))
            ids.append(fact["evidence_id"])
    return ids


def _pull(data_dir, provider, field, endpoint, params, adapter, getter):
    _authorize(data_dir, provider, field)
    requested = store.now_utc()
    url = endpoint + ("?" + urlencode(params) if params else "")
    try:
        reply = getter(endpoint, params, timeout=18)
        payload = json.loads(reply.body)
        projection, rows, latest, skipped = adapter(payload, reply)
        archive = store.archive_bytes(data_dir, provider, ev.dump(projection).encode(),
            observed_at=reply.received_at, metadata={"request_url": reply.url, "requested_at": reply.requested_at,
                "received_at": reply.received_at, "response_date": reply.headers.get("date"),
                "projection": "allowlisted_provider_fields_v1"})
        ids = _remember(data_dir, provider, rows, archive, reply)
    except (OSError, ValueError, KeyError, TypeError, IndexError) as error:
        status = "error" if isinstance(error, OSError) else "invalid"
        code = error.code if isinstance(error, HTTPError) else None
        details = str(error)[:240]
        if provider == WEATHER and isinstance(error, HTTPError) and error.fp is not None:
            try:
                message = json.loads(error.read(4096)).get("reason")
                if isinstance(message, str):
                    details += ": " + message[:240]
            except (OSError, ValueError, AttributeError):
                pass
        check = ev.observe_source(data_dir, {"source_id": provider, "field": field, "outcome": status,
            "checked_at": store.now_utc(), "requested_at": requested, "request_url": url,
            "http_status": code, "detail": details})
        return {"source_id": provider, "status": check["status"], "http_status": code, "detail": details,
                "request_url": url, "requested_at": requested, "checked_at": check["checked_at"], "evidence_ids": []}
    ev.observe_source(data_dir, {"source_id": provider, "field": field, "outcome": "ok",
        "checked_at": reply.received_at, "latest_data_at": latest, "content_hash": archive["sha256"]})
    return {"source_id": provider, "status": "ok", "requested_at": reply.requested_at,
            "received_at": reply.received_at, "request_url": reply.url, "snapshot_sha256": archive["sha256"],
            "attribution": providers()[provider]["attribution"], "rows_skipped": skipped, "evidence_ids": ids}


def _article_url(value):
    url = urlsplit(value) if isinstance(value, str) else None
    if not url or url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
        raise ValueError("invalid news article URL")
    pairs = [(k, v) for k, v in parse_qsl(url.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid"}]
    return urlunsplit((url.scheme.lower(), url.netloc.lower(), url.path or "/", urlencode(pairs), ""))


def _news_time(value):
    return store.iso(datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc))


def _gkg_fetcher(team, limit, getter):
    """Download one known GKG release and only materialize matching index metadata."""
    tokens = team.casefold().split()
    if tokens and tokens[-1] in {"fc", "afc"}:
        tokens.pop()
    if tokens and tokens[0] in {"fc", "afc"}:
        tokens.pop(0)
    team_name = " ".join(tokens)
    if not team_name:
        raise ValueError("GKG needs an identifiable club")
    pattern = re.compile(r"(?<!\w)" + re.escape(team_name) + r"(?!\w)")
    def fetch(endpoint, params, *, timeout):
        reply = getter(endpoint, params, timeout=timeout)
        first_requested = reply.requested_at
        expected_size = checksum = None
        if endpoint.endswith("lastupdate.txt"):
            manifest = [line.split() for line in reply.body.decode("ascii").splitlines()]
            releases = [r for r in manifest if len(r) == 3 and re.fullmatch(
                r"https?://data\.gdeltproject\.org/gdeltv2/\d{14}\.gkg\.csv\.zip", r[2])]
            if len(releases) != 1:
                raise ValueError("GKG index has no unique current release")
            size, checksum, release = releases[0]
            expected_size = int(size)
            if expected_size <= 0 or expected_size > 16 * 1024 * 1024:
                raise ValueError("GKG release size exceeds this adapter's read limit")
            reply = getter(GKG_BASE + release.rsplit("/", 1)[-1], {}, timeout=timeout)
        if expected_size is not None and (len(reply.body) != expected_size or
                hashlib.md5(reply.body, usedforsecurity=False).hexdigest() != checksum.lower()):
            raise ValueError("GKG release disagrees with its provider size or checksum")
        articles, scanned = [], 0
        try:
            with zipfile.ZipFile(io.BytesIO(reply.body)) as archive:
                files = archive.infolist()
                if len(files) != 1 or not re.fullmatch(r"\d{14}\.gkg\.csv", files[0].filename) or files[0].file_size > 128*1024*1024:
                    raise ValueError("unexpected GKG release member")
                with archive.open(files[0]) as stream:
                    for raw in stream:
                        scanned += 1
                        if len(articles) >= limit or len(raw) > 1_048_576:
                            continue
                        row = raw.decode("utf-8", errors="replace").rstrip("\r\n").split("\t")
                        if len(row) < 15 or row[2] != "1":
                            continue
                        organisations = row[13].casefold()
                        path = re.sub(r"[^\w]+", " ", unquote(urlsplit(row[4]).path).casefold())
                        via = "indexed_organization" if pattern.search(organisations) else "article_url" if pattern.search(path) else None
                        if via:
                            articles.append({"gkg_record_id":row[0], "url":row[4], "domain":row[3],
                                "seendate":row[1][:8] + "T" + row[1][8:] + "Z", "matched_via":via})
        except zipfile.BadZipFile as error:
            raise ValueError("invalid GKG ZIP archive") from error
        content = ev.dump({"articles":articles, "release_rows_scanned":scanned}).encode()
        return Reply(content, first_requested, reply.received_at, reply.headers, reply.url)
    return fetch


def collect_news(data_dir, fixture_id, team, *, query=None, limit=30, timespan="2d", getter=get,
                 feed=GDELT, gkg_at=None):
    fixture = store.fixture_at(data_dir, fixture_id)
    if team not in {fixture["home"], fixture["away"]} or not 1 <= limit <= 250:
        raise ValueError("news needs a scheduled team and a record limit in [1,250]")
    if feed not in {GDELT, GKG} or (gkg_at and feed != GKG):
        raise ValueError("use a supported news feed, --gkg-at only applies to gdelt-gkg")
    params = {"query": query or f'"{team}" (injury OR suspension OR lineup OR travel OR coach)',
              "mode": "ArtList", "format": "json", "sort": "DateDesc", "timespan": timespan, "maxrecords": limit}
    endpoint = NEWS_URL
    if feed == GKG:
        if query:
            raise ValueError("GKG matches the scheduled team directly; custom queries use gdelt-doc")
        endpoint, params, getter = GKG_BASE + "lastupdate.txt", {}, _gkg_fetcher(team, limit, getter)
        if gkg_at:
            batch = store.instant(gkg_at)
            if batch.minute%15 or batch.second or batch.microsecond or batch > store.instant(store.now_utc()):
                raise ValueError("gkg_at must identify an elapsed UTC quarter-hour release")
            endpoint = GKG_BASE + batch.strftime("%Y%m%d%H%M%S") + ".gkg.csv.zip"

    def adapt(payload, reply):
        articles = payload["articles"]
        if not isinstance(articles, list):
            raise ValueError("GDELT response lacks an article list")
        projection, rows, times, seen = [], [], [], set()
        skipped = {"invalid_or_missing_time": 0, "invalid_url": 0, "duplicate_url": 0}
        for article in articles:
            try:
                url = _article_url(article.get("url") if isinstance(article, dict) else None)
            except ValueError:
                skipped["invalid_url"] += 1
                continue
            headline = " ".join(str(article.get("title") or "").casefold().split())
            metadata = {key: article.get(key) for key in ("domain", "seendate", "language", "sourcecountry")}
            metadata.update(url=url, headline_fingerprint=ev.digest(headline) if headline else None)
            if feed == GKG:
                metadata = {key: article.get(key) for key in ("gkg_record_id","domain","seendate","matched_via")}
                metadata["url"] = url
            projection.append(metadata)
            try:
                index_seen = _news_time(article["seendate"])
                if store.instant(index_seen) > store.instant(reply.received_at):
                    raise ValueError("post-receipt index time")
            except (KeyError, ValueError, TypeError):
                skipped["invalid_or_missing_time"] += 1
                continue
            if url in seen:
                skipped["duplicate_url"] += 1
                continue
            seen.add(url)
            times.append(index_seen)
            domain = urlsplit(url).hostname.lower()
            value = dict(metadata, index_seen_at=index_seen, publisher_published_at=None,
                         time_basis="index_observation_only", evidence_class="news_index_lead",
                         attribution=providers()[feed]["attribution"])
            record = {"fixture_id": fixture_id, "team": team, "field": "news", "origin_id":domain,
                      "claim_key": ev.digest(url), "published_at": index_seen, "locator": url,
                      "value": value, "trial_only": True}
            rows.append((record, 1))
        output = {"articles":projection}
        if feed == GKG:
            output["release_rows_scanned"] = payload["release_rows_scanned"]
        return output, rows, max(times, default=None), skipped
    result = _pull(data_dir, feed, "news", endpoint, params, adapt, getter)
    if feed == GDELT and query is None and result["status"] == "error":
        with ev.db(data_dir) as conn:
            backup = ev.source_at(conn, GKG, ev.utc(store.now_utc()))
        if "trial" in ev.granted(backup, "news", "auto"):
            fallback = collect_news(data_dir, fixture_id, team, limit=limit, getter=getter, feed=GKG)
            fallback["primary_attempt"] = result
            fallback["fallback_scope"] = "latest 15-minute GKG release, scheduled club match"
            return fallback
    return result


def collect_weather(data_dir, fixture_id, *, latitude, longitude, venue, venue_locator, run_at, getter=get):
    fixture = store.fixture_at(data_dir, fixture_id)
    target = store.instant(fixture["kickoff"])
    run = store.instant(run_at)
    if run.minute or run.second or run.microsecond or run.hour not in {0, 6, 12, 18}:
        raise ValueError("GFS run_at must identify a 00/06/12/18 UTC initialization")
    if run > min(target, store.instant(store.now_utc())):
        raise ValueError("forecast run must already exist and precede kickoff")
    if not all(isinstance(x, (int, float)) and not isinstance(x,bool) and math.isfinite(x)
               for x in (latitude, longitude)) or not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        raise ValueError("invalid geodetic venue coordinates")
    venue, venue_locator = ev.required(venue, "venue"), ev.required(venue_locator, "venue coordinate source")
    floor = target.replace(minute=0, second=0, microsecond=0)
    hours = [floor] if target == floor else [floor, floor + timedelta(hours=1)]
    params = {"latitude": latitude, "longitude": longitude, "models": "gfs_global",
              "run": run.strftime("%Y-%m-%dT%H:%M"), "hourly": ",".join(VARIABLES),
              "wind_speed_unit": "ms", "temperature_unit": "celsius", "precipitation_unit": "mm", "timezone": "GMT"}

    def adapt(payload, reply):
        if payload.get("utc_offset_seconds") != 0:
            raise ValueError("forecast must use UTC")
        units = payload["hourly_units"]
        if any(units.get(k) != v for k, v in VARIABLES.items()):
            raise ValueError("unexpected forecast units")
        hourly = payload["hourly"]
        times = hourly["time"]
        if not isinstance(times, list) or len(set(times)) != len(times):
            raise ValueError("invalid forecast hour axis")
        if any(not isinstance(hourly.get(key), list) or len(hourly[key]) != len(times) for key in VARIABLES):
            raise ValueError("incomplete forecast variables")
        grid = {key: payload[key] for key in ("latitude", "longitude")}
        if any(not ev._number(v) for v in grid.values()):
            raise ValueError("invalid model grid coordinates")
        samples = []
        for hour in hours:
            label = hour.strftime("%Y-%m-%dT%H:%M")
            if label not in times:
                raise ValueError("requested kickoff hour is outside the available forecast run")
            index = times.index(label)
            row = {key: hourly[key][index] for key in VARIABLES}
            if any(not ev._number(value) for value in row.values()):
                raise ValueError("provider has missing kickoff values")
            samples.append(dict(row, valid_at=store.iso(hour)))
        projection = {key: payload.get(key) for key in ("latitude", "longitude", "elevation", "utc_offset_seconds")}
        projection["hourly_units"] = {k: units[k] for k in ("time", *VARIABLES) if k in units}
        projection["hourly"] = {k: hourly[k] for k in ("time", *VARIABLES)}
        value = {"forecast_for":store.iso(target), "run_at":store.iso(run), "model":"gfs_global",
                 "issue_time_basis":"first_local_response_upper_bound", "provider_published_at":None,
                 "venue":venue, "venue_locator":venue_locator, "latitude":latitude, "longitude":longitude,
                 "model_grid":grid, "units":VARIABLES, "forecast_hours":samples,
                 "attribution":providers()[WEATHER]["attribution"]}
        record = {"fixture_id": fixture_id, "team":"", "field":"weather", "origin_id":"NOAA-GFS",
                  "claim_key":"venue_forecast", "locator":reply.url, "published_at": reply.received_at,
                  "trial_only":store.instant(reply.received_at) >= target, "value":value}
        return projection, [(record, int(run.timestamp()))], store.iso(run), {}
    return _pull(data_dir, WEATHER, "weather", WEATHER_URL, params, adapt, getter)


def register(subparsers):
    feed = subparsers.add_parser("feeds", help="采集免费新闻索引与定次开球天气预报")
    commands = feed.add_subparsers(dest="feed_op", required=True)
    from . import league_press

    league_press.register_feeds(commands)

    def bind(command, function):
        command.set_defaults(func=function, handler=function)
    bind(commands.add_parser("providers"), lambda _: providers())
    approval = commands.add_parser("enable", help="登记影子试采；天气需确认本次使用属于非商业")
    approval.add_argument("provider", choices=(GDELT, GKG, WEATHER))
    approval.add_argument("--noncommercial", action="store_true")
    bind(approval, lambda a: enable(a.data_dir, a.provider, noncommercial=a.noncommercial))
    news = commands.add_parser("news", help="按已登记赛程的球队采集 GDELT 索引")
    news.add_argument("fixture_id"); news.add_argument("team")
    news.add_argument("--query"); news.add_argument("--limit", type=int, default=30)
    news.add_argument("--timespan", default="2d")
    news.add_argument("--feed", choices=(GDELT, GKG), default=GDELT)
    news.add_argument("--gkg-at", help="固定 GKG 的 UTC 15 分钟发布批次；默认查最新索引")
    bind(news, lambda a:collect_news(a.data_dir, a.fixture_id, a.team, query=a.query,
                                     limit=a.limit, timespan=a.timespan, feed=a.feed, gkg_at=a.gkg_at))
    weather = commands.add_parser("weather", help="读取一份有明确起报时间的 GFS 预报")
    weather.add_argument("fixture_id")
    weather.add_argument("--venue", required=True); weather.add_argument("--venue-locator", required=True)
    weather.add_argument("--latitude", type=float, required=True); weather.add_argument("--longitude", type=float, required=True)
    weather.add_argument("--run-at", required=True)
    bind(weather, lambda a:collect_weather(a.data_dir, a.fixture_id, latitude=a.latitude, longitude=a.longitude,
                      venue=a.venue, venue_locator=a.venue_locator, run_at=a.run_at))
