# Daemon Boot Hang — claude -p produces zero output

## Observed Behavior

All daemon agents spawn via minion-swarm but produce zero output.
Logs stop at `model-stream start`, then timeout after 360s (SIGTERM).

```
=== model-stream start: agent=fighter cmd=claude v=1 ts=11:05:40 ===
# ... 6 minutes of silence ...
[fighter] received signal 15, shutting down
=== model-stream end: agent=fighter cmd=claude v=1 shown=0 chars ===
```

## Root Cause

`--mcp-config <any-value>` combined with `stdin=subprocess.DEVNULL` causes
`claude -p` to hang silently (zero output, process alive but blocked).

The `disable_mcp` config (default=True) added `--mcp-config /dev/null --strict-mcp-config`.
Even `--mcp-config {}` (empty JSON file) triggers the same hang.

```bash
# HANGS — mcp-config + DEVNULL
Popen(['claude', '-p', 'hello', '--output-format', 'stream-json',
  '--verbose', '--mcp-config', '/dev/null', '--permission-mode', 'bypassPermissions'],
  stdin=subprocess.DEVNULL)

# WORKS — no --mcp-config flag
Popen(['claude', '-p', 'hello', '--output-format', 'stream-json',
  '--verbose', '--permission-mode', 'bypassPermissions'],
  stdin=subprocess.DEVNULL)
```

`stdin=DEVNULL` alone is fine. `--mcp-config` alone from a TTY is fine.
The combination hangs. Claude version: 2.1.47.

## Fix Applied

Removed `--mcp-config` and `--strict-mcp-config` flags from `_build_claude_command()`.
Claude works fine without MCP servers by default — no flag needed.

## Date

2026-02-19
