# Claude Code Tool Overhead — Token Costs

Measured 2026-02-19 from stream-json on Claude Code v2.1.47

## Total System Overhead

A minimal "say hello" prompt consumes **~31k tokens** before the user's prompt:
- `cache_read_input_tokens`: 22,239 (stable system prompt, cached)
- `cache_creation_input_tokens`: 8,885 (session-specific, newly cached)
- `input_tokens`: 3 (actual user prompt)

## What's In The Overhead

The `system` init event lists what's loaded:
- **Built-in tools**: Task, TaskOutput, Bash, Glob, Grep, Read, Edit, Write, NotebookEdit, WebFetch, WebSearch, TodoWrite, TaskStop, AskUserQuestion, Skill, EnterPlanMode, ExitPlanMode, TeamCreate, TeamDelete, SendMessage, ToolSearch (21 tools)
- **Agent types**: Bash, general-purpose, statusline-setup, Explore, Plan, claude-code-guide
- **Skills**: keybindings-help, debug, claude-developer-platform + 28 slash commands
- **Plugins**: clangd-lsp
- **MCP servers**: [] (none in test)
- **CLAUDE.md + rules**: loaded from project and ~/.claude/

## Per-Tool Estimates (approximate)

Each tool's JSON schema + description contributes to the overhead.
These are rough estimates — actual values depend on Claude Code version.

| Tool | ~Tokens | Notes |
|------|---------|-------|
| Task | 2,500 | Largest — all agent type descriptions |
| TeamCreate | 1,500 | Team workflow docs |
| EnterPlanMode | 800 | When-to-use heuristics |
| SendMessage | 800 | Message type docs |
| Grep | 500 | Regex syntax docs |
| AskUserQuestion | 500 | Multi-select option docs |
| TaskCreate | 500 | Field descriptions |
| TaskUpdate | 500 | Status workflow |
| Bash | 400 | Safety rules |
| Edit | 400 | Usage constraints |
| Read | 350 | Format docs |
| NotebookEdit | 300 | Cell editing |
| WebFetch | 300 | URL handling |
| Skill | 300 | Invocation rules |
| TaskList | 300 | Teammate workflow |
| ExitPlanMode | 300 | Plan approval |
| WebSearch | 250 | Search docs |
| Write | 250 | Safety rules |
| Glob | 200 | Pattern docs |
| TaskGet | 200 | Output format |
| TaskOutput | 200 | Blocking behavior |
| TaskStop | 100 | Simple |
| TeamDelete | 100 | Simple |

**Base system prompt**: ~3,500 tokens (instructions, rules, formatting)
**Project overhead** (CLAUDE.md + rules): ~4,000 tokens (varies per project)

## Implications for Agent HP

- With all tools: ~31k overhead = 15.5% of 200k context consumed before any work
- With `--allowed-tools "Bash Read Edit Glob Grep"`: significantly less
- MCP tools add per-tool (each tool schema is ~200-500 tokens)
- The API reports `contextWindow` in `modelUsage` — use that for the limit

## How Daemon Tracks This

1. Parse `result` event from stream-json for `modelUsage`
2. Total context = `inputTokens + cacheCreationInputTokens + cacheReadInputTokens`
3. Limit = `modelUsage[model].contextWindow`
4. Write via `minion update-hp --agent X --input-tokens Y --output-tokens Z --limit W`
