#!/usr/bin/env bash
# Exercise the installed NBA wheel via the shipped user units on a disposable runner.
set -euo pipefail
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"
base="$HOME/.local/share/polymarket861-nba"
units="$HOME/.config/systemd/user"
config="$HOME/.config/polymarket861-nba"
mkdir -p "$base" "$config" "$units"
python -m venv "$base/venv"
"$base/venv/bin/python" -m pip install -c constraints/research.txt "$RUNNER_TEMP"/watch-dist/*.whl
install -m 0644 deploy/polymarket861-nba-*.{service,timer} "$units/"
systemctl --user daemon-reload
systemd-analyze --user verify "$units"/polymarket861-nba-*.{service,timer}
sctl() { systemctl --user "$@"; }
nba() { "$base/venv/bin/pm861" --data-dir "$base/state" nba "$@"; }
# First verify that absence is observable, and the standalone market service then recovers.
if nba watchdog > "$RUNNER_TEMP/nba-initial.json"; then exit 1; else [[ $? == 1 ]]; fi
tip=$(python -c 'from datetime import datetime,timezone,timedelta; print((datetime.now(timezone.utc)+timedelta(minutes=63)).replace(second=0,microsecond=0).isoformat())')
cat > "$config/providers.env" <<EOF
PYTHONPATH=$GITHUB_WORKSPACE/.github/fixtures/nba-service
GITHUB_WORKSPACE=$GITHUB_WORKSPACE
BALLDONTLIE_API_KEY=ci-offline-placeholder-for-schedule
PM861_CI_NBA_TIP=$tip
EOF
chmod 600 "$config/providers.env"
sctl start polymarket861-nba-markets.service
nba watchdog --component markets > "$RUNNER_TEMP/nba-market-health.json"
sctl start polymarket861-nba-schedule.service
sctl start polymarket861-nba-forecast.service
sctl start polymarket861-nba-watchdog.service
nba watchdog > "$RUNNER_TEMP/nba-health.json"
sctl start polymarket861-nba-sources.service
nba readiness > "$RUNNER_TEMP/nba-readiness.json"
day=$(python -c 'from datetime import datetime; from zoneinfo import ZoneInfo; import sys; print(datetime.fromisoformat(sys.argv[1]).astimezone(ZoneInfo("America/New_York")).date())' "$tip")
nba fixtures --date "$day" > "$RUNNER_TEMP/nba-fixtures.json"
nba forecast-show bdl:32 > "$RUNNER_TEMP/nba-installed-forecast.json"
python - "$RUNNER_TEMP" <<'PY'
import json, sys
from pathlib import Path
root = Path(sys.argv[1])
health = json.loads((root / "nba-health.json").read_text())
assert health["state"] == "ok" and health["paper_trading"] is False, health
schedule = json.loads((root / "nba-fixtures.json").read_text())
assert [g["game_id"] for g in schedule["games"]] == ["bdl:32"], schedule
forecast = json.loads((root / "nba-installed-forecast.json").read_text())
seal, = [w for w in forecast["windows"] if w["horizon"] == "T-60m"]
assert seal["state"] == "sealed", seal
assert set(seal["prediction"]["market_at_prediction"]) == {"win", "total", "home_total", "away_total"}, seal
assert seal["prediction"]["warmup"]["eligible"] is False, seal
readiness = json.loads((root / "nba-readiness.json").read_text())
assert readiness["state"] == "collecting" and readiness["phase_two_activated"] is False, readiness
assert set(seal["prediction"]["forecast"]["models"]) == {"N0", "N1", "N2"}, seal
PY
# A deployed 0.5.0a4 process cannot see guards in the new package. Test its actual wheel.
legacy="$RUNNER_TEMP/old-football-nba-check"
old_sha=60c1a2d1aa4782f59c956f0c728ba8f7df835433
mkdir -p "$legacy/src" "$legacy/wheels"
if ! git cat-file -e "$old_sha^{commit}" 2>/dev/null; then
    git fetch --no-tags --depth=1 origin "$old_sha"
fi
git archive "$old_sha" | tar -C "$legacy/src" -xf -
python -m pip wheel --no-deps -w "$legacy/wheels" "$legacy/src"
python -m venv "$legacy/venv"
"$legacy/venv/bin/python" -m pip install -c constraints/research.txt "$legacy"/wheels/*.whl
[[ "$("$legacy/venv/bin/pm861" --version)" = "0.5.0a4" ]]
fingerprint() {
    (cd "$base/state" && find . -type f -print0 | sort -z | xargs -0r sha256sum)
}
fingerprint > "$legacy/before"
for cmd in "jobs status" "fixture list"; do
    read -r -a args <<< "$cmd"
    if "$legacy/venv/bin/pm861" --data-dir "$base/state" "${args[@]}"; then exit 1; fi
done
fingerprint > "$legacy/after"
cmp "$legacy/before" "$legacy/after"
nba watchdog > /dev/null
for component in markets schedule forecast sources watchdog; do
    sctl start "polymarket861-nba-$component.timer"
    sctl is-active --quiet "polymarket861-nba-$component.timer"
    sctl stop "polymarket861-nba-$component.timer"
done
