"""Read-only local dashboard and portable single-file HTML export."""

from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import sys
import tempfile
from collections.abc import Iterable, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ASSETS = Path(__file__).with_name("assets")
PAGES = (
    ("matches", "比赛详情"),
    ("sources", "数据源"),
    ("rounds", "轮次成绩"),
    ("incidents", "错误与修复"),
    ("experiments", "实验与版本"),
    ("jobs", "并行任务"),
)


def _asset_hash(text: str) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return "'sha256-" + base64.b64encode(digest).decode("ascii") + "'"


def render_dashboard(reports: Iterable[Mapping]) -> str:
    """Build a self-contained dashboard. Report values remain inert JSON."""
    records = []
    for report in reports:
        kind, key = str(report["kind"]), str(report["key"])
        report_id = hashlib.sha256(f"{kind}\0{key}".encode("utf-8")).hexdigest()
        records.append({
            "id": report_id,
            "kind": kind,
            "key": key,
            "updated_at": str(report.get("updated_at") or ""),
            "payload": report.get("payload"),
        })
    records.sort(key=lambda row: (row["updated_at"], row["kind"], row["key"]), reverse=True)
    data = json.dumps(records, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    data = data.translate(str.maketrans({
        "<": r"\u003c", ">": r"\u003e", "&": r"\u0026",
        "\u2028": r"\u2028", "\u2029": r"\u2029",
    }))
    css = (ASSETS / "dashboard.css").read_text(encoding="utf-8")
    js = (ASSETS / "dashboard.js").read_text(encoding="utf-8")
    policy = (
        f"default-src 'none'; script-src {_asset_hash(js)}; style-src {_asset_hash(css)}; "
        "base-uri 'none'; object-src 'none'; form-action 'none'"
    )
    nav = "".join(
        f'<a class="nav-link" data-page="{page}" href="#{page}"><span>{label}</span>'
        '<span class="nav-count"></span></a>' for page, label in PAGES
    )
    plain_index = "".join(
        f'<li>{html.escape(row["kind"])} · {html.escape(row["key"])}</li>' for row in records
    ) or "<li>尚无本地报告。</li>"
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="{html.escape(policy, quote=True)}">
<meta name="color-scheme" content="light">
<title>英超预测工作台 · polymarket861</title>
<style>{css}</style>
</head>
<body>
<a class="skip" href="#main-content">跳至报告内容</a>
<div class="layout">
<aside class="sidebar">
  <a class="brand" href="#matches" aria-label="polymarket861 比赛详情">
    <span class="mark" aria-hidden="true"><i></i><i></i><i></i></span>
    <span>polymarket<b>861</b></span>
  </a>
  <div class="product">英超预测工作台</div>
  <nav id="main-nav" aria-label="工作区">{nav}</nav>
  <div class="side-foot"><i aria-hidden="true"></i>模拟买入 · 持有至结算</div>
</aside>
<main id="main-content" tabindex="-1">
  <header class="topline"><span>Polymarket 国际站</span><span id="updated"></span></header>
  <div class="page-title"><h1 id="page-name">英超预测工作台</h1><div id="page-count"></div></div>
  <div class="filters" id="filters" hidden>
    <label for="search" class="visually-hidden">搜索当前页面的报告</label>
    <input type="search" id="search" placeholder="搜索球队、名称或记录编号" autocomplete="off">
    <label for="kind" class="visually-hidden">报告类别</label><select id="kind"></select>
  </div>
  <div id="workspace" class="workspace">
    <section class="records" id="records-pane" aria-label="报告列表" hidden>
      <h2 class="list-heading" id="list-status" aria-live="polite">报告列表</h2>
      <div id="records"></div>
    </section>
    <article id="detail" aria-label="当前报告"></article>
  </div>
  <noscript><p>启用 JavaScript 可切换页面、筛选并查看报告详情。</p><ul>{plain_index}</ul></noscript>
</main></div>
<script type="application/json" id="report-data">{data}</script>
<script>{js}</script>
</body></html>
"""


def _report_rows(data_dir: Path) -> list:
    from .ledger import ledger_details
    from .store import list_reports

    rows = list_reports(data_dir)
    for row in rows:
        if row["kind"] == "ledger" and row["payload"].get("detail_view") == "on_demand":
            row["payload"] = ledger_details(data_dir, row["key"])
    return rows


def export_dashboard(data_dir: Path, output: Path) -> dict:
    output = Path(output)
    if output.suffix.lower() not in (".htm", ".html"):
        raise ValueError("看板导出文件须使用 .html 或 .htm 扩展名")
    reports = _report_rows(data_dir)
    document = render_dashboard(reports)
    output.parent.mkdir(parents=True, exist_ok=True)
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=output.parent, encoding="utf-8", delete=False) as f:
            temp_name = f.name
            f.write(document)
        os.replace(temp_name, output)
    finally:
        if temp_name is not None:
            Path(temp_name).unlink(missing_ok=True)
    return {"file": str(output.resolve()), "reports": len(reports)}


def make_server(data_dir: Path, port: int) -> ThreadingHTTPServer:
    """Bind only to loopback. Every request is a fresh view of committed reports."""
    if not 0 <= port <= 65535:
        raise ValueError("端口范围为 0 至 65535")

    class DashboardHandler(BaseHTTPRequestHandler):
        def _respond(self, *, body: bool) -> None:
            host = self.headers.get("Host", "")
            allowed_hosts = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
            if self.server.server_port == 80:
                allowed_hosts.update(("127.0.0.1", "localhost"))
            if host.lower() not in allowed_hosts:
                self.send_error(403, "Local host required")
                return
            if urlsplit(self.path).path not in ("/", "/index.html"):
                self.send_error(404)
                return
            page = render_dashboard(_report_rows(data_dir)).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            if body:
                self.wfile.write(page)

        def do_GET(self) -> None:
            self._respond(body=True)

        def do_HEAD(self) -> None:
            self._respond(body=False)

    return ThreadingHTTPServer(("127.0.0.1", port), DashboardHandler)


def _export(args):
    return export_dashboard(args.data_dir, args.output or (Path(args.data_dir) / "dashboard.html"))


def _serve(args):
    with make_server(args.data_dir, args.port) as server:
        url = f"http://127.0.0.1:{server.server_port}/"
        print(url, file=sys.stderr, flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        return {"url": url, "state": "stopped"}


def register(subparsers) -> None:
    parser = subparsers.add_parser("dashboard", help="查看本地英超研究看板")
    commands = parser.add_subparsers(dest="dashboard_action", required=True)
    export = commands.add_parser("export", help="导出可离线打开的单文件 HTML")
    export.add_argument("--output", type=Path, help="输出 HTML，默认在数据目录")
    export.set_defaults(func=_export)
    serve = commands.add_parser("serve", help="在 127.0.0.1 启动只读看板")
    serve.add_argument("--port", type=int, default=8765)
    serve.set_defaults(func=_serve)
