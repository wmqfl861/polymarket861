(() => {
  "use strict";

  const byId = (id) => document.getElementById(id);
  const reports = JSON.parse(byId("report-data").textContent);
  const pages = {
    matches: ["比赛详情", ["match", "ledger"], "赛前预测或模拟账本"],
    sources: ["数据源", ["source", "market-coverage"], "数据源或盘口覆盖报告"],
    rounds: ["轮次成绩", ["round", "backtest", "market-replay", "trial-market-study"], "轮次成绩或历史回测"],
    incidents: ["错误与修复", ["incident"], "错误与修复报告"],
    experiments: ["实验与版本", ["experiment", "strategy", "qualification"], "实验和版本报告"],
    jobs: ["并行任务", ["jobs"], "并行任务报告"],
  };
  const kindNames = {
    match: "赛前预测",
    ledger: "模拟账本",
    source: "来源档案",
    "market-coverage": "盘口覆盖",
    round: "轮报",
    backtest: "概率回测",
    "market-replay": "盘口回放",
    "trial-market-study": "盘口研究凭据",
    incident: "纠错记录",
    experiment: "实验记录",
    strategy: "选单策略",
    qualification: "模拟资格",
    jobs: "任务记录",
  };
  const names = {
    total: "全场",
    home: "主队",
    away: "客队",
    result: "胜平负",
    moneyline: "胜平负",
    home_total: "主队大小",
    away_total: "客队大小",
    total_over_2_5: "全场大 2.5",
    home_over_1_5: "主队大 1.5",
    away_over_1_5: "客队大 1.5",
    formal: "正式模拟",
    technical: "建设期影子",
    early: "提前技术快照",
    no_paper_order: "本次只记录预测",
    result_grades: "训练赛果的时间依据",
    archived_upper_bound: "带时间戳的归档上界",
    lag_assumption: "按延迟假设重建",
    observed_schedule: "已观测赛程",
    replay: "历史重放",
    provisional: "阶段成绩",
    missing_forecast: "缺少赛前预测",
    eligible_for_price_check: "可核验入场价格",
    skip: "本次跳过",
    shadow: "影子",
    historical: "历史回放",
    frozen: "已封存",
    pending: "待处理",
    skipped: "已跳过",
    filled: "已模拟成交",
    settled: "已结算",
    active: "启用",
    registered: "已登记",
    running: "运行中",
    planned: "已计划",
    completed: "已完成",
    open: "处理中",
    resolved: "已解决",
    closed: "已关闭",
    candidate: "候选",
    quarantined: "隔离",
    approved: "已验证",
    retired: "已停用",
    scored: "已评分",
    preliminary: "待补赛",
    incomplete: "覆盖不全",
    final: "已定稿",
    cancelled: "已取消",
    done: "已完成",
    ok: "正常",
    error: "错误",
    late: "延迟",
    invalid: "格式异常",
    freshness_unknown: "时效待核",
    home_win: "主胜",
    draw: "平局",
    away_win: "客胜",
    lineup: "阵容",
    injury: "伤病",
    availability: "可用性",
    suspension: "停赛",
    card: "红黄牌",
    form: "近期状态",
    news: "新闻",
    weather: "天气",
    prediction: "预测",
    training: "训练",
    manual: "人工记录",
    automated: "自动采集",
    succeeded: "已完成",
    failed: "失败",
    queued: "排队中",
    blocked: "受阻",
    missing: "缺失",
  };
  const fs = {
    input_count: "输入场次",
    training_matches: "训练场次",
    latest_available_at: "最近可用证据",
    b0_matches: "基线样本",
    latest_result_available_at: "最晚可用赛果",
    schedule_grade: "赛程时间证据",
    origin: "原始来源",
    input_sha256: "训练输入指纹",
    confidence: "可信度",
    competition: "赛事",
    remaining: "剩余场次",
    temperature: "气温",
    reported_at: "报道时间",
    run_at: "预报起报",
    valid_at: "预报所指时点",
    grade: "证据等级",
    evidence_grade: "证据等级",
    rationale: "依据",
    attack_multiplier: "进攻乘数",
    defense_multiplier: "防守乘数",
    field: "字段",
    value: "内容",
    locator: "证据位置",
    sample_count: "试采样本",
    eligible_prediction_count: "可用于预测",
    eligible_training_count: "可用于训练",
    confirmed_count: "已确认",
    name: "名称",
    free: "免费",
    status: "状态",
    state: "状态",
    summary: "摘要",
    note: "说明",
    unit: "单位",
  };
  const nf = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 3 });
  const pct = new Intl.NumberFormat("zh-CN", { style: "percent", maximumFractionDigits: 1 });
  const num = (value) => {
    if (typeof value !== "number" && (typeof value !== "string" || !value.trim())) return null;
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  };
  const format = (value) => {
    const n = num(value);
    return n === null ? "—" : nf.format(n);
  };
  const probability = (value) => {
    const n = num(value);
    return n !== null && n >= 0 && n <= 1 ? pct.format(n) : "—";
  };
  const translated = (value) =>
    typeof value === "string" && Object.hasOwn(names, value) ? names[value] : value;
  const safeText = (value) => {
    if (value === null || value === undefined || value === "") return "—";
    if (typeof value === "boolean") return value ? "是" : "否";
    return String(value);
  };
  const object = (value) =>
    value !== null && typeof value === "object" && !Array.isArray(value) ? value : {};
  const array = (value) => (Array.isArray(value) ? value : []);
  const first = (...values) => values.find((v) => v !== null && v !== undefined && v !== "");
  const compact = (value) => {
    if (Array.isArray(value)) return value.map(compact).join("、");
    if (value !== null && typeof value === "object")
      return Object.entries(value)
        .map(([k, v]) => (Object.hasOwn(fs, k) ? fs[k] : k) + " " + compact(v))
        .join("，");
    return safeText(translated(value));
  };
  function time(value) {
    if (typeof value !== "string" || !value) return "—";
    const date = new Date(value);
    return Number.isFinite(date.getTime()) && /(?:Z|[+-]\d\d:\d\d)$/i.test(value)
      ? date.toISOString().slice(0, 16).replace("T", " ") + " UTC"
      : value;
  }
  function create(tag, className, text) {
    const el = document.createElement(tag);
    if (className) el.className = className;
    if (text !== undefined && text !== null) el.textContent = safeText(text);
    return el;
  }
  function safeLink(value) {
    if (!value || typeof value !== "string") return null;
    try {
      const url = new URL(value);
      if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) return null;
      const a = create("a", "", value);
      a.href = url.href;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      return a;
    } catch {
      return null;
    }
  }
  function panel(title) {
    const out = create("section", "panel");
    if (title) out.append(create("h3", "", title));
    return out;
  }
  function facts(parent, pairs) {
    const dl = create("dl", "data-list");
    pairs.forEach(([name, value]) => {
      if (value === null || value === undefined || value === "") return;
      dl.append(create("dt", "", name));
      const dd = create("dd", "datum");
      if (value instanceof Node) dd.append(value);
      else dd.textContent = compact(value);
      dl.append(dd);
    });
    if (dl.childElementCount) parent.append(dl);
  }
  function metrics(parent, pairs) {
    const row = create("div", "metrics");
    pairs.forEach(([name, value]) => {
      const cell = create("div", "metric");
      cell.append(create("span", "metric-name", name), create("strong", "metric-value", safeText(value)));
      row.append(cell);
    });
    parent.append(row);
  }
  function table(parent, headers, rows, maxRows = 80) {
    if (!rows.length) return;
    const wrapper = create("div", "table-wrap"),
      t = create("table"),
      tr = create("tr");
    headers.forEach((name) => {
      const th = create("th", "", name);
      th.scope = "col";
      tr.append(th);
    });
    const head = create("thead");
    head.append(tr);
    t.append(head);
    const body = create("tbody");
    rows.slice(0, maxRows).forEach((row) => {
      const line = create("tr");
      row.forEach((value) => {
        const td = create("td");
        if (value instanceof Node) td.append(value);
        else td.textContent = safeText(value);
        line.append(td);
      });
      body.append(line);
    });
    t.append(body);
    wrapper.append(t);
    parent.append(wrapper);
    if (rows.length > maxRows)
      parent.append(
        create(
          "p",
          "note",
          "显示前 " + maxRows + " 项，共 " + rows.length + " 项。完整记录可在原始报告中查看。",
        ),
      );
  }
  function addNotes(parent, items) {
    const list = create("ul", "notes");
    array(items)
      .filter((x) => typeof x === "string" && x)
      .forEach((x) => list.append(create("li", "", x)));
    if (list.childElementCount) parent.append(list);
  }
  function getPage(row) {
    return Object.keys(pages).find((page) => pages[page][1].includes(row.kind)) || "experiments";
  }
  function title(row) {
    const p = object(row.payload);
    if (row.kind === "match" && p.home && p.away) return p.home + " vs " + p.away;
    if (row.kind === "round" && first(p.round_id, p.round) !== undefined) {
      const round = String(first(p.round_id, p.round));
      return (p.season ? p.season + " · " : "") + (/^\d+$/.test(round) ? "第 " + round + " 轮" : round);
    }
    if (row.kind === "backtest") {
      if (p.title) return p.title;
      const lag = object(p.protocol).archive_lag_hours;
      if (num(lag) !== null) return "赛果回测 · " + format(lag) + " 小时延迟假设";
      if (lag === null) return "赛果回测 · 严格截止凭据";
      return "历史赛果回测";
    }
    if (row.kind === "qualification") return "正式前向模拟的启用条件";
    if (row.kind === "strategy") return "选单策略 · " + (p.trial || row.key);
    if (row.kind === "source" && row.key === "openfootball" && p.registered_fixtures != null)
      return "OpenFootball 英超赛程与赛果";
    if (row.kind === "market-coverage") return p.title || "英超盘口覆盖";
    if (row.kind === "market-replay") return p.title || "历史盘口分档";
    if (row.kind === "trial-market-study") return "归档与模拟回放对账";
    if (row.kind === "jobs" && array(p.jobs).length) return "任务队列";
    if (row.kind === "experiment" && p.candidates) return "候选与正式版本";
    return first(p.title, p.name, p.account, p.root_cause, row.key) || row.kind;
  }
  function reportLink(row, label) {
    const link = create("a", "", label || title(row));
    link.href = "#" + getPage(row) + "/" + row.id;
    return link;
  }
  function hero(row, detail) {
    const p = object(row.payload);
    const box = panel();
    box.classList.add("hero");
    box.append(create("p", "eyebrow", kindNames[row.kind] || row.kind), create("h2", "", title(row)));
    const details =
      row.kind === "match"
        ? [
            p.season,
            first(
              translated(p.horizon),
              p.cutoff_minutes !== undefined ? "赛前 " + p.cutoff_minutes + " 分钟" : undefined,
            ),
            first(p.model_version, p.model),
            p.mode && translated(p.mode),
          ]
        : [row.key, p.status && translated(p.status)];
    const meta = create("p", "meta");
    details
      .filter((x) => ["number", "string"].includes(typeof x) && x !== "")
      .forEach((x) => meta.append(create("span", "", x)));
    if (meta.childElementCount) box.append(meta);
    detail.append(box);
    return box;
  }
  function relatedPanel(detail, rows, label = "相关报告") {
    if (!rows.length) return;
    const box = panel(label),
      links = create("div", "inline-links");
    rows.slice(0, 20).forEach((row) => links.append(reportLink(row)));
    box.append(links);
    detail.append(box);
  }
  function modelOdds(value) {
    const n = num(value);
    return n !== null && n > 0 && n <= 1 ? format(1 / n) : "—";
  }
  function matchesDecision(prediction, intent) {
    const p = object(prediction.payload);
    if (prediction.kind !== "match" || !p.cutoff || !intent.decision_at) return false;
    if (!p.fixture_id || p.fixture_id !== first(intent.match_id, intent.fixture_id)) return false;
    if (p.model_version && intent.model_version && p.model_version !== intent.model_version) return false;
    return Date.parse(p.cutoff) === Date.parse(intent.decision_at);
  }
  function markProbability(cell, label, value) {
    cell.append(create("span", "", label), create("strong", "", probability(value)));
    if (probability(value) !== "—") {
      const meter = create("meter");
      meter.min = 0;
      meter.max = 1;
      meter.value = Number(value);
      meter.setAttribute("aria-label", label);
      cell.append(meter, create("small", "fair-odds", "公平赔率 " + modelOdds(value)));
    }
  }
  function renderMatch(row, detail, heading) {
    const p = object(row.payload),
      probs = object(p.probabilities);
    const health = object(p.source_health);
    if (health.critical_stale === true) {
      const overdue = object(health.overdue_team_results);
      const counts = ["home", "away"]
        .filter((k) => Array.isArray(overdue[k]))
        .map((k) => names[k] + " " + overdue[k].length + " 场");
      const callout = create(
        "p",
        "health-warning",
        "关键赛果数据滞后" + (counts.length ? " · " + counts.join("、") + "到期赛果待核" : ""),
      );
      heading.append(callout);
    }
    const result = create("div", "result-grid");
    [
      ["主胜", probs.home_win],
      ["平局", probs.draw],
      ["客胜", probs.away_win],
    ].forEach(([name, val]) => {
      const cell = create("div", "result-cell");
      markProbability(cell, name, val);
      result.append(cell);
    });
    heading.append(result);
    const box = panel("进球大小");
    const lineRows = [];
    for (const family of ["total", "home", "away"]) {
      const values = object(object(p.totals)[family]);
      Object.keys(values)
        .sort((a, b) => Number(a) - Number(b))
        .forEach((line) => {
          const outcome = object(values[line]);
          lineRows.push([
            names[family],
            line,
            probability(outcome.over),
            probability(outcome.under),
            modelOdds(outcome.over),
            modelOdds(outcome.under),
          ]);
        });
    }
    if (lineRows.length)
      table(box, ["范围", "进球线", "大球概率", "小球概率", "大球公平赔率", "小球公平赔率"], lineRows);
    else
      table(
        box,
        ["范围", "进球线", "大球概率"],
        [
          ["全场", "2.5", probability(probs.total_over_2_5)],
          ["主队", "1.5", probability(probs.home_over_1_5)],
          ["客队", "1.5", probability(probs.away_over_1_5)],
        ],
      );
    box.append(
      create("p", "note", "足球公平赔率按正常完赛概率的倒数计算。合约实际费用与特殊兑付见对应账本。"),
    );
    detail.append(box);
    const fixtureId = first(p.fixture_id, p.match_id);
    const matchIntents = [];
    if (fixtureId) {
      reports
        .filter((r) => r.kind === "ledger")
        .forEach((r) =>
          array(object(r.payload).intents)
            .filter((intent) => matchesDecision(row, intent))
            .forEach((intent) => matchIntents.push([r, intent])),
        );
    }
    const execution = panel("模拟入场");
    const decision = object(p.decision);
    if (decision.state || decision.reason)
      facts(execution, [
        ["状态", translated(decision.state)],
        ["依据", decision.reason],
      ]);
    if (matchIntents.length)
      table(
        execution,
        ["合约方向", "状态", "保守公平价", "净份全成本", "成交赔率¹", "账本"],
        matchIntents.map(([r, i]) => [
          first(i.selection, i.token_id),
          translated(i.status),
          format(i.fair_low),
          format(i.unit_cost),
          num(i.unit_cost) > 0 ? format(1 / Number(i.unit_cost)) : "—",
          reportLink(r, r.key),
        ]),
      );
    if (matchIntents.length)
      execution.append(create("p", "note", "¹ 正常命中每净份兑付 1 时，成交赔率为每净份全成本的倒数。"));
    else execution.append(create("p", "note", "此预测时点尚无匹配的模拟成交赔率。"));
    detail.append(execution);
    const context = object(p.context),
      teams = object(context.teams),
      confirmed = object(context.confirmed);
    const dates = panel("名单与赛前证据");
    facts(dates, [
      ["开球时间", p.kickoff && time(p.kickoff)],
      ["决策截止", p.cutoff && time(p.cutoff)],
      ["实际取数截止", p.input_cutoff && time(p.input_cutoff)],
      ["时间见证", p.time_witness === "local_only" ? "仅本地封存" : p.time_witness],
      ["封存时间", p.sealed_at && time(p.sealed_at)],
      ["主队名单", lineupStatus(confirmed.home, object(teams.home).confirmed)],
      ["客队名单", lineupStatus(confirmed.away, object(teams.away).confirmed)],
      ["模型降级", p.fallback_reason],
      ["封存指纹", p.digest],
    ]);
    const contextFacts = array(context.facts);
    for (const side of ["home", "away"]) {
      const lineup = object(object(teams[side]).lineup);
      const players = array(lineup.starters)
        .map((player) =>
          typeof player === "string"
            ? player
            : first(object(player).name, object(player).player_id, object(player).id),
        )
        .filter(Boolean);
      if (players.length)
        facts(dates, [[names[side] + (lineup.confirmed === true ? "正式首发" : "预计阵容"), players]]);
    }
    if (contextFacts.length)
      table(
        dates,
        ["事实", "球员／球队", "内容", "出处", "收到时间"],
        contextFacts.map((f) => {
          const value = object(f.value);
          const summary =
            f.field === "lineup"
              ? lineupStatus(value.confirmed)
              : compact(Object.fromEntries(Object.entries(value).filter(([k]) => k !== "impact")));
          const link = safeLink(f.locator);
          if (link) link.textContent = f.source_id || "来源";
          return [
            translated(first(f.field, f.kind, f.evidence_id)),
            first(f.subject, f.team),
            summary,
            link || f.source_id,
            time(f.received_at),
          ];
        }),
      );
    const ids = array(first(context.evidence_ids, p.evidence_ids));
    if (ids.length) facts(dates, [["证据编号", ids]]);
    const modelEvidence = object(p.evidence);
    const grades = Object.entries(object(modelEvidence.result_grades));
    if (grades.length)
      facts(dates, [["训练赛果的时间依据", grades.map(([k, v]) => translated(k) + " " + v + " 场")]]);
    if (health.critical_stale === true) {
      const overdue = object(health.overdue_team_results);
      facts(dates, [
        ["主队待核赛果", array(overdue.home)],
        ["客队待核赛果", array(overdue.away)],
      ]);
    }
    facts(
      dates,
      Object.entries(modelEvidence)
        .filter(([k]) => Object.hasOwn(fs, k))
        .map(([k, v]) => [fs[k], v]),
    );
    detail.append(dates);
    const sameFixture = fixtureId
      ? reports.filter(
          (r) =>
            r.kind === "match" &&
            r.id !== row.id &&
            object(r.payload).model_version === p.model_version &&
            object(r.payload).mode === p.mode &&
            object(r.payload).kickoff === p.kickoff &&
            first(object(r.payload).fixture_id, object(r.payload).match_id) === fixtureId,
        )
      : [];
    const older = sameFixture
      .filter((r) => Date.parse(object(r.payload).cutoff) < Date.parse(p.cutoff))
      .sort((a, b) => Date.parse(b.payload.cutoff) - Date.parse(a.payload.cutoff))[0];
    if (older) {
      const movement = panel("相对上次预测的变化"),
        prev = object(older.payload.probabilities);
      const shifts = [
        ["主胜", "home_win"],
        ["平局", "draw"],
        ["客胜", "away_win"],
        ["全场大 2.5", "total_over_2_5"],
        ["主队大 1.5", "home_over_1_5"],
        ["客队大 1.5", "away_over_1_5"],
      ];
      table(
        movement,
        ["方向", "上次", "本次", "变化（百分点）"],
        shifts.map(([label, key]) => {
          const a = num(probs[key]),
            b = num(prev[key]);
          return [
            label,
            probability(b),
            probability(a),
            a === null || b === null ? "—" : format((a - b) * 100),
          ];
        }),
      );
      movement.append(reportLink(older, "查看上次封存的预测"));
      detail.append(movement);
    }
    const srcIds = new Set(contextFacts.map((f) => f.source_id).filter(Boolean)),
      used = new Set(ids);
    const related = reports.filter((r) => {
      const x = object(r.payload);
      if (r.kind === "source") return srcIds.has(x.source_id) || srcIds.has(r.key);
      if (r.kind === "incident") return array(x.affected_evidence_ids).some((i) => used.has(i));
      if (r.kind === "round")
        return (
          p.season &&
          p.season === x.season &&
          first(p.round_id, p.round) != null &&
          String(first(p.round_id, p.round)) === String(first(x.round_id, x.round))
        );
      return false;
    });
    relatedPanel(detail, related);
  }
  function lineupStatus(...values) {
    const status = values.find((v) => typeof v === "boolean");
    return status === true ? "已收到正式名单" : status === false ? "未确认" : "未记录";
  }
  function renderRound(row, detail) {
    const p = object(row.payload),
      box = panel("本轮结果"),
      item = object(first(p.item_scores, p.per_item));
    metrics(box, [
      ["已评分场次", format(first(p.scored, p.count))],
      ["应有场次", format(first(p.expected, p.scheduled))],
      ["漏发场次", format(p.missing)],
      ["本轮均分", format(first(p.mean_score, p.score))],
    ]);
    facts(box, [
      ["待补场次", p.pending],
      ["已完赛", p.completed],
      [
        "全轮分数范围",
        array(first(p.full_round_range, p.score_range)).length
          ? first(p.full_round_range, p.score_range).map(format).join(" 至 ")
          : null,
      ],
      ["已登记赛程", p.scheduled],
      ["待登记赛程", p.schedule_missing],
      ["评分覆盖", p.coverage !== undefined ? probability(p.coverage) : null],
      ["评估状态", translated(p.status)],
    ]);
    const scored = Object.entries(item);
    if (scored.length)
      table(
        box,
        ["玩法", "分数"],
        scored.map(([k, v]) => [translated(k), format(v)]),
      );
    const games = array(p.fixtures);
    if (games.length)
      table(
        box,
        ["比赛", "状态", "比分", "模型分"],
        games.map((g) => {
          const label = g.home && g.away ? g.home + " vs " + g.away : first(g.fixture_id, g.match_id);
          const match = reports.find((r) => {
            const forecast = object(r.payload);
            if (
              r.kind !== "match" ||
              !forecast.fixture_id ||
              forecast.fixture_id !== first(g.fixture_id, g.match_id)
            )
              return false;
            if (g.digest) return forecast.digest === g.digest;
            if (
              (p.model_version && forecast.model_version !== p.model_version) ||
              (p.horizon && forecast.horizon !== p.horizon)
            )
              return false;
            if (g.kickoff && num(p.cutoff_minutes) > 0)
              return Date.parse(forecast.cutoff) === Date.parse(g.kickoff) - num(p.cutoff_minutes) * 60000;
            return false;
          });
          return [
            match ? reportLink(match, label) : label,
            translated(g.status),
            Array.isArray(g.goals) ? g.goals.join("∶") : Array.isArray(g.score) ? g.score.join("∶") : "—",
            format(first(g.model_score, g.mean_score, typeof g.score === "number" ? g.score : null)),
          ];
        }),
      );
    detail.append(box);
    const ids = new Set(games.map((g) => first(g.fixture_id, g.match_id)).filter(Boolean));
    const accounts = [];
    for (const report of reports.filter((r) => r.kind === "ledger")) {
      const account = object(report.payload);
      const intents = array(account.intents).filter((i) => ids.has(i.match_id));
      if (!intents.length) continue;
      const filled = intents.filter((i) => i.status === "filled" || i.status === "settled");
      const settled = filled.filter((i) => i.status === "settled");
      const amounts = settled.map((i) => num(i.pnl));
      const realized =
        amounts.length && amounts.every((v) => v !== null) ? format(amounts.reduce((a, b) => a + b, 0)) : "—";
      accounts.push([
        reportLink(report, account.account || report.key),
        translated(account.mode),
        intents.length,
        filled.length,
        settled.length,
        realized,
      ]);
    }
    if (accounts.length) {
      const money = panel("本轮模拟结算");
      table(money, ["账户", "模式", "意图", "已成交", "已结算", "已结算损益"], accounts);
      detail.append(money);
    }
  }
  function renderBacktest(row, detail) {
    const p = object(row.payload),
      horizons = object(p.horizons),
      keys = Object.keys(horizons);
    const box = panel("历史概率表现");
    const delay = object(p.protocol).archive_lag_hours;
    if (num(delay) !== null)
      box.append(
        create(
          "p",
          "callout",
          "缺少当年发布时间的赛果按开球后 " + format(delay) + " 小时可用重建。本页给出该假设下的模型表现。",
        ),
      );
    else if (delay === null)
      box.append(create("p", "callout", "本次仅计入有证据证明在决策截止前已可用的历史赛果。"));
    if (!keys.length) {
      box.append(create("p", "note", "尚无按决策时点汇总的成绩。"));
      detail.append(box);
      return;
    }
    const label = create("label", "selector", "决策时点"),
      select = create("select");
    keys.forEach((key) => {
      const o = create("option", "", key);
      o.value = key;
      select.append(o);
    });
    if (keys.includes("T-60")) select.value = "T-60";
    label.append(select);
    box.append(label);
    const summary = create("div");
    box.append(summary);
    function update() {
      summary.replaceChildren();
      const h = object(horizons[select.value]),
        s = object(h.summary);
      metrics(summary, [
        ["有效场次", format(s.n)],
        ["球队模型", format(s.model_score)],
        ["联赛基线", format(s.b0_score)],
        ["配对分差", format(s.paired_delta)],
      ]);
      const ci = array(s.paired_round_ci95);
      facts(summary, [
        ["95% 按轮配对区间", ci.length === 2 ? ci.map(format).join(" 至 ") : null],
        ["覆盖率", s.coverage === undefined ? null : probability(s.coverage)],
      ]);
      let seasons = Object.entries(object(h.by_season));
      if (Array.isArray(h.by_season)) seasons = h.by_season.map((x) => [x.season, x]);
      table(
        summary,
        ["赛季", "场次", "基线分", "模型分", "配对差"],
        seasons.map(([year, v]) => [
          year,
          format(v.n),
          format(v.b0_score),
          format(v.model_score),
          format(v.paired_delta),
        ]),
      );
      const losses = object(s.model_losses),
        base = object(s.baseline_losses);
      table(
        summary,
        ["玩法", "模型损失", "基线损失"],
        Object.entries(losses).map(([k, v]) => [translated(k), format(v), format(base[k])]),
      );
      const misses = Object.entries(object(h.omissions));
      if (misses.length)
        table(
          summary,
          ["无法评分的原因", "场次"],
          misses.map(([reason, n]) => [
            reason === "no eligible historical results at cutoff" ? "截止前缺少可证实的历史赛果" : reason,
            format(n),
          ]),
        );
      if (num(s.n) > 0) calibration(summary, s.calibration);
    }
    select.addEventListener("change", update);
    update();
    addNotes(box, p.limitations);
    detail.append(box);
  }
  function calibration(parent, value) {
    const cal = object(value),
      model = object(cal.model),
      selections = Object.keys(model);
    if (!selections.length) return;
    const wrapper = create("details", "calibration");
    wrapper.append(create("summary", "", "概率校准分箱"));
    const label = create("label", "selector", "预测方向"),
      select = create("select");
    selections.forEach((k) => {
      const option = create("option", "", translated(k));
      option.value = k;
      select.append(option);
    });
    label.append(select);
    wrapper.append(label);
    const grid = create("div");
    wrapper.append(grid);
    function update() {
      grid.replaceChildren();
      const edges = array(cal.bins);
      table(
        grid,
        ["概率区间", "样本数", "平均预测概率", "实际发生率"],
        array(model[select.value]).map((bin, i) => [
          edges.length > i + 1 ? probability(edges[i]) + " – " + probability(edges[i + 1]) : "—",
          format(bin.n),
          probability(bin.mean_p),
          probability(bin.observed),
        ]),
      );
    }
    select.addEventListener("change", update);
    update();
    parent.append(wrapper);
  }
  function renderCoverage(row, detail) {
    const p = object(row.payload),
      box = panel(row.kind === "market-replay" ? "历史可执行证据" : "行情采集覆盖");
    if (p.sample_scope) box.append(create("p", "note", p.sample_scope));
    const groups = array(first(p.groups, p.coverage));
    if (row.kind === "market-coverage") {
      facts(box, [["赛事样本", p.fixtures]]);
      table(
        box,
        ["赛季", "玩法", "应有场", "挂牌场", "合约", "A 代币", "B 代币", "C 代币", "缺证代币"],
        groups.map((g) => {
          const e = object(g.evidence);
          return [
            g.season,
            translated(g.family),
            g.expected_fixtures,
            g.listed_fixtures,
            g.contracts,
            e.A,
            e.B,
            e.C,
            e.missing,
          ];
        }),
      );
    } else {
      const counts = object(p.counts);
      metrics(box, [
        ["可执行 A", format(counts.A)],
        ["逐笔成交 B", format(counts.B)],
        ["价格曲线 C", format(counts.C)],
        ["缺失", format(counts.missing)],
      ]);
      facts(box, [["可执行样本收益率", probabilitySigned(p.roi)]]);
      const opp = array(p.opportunities);
      table(
        box,
        ["比赛", "玩法", "方向／代币", "档位", "状态"],
        opp.map((o) => [
          o.match_id,
          translated(o.family),
          first(o.selection, o.token_id),
          o.tier,
          translated(o.status),
        ]),
      );
    }
    box.append(
      create(
        "p",
        "note",
        "A 档有可核实的双时点盘口和费用。B 档有成交记录，C 档有价格曲线。后两档用于价格研究。",
      ),
    );
    addNotes(box, first(p.limits, p.limitations));
    if (row.kind === "trial-market-study") {
      facts(box, [
        ["市场站点", p.venue],
        ["赛事样本", p.fixture_count],
        ["赛程冲突数", Array.isArray(p.schedule_conflicts) ? p.schedule_conflicts.length : null],
        ["清单指纹", p.manifest_sha256],
      ]);
      table(
        box,
        ["赛季", "玩法", "应有场", "挂牌场", "A", "B", "C"],
        groups.map((g) => [
          g.season,
          translated(g.family),
          g.expected_fixtures,
          g.listed_fixtures,
          object(g.evidence).A,
          object(g.evidence).B,
          object(g.evidence).C,
        ]),
      );
    }
    detail.append(box);
    const calls = array(p.recent_requests);
    if (calls.length) {
      const io = panel("最近采集");
      table(
        io,
        ["操作", "状态", "接收时间"],
        calls.map((c) => [c.operation, c.status, time(c.received_at)]),
      );
      detail.append(io);
    }
  }
  function probabilitySigned(value) {
    const n = num(value);
    return n === null ? "—" : pct.format(n);
  }
  function renderLedger(row, detail) {
    const p = object(row.payload),
      box = panel("模拟资金");
    metrics(box, [
      ["现金余额", format(p.cash)],
      ["可用现金", format(p.available_cash)],
      ["已实现损益", format(p.realized_pnl)],
      ["已结算收益率", probabilitySigned(p.realized_roi)],
    ]);
    facts(box, [
      ["账户", p.account],
      ["模式", translated(p.mode)],
      ["初始现金", p.initial_cash],
      ["已保留现金", p.reserved_cash],
      ["累计支出", p.total_spent],
      ["成交笔数", p.fills],
      ["未结算仓位", p.open_positions],
      ["已结算仓位", p.settled_positions],
      ["累计费用", p.total_fees_cash_equivalent],
      ["成本估值下的最大回撤", probabilitySigned(p.max_drawdown_at_cost)],
      ["有 CLV 的成交数", p.clv_covered_fills],
    ]);
    detail.append(box);
    const orders = panel("订单与结算"),
      intents = array(p.intents);
    if (!intents.length) orders.append(create("p", "note", "该账户尚无订单意图。"));
    table(
      orders,
      ["比赛", "买入方向", "状态", "份数", "实付", "兑付／份", "损益", "每份 CLV", "原因"],
      intents.map((i) => {
        const r = reports.find((r) => matchesDecision(r, i));
        return [
          r ? reportLink(r) : i.match_id,
          first(i.selection, i.token_id),
          translated(i.status),
          format(i.net_shares),
          format(i.spent),
          format(i.payout),
          format(i.pnl),
          format(i.clv),
          i.reason,
        ];
      }),
    );
    detail.append(orders);
  }
  function renderSource(row, detail) {
    const p = object(row.payload),
      box = panel("来源与许可");
    facts(box, [
      ["来源编号", first(p.source_id, p.source, row.key)],
      ["固定版本", p.revision],
      ["时间证据", p.evidence_grade],
      ["登记比赛数", p.registered_fixtures],
      ["赛季", p.seasons],
      ["数据冲突", p.data_conflicts],
      ["归档接收时间", p.observed_at && time(p.observed_at)],
      ["是否免费", p.free],
      ["状态", translated(p.status)],
      ["上游", p.upstream],
      ["用途说明", safeLink(p.terms_locator) || p.terms_locator],
      ["最近更新", p.updated_at && time(p.updated_at)],
    ]);
    const fields = object(p.fields);
    table(
      box,
      ["字段", "当前状态", "允许用途", "方法", "单位"],
      Object.entries(fields).map(([k, v]) => [translated(k), translated(object(p.field_status)[k] || p.status), compact(v.uses), compact(v.methods), v.unit]),
    );
    const health = Object.entries(object(p.health));
    if (health.length)
      table(
        box,
        ["字段", "时效", "应更新", "实际数据时间", "检查时间"],
        health.map(([field, h]) => [
          translated(field),
          h.status === "frozen" ? "内容冻结" : translated(h.status),
          time(h.expected_data_at),
          time(h.latest_data_at),
          time(h.checked_at),
        ]),
      );
    detail.append(box);
    const trial = object(p.trial);
    if (Object.keys(trial).length) {
      const t = panel("字段试采");
      metrics(t, [
        ["样本", format(trial.sample_count)],
        ["可用于预测", format(trial.eligible_prediction_count)],
        ["可用于训练", format(trial.eligible_training_count)],
        ["已确认", format(trial.confirmed_count)],
      ]);
      table(t, ["字段", "样本数"], Object.entries(object(trial.field_counts)));
      detail.append(t);
    }
  }
  function renderIncident(row, detail) {
    const p = object(row.payload),
      box = panel("问题与修复");
    facts(box, [
      ["状态", translated(p.status)],
      ["问题表现", p.symptom],
      ["根因", p.root_cause],
      ["修复方法", p.remedy],
      ["修复版本", p.fix_version],
      ["回归用例", p.regression_case],
      ["发现时间", p.opened_at && time(p.opened_at)],
      ["关闭时间", p.closed_at && time(p.closed_at)],
      ["证据", p.affected_evidence_ids],
    ]);
    table(
      box,
      ["受影响对象", "记录编号", "处理"],
      array(p.impacted).map((i) => [i.kind, i.key, i.action]),
    );
    detail.append(box);
    const evidence = new Set(array(p.affected_evidence_ids)),
      impacted = new Set(array(p.impacted).map((i) => i.key));
    relatedPanel(
      detail,
      reports.filter(
        (r) =>
          r.id !== row.id &&
          (impacted.has(r.key) ||
            (r.kind === "match" &&
              array(object(object(r.payload).context).evidence_ids).some((i) => evidence.has(i)))),
      ),
    );
  }
  function renderQualification(row, detail) {
    const p = object(row.payload),
      box = panel("正式账户资格");
    const criteria = {
      multi_season_probability_report: "多赛季概率回测报告",
      verified_historical_cutoff_report: "历史截止时点可验证",
      strict_historical_coverage_measured: "已测量严格历史截止覆盖",
      two_consecutive_observed_technical_rounds: "连续两轮十场均按两个时点完成技术预测",
      archive_linked_market_replay: "盘口回放与真实归档可对账",
      paper_ledger_idempotency: "模拟账本幂等验收",
    };
    facts(box, [
      ["状态", p.ready === true ? "满足报告中的启用条件" : "等待验证"],
      ["协议", p.protocol],
      ["历史可执行收益率", probabilitySigned(p.historical_executable_roi)],
    ]);
    table(
      box,
      ["验收项", "本次结果"],
      Object.entries(object(p.checks)).map(([k, v]) => [
        Object.hasOwn(criteria, k) ? criteria[k] : k,
        v === true ? "已通过" : "待验证",
      ]),
    );
    detail.append(box);
    relatedPanel(
      detail,
      reports.filter((r) => ["backtest", "market-replay"].includes(r.kind)),
    );
  }
  function renderStrategy(row, detail) {
    const p = object(row.payload), box = panel("同场、同预算的策略比较");
    facts(box, [
      ["每场模拟预算", p.budget], ["已登记候选数", p.preregistered_candidates],
      ["晋级证据", p.qualified === true ? "通过" : "等待样本"],
    ]);
    table(box, ["样本", "配对场数", "有成交场数", "收盘覆盖", "CLV 差的区间下界"], [
      ["历史 A 档", p.history], ["独立前向", p.forward],
    ].map(([name, result]) => {
      const phase = object(result), counts = object(phase.counts);
      return [name, counts.paired_fixtures, counts.traded_fixtures, probability(phase.close_coverage),
        object(phase.interval).lower];
    }));
    detail.append(box);
  }
  function renderExperiment(row, detail) {
    const p = object(row.payload),
      box = panel("候选和当前版本"),
      candidates = array(p.candidates),
      active = array(p.active);
    table(
      box,
      ["候选", "类型", "版本", "状态", "窗口", "对照"],
      candidates.map((c) => [c.key, c.kind, c.version, translated(c.state), c.window, c.baseline]),
    );
    table(
      box,
      ["启用版本", "类型", "状态"],
      active.map((c) => [first(c.version, c.key), c.kind, translated(c.state)]),
    );
    if (!candidates.length && !active.length) {
      facts(box, [
        ["实验", p.key],
        ["版本", p.version],
        ["状态", translated(first(p.status, p.state))],
        ["窗口", p.window],
        ["基准", p.baseline],
      ]);
      if (!box.querySelector("dl")) box.append(create("p", "note", "当前报告没有登记候选或正式版本。"));
    }
    const events = array(p.events);
    table(
      box,
      ["事件", "版本／对象", "时间"],
      events.map((e) => [
        first(e.event, e.action, e.kind),
        first(e.version, e.key),
        time(first(e.at, e.created_at, e.timestamp)),
      ]),
    );
    detail.append(box);
  }
  function renderJobs(row, detail) {
    const p = object(row.payload),
      box = panel("任务队列"),
      jobs = array(p.jobs);
    const counts = new Map();
    jobs.forEach((j) => counts.set(j.state, (counts.get(j.state) || 0) + 1));
    if (counts.size)
      metrics(
        box,
        Array.from(counts, ([state, n]) => [translated(state), n]),
      );
    table(
      box,
      ["任务", "目标", "类型", "状态", "尝试次数", "等待前件", "最近错误"],
      jobs.map((j) => [
        j.key,
        j.task,
        j.kind,
        translated(j.state),
        format(j.attempts),
        compact(j.blocked_by),
        j.error,
      ]),
    );
    if (!jobs.length) box.append(create("p", "note", "当前报告没有任务。"));
    detail.append(box);
    const runs = array(p.attempts);
    if (runs.length) {
      const attempts = panel("执行记录");
      table(
        attempts,
        ["任务", "执行者", "次数", "开始", "结束", "状态", "产物目录"],
        runs.map((a) => [
          a.job_key,
          a.worker,
          a.attempt,
          time(a.started),
          time(a.ended),
          translated(a.state),
          a.output_dir,
        ]),
      );
      detail.append(attempts);
    }
  }
  const renderers = {
    match: renderMatch,
    ledger: renderLedger,
    source: renderSource,
    round: renderRound,
    backtest: renderBacktest,
    incident: renderIncident,
    experiment: renderExperiment,
    strategy: renderStrategy,
    qualification: renderQualification,
    jobs: renderJobs,
    "market-coverage": renderCoverage,
    "market-replay": renderCoverage,
    "trial-market-study": renderCoverage,
  };
  function showDetail(row) {
    const detail = byId("detail");
    detail.replaceChildren();
    const heading = hero(row, detail);
    if (Object.hasOwn(renderers, row.kind)) renderers[row.kind](row, detail, heading);
    const raw = create("details", "panel raw"),
      summary = create("summary", "", "查看原始报告");
    raw.append(summary);
    raw.addEventListener("toggle", () => {
      if (raw.open && !raw.querySelector("pre")) {
        const pre = create("pre");
        pre.textContent = JSON.stringify(row.payload, null, 2);
        raw.append(pre);
      }
    });
    detail.append(raw);
  }

  const snapshotAt = Date.parse(reports[0]?.updated_at);
  function forecastOrder(a, b) {
    const x = object(a.payload),
      y = object(b.payload);
    function priority(p) {
      if (p.mode === "replay" || p.mode === "historical") return 2;
      return Date.parse(p.kickoff) >= snapshotAt ? 0 : 1;
    }
    const diff = priority(x) - priority(y);
    if (diff) return diff;
    const kx = Date.parse(x.kickoff),
      ky = Date.parse(y.kickoff);
    if (Number.isFinite(kx) && Number.isFinite(ky) && kx !== ky) return priority(x) === 0 ? kx - ky : ky - kx;
    const tx = Date.parse(x.cutoff),
      ty = Date.parse(y.cutoff);
    return Number.isFinite(tx) && Number.isFinite(ty) ? ty - tx : 0;
  }

  const grouped = {};
  Object.keys(pages).forEach((name) => {
    const rank = pages[name][1];
    grouped[name] = reports
      .filter((r) => getPage(r) === name)
      .sort((a, b) => {
        if (a.kind === "match" && b.kind === "match") return forecastOrder(a, b);
        const ai = rank.indexOf(a.kind),
          bi = rank.indexOf(b.kind);
        return (ai < 0 ? rank.length : ai) - (bi < 0 ? rank.length : bi);
      });
  });
  reports.forEach((r) => {
    const p = object(r.payload);
    r.searchable = [
      r.key,
      title(r),
      kindNames[r.kind],
      p.season,
      p.horizon,
      p.cutoff,
      p.mode,
      translated(p.mode),
      translated(p.horizon),
      p.fixture_id,
      p.model_version,
      p.source_id,
      p.root_cause,
    ]
      .filter(Boolean)
      .join(" ")
      .toLocaleLowerCase();
  });
  document.querySelectorAll(".nav-link").forEach((a) => {
    a.querySelector(".nav-count").textContent = String(grouped[a.dataset.page].length);
  });
  byId("updated").textContent = reports[0]?.updated_at
    ? "报告更新 " + time(reports[0].updated_at)
    : "尚无更新";
  const search = byId("search"),
    kind = byId("kind");
  let activePage = null,
    activeId = null,
    limit = 60;
  function readHash() {
    const parts = location.hash.slice(1).split("/");
    const fallback = Object.keys(pages).find((p) => grouped[p].length) || "matches";
    return [Object.hasOwn(pages, parts[0]) ? parts[0] : fallback, parts[1] || null];
  }
  function emptyMessage(heading, text) {
    const box = create("section", "empty");
    box.append(create("h2", "", heading), create("p", "", text));
    byId("detail").replaceChildren(box);
  }
  function drawList(target, filterChange = false) {
    const rows = grouped[activePage],
      term = search.value.trim().toLocaleLowerCase();
    const visible = rows.filter(
      (r) => (!kind.value || kind.value === r.kind) && (!term || r.searchable.includes(term)),
    );
    const selected = visible.find((r) => r.id === target) || visible[0];
    byId("list-status").textContent = "报告列表 · " + visible.length + " / " + rows.length;
    const container = byId("records");
    container.replaceChildren();
    let shown = visible.slice(0, limit);
    if (selected && !shown.includes(selected)) shown = [selected, ...shown];
    shown.forEach((r) => {
      const a = reportLink(r);
      a.className = "record";
      a.replaceChildren();
      a.append(
        create(
          "span",
          "record-type",
          [
            r.kind === "match" && object(r.payload).mode
              ? translated(r.payload.mode)
              : kindNames[r.kind] || r.kind,
            translated(object(r.payload).horizon),
          ]
            .filter(Boolean)
            .join(" · "),
        ),
        create("span", "record-title", title(r)),
      );
      const recordDate = r.kind === "match" ? first(object(r.payload).cutoff, r.updated_at) : r.updated_at;
      if (recordDate) a.append(create("span", "record-date", time(recordDate)));
      if (r === selected) a.setAttribute("aria-current", "true");
      container.append(a);
    });
    if (visible.length > limit) {
      const next = create("button", "more", "显示更多报告");
      next.type = "button";
      next.addEventListener("click", () => {
        limit += 60;
        drawList(activeId);
      });
      container.append(next);
    }
    if (!rows.length) {
      activeId = null;
      emptyMessage("尚无" + pages[activePage][2], "本地有相应记录后会显示在这里。");
    } else if (!selected) {
      activeId = null;
      emptyMessage("没有匹配的报告", "请修改搜索内容或报告类别。");
    } else if (selected.id !== activeId || !byId("detail").childElementCount) {
      activeId = selected.id;
      showDetail(selected);
    }
    if (filterChange) {
      const hash = "#" + activePage + (selected ? "/" + selected.id : "");
      if (location.hash !== hash) history.replaceState(null, "", hash);
    }
  }
  function sync() {
    if (location.hash === "#main-content") {
      byId("main-content").focus();
      return;
    }
    const [page, target] = readHash(),
      changed = page !== activePage;
    if (changed) {
      activePage = page;
      activeId = null;
      limit = 60;
      search.value = "";
      byId("page-name").textContent = pages[page][0];
      document.title = pages[page][0] + " · 英超预测工作台";
      const rows = grouped[page],
        types = [...new Set(rows.map((r) => r.kind))];
      byId("page-count").textContent = rows.length + " 份报告";
      byId("filters").hidden = !rows.length;
      byId("records-pane").hidden = !rows.length;
      byId("workspace").classList.toggle("empty-page", !rows.length);
      document.querySelectorAll(".nav-link").forEach((a) => {
        if (a.dataset.page === page) a.setAttribute("aria-current", "page");
        else a.removeAttribute("aria-current");
      });
      kind.replaceChildren();
      const all = create("option", "", "全部类别");
      all.value = "";
      kind.append(all);
      types.forEach((type) => {
        const option = create("option", "", kindNames[type] || type);
        option.value = type;
        kind.append(option);
      });
      kind.hidden = types.length < 2;
    } else if (target && target !== activeId) {
      // A shared or cross-report link must remain reachable after filtering.
      const requested = grouped[page].find((r) => r.id === target);
      if (
        requested &&
        ((kind.value && kind.value !== requested.kind) ||
          !requested.searchable.includes(search.value.trim().toLocaleLowerCase()))
      ) {
        search.value = "";
        kind.value = "";
      }
    }
    drawList(target);
  }
  search.addEventListener("input", () => {
    limit = 60;
    drawList(activeId, true);
  });
  kind.addEventListener("change", () => {
    limit = 60;
    drawList(activeId, true);
  });
  window.addEventListener("hashchange", sync);
  sync();
})();
