"""Read-only match and player views on the existing local football dashboard."""

import html
import hashlib
import json
import sqlite3
from contextlib import closing
from urllib.parse import parse_qs, urlencode, urlsplit

from . import big5_scheduler, dashboard, football, league_registry, news_events, news_model, news_roster, news_scope
from . import news_sources, store, wire_match_sheets

NAMES = {code: {"en": "英超", "es": "西甲", "de": "德甲", "it": "意甲", "fr": "法甲"}[p.archive_code]
         for code, p in league_registry.PROFILES.items()}
HORIZONS = {"preview": "首次预览", "early": "提前预测", "news_update": "历史新闻封存",
            "forecast_update": "预测或伤停情景更新",
            "T-24h": "距开球 24 小时", "T-60m": "距开球 60 分钟"}
STATES = {"not_started": "尚无运行记录", "pending": "等待观测", "unknown": "缺少观测",
          "rate_limited": "来源限流", "not_configured": "未配置数据源", "provider_backoff": "等待来源重试",
          "no_published_players": "来源未列出球员", "fixture_identity_unmatched": "尚未匹配本场",
          "fixture_identity_ambiguous": "比赛标识待核实", "subscription_or_access_denied": "来源访问受限",
          "unavailable": "来源暂不可用", "network_error": "来源暂不可达", "reported": "已获报告",
          "published": "已公布", "retry": "等待重试", "ok": "运行中", "partial": "部分数据可用",
          "stale": "超过两分钟未完成调度", "exited": "调度进程已退出", "stopped": "调度进程已停止",
          "starting": "调度正在启动", "one_shot": "仅完成单次运行",
          "free_season_unavailable": "免费方案未覆盖本赛季", "rights_unverified": "来源使用权限待核实",
          "unpublished": "来源尚未公布", "unmonitored": "尚无常驻进程心跳",
          "initializing": "正在完成首次调度", "clock_conflict": "系统时间与收件时间冲突",
          "source_conflict": "来源修订冲突，等待复核"}


def _e(value):
    return html.escape(str(value), quote=True)


def _url(league=None, fixture=None):
    if not league:
        return "/leagues"
    params = {"league": league}
    if fixture:
        params["fixture"] = fixture
    return "/leagues?" + _e(urlencode(params))


def _time(timestamp):
    if not timestamp:
        return "缺少时间"
    return store.instant(timestamp).strftime("%m-%d %H:%M UTC")


def _probabilities(values):
    return [f"{100 * values[key]:.1f}%" if isinstance(values.get(key), (int, float)) else "—"
            for key in ("home_win", "draw", "away_win")]


def _total_lines(totals, match):
    rows = []
    for group, label in (("total", "全场"), ("home", match["home"]), ("away", match["away"])):
        for line, values in sorted(totals.get(group, {}).items(), key=lambda v: float(v[0])):
            rows.append(f"<tr><td>{_e(label)}</td><td>{_e(line)}</td>"
                        f"<td>{values['over']:.1%}</td><td>{values['under']:.1%}</td></tr>")
    if not rows:
        return ""
    return ("<details><summary>全场和单队大小球概率</summary><div class='table-wrap'><table>"
            "<thead><tr><th>进球范围</th><th>球数线</th><th>大球</th><th>小球</th></tr></thead>"
            "<tbody>" + "".join(rows) + "</tbody></table></div></details>")


def _season_matches(data_dir, code, fixture_id=None):
    file = data_dir / "runtime.sqlite3"
    if not file.is_file():
        return []
    since = store.instant(store.now_utc())
    until = since + big5_scheduler.LOOKAHEAD
    with closing(sqlite3.connect(file.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='forecast_seals'").fetchone():
            return []
        target = " AND s.fixture_id=?" if fixture_id is not None else ""
        rows = db.execute("SELECT s.data_json,s.digest,s.sealed_at FROM forecast_seals s "
                          "WHERE s.kickoff>? AND s.kickoff<=? AND s.sealed_at<=?" + target + " AND s.kickoff=("
                          "SELECT f.kickoff FROM fixture_versions f WHERE f.fixture_id=s.fixture_id "
                          "AND f.known_at<=? ORDER BY f.known_at DESC LIMIT 1) "
                          "ORDER BY s.kickoff,s.fixture_id,s.sealed_at DESC",
                          (store.iso(since), store.iso(until), store.iso(since),
                           *((fixture_id,) if fixture_id is not None else ()), store.iso(since)))
        seen = set()
        results = []
        for row in rows:
            payload = json.loads(row["data_json"])
            fid = payload.get("fixture_id")
            if not fid or fid in seen or store.digest(payload) != row["digest"]:
                continue
            seen.add(fid)
            results.append({"fixture_id": fid, "league": code, "season": payload["season"],
                            "home": payload["home"], "away": payload["away"],
                            "kickoff": payload["kickoff"], "latest_seal": {"horizon": payload["horizon"],
                            "sealed": row["sealed_at"], "digest": row["digest"], "lineups": payload.get("lineups", {}),
                            "probabilities": payload["probabilities"], "totals": payload.get("totals", {}),
                            "model": payload.get("model", "—"), "mode": payload.get("mode", "—")}})
        return results


def _link(value, label):
    try:
        parsed = urlsplit(value) if isinstance(value, str) else None
        valid = parsed and parsed.scheme in {"https", "http"} and parsed.hostname and not (
            parsed.username is not None or parsed.password is not None)
    except ValueError:
        valid = False
    if valid:
        return f'<a rel="noreferrer" href="{_e(value)}">{_e(label)}</a>'
    return _e(label)


def _table(matches, code):
    lines = []
    for match in matches:
        seal = match.get("latest_seal", {})
        name = _e(match["home"]) + " – " + _e(match["away"])
        link = _url(code, match["fixture_id"])
        probs = "".join(f"<td>{p}</td>" for p in _probabilities(seal.get("probabilities", {})))
        stage = HORIZONS.get(seal.get("horizon"), seal.get("horizon", "等待首次预测"))
        lines.append(f'<tr><td>{_time(match["kickoff"])}</td><td><a href="{link}">{name}</a></td>'
                     f'{probs}<td>{_e(seal.get("model", "—"))} · {_e(stage)}</td></tr>')
    if not lines:
        return "<p>未来七天暂无已登记的可展示比赛。</p>"
    return ("<div class='table-wrap'><table><thead><tr><th>开球时间</th><th>主队 – 客队</th>"
            "<th>主胜</th><th>平局</th><th>客胜</th><th>预测版本</th></tr></thead><tbody>" +
            "".join(lines) + "</tbody></table></div>")


def _people(rows):
    return "、".join(_e(row.get("name")) if isinstance(row, dict) else _e(row) for row in rows)


def _team_fields(fixture, side, news):
    base = news.get("teams", {}).get(side, {})
    roster = base.get("roster", {})
    players = roster.get("players", ())
    out = [f'<section class="panel"><h3>{_e(fixture[side])}</h3>']
    if players:
        out.append(f"<details><summary>球队名单（{len(players)} 人）</summary><p>{_people(players)}</p></details>")
    else:
        out.append("<p>球队名单尚缺已核实的本地记录。</p>")
    if roster.get("ready_at"):
        out.append("<p>球队名单可用于研究的时间　" + _e(_time(roster["ready_at"])) + "</p>")
    langs = {"es": "西班牙语", "de": "德语", "it": "意大利语", "fr": "法语", "en": "英语"}
    for ev in roster.get("evidence", ()):
        label = langs.get(ev.get("language"), ev.get("language") or "原文")
        out.append("<p><small>" + _link(ev.get("revision_url"), f"{label} · {ev.get('title') or '球队名单'}") +
                   " · 修订 " + _e(_time(ev.get("published_at"))) + " · 本机收件 " +
                   _e(_time(ev.get("received_at"))) + " · " + _e(ev.get("attribution") or "Wikipedia contributors") +
                   " · " + _link(ev.get("license"), "CC BY-SA") + "</small></p>")
    markers = base.get("availability", {}).get("items", [])
    if markers:
        out.append("<p><strong>球队名单伤病标注</strong>　" + "、".join(
            _e(v["player"]) + "（原文 " + _e(v["source_label"]) + "）" for v in markers) +
            "。适用比赛待核实。</p>")
    totals = [(p, p["league_totals"]) for p in players if p.get("league_totals", {}).get("scope") == "club_career"]
    if totals:
        rows = "".join(f'<tr><td>{_e(p["name"])}</td><td>{_e(t["appearances"])}</td><td>{_e(t["goals"])}</td>'
                       f'<td>{_e(t.get("as_of_source_text") or "来源未列出日期")}</td></tr>' for p, t in totals)
        out.append("<details><summary>球员效力本队期间的联赛累计数据</summary><div class='table-wrap'>"
                   "<table><thead><tr><th>球员</th><th>出场</th><th>进球</th><th>来源统计截至</th>"
                   "</tr></thead><tbody>" + rows + "</tbody></table></div></details>")
    change = roster.get("changes", {})
    if change and change.get("scope") == "observed_roster_revision":
        out.append("<details><summary>两次本地名单的增减</summary><p>" + _e(_time(change["compared_with"])) +
                   " 至 " + _e(_time(change["observed_at"])) + "。新增　" + (_people(change["added"]) or "无") +
                   "。移出　" + (_people(change["removed"]) or "无") + "。</p>")
        for prior in change.get("prior_evidence", ()):
            out.append("<small>" + _link(prior.get("revision_url"), "上一次名单原文") + "</small>")
        out.append("</details>")
    private = news.get("player_status", {})
    for field, title in (("injuries", "伤停报告"), ("lineup", "已公布首发")):
        snapshot = private.get(field, {})
        team = snapshot.get("teams", {}).get(side, {})
        rows = team.get("players", [])
        if rows:
            if field == "injuries":
                text = "、".join(_e(r["name"]) +
                                ("（" + _e(r.get("reason") or r.get("provider_type")) + "）"
                                 if r.get("reason") or r.get("provider_type") else "") for r in rows)
            else:
                text = _people(rows)
            source = "已登记的免费来源" if snapshot.get("source") == "registered-free-evidence" else "API-Football"
            out.append(f"<p><strong>{title}</strong>　{text}</p>"
                       f"<small>{source} · 本机收件于 {_time(snapshot.get('received_at'))}</small>")
        else:
            state = snapshot.get("state", "unknown")
            out.append(f"<p><strong>{title}</strong>　{_e(STATES.get(state, '缺少本队本场记录'))}</p>")
    return "".join(out) + "</section>"


def _headlines(press, match):
    out = ["<p>索引按俱乐部匹配，报道与本场的关联待核实。</p>"]
    for side in ("home", "away"):
        team = press.get("teams", {}).get(side, {})
        stories = []
        for item in team.get("items", ()):
            url = item.get("url")
            title = item.get("title") or item.get("headline")
            domain = item.get("domain") or "来源链接"
            label = title or domain
            seen = item.get("index_seen_at") or item.get("received_at")
            metadata = [domain] if title and item.get("domain") else []
            if not title:
                metadata.append("索引未提供标题")
            if seen:
                metadata.append(("索引时间 " if item.get("index_seen_at") else "本机收件 ") + _time(seen))
            if item.get("title_received_at"):
                metadata.append("标题收件 " + _time(item["title_received_at"]))
            if item.get("club_label"):
                metadata.append("命中球队名 " + item["club_label"])
            details = " · ".join(_e(field) for field in metadata)
            stories.append("<li>" + _link(url, label) + (f" <small>{details}</small>" if details else "") + "</li>")
        out.append(f'<h3>{_e(match[side])}</h3>')
        out.append("<ul>" + "".join(stories) + "</ul>" if stories else "<p>尚无可显示的已匹配报道。</p>")
    if press.get("attribution"):
        out.append("<small>" + _e(press["attribution"]) + "</small>")
    return "".join(out)


def _season_people(data_dir, fixture, cutoff):
    fid, kick = fixture["fixture_id"], store.iso(fixture["kickoff"])
    at = min(store.iso(cutoff), kick)
    file = data_dir / "runtime.sqlite3"
    empty = {"teams": {}, "snapshot": {}, "report_link": None, "as_of": at}
    if not file.is_file():
        return empty
    # The coordinator already projected fixture-bounded evidence into immutable news editions.
    # Reading the latest usable edition avoids rebuilding the season-wide news pipeline on a page view.
    with closing(sqlite3.connect(file.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        teams = {side: fixture[side] for side in ("home", "away")}
        views = news_roster.roster_views(db, teams.values(), fixture["season"], at)
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                            "AND name IN ('reports','news_event_updates')")}
        digest = fixture.get("latest_seal", {}).get("digest")
        report_link = None
        if "reports" in tables and digest and db.execute(
            "SELECT 1 FROM reports WHERE kind='match' AND key=?", (digest,)
        ).fetchone():
            report_link = "/#matches/" + hashlib.sha256(("match\0" + digest).encode()).hexdigest()
        snapshot = {}
        if "news_event_updates" in tables:
            rows = db.execute("SELECT * FROM news_event_updates WHERE fixture_id=? AND kickoff=? "
                              "ORDER BY revision DESC", (fid, kick))
            for row in rows:
                doc = news_events._verified(row)
                if (doc.get("season") != fixture["season"] or doc.get("home") != teams["home"]
                        or doc.get("away") != teams["away"] or not doc.get("cutoff") or not doc.get("created_at")
                        or not doc["cutoff"] <= doc["created_at"] <= at or doc["created_at"] >= kick):
                    continue
                snap = doc["snapshot"]
                if (snap.get("fixture_id") == fid and snap.get("cutoff") == doc["cutoff"]
                        and store.iso(snap["schedule"]["kickoff"]) == kick):
                    snapshot = snap
                    break
    return {"teams": {side: views[news_roster._key(team)] for side, team in teams.items()},
            "snapshot": snapshot, "report_link": report_link, "as_of": at}


def _season_fields(fixture, evidence, side):
    team = fixture[side]
    view = evidence["teams"].get(side, {})
    rows = view.get("players", [])
    out = [f'<section class="panel"><h3>{_e(team)}</h3>']
    if rows:
        original = rows[0]
        out.extend([f"<details open><summary>已核名单（{len(rows)} 人）</summary><p>{_people(rows)}</p></details>",
                    "<p><small>" + _link(original.get("source_url"), "English Wikipedia contributors · 球队名单原件") +
                    " · " + _link(original.get("license"), "CC BY-SA") +
                    " · 来源修订 " + _e(_time(original.get("published_at"))) +
                    " · 本机收件 " + _e(_time(view["seen_at"])) + "</small></p>"])
    else:
        out.append("<p>缺少有效的本赛季球队名单。</p>")
    snapshot = evidence["snapshot"]
    edition = snapshot.get("cutoff")
    scope = snapshot.get("coverage", {}).get(side, {})
    feed = next((src for src in scope.get("sources", []) if src.get("id") == news_sources.WIRE), {})
    facts = []
    for item in snapshot.get("facts", []):
        if (not edition or item.get("source") != news_sources.WIRE or item.get("fixture_id") != fixture["fixture_id"]
                or not item.get("kickoff") or store.iso(item["kickoff"]) != store.iso(fixture["kickoff"])
                or football.team_key(str(item.get("team", ""))) != football.team_key(team)
                or item.get("event") not in {"injury", "suspension", "return", "availability"}
                or item.get("status") not in {"reported", "confirmed", "denied"}
                or not item.get("known_at") or store.iso(item["known_at"]) > edition
                or item.get("conflict") or not news_scope.suspension_applies(item, fixture["league"])
                or not news_model.usable_wire_observation(item, store.instant(edition))):
            continue
        av = {"out": "来源报缺阵", "doubtful": "出场存疑", "available": "可出场"}.get(
            item.get("availability"), "可用性待核")
        kind = {"injury": "伤病", "suspension": "停赛", "return": "归队", "availability": "出场状态"}[item["event"]]
        chance = item.get("chance_of_playing")
        pct = f" · 来源估计出场率 {_e(chance)}%" if type(chance) in (int, float) and 0 <= chance <= 100 else ""
        seen = item.get("last_seen_at") or item.get("received_at")
        facts.append(f'<li><strong>{_e(item.get("player_name") or item.get("name"))}</strong>　{kind} · {av}{pct}'
                     f' · {_link(item.get("url"), "来源")} · 本机收件 {_e(_time(seen))}</li>')
    out.append("<h4>Touchline 逐场球员状态</h4>")
    if facts:
        out.append("<ul>" + "".join(facts) + "</ul>")
    else:
        out.append("<p>该场最近的新闻快照尚无可展示的球员状态。</p>")
    if edition:
        out.append("<p><small>本场证据截止 " + _e(_time(edition)) + "。Tactical Data by " +
                   _link(news_sources.WIRE_HOST + "/docs#attribution", "Touchline Wire") + "。</small></p>")
    if not feed or feed.get("status") != "ok" or not scope.get("observed"):
        out.append("<p>来源覆盖有缺项，其他球员状态待核实。</p>")
    lineup = fixture.get("latest_seal", {}).get("lineups", {}).get(side, {})
    out.append("<p>预测封存时的首发　" + ("已确认" if lineup.get("confirmed") else "缺少确认名单") + "</p>")
    sheet = evidence.get("match_sheet")
    if sheet:
        players = sheet.get("teams", {}).get(side, [])
        label = _people(players) or _e(STATES.get(sheet["state"], "尚无本队逐场名单"))
        out.append("<p>逐场首发观测（研究）　" + label + "</p><small>" +
                   _link(sheet.get("source_url"), "Tactical Data by Touchline Wire") +
                   " · 本机收件 " + _e(_time(sheet["received_at"])) + "</small>")
    return "".join(out) + "</section>"


def _match(data_dir, root, code, fid):
    if code not in big5_scheduler.NEW_LEAGUES:
        from . import league_press

        match = next(iter(_season_matches(data_dir, code, fid)), None)
        if not match:
            raise ValueError("unknown or inactive season fixture")
        at = store.now_utc()
        current = league_press.fixture_news_as_of(root, match, at)
        evidence = _season_people(data_dir, match, at)
        try:
            evidence["match_sheet"] = wire_match_sheets.latest(data_dir, match, at)
        except (ValueError, sqlite3.Error):
            evidence["sheet_integrity_error"] = True
        link = (f'<p><a href="{evidence["report_link"]}">打开本场英超原始预测报告</a></p>'
                if evidence["report_link"] else "")
        sheet_notice = "<p>逐场首发资料校验失败，已暂缓展示。</p>" if evidence.get("sheet_integrity_error") else ""
        return (f'<h1>{_e(match["home"])} – {_e(match["away"])}</h1>' + _table([match], code) +
                _total_lines(match["latest_seal"].get("totals", {}), match) + link +
                "<h2>本场球队名单与伤停</h2>" + sheet_notice +
                "".join(_season_fields(match, evidence, side) for side in ("home", "away")) +
                "<h2>球队新闻</h2>" +
                _headlines(current, match))
    entry = big5_scheduler.status(root, leagues=[code], fixture_id=fid)["leagues"][code]
    jobs = entry.get("fixture_dispatch", [])
    match = next((m for m in entry.get("eligible_upcoming", []) if m["fixture_id"] == fid), None)
    if not match:
        raise ValueError("unknown or inactive fixture")
    seals = [j for j in jobs if j["sealed"] and j["kickoff"] == match["kickoff"]]
    current = max(seals, key=lambda j: j["sealed"]) if seals else None
    title = _e(match["home"]) + " – " + _e(match["away"])
    out = [f"<h1>{title}</h1><p>{NAMES[code]} · {_time(match['kickoff'])}</p>", _table([match], code)]
    if current:
        payload = current["payload"]
        if store.digest(payload) != current["digest"]:
            raise ValueError("invalid forecast digest")
        base, pair = payload["baseline"], payload["pair"]
        paired = "新闻候选调整了概率。" if pair.get("changed") else "本次新闻候选与基准概率一致。"
        out.append(f"<p>本场基准使用 {base['evidence']['training_matches']} 场已收赛果。"
                   f"输入截止于 {_time(payload['input_cutoff'])}，封存于 {_time(current['sealed'])}。{paired}</p>")
        out.append(_total_lines(base.get("totals", {}), match))
        scenario = payload.get("player_scenario", {})
        if scenario.get("changed"):
            h, d, a = _probabilities(scenario.get("probabilities", {}))
            out.append(f"<h2>伤停情景（未校准）</h2><p>按逐场伤停报告与名单身份匹配，"
                       f"主胜 {h}，平局 {d}，客胜 {a}。采用的报告涉及 {_people(scenario['applied'])}。</p>")
        if payload.get("changed_outputs"):
            labels = {"baseline": "攻防基准概率", "paired_news": "新闻对照概率", "player_scenario": "伤停情景"}
            out.append("<p>本次封存更新了" + "、".join(
                _e(labels.get(key, key)) for key in payload["changed_outputs"]) + "。</p>")
        out.append("<details><summary>赛程来源与预测指纹</summary><p>Polymarket Gamma · " +
                   _e(payload["schedule_evidence"].get("slug")) + " · " +
                   _time(payload["schedule_evidence"].get("receipt")) + "</p><code>" +
                   _e(current["digest"]) + "</code></details>")
    else:
        out.append("<p>本场尚无已封存预测。</p>")
    # Re-read the latest locally observed source records; the timing and probabilities above stay sealed.
    newest = big5_scheduler._news(root, match, store.instant(store.now_utc()))
    out.append("<h2>最新球队资料</h2>")
    out.extend(_team_fields(match, side, newest) for side in ("home", "away"))
    out.append("<h2>球队新闻</h2>" + _headlines(newest.get("press", {}), match))
    return "".join(out)


def render(data_dir, root, query):
    args = parse_qs(query, keep_blank_values=True, max_num_fields=3)
    if set(args) - {"league", "fixture"} or any(len(v) != 1 for v in args.values()):
        raise ValueError("invalid league dashboard query")
    code, fid = args.get("league", [None])[0], args.get("fixture", [None])[0]
    if code is not None and code not in NAMES or fid is not None and (
            code not in NAMES or len(fid) > 200 or not fid.startswith(code + "/")):
        raise ValueError("unknown league or fixture")
    nav = [f'<a class="nav-link" href="{_url()}">五大联赛</a>']
    nav.extend(f'<a class="nav-link" href="{_url(k)}"' +
               (' aria-current="page"' if code == k else '') + f'>{v}</a>' for k, v in NAMES.items())
    if fid:
        content = _match(data_dir, root, code, fid)
    else:
        selected = [code] if code else list(NAMES)
        domestic = [key for key in selected if key in big5_scheduler.NEW_LEAGUES]
        reports = big5_scheduler.status(root, leagues=domestic)["leagues"] if domestic else {}
        title = NAMES.get(code, "五大联赛")
        sections = [f"<h1>{title}赛前预测</h1><p>主胜、平局、客胜均按常规时间及伤停补时计算。"
                    "西甲、德甲、意甲和法甲展示技术研究预测。B1 使用球队攻防强度和低比分修正，"
                    "训练样本不足时使用同联赛历史基准 B0。</p>"]
        for league in selected:
            sections.append(f"<section class='panel'><h2>{NAMES[league]}</h2>")
            if league not in big5_scheduler.NEW_LEAGUES:
                sections.append(_table(_season_matches(data_dir, league), league))
                sections.append('<p><a href="/#matches">英超比赛详情和轮次报告</a></p>')
            else:
                report = reports[league]
                sections.append(_table(report.get("eligible_upcoming", []), league))
                state = STATES.get(report["state"], report["state"])
                finished = (report.get("last_cycle") or {}).get("finished")
                when = " · 最近完成 " + _e(_time(finished)) if finished else ""
                sections.append(f"<p><small>调度状态　{_e(state)}{when}</small></p>")
                if report.get("evidence_only_changes") is not None:
                    sections.append(f"<p>仅资料变化 {_e(report['evidence_only_changes'])} 次。</p>")
                scores = report.get("match_weighted_scores", {})
                if scores:
                    lines = "".join(
                        f"<tr><td>{_e(HORIZONS.get(key, key))}</td><td>{_e(item['fixtures'])}</td>"
                        f"<td>{item['mean_no_news']:.2f}</td><td>{item['mean_with_news']:.2f}</td></tr>"
                        for key, item in scores.items())
                    sections.append("<details><summary>已完赛的逐时点评分</summary>"
                                    "<p>每场比赛在同一时点只计一次。</p><div class='table-wrap'><table>"
                                    "<thead><tr><th>预测时点</th><th>比赛数</th><th>基准平均分</th>"
                                    "<th>新闻对照平均分</th></tr></thead><tbody>" + lines +
                                    "</tbody></table></div></details>")
            sections.append("</section>")
        content = "".join(sections)
    css = (dashboard.ASSETS / "dashboard.css").read_text(encoding="utf-8")
    policy = "default-src 'none'; style-src " + dashboard._asset_hash(css) + "; base-uri 'none'; form-action 'none'"
    return (f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<meta http-equiv="Content-Security-Policy" content="{_e(policy)}">'
            '<title>五大联赛赛前预测 · polymarket861</title><style>' + css +
            '</style></head><body><a class="skip" href="#main-content">跳至比赛</a><div class="layout">'
            '<aside class="sidebar"><a class="brand" href="/">polymarket<b>861</b></a>'
            '<div class="product">足球预测工作台</div><nav id="main-nav" aria-label="联赛">' +
            "".join(nav) + '</nav></aside><main id="main-content" tabindex="-1">' + content +
            '</main></div></body></html>')
