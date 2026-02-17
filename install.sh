#!/usr/bin/env bash
set -euo pipefail

SOURCE_PATH="${BASH_SOURCE[0]}"
while [[ -L "${SOURCE_PATH}" ]]; do
  SOURCE_DIR="$(cd -P "$(dirname "${SOURCE_PATH}")" && pwd)"
  SOURCE_PATH="$(readlink "${SOURCE_PATH}")"
  [[ "${SOURCE_PATH}" != /* ]] && SOURCE_PATH="${SOURCE_DIR}/${SOURCE_PATH}"
done
ROOT_DIR="$(cd -P "$(dirname "${SOURCE_PATH}")" && pwd)"

if [[ ! -f "${ROOT_DIR}/requirements.txt" ]] || [[ ! -f "${ROOT_DIR}/minion-swarm.yaml.example" ]]; then
  echo "Error: install.sh must be run from a checked-out minion-swarm repo." >&2
  echo "Clone first, then run ./install.sh from that clone." >&2
  exit 2
fi

VENV_PY="${ROOT_DIR}/.venv/bin/python3"
DEFAULT_CONFIG="${HOME}/.minion-swarm/minion-swarm.yaml"
INVOKE_DIR="$(pwd)"

CONFIG_PATH="${MINION_SWARM_CONFIG:-${DEFAULT_CONFIG}}"
PROJECT_DIR="${MINION_SWARM_PROJECT_DIR:-}"
PROJECT_DIR_EXPLICIT=0
CREATE_SYMLINK=1
OVERWRITE_CONFIG=0

if [[ -n "${PROJECT_DIR}" ]]; then
  PROJECT_DIR_EXPLICIT=1
fi

usage() {
  cat <<'USAGE'
Usage: ./install.sh [options]

Options:
  --project-dir PATH     Optional target repo path agents should operate on.
                         If omitted, keeps existing config project_dir or defaults to current shell dir.
  --config PATH          Config file path (default: ~/.minion-swarm/minion-swarm.yaml).
  --overwrite-config     Replace existing config YAML before patching.
  --no-symlink           Do not install launcher symlinks to ~/.local/bin.
  --help                 Show this help text.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project-dir)
      PROJECT_DIR="$2"
      PROJECT_DIR_EXPLICIT=1
      shift 2
      ;;
    --config)
      CONFIG_PATH="$2"
      shift 2
      ;;
    --overwrite-config)
      OVERWRITE_CONFIG=1
      shift
      ;;
    --no-symlink)
      CREATE_SYMLINK=0
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

CONFIG_PATH="$(python3 - <<'PY' "${CONFIG_PATH}"
from pathlib import Path
import sys
print(str(Path(sys.argv[1]).expanduser()))
PY
)"
CONFIG_DIR="$(dirname "${CONFIG_PATH}")"
mkdir -p "${CONFIG_DIR}"

PROJECT_DIR_ABS=""
if [[ "${PROJECT_DIR_EXPLICIT}" -eq 1 ]]; then
  PROJECT_DIR_ABS="$(python3 - <<'PY' "${PROJECT_DIR}"
from pathlib import Path
import sys
print(str(Path(sys.argv[1]).expanduser()))
PY
)"
  if [[ ! -d "${PROJECT_DIR_ABS}" ]]; then
    echo "Project directory not found: ${PROJECT_DIR_ABS}" >&2
    exit 2
  fi
  PROJECT_DIR_ABS="$(cd "${PROJECT_DIR_ABS}" && pwd)"
fi

seed_config() {
  local seed_source=""
  local candidates=(
    "${ROOT_DIR}/minion-swarm.yaml"
    "${HOME}/.minion-swarm/minion-swarm.yaml"
  )

  for candidate in "${candidates[@]}"; do
    if [[ -f "${candidate}" && "${candidate}" != "${CONFIG_PATH}" ]]; then
      seed_source="${candidate}"
      break
    fi
  done

  if [[ -z "${seed_source}" ]]; then
    seed_source="${ROOT_DIR}/minion-swarm.yaml.example"
  fi

  cp "${seed_source}" "${CONFIG_PATH}"
  echo "Seeded config from: ${seed_source}"
}

if [[ -f "${CONFIG_PATH}" && "${OVERWRITE_CONFIG}" -eq 0 && -t 0 ]]; then
  read -r -p "Config exists at ${CONFIG_PATH}. Overwrite it before patching? [y/N] " reply
  case "${reply}" in
    y|Y|yes|YES)
      OVERWRITE_CONFIG=1
      ;;
  esac
fi

if [[ ! -f "${CONFIG_PATH}" ]]; then
  seed_config
elif [[ "${OVERWRITE_CONFIG}" -eq 1 ]]; then
  seed_config
fi

if [[ ! -x "${VENV_PY}" ]]; then
  python3 -m venv "${ROOT_DIR}/.venv"
fi

if ! "${VENV_PY}" -m pip --version >/dev/null 2>&1; then
  "${VENV_PY}" -m ensurepip --upgrade >/dev/null 2>&1 || true
fi

if ! "${VENV_PY}" -c "import click, yaml, watchdog" >/dev/null 2>&1; then
  "${VENV_PY}" -m pip install -r "${ROOT_DIR}/requirements.txt"
fi

"${VENV_PY}" - <<'PY' "${CONFIG_PATH}" "${PROJECT_DIR_ABS}" "${INVOKE_DIR}" "${ROOT_DIR}/minion-swarm.yaml.example" "${PROJECT_DIR_EXPLICIT}"
from pathlib import Path
import sys
import yaml

cfg_path = Path(sys.argv[1])
project_dir_abs = sys.argv[2]
invoke_dir = str(Path(sys.argv[3]).resolve())
example_path = Path(sys.argv[4])
project_dir_explicit = sys.argv[5] == "1"

raw = yaml.safe_load(cfg_path.read_text()) or {}
if not isinstance(raw, dict):
    raise SystemExit("Config root must be a YAML mapping")

example = yaml.safe_load(example_path.read_text()) or {}
if not isinstance(example, dict):
    example = {}

if project_dir_explicit:
    project_dir = str(Path(project_dir_abs).resolve())
else:
    existing_project_dir = raw.get("project_dir")
    if existing_project_dir:
        existing_path = Path(str(existing_project_dir)).expanduser()
        if not existing_path.is_absolute():
            existing_path = (cfg_path.parent / existing_path).resolve()
        project_dir = str(existing_path)
    else:
        project_dir = invoke_dir

raw["project_dir"] = project_dir
raw.setdefault("dead_drop_dir", ".dead-drop")
raw.setdefault("dead_drop_db", "~/.dead-drop/messages.db")

if not isinstance(raw.get("agents"), dict) or not raw.get("agents"):
    example_agents = example.get("agents")
    if isinstance(example_agents, dict) and example_agents:
        raw["agents"] = example_agents
    else:
        raise SystemExit("No agents defined and no agents available in example config")

agents = raw.get("agents", {})
protocol_line = "Re-read .dead-drop/debug-protocol.md and BACKLOG.md before every task."
lead_policy_lines = [
    "Never delegate work without a written task file in `.dead-drop/tasks/<TASK-ID>/task.md`.",
    "Capture newly discovered ideas in `.dead-drop/BACKLOG.md`.",
    "After each completed task, check backlog and assign the next written task.",
]

for name, agent in list(agents.items()):
    if not isinstance(agent, dict):
        raise SystemExit(f"Agent '{name}' config must be a mapping")

    system = str(agent.get("system", "")).strip()
    if not system:
        role = str(agent.get("role", "coder"))
        system = (
            f"You are {name} ({role}) running under minion-swarm. "
            "Check dead-drop inbox, execute tasks, and report via dead-drop."
        )

    if "debug-protocol.md" not in system or "BACKLOG.md" not in system:
        system = f"{system}\n{protocol_line}"

    if name == "swarm-lead":
        for line in lead_policy_lines:
            if line not in system:
                system = f"{system}\n{line}"

    agent["system"] = system

cfg_path.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=False))
print(f"Patched config: {cfg_path}")
print(f"Effective project_dir: {project_dir}")
PY

if [[ "${CREATE_SYMLINK}" -eq 1 ]]; then
  BIN_DIR="${HOME}/.local/bin"
  mkdir -p "${BIN_DIR}"
  ln -sf "${ROOT_DIR}/minion-swarm" "${BIN_DIR}/minion-swarm"
  ln -sf "${ROOT_DIR}/run-minion.sh" "${BIN_DIR}/run-minion"
fi

echo ""
echo "Install complete."
echo "Config: ${CONFIG_PATH}"
echo ""
echo "Run from your target repo:"
echo "  cd /path/to/repo && run-minion swarm-lead"
echo ""
echo "If ~/.local/bin is not on PATH, use:"
echo "  ${ROOT_DIR}/run-minion.sh swarm-lead"
