#!/usr/bin/env bash
# Generate and persist an OAuth token for headless daemon use.
#
# Flow:
#   1. Runs `claude setup-token` (interactive — opens browser)
#   2. After auth succeeds, prompts you to paste the token
#   3. Writes it to ~/.claude/oauth-token (chmod 600)
#
# The ts-daemon reads this file on startup — no env var needed.
set -euo pipefail

TOKEN_FILE="$HOME/.claude/oauth-token"

echo "=== minion-swarm daemon auth setup ==="
echo ""
echo "This will run 'claude setup-token' to generate an OAuth token."
echo "After the browser flow completes, paste the token here."
echo ""

# Run setup-token (interactive)
claude setup-token

echo ""
echo "Paste the token below (starts with sk-ant-oat01-):"
read -r TOKEN

if [[ ! "$TOKEN" =~ ^sk-ant- ]]; then
  echo "ERROR: Token doesn't look right (expected sk-ant-... prefix)" >&2
  exit 1
fi

# Write with restricted permissions
mkdir -p "$(dirname "$TOKEN_FILE")"
echo -n "$TOKEN" > "$TOKEN_FILE"
chmod 600 "$TOKEN_FILE"

echo ""
echo "Token saved to $TOKEN_FILE"
echo "The daemon will read it automatically on startup."
