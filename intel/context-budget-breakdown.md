# Context Budget Breakdown

Measured 2026-02-19 on Claude Code v2.1.47, claude-opus-4-6

## The Problem

Agents start at 35-68% HP before doing any work. On a 200k context window,
48k-129k tokens are consumed by system overhead.

## Token Budget (measured)

### Bare `claude -p` (no MCP, minimal prompt)

Total: **31,127 tokens** for "say hello"

| Layer | ~Tokens | Source |
|-------|---------|--------|
| Claude Code system prompt | 3,500 | Instructions, safety rules, formatting |
| 21 built-in tools | 10,000 | JSON schemas + descriptions |
| 6 agent type descriptions | 5,000 | Embedded in Task tool definition |
| 33 slash commands/skills | 5,000 | Skill triggers loaded from ~/.skills/ |
| CLAUDE.md + rules | 5,200 | Project + global + ~/.claude/rules/*.md |
| Plugin (clangd-lsp) | 2,000 | LSP tool definitions |
| **Total** | **~31,000** | **15.5% of 200k** |

### Agent with minion-comms MCP (what daemon spawns)

| Layer | ~Tokens | Source |
|-------|---------|--------|
| Bare claude overhead | 31,000 | See above |
| 38 minion-comms MCP tools | 17,000 | Each schema ~450 tokens |
| Daemon boot prompt | 500 | Protocol + rules + boot commands |
| **Total** | **~48,500** | **24% of 200k** |

### Measured at boot (4 agents, concurrent)

| Agent | Tokens | HP | Notes |
|-------|--------|-----|-------|
| thief (6 tools) | 64,048 | 68% | Restricted: Read,Glob,Grep,Bash,WebSearch,WebFetch |
| fighter (ALL) | 96,382 | 52% | All built-in tools |
| blackmage (ALL) | 96,478 | 52% | All built-in tools |
| whitemage (ALL) | 129,251 | 35% | All tools + cache creation overhead |

## Why Higher Than Expected

Estimated 48.5k but measured 64k-129k. Delta explained by:
1. **Cache creation tokens** counted at full weight (not discounted like cache reads)
2. **Concurrent boot** — 4 agents compete for cache, some create while others read
3. **Possible double-counting** in stream-json (known issue #6805)

## Recommendations

### 1. Restrict tools per agent role
Thief (6 tools) = 68% HP vs fighter (ALL) = 52%. Fewer tools = more context for work.

Suggested tool sets:
- **coder**: Bash, Read, Edit, Write, Glob, Grep
- **oracle**: Read, Glob, Grep, WebSearch, WebFetch
- **builder**: Bash, Read, Edit, Write, Glob, Grep
- **recon**: Read, Glob, Grep, Bash, WebSearch, WebFetch
- **lead**: ALL (needs Task, Team tools)

### 2. Drop minion-comms MCP for CLI agents
v2 agents use `minion` CLI via Bash — they don't need the MCP server.
Removing MCP saves ~17k tokens per agent.

### 3. Reduce skills/slash commands
33 slash commands loaded but agents never use them.
Use `--disallowed-tools` or a minimal profile.

### 4. Run from a minimal project dir
Agent work dir's CLAUDE.md + rules get loaded.
Consider a lightweight project dir for daemon agents.
