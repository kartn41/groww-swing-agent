#!/usr/bin/env bash
# update-agent: deploy the newest groww-swing-agent zip from /tmp to the agent user, safely.
#
#   sudo update-agent                 # newest /tmp/groww-swing-agent*.zip
#   sudo update-agent --dry-run       # show what would happen, change nothing
#   sudo update-agent --zip /path/to.zip --full-tests
#
# Steps: validate zip -> (wait for running jobs, then stop agent) -> back up old version -> unpack ->
# carry over .env and data/ (adding any NEW settings) -> fresh venv -> tests -> restart services.
# ANY failure after the old version is moved aside restores it and restarts what was running.
# Install once:  sudo install -m 755 update.sh /usr/local/sbin/update-agent
set -euo pipefail

AGENT_USER="${AGENT_USER:-trader}"
HOME_DIR="${HOME_DIR:-/home/${AGENT_USER}}"
APP_NAME="groww-swing-agent"
APP="${HOME_DIR}/${APP_NAME}"
UNIT_DIR="${UNIT_DIR:-/etc/systemd/system}"
ZIP_DIR="${ZIP_DIR:-/tmp}"
LOCK_FILE="${LOCK_FILE:-/var/lock/groww-agent-update.lock}"
WAIT_SECONDS="${WAIT_SECONDS:-300}"
PYTHON_BIN="${PYTHON_BIN:-python3.12}"
SMOKE_TESTS="${SMOKE_TESTS:-tests.test_core tests.test_review_fixes tests.test_monitor tests.test_foundation tests.test_notify tests.test_events tests.test_strategies_more tests.test_bhavcopy tests.test_etf}"
KEEP=3
ZIP="" DRY=0 FORCE=0 TESTS=smoke INSTALL_UNITS=0

while (( $# )); do
  case "$1" in
    --zip) ZIP="${2:?--zip needs a path}"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    --force) FORCE=1; shift ;;
    --full-tests) TESTS=full; shift ;;
    --skip-tests) TESTS=none; shift ;;
    --install-units) INSTALL_UNITS=1; shift ;;
    --keep) KEEP="${2:?}"; shift 2 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown option: $1 (try --help)" >&2; exit 2 ;;
  esac
done

say() { printf '%s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
[[ "$APP" == /?*/* ]] || die "refusing to work with APP='${APP}'"

if [[ -z "${NO_SYSTEMCTL:-}" && -z "${ALLOW_NON_ROOT:-}" && $EUID -ne 0 ]]; then
  die "run with sudo (needs to manage systemd and file ownership)"
fi
run_as() { if [[ "$(id -un)" == "$AGENT_USER" ]]; then "$@"; else sudo -u "$AGENT_USER" -H "$@"; fi; }
sysd() { [[ -n "${NO_SYSTEMCTL:-}" ]] && return 0; systemctl "$@"; }
own() { if [[ $EUID -eq 0 ]]; then chown -R "${AGENT_USER}:" "$@"; fi; }

mkdir -p "$HOME_DIR"
exec 9>"$LOCK_FILE"
flock -n 9 || die "another update is already running"
LOG="${HOME_DIR}/update.log"
exec > >(tee -a "$LOG") 2>&1
TS="$(date +%Y%m%d-%H%M%S)"
say "=== update-agent ${TS} (user ${AGENT_USER}) ==="

UNITS=(groww-agent-daily.timer groww-agent-dashboard.service groww-agent-monitor.timer groww-agent-etf-monitor.timer)
ENABLED=() BACKUP="" STAGE="" SWAPPED=0 STOPPED=0 UNITS_BACKUP=""

# ---------------------------------------------------------------- pre-flight (changes nothing)
if [[ -z "$ZIP" ]]; then
  shopt -s nullglob
  cands=("${ZIP_DIR}"/${APP_NAME}*.zip)
  shopt -u nullglob
  (( ${#cands[@]} )) || die "no ${ZIP_DIR}/${APP_NAME}*.zip found; upload the zip first"
  ZIP="$(ls -1t "${cands[@]}" | head -n1)"
fi
[[ -f "$ZIP" && ! -L "$ZIP" ]] || die "${ZIP} is missing or a symlink"
say "zip: ${ZIP} ($(du -h "$ZIP" | cut -f1))"

# /tmp is world-writable: work on a private copy so the zip cannot change after it is checked.
REL_DIR="${HOME_DIR}/releases"; mkdir -p "$REL_DIR"
COPY="${REL_DIR}/${APP_NAME}-${TS}.zip"
cp -- "$ZIP" "$COPY"; chmod 600 "$COPY"; own "$REL_DIR"
SHA="$(sha256sum "$COPY" | cut -c1-16)"
say "sha256: ${SHA}  (compare with 'sha256sum' on the machine you uploaded from)"

python3 - "$COPY" <<'PY' || die "zip failed validation (corrupt, unsafe paths, symlinks, or not a groww-swing-agent release)"
import sys, zipfile
z = zipfile.ZipFile(sys.argv[1])
bad = z.testzip()
if bad:
    raise SystemExit(f"corrupt member: {bad}")
root = "groww-swing-agent/"
names = z.namelist()
for i in z.infolist():
    n = i.filename
    if not n.startswith(root) or n.startswith("/") or ".." in n.split("/"):
        raise SystemExit(f"unsafe path: {n}")
    if (i.external_attr >> 16) & 0o170000 == 0o120000:
        raise SystemExit(f"symlink inside zip: {n}")
for need in ("app/run.py", "app/config.py", "tests/test_core.py", "deploy/install_systemd.sh"):
    if root + need not in names:
        raise SystemExit(f"missing {need}")
PY

MODE=""
if [[ -f "${APP}/.env" ]]; then
  MODE="$(grep -E '^GROWW_MODE=' "${APP}/.env" | tail -n1 | cut -d= -f2- | tr -d "\"' " | tr '[:lower:]' '[:upper:]' || true)"
fi
NOW="${UPDATE_NOW:-$(TZ=Asia/Kolkata date '+%Y-%m-%d %H:%M')}"
DOW="$(date -d "$NOW" +%u)"; HM="$(date -d "$NOW" +%H%M)"
if [[ "$MODE" == "LIVE" && $FORCE -eq 0 ]] && (( DOW <= 5 )) && (( 10#$HM >= 900 && 10#$HM <= 1545 )); then
  die "GROWW_MODE=LIVE and the market is open (${NOW} IST). Update after 15:45 IST, or pass --force."
fi
[[ "$MODE" == "LIVE" ]] && warn "LIVE mode: GTT stops stay on Groww's servers during the update, but nothing monitors them until the restart."

for u in "${UNITS[@]}"; do
  if [[ -z "${NO_SYSTEMCTL:-}" ]] && systemctl is-enabled "$u" >/dev/null 2>&1; then ENABLED+=("$u"); fi
done
if [[ -n "${NO_SYSTEMCTL:-}" || -n "${ASSUME_ENABLED:-}" ]]; then read -r -a ENABLED <<<"${ASSUME_ENABLED:-}"; fi
if (( INSTALL_UNITS )); then
  for u in groww-agent-daily.timer groww-agent-dashboard.service groww-agent-etf-monitor.timer; do
    [[ " ${ENABLED[*]:-} " == *" $u "* ]] || ENABLED+=("$u")
  done
fi

PROCS="${APP}/.venv/bin/python -m app.run"
say "mode: ${MODE:-unknown}; services to restart afterwards: ${ENABLED[*]:-none}"
say "existing install: $([[ -d $APP ]] && echo yes || echo 'no (first install)')"
if (( DRY )); then
  say "--- dry run: nothing was changed ---"
  rm -f "$COPY"
  exit 0
fi

# ---------------------------------------------------------------- rollback machinery
restart_services() {
  [[ ${#ENABLED[@]} -eq 0 ]] && return 0
  sysd daemon-reload || true
  for u in "${ENABLED[@]}"; do sysd restart "$u" || warn "could not restart $u"; done
}

rollback() {
  warn "update failed: restoring the previous version"
  sysd stop groww-agent-daily.timer groww-agent-monitor.timer groww-agent-etf-monitor.timer >/dev/null 2>&1 || true
  if [[ -n "$BACKUP" && -d "$BACKUP" ]]; then
    [[ -d "$APP" ]] && mv "$APP" "${APP}.failed-${TS}"
    mv "$BACKUP" "$APP"
    say "restored ${APP} from ${BACKUP}"
  fi
  if [[ -n "$UNITS_BACKUP" && -d "$UNITS_BACKUP" ]]; then
    for f in "$UNITS_BACKUP"/*; do [[ -e "$f" ]] && cp -f "$f" "$UNIT_DIR/"; done
  fi
  restart_services
}

on_exit() {
  local rc=$?
  if (( rc != 0 )); then
    if (( SWAPPED )); then rollback
    elif (( STOPPED )); then warn "update aborted before any change; restarting services"; restart_services
    fi
    [[ -n "$STAGE" && -d "$STAGE" ]] && rm -rf "$STAGE"
    say "=== update FAILED (exit ${rc}); previous version is running. Log: ${LOG} ==="
  fi
}
trap on_exit EXIT

# ---------------------------------------------------------------- stop the agent
say "--- stopping the agent"
sysd stop groww-agent-daily.timer groww-agent-monitor.timer groww-agent-etf-monitor.timer >/dev/null 2>&1 || true     # no new runs
STOPPED=1
waited=0
while pgrep -f "${PROCS} (daily|monitor|fetch|paper|backtest)" >/dev/null 2>&1 && (( waited < WAIT_SECONDS )); do
  (( waited == 0 )) && say "a job is running; waiting up to ${WAIT_SECONDS}s for it to finish"
  sleep 5; waited=$((waited + 5))
done
sysd stop groww-agent-daily.service groww-agent-monitor.service groww-agent-dashboard.service >/dev/null 2>&1 || true
if pids="$(pgrep -f "${PROCS}" 2>/dev/null)" && [[ -n "$pids" ]]; then
  say "killing leftover agent processes: ${pids//$'\n'/ }"
  kill -TERM $pids 2>/dev/null || true
  for _ in 1 2 3 4 5 6; do pgrep -f "${PROCS}" >/dev/null 2>&1 || break; sleep 1; done
  if pids="$(pgrep -f "${PROCS}" 2>/dev/null)"; then kill -KILL $pids 2>/dev/null || true; sleep 1; fi
fi
! pgrep -f "${PROCS}" >/dev/null 2>&1 || die "could not stop all agent processes"

# ---------------------------------------------------------------- unpack to staging, then swap
STAGE="${HOME_DIR}/.stage-${TS}"
run_as mkdir -p "$STAGE"
run_as python3 - "$COPY" "$STAGE" <<'PY'
import os, sys, zipfile
zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])
for dp, _, fs in os.walk(sys.argv[2]):
    for f in fs:
        if f.endswith(".sh"):
            os.chmod(os.path.join(dp, f), 0o755)
PY
NEW="${STAGE}/${APP_NAME}"
[[ -f "${NEW}/app/run.py" ]] || die "unpacked release looks wrong"

if [[ -d "$APP" ]]; then
  BACKUP="${APP}.bak-${TS}"
  mv "$APP" "$BACKUP"
fi
mv "$NEW" "$APP"; rmdir "$STAGE"; STAGE=""
SWAPPED=1
say "--- new version in place; old version kept at ${BACKUP:-<none>}"

# ---------------------------------------------------------------- carry over config and data
if [[ -n "$BACKUP" ]]; then
  if [[ -f "${BACKUP}/.env" ]]; then
    cp -p "${BACKUP}/.env" "${APP}/.env"; chmod 600 "${APP}/.env"
    added=()
    while IFS= read -r line; do
      [[ "$line" =~ ^([A-Z][A-Z0-9_]*)=(.*)$ ]] || continue
      key="${BASH_REMATCH[1]}"
      if ! grep -q "^${key}=" "${APP}/.env"; then
        (( ${#added[@]} == 0 )) && printf '\n# added by update-agent %s\n' "$TS" >>"${APP}/.env"
        printf '%s\n' "$line" >>"${APP}/.env"; added+=("$key")
      fi
    done <"${APP}/.env.example"
    (( ${#added[@]} )) && say "new settings added to .env (check them): ${added[*]}"
  else
    warn "no .env in the old version; copy .env.example to .env and fill it in"
  fi
  [[ -d "${BACKUP}/data" ]] && cp -a "${BACKUP}/data/." "${APP}/data/"
  for f in requirements.lock check_login.py; do [[ -f "${BACKUP}/$f" ]] && cp -p "${BACKUP}/$f" "${APP}/$f"; done
fi
mkdir -p "${APP}/data/universe" "${APP}/data/events"
own "$APP"

# ---------------------------------------------------------------- environment and checks
say "--- building the Python environment"
PY="$PYTHON_BIN"; command -v "$PY" >/dev/null 2>&1 || PY=python3
run_as "$PY" -m venv ${VENV_ARGS:-} "${APP}/.venv"
if [[ -z "${SKIP_PIP:-}" ]]; then
  if [[ -f "${APP}/requirements.lock" ]]; then
    run_as "${APP}/.venv/bin/pip" install --quiet -r "${APP}/requirements.lock"
  else
    run_as "${APP}/.venv/bin/pip" install --quiet pandas numpy growwapi pyotp
    run_as bash -c "'${APP}/.venv/bin/pip' freeze > '${APP}/requirements.lock'"
  fi
fi
run_as bash -c "cd '${APP}' && .venv/bin/python -c 'import app.run, app.dashboard, app.backtest, app.execution.monitor'" \
  || die "the new version cannot be imported"
case "$TESTS" in
  none) warn "tests skipped (--skip-tests)" ;;
  smoke) say "--- running smoke tests"
         run_as bash -c "cd '${APP}' && .venv/bin/python -m unittest ${SMOKE_TESTS} 2>&1 | tail -n 5; exit \${PIPESTATUS[0]}" \
           || die "smoke tests failed" ;;
  full)  say "--- running the full test suite (several minutes)"
         run_as bash -c "cd '${APP}' && timeout 1800 .venv/bin/python -m unittest discover -s tests 2>&1 | tail -n 5; exit \${PIPESTATUS[0]}" \
           || die "test suite failed" ;;
esac
printf 'released %s\nsha256 %s\nzip %s\n' "$TS" "$SHA" "$(basename "$ZIP")" >"${APP}/.release"
own "${APP}/.release"

# ---------------------------------------------------------------- systemd units, then restart
if [[ -n "${NO_SYSTEMCTL:-}" || -n "${ASSUME_ENABLED:-}" || ${#ENABLED[@]} -gt 0 || -f "${UNIT_DIR}/groww-agent-daily.service" ]]; then
  say "--- refreshing systemd unit files"
  UNITS_BACKUP="${HOME_DIR}/.units-bak-${TS}"; mkdir -p "$UNITS_BACKUP"
  for f in "$UNIT_DIR"/groww-agent-*; do [[ -e "$f" ]] && cp -f "$f" "$UNITS_BACKUP/"; done
  extra=(); [[ " ${ENABLED[*]:-} " == *" groww-agent-monitor.timer "* || -f "${UNIT_DIR}/groww-agent-monitor.timer" ]] && extra=(--with-live-monitor)
  DEST="$UNIT_DIR" NO_SYSTEMCTL=1 AGENT_USER="$AGENT_USER" AGENT_DIR="$APP" bash "${APP}/deploy/install_systemd.sh" "${extra[@]}" >/dev/null
fi
if (( INSTALL_UNITS )) && [[ -z "${NO_SYSTEMCTL:-}" ]]; then
  for u in "${ENABLED[@]}"; do sysd enable "$u" >/dev/null 2>&1 || true; done
fi
say "--- restarting: ${ENABLED[*]:-nothing was enabled}"
restart_services
sleep "${SETTLE_SECONDS:-3}"
for u in "${ENABLED[@]:-}"; do
  [[ -n "$u" ]] || continue
  if [[ -z "${NO_SYSTEMCTL:-}" ]] && ! systemctl is-active "$u" >/dev/null 2>&1; then
    if [[ "$u" == *.timer ]]; then die "$u did not start"; fi
    warn "$u is not running (a dashboard without DASHBOARD_PASSWORD in .env stops by design). See: journalctl -u $u -n 20"
  fi
done

# ---------------------------------------------------------------- tidy up (only after success)
SWAPPED=0; STOPPED=0          # success: from here nothing may roll back or restart again
# `ls` fails when a pattern matches nothing, which under pipefail would abort a finished update.
prune() { local keep="$1"; shift; { ls -1dt "$@" 2>/dev/null || true; } | tail -n +$((keep + 1)) | xargs -r rm -rf; }
prune "$KEEP" "${APP}".bak-*
prune 1 "${APP}".failed-*
prune "$KEEP" "${REL_DIR}"/${APP_NAME}-*.zip
prune 1 "${HOME_DIR}"/.units-bak-*
[[ "$ZIP" == "${ZIP_DIR}"/* ]] && rm -f -- "$ZIP"
say "--- done"
say "now running: $(grep -m1 '^## Status' "${APP}/README.md" 2>/dev/null || echo 'release '"$TS")"
[[ -n "$BACKUP" ]] && say "previous version kept at ${BACKUP} (roll back by hand: stop services, swap folders)"
[[ -z "${NO_SYSTEMCTL:-}" ]] && systemctl list-timers 'groww-agent-*' --no-pager 2>/dev/null || true
say "=== update OK ==="
