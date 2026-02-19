# Claude Code stream-json Token Format

Captured from `claude -p "say the word hello" --output-format stream-json --verbose`
Date: 2026-02-19

## Event Types

### 1. `rate_limit_event` (first)
No token data.

### 2. `system` / `init` (second)
Contains tool list, MCP servers, model, context but no token counts.
```json
{
  "type": "system",
  "subtype": "init",
  "tools": ["Task", "Bash", "Read", "Edit", ...],
  "model": "claude-opus-4-6",
  "mcp_servers": []
}
```

### 3. `assistant` (per API turn)
Token usage split across cache fields:
```json
{
  "type": "assistant",
  "message": {
    "usage": {
      "input_tokens": 3,
      "cache_creation_input_tokens": 8885,
      "cache_read_input_tokens": 22239,
      "output_tokens": 1
    }
  }
}
```

### 4. `result` (final)
Aggregated totals + `modelUsage` with `contextWindow`:
```json
{
  "type": "result",
  "total_cost_usd": 0.06676575,
  "usage": {
    "input_tokens": 3,
    "cache_creation_input_tokens": 8885,
    "cache_read_input_tokens": 22239,
    "output_tokens": 4
  },
  "modelUsage": {
    "claude-opus-4-6": {
      "inputTokens": 3,
      "outputTokens": 4,
      "cacheReadInputTokens": 22239,
      "cacheCreationInputTokens": 8885,
      "contextWindow": 200000,
      "maxOutputTokens": 32000,
      "costUSD": 0.06676575
    }
  }
}
```

## Key Findings

1. **Total context consumed** = `input_tokens` + `cache_creation_input_tokens` + `cache_read_input_tokens`
   - For a trivial "say hello" prompt: 3 + 8885 + 22239 = **31,127 tokens**
   - The 31k is Claude Code's system prompt + tool definitions + CLAUDE.md

2. **`input_tokens` alone is misleading** — it's only the non-cached portion (often single digits)

3. **`modelUsage` uses camelCase** (not snake_case like `usage`)
   - `inputTokens` vs `input_tokens`
   - `cacheReadInputTokens` vs `cache_read_input_tokens`

4. **`contextWindow` is in `modelUsage`** — the API tells us the actual limit per model

5. **System overhead breakdown** (from a minimal "say hello" prompt):
   - Cache read: 22,239 tokens (previously cached system prompt)
   - Cache creation: 8,885 tokens (new tokens being cached)
   - User prompt: 3 tokens
   - Total system overhead: ~31k tokens on a 200k context window

## HP Tracking Strategy

- Parse the `result` event (last line) for `modelUsage`
- Total input = `inputTokens + cacheCreationInputTokens + cacheReadInputTokens`
- Limit = `contextWindow` from `modelUsage`
- HP% = (limit - total_input) / limit * 100
- Use last-wins (not accumulation) since result event has session totals
- Session-level: sum across invocations since each depletes context differently
