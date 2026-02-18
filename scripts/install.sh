#!/usr/bin/env bash
set -euo pipefail

# minion-swarm installer
# Usage: curl -sSL https://raw.githubusercontent.com/ai-janitor/minion-swarm/main/scripts/install.sh | bash

REPO="https://github.com/ai-janitor/minion-swarm.git"

info()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()    { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn()  { printf '\033[1;33m==>\033[0m %s\n' "$*"; }
die()   { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# ── Step 1: Install the Python package ──────────────────────────────────────

info "Installing minion-swarm..."

if command -v pipx &>/dev/null; then
    info "Using pipx (isolated environment)"
    pipx install "git+${REPO}" --force 2>/dev/null \
        || pipx install "git+${REPO}" 2>/dev/null \
        || die "pipx install failed. Check Python 3.10+ is available."
elif command -v uv &>/dev/null; then
    info "Using uv"
    uv tool install "git+${REPO}" --force 2>/dev/null \
        || uv tool install "git+${REPO}" 2>/dev/null \
        || die "uv tool install failed."
elif command -v pip &>/dev/null; then
    warn "pipx/uv not found — falling back to pip (may pollute global env)"
    pip install "git+${REPO}" --user --break-system-packages 2>/dev/null \
        || pip install "git+${REPO}" --user 2>/dev/null \
        || pip install "git+${REPO}" 2>/dev/null \
        || die "pip install failed. Install pipx first: python3 -m pip install --user pipx"
else
    die "No Python package manager found. Install pipx: https://pipx.pypa.io"
fi

# Verify the command exists and PATH is set up
if ! command -v minion-swarm &>/dev/null; then
    warn "minion-swarm not found on PATH."
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

# ── Step 2: Initialize config ───────────────────────────────────────────────

info "Initializing config..."

if command -v minion-swarm &>/dev/null; then
    minion-swarm init 2>/dev/null \
        && ok "Config initialized" \
        || warn "minion-swarm init failed — run manually: minion-swarm init"
else
    warn "Skipping config init (minion-swarm not on PATH yet)"
fi

# ── Done ────────────────────────────────────────────────────────────────────

echo ""
ok "minion-swarm installed!"
echo ""
echo "  Config:  ~/.minion-swarm/minion-swarm.yaml"
echo ""
echo "  Usage:"
echo "    cd /path/to/repo && run-minion swarm-lead"
echo "    minion-swarm start swarm-lead"
echo "    minion-swarm status"
echo "    minion-swarm logs swarm-lead --lines 0"
echo ""
