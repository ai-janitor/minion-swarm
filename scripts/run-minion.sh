#!/usr/bin/env bash
set -euo pipefail

# Convenience wrapper: start one agent, follow logs, cleanup on exit.
# Usage: run-minion [agent-name] [config-path] [project-dir]

AGENT_NAME="${1:-swarm-lead}"
CONFIG_PATH="${2:-${MINION_SWARM_CONFIG:-${HOME}/.minion-swarm/minion-swarm.yaml}}"
PROJECT_DIR="${3:-${MINION_SWARM_PROJECT_DIR:-$(pwd)}}"

command -v minion-swarm &>/dev/null || { echo "Error: minion-swarm not found. Run: scripts/install.sh" >&2; exit 2; }

# Ensure config is seeded and patched
minion-swarm init --project-dir "${PROJECT_DIR}" --config "${CONFIG_PATH}" >/dev/null

cleanup() {
  if [[ "${LEAVE_RUNNING:-0}" == "1" ]]; then
    return
  fi
  minion-swarm stop "${AGENT_NAME}" --config "${CONFIG_PATH}" >/dev/null 2>&1 || true
}

trap cleanup EXIT INT TERM

minion-swarm start "${AGENT_NAME}" --config "${CONFIG_PATH}"
minion-swarm status --config "${CONFIG_PATH}"
exec minion-swarm logs "${AGENT_NAME}" --config "${CONFIG_PATH}" --lines 0
