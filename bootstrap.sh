#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${MINION_SWARM_REPO_URL:-https://github.com/ai-janitor/minion-swarm.git}"
INSTALL_DIR="${MINION_SWARM_INSTALL_DIR:-${HOME}/.minion-swarm/minion-swarm}"
PROJECT_DIR="${MINION_SWARM_PROJECT_DIR:-$(pwd)}"

usage() {
  cat <<'USAGE'
Usage: bootstrap.sh [options]

Options:
  --project-dir PATH  Target repo path for agent work (default: current dir).
  --install-dir PATH  Install/update minion-swarm clone path (default: ~/.minion-swarm/minion-swarm).
  --repo-url URL      Git URL to clone/update from.
  --help              Show this help text.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project-dir)
      PROJECT_DIR="$2"
      shift 2
      ;;
    --install-dir)
      INSTALL_DIR="$2"
      shift 2
      ;;
    --repo-url)
      REPO_URL="$2"
      shift 2
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

PROJECT_DIR="$(python3 - <<'PY' "${PROJECT_DIR}"
from pathlib import Path
import sys
print(str(Path(sys.argv[1]).expanduser()))
PY
)"

if [[ ! -d "${PROJECT_DIR}" ]]; then
  echo "Project directory not found: ${PROJECT_DIR}" >&2
  exit 2
fi
PROJECT_DIR="$(cd "${PROJECT_DIR}" && pwd)"

INSTALL_DIR="$(python3 - <<'PY' "${INSTALL_DIR}"
from pathlib import Path
import sys
print(str(Path(sys.argv[1]).expanduser()))
PY
)"

mkdir -p "$(dirname "${INSTALL_DIR}")"

if [[ -d "${INSTALL_DIR}/.git" ]]; then
  git -C "${INSTALL_DIR}" fetch --all --prune
  git -C "${INSTALL_DIR}" pull --ff-only
else
  git clone "${REPO_URL}" "${INSTALL_DIR}"
fi

"${INSTALL_DIR}/install.sh" --project-dir "${PROJECT_DIR}"
