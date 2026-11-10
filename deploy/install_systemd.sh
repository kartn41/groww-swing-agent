#!/usr/bin/env bash
# Install systemd units. Run as the admin user (ubuntu), not as trader:
#   sudo bash deploy/install_systemd.sh               # daily paper job + dashboard
#   sudo bash deploy/install_systemd.sh --with-live-monitor   # ALSO the live GTT monitor (live mode only)
# DEST/NO_SYSTEMCTL env vars exist for testing (render units without touching the system).
set -euo pipefail
AGENT_USER="${AGENT_USER:-trader}"
AGENT_DIR="${AGENT_DIR:-/home/${AGENT_USER}/groww-swing-agent}"
DEST="${DEST:-/etc/systemd/system}"
SRC="$(cd "$(dirname "$0")/systemd" && pwd)"
WITH_MONITOR=0
[[ "${1:-}" == "--with-live-monitor" ]] && WITH_MONITOR=1

[[ -x "${AGENT_DIR}/.venv/bin/python" ]] || [[ -n "${NO_SYSTEMCTL:-}" ]] || {
  echo "Missing ${AGENT_DIR}/.venv. Create the virtual environment first." >&2; exit 1; }

units=(groww-agent-daily.service groww-agent-daily.timer groww-agent-dashboard.service
       groww-agent-etf-monitor.service groww-agent-etf-monitor.timer)
(( WITH_MONITOR )) && units+=(groww-agent-monitor.service groww-agent-monitor.timer)
mkdir -p "$DEST"
for u in "${units[@]}"; do
  sed -e "s#__USER__#${AGENT_USER}#g" -e "s#__DIR__#${AGENT_DIR}#g" "${SRC}/${u}" > "${DEST}/${u}"
  echo "installed ${DEST}/${u}"
done
if [[ -z "${NO_SYSTEMCTL:-}" ]]; then
  systemctl daemon-reload
  systemctl enable --now groww-agent-daily.timer groww-agent-dashboard.service groww-agent-etf-monitor.timer
  (( WITH_MONITOR )) && systemctl enable --now groww-agent-monitor.timer
  systemctl list-timers 'groww-agent-*' --no-pager
fi
