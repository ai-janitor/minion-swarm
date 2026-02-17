#!/usr/bin/env bash
set -euo pipefail

SOURCE_PATH="${BASH_SOURCE[0]}"
while [[ -L "${SOURCE_PATH}" ]]; do
  SOURCE_DIR="$(cd -P "$(dirname "${SOURCE_PATH}")" && pwd)"
  SOURCE_PATH="$(readlink "${SOURCE_PATH}")"
  [[ "${SOURCE_PATH}" != /* ]] && SOURCE_PATH="${SOURCE_DIR}/${SOURCE_PATH}"
done
ROOT_DIR="$(cd -P "$(dirname "${SOURCE_PATH}")" && pwd)"
INVOKE_DIR="$(pwd)"
cd "${ROOT_DIR}"

AGENT_NAME="${1:-swarm-lead}"
CONFIG_PATH="${2:-${MINION_SWARM_CONFIG:-${HOME}/.minion-swarm/minion-swarm.yaml}}"
PROJECT_DIR="${3:-${MINION_SWARM_PROJECT_DIR:-${INVOKE_DIR}}}"
VENV_PY="${ROOT_DIR}/.venv/bin/python3"

"${ROOT_DIR}/install.sh" --project-dir "${PROJECT_DIR}" --config "${CONFIG_PATH}" --no-symlink >/dev/null

if [[ ! -x "${VENV_PY}" ]]; then
  echo "Error: virtualenv missing after install bootstrap: ${VENV_PY}" >&2
  exit 2
fi

if ! "${VENV_PY}" - "${CONFIG_PATH}" "${AGENT_NAME}" <<'PY'
import sys
from pathlib import Path
import yaml

cfg_path = Path(sys.argv[1]).expanduser()
agent_name = sys.argv[2]

try:
    data = yaml.safe_load(cfg_path.read_text()) or {}
except Exception as exc:
    print(f"Error: Failed to read config {cfg_path}: {exc}")
    raise SystemExit(2)

agents = data.get("agents")
if not isinstance(agents, dict) or not agents:
    print(f"Error: No agents defined in {cfg_path}")
    raise SystemExit(2)

if agent_name not in agents:
    print(f"Error: Agent '{agent_name}' not found in {cfg_path}")
    print("Available agents:")
    for name in agents.keys():
        print(f"  - {name}")
    raise SystemExit(2)
PY
then
  exit 2
fi

cleanup() {
  if [[ "${LEAVE_RUNNING:-0}" == "1" ]]; then
    return
  fi
  "${VENV_PY}" -m minion_swarm.cli stop "${AGENT_NAME}" --config "${CONFIG_PATH}" >/dev/null 2>&1 || true
}

trap cleanup EXIT INT TERM

"${VENV_PY}" -m minion_swarm.cli start "${AGENT_NAME}" --config "${CONFIG_PATH}"
"${VENV_PY}" -m minion_swarm.cli status --config "${CONFIG_PATH}"
exec "${VENV_PY}" -m minion_swarm.cli logs "${AGENT_NAME}" --config "${CONFIG_PATH}" --lines 0
