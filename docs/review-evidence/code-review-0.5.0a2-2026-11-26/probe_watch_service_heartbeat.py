#!/usr/bin/env python3
"""四国值守服务与 systemd 之间的就绪和心跳通知，实际走一遍。

deploy/polymarket861-five-watch.service 是 Type=notify、WatchdogSec=3min 的用户服务：
进程要通过 NOTIFY_SOCKET 指向的 Unix 数据报套接字报告 READY=1，之后至少每 3 分钟报告一次
WATCHDOG=1，否则 systemd 会在 5 分钟后判定启动超时，或在 3 分钟没有心跳时重启它。
开发方自己的测试环境不允许 Unix 数据报套接字，对应的那一项测试在三个解释器上都被跳过。

这个探针不需要 systemd：它自己绑定一个数据报套接字充当 systemd，然后用与服务文件相同的
命令行入口（market league-watch --cycles 0）启动一个子进程，记录收到的每一条通知；
其间用 market league-watchdog 的同一入口查询心跳。一共六遍：

  1. 路径形式的套接字，用 SIGINT（服务文件的 KillSignal）停止。
  2. 抽象名字空间形式的套接字（@…），用 SIGKILL 结束，看失联判定。
  3. 上游从一开始就连不上（目录一次也取不到），看这时的就绪通知、心跳和看护命令的结论。
  4. 进程启动时 SIGINT 已经被设成“忽略”（从 nohup、脚本里的 & 这类后台方式启动时就是这样），
     看 SIGINT 还能不能让它停下来。
  5. 第一次后台目录刷新抛出 sqlite3.OperationalError("database is locked")。
     这个异常在真实循环里怎样产生见 probe_big5_refresh_overlap.py；这里在同一个位置直接抛出它，
     只看进程怎样收场：退出码、标准错误、停止通知和心跳状态。
  6. 第五遍退出 5 秒之后，在同一个数据目录上再启动一次（服务文件的 Restart=on-failure、RestartSec=5s
     做的就是这件事），看新进程能不能照常就绪、心跳状态能不能恢复。

systemd 启动服务时信号处置是默认的。探针自己如果是从后台任务启动的，SIGINT 会被继承为“忽略”，
Python 就不会装上 KeyboardInterrupt 处理，所以每一遍都在子进程启动前把 SIGINT 的处置显式定下来。

上游用 tests/test_big5_market.py 的合成赛事代替，不联网。时钟是真实时钟。

用法：
  python -I probe_watch_service_heartbeat.py --src <仓库>/src --tests <仓库>/tests <空的临时目录> [--seconds 40]
"""

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
from contextlib import ExitStack
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--src", required=True)
parser.add_argument("--tests", required=True)
parser.add_argument("scratch")
parser.add_argument("--seconds", type=int, default=40)
parser.add_argument("--role", choices=("main", "service", "check"), default="main")
parser.add_argument("--max-age", type=int)
args = parser.parse_args()
sys.path[:0] = [args.src, args.tests]

import test_big5_market as fixture  # noqa: E402
from polymarket861 import big5_market as big5, cli, market_provider as provider  # noqa: E402

WATCH = ["market", "league-watch", "--league", "sea", "--start", fixture.START, "--end", fixture.END]
CHECK = ["market", "league-watchdog", "--league", "sea", "--start", fixture.START, "--end", fixture.END]


def service(data):
    """子进程：与服务文件的 ExecStart 相同的入口，只把上游换成合成回复。"""
    import sqlite3
    from unittest.mock import patch

    mode = os.environ.get("PM861_PROBE_MODE", "")
    calls = []
    real_catalog = big5.catalog

    def unreachable(url, params=None, **kwargs):
        raise OSError("simulated: Gamma unreachable")

    def catalog(*a, **k):
        calls.append(1)
        if len(calls) > 1:
            raise sqlite3.OperationalError("database is locked")
        # 第一次（同步的）目录照常走，上游连不上，所以翻页没走完，下一次刷新在 30 秒后而不是 300 秒后。
        return real_catalog(*a, **k)

    with ExitStack() as stack:
        stack.enter_context(patch.object(provider, "get", side_effect=fixture.gamma if mode == "" else unreachable))
        if mode == "refresh-locked":
            stack.enter_context(patch.object(big5, "catalog", catalog))
        return cli.main(["--data-dir", data, *WATCH, "--cycles", "0", "--interval", "1"])


def heartbeat(data, *extra):
    """与看护定时器相同的入口；返回退出码和状态。"""
    done = subprocess.run(
        [sys.executable, "-I", __file__, "--src", args.src, "--tests", args.tests, data, "--role", "check", *extra],
        capture_output=True,
        text=True,
    )
    body = json.loads(done.stdout) if done.stdout.strip().startswith("{") else {"stderr": done.stderr.strip()[-200:]}
    return {"exit": done.returncode, **{k: body.get(k) for k in ("healthy", "state", "ticks", "catalog_complete_by_league")}}


def run(name, address, bind, stop_signal, mode="", sigint=signal.SIG_DFL, patience=60):
    """stop_signal 为 None 时不发信号，等进程自己退出。"""
    data = str(Path(args.scratch) / name)
    receiver = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    receiver.bind(bind)
    receiver.settimeout(0.5)
    began = time.monotonic()
    child = subprocess.Popen(
        [sys.executable, "-I", __file__, "--src", args.src, "--tests", args.tests, data, "--role", "service"],
        env={**os.environ, "NOTIFY_SOCKET": address, "PYTHONDONTWRITEBYTECODE": "1", "PM861_PROBE_MODE": mode},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        preexec_fn=lambda: signal.signal(signal.SIGINT, sigint),
    )
    messages, checks = [], []

    def drain(until, while_running=False):
        while time.monotonic() < until and not (while_running and child.poll() is not None):
            try:
                text = receiver.recv(4096).decode()
            except TimeoutError:
                continue
            messages.append({"at_second": round(time.monotonic() - began, 1), "message": text.replace("\n", " | ")})

    drain(began + args.seconds / 2)
    checks.append({"when": "running", **heartbeat(data)})
    if stop_signal is None:
        drain(began + args.seconds + patience, while_running=True)
        label = "the process exited by itself"
    else:
        drain(began + args.seconds)
        child.send_signal(stop_signal)
        drain(time.monotonic() + patience, while_running=True)
        label = stop_signal.name
    survived = child.poll() is None
    if survived:
        child.terminate()
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
        label += ", then SIGTERM"
    exited_at = round(time.monotonic() - began, 1)
    drain(time.monotonic() + 2)
    checks.append({"when": "just after " + label, **heartbeat(data)})
    if stop_signal == signal.SIGKILL:
        time.sleep(6)
        checks.append({"when": "6 s later, --max-age 5", **heartbeat(data, "--max-age", "5")})
    receiver.close()
    gaps = [round(b["at_second"] - a["at_second"], 1) for a, b in zip(messages, messages[1:]) if "WATCHDOG" in b["message"]]
    return {
        "notify_socket_form": "abstract (@name)" if address.startswith("@") else "path",
        "sigint_disposition_at_start": "ignored" if sigint == signal.SIG_IGN else "default",
        "stopped_with": label,
        "still_running_seconds_after_the_signal": patience if survived else None,
        "first_message": messages[0] if messages else None,
        "watchdog_messages": sum("WATCHDOG=1" in m["message"] for m in messages),
        "longest_gap_between_watchdog_messages_seconds": max(gaps) if gaps else None,
        "stopping_message_received": any("STOPPING=1" in m["message"] for m in messages),
        "exit_status": child.returncode,
        "exited_at_second": exited_at,
        "stderr_tail": (child.stderr.read() or "").strip().splitlines()[-1:] if child.stderr else None,
        "watchdog_command": checks,
        "messages": messages,
    }


if args.role == "check":
    extra = ["--max-age", str(args.max_age)] if args.max_age else []
    raise SystemExit(cli.main(["--data-dir", args.scratch, *CHECK, *extra]))
if args.role == "service":
    raise SystemExit(service(args.scratch))

scratch = Path(args.scratch)
scratch.mkdir(parents=True, exist_ok=True)  # keep the path short: a Unix socket path is limited to 107 bytes


def path(name):
    return str(scratch / name), str(scratch / name)


result = {
    "path socket, SIGINT": run("a", *path("notify.sock"), signal.SIGINT),
    "abstract socket, SIGKILL": run("b", f"@pm861-probe-{os.getpid()}", f"\0pm861-probe-{os.getpid()}", signal.SIGKILL),
    "path socket, upstream unreachable from the start, SIGINT": run(
        "c", *path("notify-c.sock"), signal.SIGINT, mode="gamma-down"
    ),
    "path socket, SIGINT already ignored when the process starts (as under nohup or a script's &), SIGINT": run(
        "d", *path("notify-d.sock"), signal.SIGINT, sigint=signal.SIG_IGN, patience=15
    ),
    "path socket, the first background catalogue refresh raises 'database is locked', no signal sent": run(
        "e", *path("notify-e.sock"), None, mode="refresh-locked"
    ),
}
time.sleep(5)  # RestartSec=5s
result["the same data directory started again 5 s after that exit, SIGINT"] = run(
    "e", *path("notify-f.sock"), signal.SIGINT
)
json.dump(result, sys.stdout, ensure_ascii=False, indent=1)
print()
