#!/usr/bin/env bash
set -euo pipefail

# minion-swarm bootstrap installer
# Usage: curl -fsSL https://raw.githubusercontent.com/ai-janitor/minion-swarm/main/bootstrap.sh | bash
#    or: curl -fsSL ... | bash -s -- --project-dir /path/to/repo

REPO_URL="${MINION_SWARM_REPO_URL:-https://github.com/ai-janitor/minion-swarm.git}"
INSTALL_DIR="${MINION_SWARM_INSTALL_DIR:-${HOME}/.minion-swarm/minion-swarm}"
PROJECT_DIR="${MINION_SWARM_PROJECT_DIR:-}"
INSTALL_ARGS=()

# ── Output helpers ───────────────────────────────────────────────────────────

info()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()    { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn()  { printf '\033[1;33m==>\033[0m %s\n' "$*"; }
die()   { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# ── Args ─────────────────────────────────────────────────────────────────────

usage() {
  cat <<'USAGE'
Usage: bootstrap.sh [options]

Options:
  --project-dir PATH     Target repo path for agent work (default: current dir).
  --install-dir PATH     Clone path (default: ~/.minion-swarm/minion-swarm).
  --repo-url URL         Git URL to clone/update from.
  --overwrite-config     Replace existing config YAML before patching.
  --no-symlink           Do not install launcher symlinks to ~/.local/bin.
  --help                 Show this help text.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project-dir)
      PROJECT_DIR="$2"
      INSTALL_ARGS+=(--project-dir "$2")
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
    --overwrite-config)
      INSTALL_ARGS+=(--overwrite-config)
      shift
      ;;
    --no-symlink)
      INSTALL_ARGS+=(--no-symlink)
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      die "Unknown argument: $1"
      ;;
  esac
done

# ── Resolve paths ────────────────────────────────────────────────────────────

command -v python3 &>/dev/null || die "python3 required but not found."
command -v git &>/dev/null || die "git required but not found."

resolve_path() {
  python3 -c "from pathlib import Path; import sys; print(str(Path(sys.argv[1]).expanduser().resolve()))" "$1"
}

INSTALL_DIR="$(resolve_path "${INSTALL_DIR}")"

if [[ -n "${PROJECT_DIR}" ]]; then
  PROJECT_DIR="$(resolve_path "${PROJECT_DIR}")"
  [[ -d "${PROJECT_DIR}" ]] || die "Project directory not found: ${PROJECT_DIR}"
fi

# ── Step 1: Clone or update ─────────────────────────────────────────────────

mkdir -p "$(dirname "${INSTALL_DIR}")"

if [[ -d "${INSTALL_DIR}/.git" ]]; then
  info "Updating minion-swarm (${INSTALL_DIR})..."
  git -C "${INSTALL_DIR}" fetch --all --prune --quiet
  git -C "${INSTALL_DIR}" pull --ff-only --quiet \
    || warn "Pull failed (diverged branch?) — using existing checkout"
  ok "Updated to latest"
else
  info "Cloning minion-swarm to ${INSTALL_DIR}..."
  git clone --quiet "${REPO_URL}" "${INSTALL_DIR}" \
    || die "git clone failed. Check network and repo URL."
  ok "Cloned minion-swarm"
fi

# ── Step 2: Run installer ───────────────────────────────────────────────────

info "Running install.sh..."
"${INSTALL_DIR}/install.sh" "${INSTALL_ARGS[@]+"${INSTALL_ARGS[@]}"}"

# ── Step 3: Check PATH ───────────────────────────────────────────────────────

if ! command -v minion-swarm &>/dev/null || ! command -v run-minion &>/dev/null; then
  warn "minion-swarm/run-minion not found on PATH."
  echo ""
  warn "Add this to your shell config and restart your terminal:"
  if [[ "${SHELL:-}" == */zsh ]]; then
    warn "  echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.zshrc"
  elif [[ "${SHELL:-}" == */bash ]]; then
    warn "  echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.bashrc"
  else
    warn "  echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.profile"
  fi
  echo ""
fi

# ── Done ─────────────────────────────────────────────────────────────────────

echo ""
ok "minion-swarm installed!"
echo ""
echo "  Clone:   ${INSTALL_DIR}"
echo "  Config:  ~/.minion-swarm/minion-swarm.yaml"
echo ""
echo "  Usage:"
echo "    cd /path/to/repo && run-minion swarm-lead"
echo "    minion-swarm start swarm-lead"
echo "    minion-swarm status"
echo "    minion-swarm logs swarm-lead --lines 0"
echo ""
