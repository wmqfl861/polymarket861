#!/usr/bin/env bash
# Installed wheel against Ubuntu's user manager. Only the upstream inputs are simulated.
set -euo pipefail
base="$HOME/.local/share/polymarket861-five"
units="$HOME/.config/systemd/user"
forecast=polymarket861-five-forecast.service
check=polymarket861-five-forecast-watchdog.service
timer=polymarket861-five-forecast-watchdog.timer
outage="$RUNNER_TEMP/forecast-outage"
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"
mkdir -p "$units/$forecast.d" "$units/$timer.d"
cat > "$units/$forecast.d/acceptance.conf" <<EOF
[Service]
Environment=PYTHONPATH=$GITHUB_WORKSPACE/.github/fixtures/forecast-service
Environment=PM861_CI_FORECAST_OUTAGE=$outage
WatchdogSec=50s
TimeoutAbortSec=3s
LimitCORE=0
EOF
cat > "$units/$timer.d/acceptance.conf" <<'EOF'
[Timer]
OnStartupSec=
OnActiveSec=2s
OnUnitActiveSec=8s
AccuracySec=1s
EOF
sctl() { systemctl --user "$@"; }
health() { "$base/venv/bin/pm861" --data-dir "$base/research" big5-scheduler watchdog; }
until_ok() {
    local end=$((SECONDS + 115))
    until "$@"; do
        if (( SECONDS >= end )); then echo "Timed out waiting for $*" >&2; return 1; fi
        sleep 1
    done
}
main_pid() { sctl show "$forecast" -p MainPID --value; }
recovered() {
    local next
    next=$(main_pid)
    [[ "$next" =~ ^[1-9][0-9]*$ && "$next" != "$1" ]] &&
        sctl is-active --quiet "$forecast" && health > "$RUNNER_TEMP/forecast-healthy.json"
}
trap 'journalctl --user -u "$forecast" -u "$check" -n 75 --no-pager || true' EXIT
sctl daemon-reload
touch "$outage"
sctl enable --now "$forecast" "$timer"
sctl is-active --quiet "$forecast"
until_ok sctl is-failed --quiet "$check"
if health > "$RUNNER_TEMP/forecast-outage.json"; then exit 1; else [[ $? == 1 ]]; fi
python - "$RUNNER_TEMP/forecast-outage.json" <<'PY'
import json, sys
by_league = json.load(open(sys.argv[1]))["leagues"]
assert by_league["sea"]["state"] != "ok", by_league
assert all(by_league[k]["state"] == "ok" for k in ("lal", "bun", "fl1")), by_league
PY
pid=$(main_pid)
rm "$outage"
kill -KILL "$pid"
until_ok recovered "$pid"
sctl start "$check"
pid=$(main_pid)
before=$(sctl show "$forecast" -p NRestarts --value)
kill -STOP "$pid"
until_ok recovered "$pid"
after=$(sctl show "$forecast" -p NRestarts --value)
(( after > before && before >= 1 ))
sctl stop "$timer" "$forecast"
[[ "$(sctl show "$forecast" -p Result --value)" == success ]]
if health > "$RUNNER_TEMP/forecast-stopped.json"; then exit 1; else [[ $? == 1 ]]; fi
