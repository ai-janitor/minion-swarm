# Trap: --mcp-config /dev/null hangs claude CLI

## Observed Behavior

`claude -p` with `--mcp-config /dev/null` produces zero output and hangs indefinitely.
Adding `--strict-mcp-config` makes no difference — both flags together also hang.

Confirmed on claude 2.1.47. Process stays alive, zero bytes on stdout, no error message.
After the daemon's `no_output_timeout_sec` (default 360s), it gets SIGTERM'd.

## Reproduction

```bash
# HANGS — zero output
CLAUDECODE= claude -p "say hello" --output-format stream-json --verbose \
  --mcp-config /dev/null --permission-mode bypassPermissions

# WORKS — same command without --mcp-config
CLAUDECODE= claude -p "say hello" --output-format stream-json --verbose \
  --permission-mode bypassPermissions
```

## Root Cause

`/dev/null` is not valid JSON. The MCP config parser reads it, gets empty content,
and enters a blocking state instead of failing with an error.

## Safe Alternative

If you need to disable MCP servers, write an empty JSON file:
```bash
echo '{}' > ~/.minion-swarm/no-mcp.json
claude -p "..." --mcp-config ~/.minion-swarm/no-mcp.json
```

Or simply don't pass `--mcp-config` at all — claude runs fine without MCP servers.

## Context

This was introduced as an uncommitted `disable_mcp` feature (defaulting to True)
in the daemon command builder. Since all daemon agents had it enabled by default,
every agent hung at boot. Reverting the uncommitted changes fixed it.

The committed codebase never had `disable_mcp` — agents boot fine without any
`--mcp-config` flag.

## Date

2026-02-19
