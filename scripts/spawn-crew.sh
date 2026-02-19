#!/usr/bin/env bash
set -euo pipefail

# Open OS terminal windows for each member of a crew.
# Usage: spawn-crew <crew-name> [project-dir]
#
# The crew YAML lives in crews/<name>.yaml (relative to this repo).
# One special agent "fighter" is always spawned as an interactive claude session.
# All agents defined in the YAML are spawned as minion-swarm daemons with log tailing.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

CREW_NAME="${1:?Usage: spawn-crew <crew-name> [project-dir]}"
PROJECT_DIR="${2:-$(pwd)}"
PROJECT_DIR="$(cd "$PROJECT_DIR" && pwd)"

# Search order: repo crews/, then ~/.minion-swarm/crews/
CREW_FILE=""
for dir in "$REPO_DIR/crews" "$HOME/.minion-swarm/crews"; do
  if [[ -f "$dir/${CREW_NAME}.yaml" ]]; then
    CREW_FILE="$dir/${CREW_NAME}.yaml"
    break
  fi
done

if [[ -z "$CREW_FILE" ]]; then
  echo "Error: crew '${CREW_NAME}' not found" >&2
  echo "Searched: $REPO_DIR/crews/, ~/.minion-swarm/crews/" >&2
  echo "Available crews:" >&2
  for dir in "$REPO_DIR/crews" "$HOME/.minion-swarm/crews"; do
    ls "$dir"/*.yaml 2>/dev/null | xargs -I{} basename {} .yaml | sed 's/^/  /'
  done >&2
  exit 1
fi

command -v minion-swarm &>/dev/null || { echo "Error: minion-swarm not found. Run: scripts/install.sh" >&2; exit 2; }

# Write crew config to a temp location that minion-swarm can use
CREW_CONFIG="$HOME/.minion-swarm/${CREW_NAME}.yaml"
mkdir -p "$HOME/.minion-swarm"
cp "$CREW_FILE" "$CREW_CONFIG"

# Patch project_dir in the crew config
if command -v sed &>/dev/null; then
  sed -i.bak "s|^project_dir:.*|project_dir: ${PROJECT_DIR}|" "$CREW_CONFIG"
  rm -f "${CREW_CONFIG}.bak"
fi

# Seed runtime dirs
minion-swarm init --config "$CREW_CONFIG" --project-dir "$PROJECT_DIR" >/dev/null 2>&1 || true

# Extract agent names from YAML (agents listed under 'agents:' key)
AGENTS=$(python3 -c "
import yaml, sys
with open('$CREW_FILE') as f:
    cfg = yaml.safe_load(f)
for name in cfg.get('agents', {}):
    print(name)
")

# Fighter system prompt (interactive lead)
FIGHTER_SYSTEM="You are fighter (coder class), the party lead for the ${CREW_NAME} crew.
You coordinate the party through dead-drop messages.
Your teammates: $(echo $AGENTS | tr '\n' ', ' | sed 's/,$//'). They are minion-swarm daemons.
Use dead-drop send to assign tasks and dead-drop check_inbox to read replies.
The human is the raid lead — follow their orders."

# --- OS Terminal Spawning ---

open_terminal_macos() {
  local title="$1"
  local cmd="$2"
  osascript <<EOF
tell application "Terminal"
  activate
  set newTab to do script "${cmd}"
  set custom title of front window to "${title}"
end tell
EOF
}

open_terminal_linux() {
  local title="$1"
  local cmd="$2"
  if command -v gnome-terminal &>/dev/null; then
    gnome-terminal --title="$title" -- bash -c "$cmd; exec bash"
  elif command -v xterm &>/dev/null; then
    xterm -T "$title" -e "$cmd" &
  elif command -v x-terminal-emulator &>/dev/null; then
    x-terminal-emulator -T "$title" -e "$cmd" &
  else
    echo "Error: no supported terminal emulator found (tried gnome-terminal, xterm, x-terminal-emulator)" >&2
    exit 3
  fi
}

open_terminal() {
  local title="$1"
  local cmd="$2"
  case "$(uname -s)" in
    Darwin) open_terminal_macos "$title" "$cmd" ;;
    Linux)  open_terminal_linux "$title" "$cmd" ;;
    *)      echo "Error: unsupported OS: $(uname -s)" >&2; exit 3 ;;
  esac
}

echo "=== Spawning ${CREW_NAME} crew ==="
echo "Project: $PROJECT_DIR"
echo ""

# Terminal 1: fighter (interactive claude session)
echo "[fighter] Interactive claude session"
FIGHTER_CMD="cd ${PROJECT_DIR} && claude --system-prompt '${FIGHTER_SYSTEM//\'/\\'\\'}'"
open_terminal "fighter" "$FIGHTER_CMD"

# Terminals 2-N: daemon agents with log tailing
for agent in $AGENTS; do
  echo "[${agent}] Daemon + log tail"
  AGENT_CMD="cd ${PROJECT_DIR} && minion-swarm start ${agent} --config ${CREW_CONFIG} && exec minion-swarm logs ${agent} --config ${CREW_CONFIG} --lines 0"
  open_terminal "$agent" "$AGENT_CMD"
done

echo ""
echo "=== All terminals spawned ==="
echo "Config: $CREW_CONFIG"
echo ""
echo "Controls:"
echo "  minion-swarm status --config $CREW_CONFIG"
echo "  minion-swarm stop <agent> --config $CREW_CONFIG"
echo "  minion-swarm stop --config $CREW_CONFIG   # stop all"
