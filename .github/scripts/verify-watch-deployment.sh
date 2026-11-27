#!/usr/bin/env bash
# Run on a disposable Ubuntu Actions runner with a real systemd user manager.
set -euo pipefail
base="$HOME/.local/share/polymarket861-five"
units="$HOME/.config/systemd/user"
watch=polymarket861-five-watch.service
health=polymarket861-five-watchdog.service
timer=polymarket861-five-watchdog.timer

sudo loginctl enable-linger "$(id -un)"
sudo systemctl start "user@$(id -u).service"
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"

mkdir -p "$base" "$units/$watch.d" "$units/$timer.d" "$RUNNER_TEMP/watch-dist"
python -m pip wheel --no-deps --wheel-dir "$RUNNER_TEMP/watch-dist" .
python -m venv "$base/venv"
"$base/venv/bin/python" -m pip install -c constraints/research.txt "$RUNNER_TEMP"/watch-dist/*.whl
"$base/venv/bin/pm861" --version
install -m 0644 deploy/polymarket861-five-*.* "$units/"
cat > "$units/$watch.d/acceptance.conf" <<EOF
[Service]
Environment=PYTHONPATH=$GITHUB_WORKSPACE/.github/fixtures/watch-service
Environment=PM861_CI_GAMMA_OFFLINE=$RUNNER_TEMP/gamma-offline
WatchdogSec=35s
TimeoutAbortSec=4s
LimitCORE=0
EOF
cat > "$units/$timer.d/acceptance.conf" <<'EOF'
[Timer]
OnStartupSec=
OnActiveSec=2s
OnUnitActiveSec=8s
AccuracySec=1s
EOF

diagnostics() {
    journalctl --user -u "$watch" -u "$health" --no-pager -n 100 || true
    systemctl --user show "$watch" -p ActiveState -p Result -p NRestarts -p MainPID || true
}
trap diagnostics EXIT
systemctl --user daemon-reload
systemd-analyze --user verify "$units"/polymarket861-five-*.service "$units"/polymarket861-five-*.timer
"$base/venv/bin/pm861" --data-dir "$base/research" big5-scheduler status > "$RUNNER_TEMP/forecast-initial.json"
python - "$RUNNER_TEMP/forecast-initial.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))["leagues"]
assert set(d) == {"lal", "bun", "sea", "fl1"} and all(v["state"] == "not_started" for v in d.values()), d
PY
sctl() { systemctl --user "$@"; }
read_health() {
    "$base/venv/bin/pm861" --data-dir "$base/state" market league-watchdog --rolling
}
await_condition() {
    local deadline=$((SECONDS + 115))
    until "$@"; do
        if (( SECONDS >= deadline )); then
            echo "Timed out waiting for $*" >&2
            return 1
        fi
        sleep 1
    done
}
main_pid() { sctl show "$watch" -p MainPID --value; }
recovered() {
    local pid
    pid=$(main_pid)
    [[ "$pid" =~ ^[1-9][0-9]*$ && "$pid" != "$1" ]] &&
        sctl is-active --quiet "$watch" && read_health > "$RUNNER_TEMP/watch-healthy.json"
}
touch "$RUNNER_TEMP/gamma-offline"
sctl enable --now "$watch" "$timer"
sctl is-active --quiet "$watch"
if read_health > "$RUNNER_TEMP/watch-offline.json"; then exit 1; else [[ $? == 1 ]]; fi
python - "$RUNNER_TEMP/watch-offline.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
assert d["scheduler_alive"] and not d["healthy"] and d["catalog_state"] == "missing", d
PY
sctl is-active --quiet "$timer"
await_condition sctl is-failed --quiet "$health"

old_pid=$(main_pid)
rm "$RUNNER_TEMP/gamma-offline"
kill -KILL "$old_pid"
await_condition recovered "$old_pid"
sctl start "$health"
python - "$RUNNER_TEMP/watch-healthy.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
assert d["scheduler_alive"] and d["healthy"] and d["catalog_state"] == "fresh", d
assert set(d["catalog_by_league"]) == {"lal", "bun", "sea", "fl1"}, d
assert all(v["age_seconds"] is not None and v["state"] == "fresh" for v in d["catalog_by_league"].values()), d
PY
restarts=$(sctl show "$watch" -p NRestarts --value)
(( restarts >= 1 ))
old_pid=$(main_pid)
kill -STOP "$old_pid"
await_condition recovered "$old_pid"
after_watchdog=$(sctl show "$watch" -p NRestarts --value)
(( after_watchdog > restarts ))
sctl stop "$timer" "$watch"
[[ "$(sctl show "$watch" -p Result --value)" == success ]]
if read_health > "$RUNNER_TEMP/watch-stopped.json"; then exit 1; else [[ $? == 1 ]]; fi
python - "$RUNNER_TEMP/watch-stopped.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
assert d["state"] == "stopped" and not d["healthy"], d
PY
echo "Verified offline READY with failed timer check, four fresh league receipts, crash restart, watchdog restart, and clean stop."
