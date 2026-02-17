#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${ROOT_DIR}"

AGENT_NAME="${1:-opus-engineer}"
CONFIG_PATH="${2:-${ROOT_DIR}/claude-swarm.yaml}"
VENV_PY="${ROOT_DIR}/.venv/bin/python3"

if [[ ! -x "${VENV_PY}" ]]; then
  python3 -m venv "${ROOT_DIR}/.venv"
fi

if ! "${VENV_PY}" -m pip --version >/dev/null 2>&1; then
  "${VENV_PY}" -m ensurepip --upgrade >/dev/null 2>&1 || true
fi

if ! "${VENV_PY}" -c "import click, yaml, watchdog" >/dev/null 2>&1; then
  "${VENV_PY}" -m pip install -r "${ROOT_DIR}/requirements.txt"
fi

if [[ ! -f "${CONFIG_PATH}" ]]; then
  cp "${ROOT_DIR}/claude-swarm.yaml.example" "${CONFIG_PATH}"
  echo "Created config: ${CONFIG_PATH}"
  echo "Edit provider/agents if needed, then re-run this script."
fi

if ! "${VENV_PY}" - "${CONFIG_PATH}" "${AGENT_NAME}" <<'PY'
import sys
from pathlib import Path
import yaml

cfg_path = Path(sys.argv[1])
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
  "${VENV_PY}" -m claude_swarm.cli stop "${AGENT_NAME}" --config "${CONFIG_PATH}" >/dev/null 2>&1 || true
}

trap cleanup EXIT INT TERM

"${VENV_PY}" -m claude_swarm.cli start "${AGENT_NAME}" --config "${CONFIG_PATH}"
"${VENV_PY}" -m claude_swarm.cli status --config "${CONFIG_PATH}"
exec "${VENV_PY}" -m claude_swarm.cli logs "${AGENT_NAME}" --config "${CONFIG_PATH}"
