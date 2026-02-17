# claude-swarm

Autonomous multi-agent daemon that runs Claude Code agents as background processes, coordinated through dead-drop MCP messaging.

## Problem

Today we run agents manually — one Claude Code interactive session per agent, human copy-pasting tasks. This doesn't scale. We want N agents running autonomously, each watching for messages, doing work, and reporting back. The human (lead) steers by sending messages, not by babysitting terminals.

## Architecture

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│ opus-engineer│     │  opus-q1    │     │   codex     │
│  (daemon)   │     │  (daemon)   │     │  (daemon)   │
│             │     │             │     │             │
│ claude -p   │     │ claude -p   │     │ claude -p   │
│ --continue  │     │ --continue  │     │ --continue  │
└──────┬──────┘     └──────┬──────┘     └──────┬──────┘
       │                   │                   │
       └───────────┬───────┘───────────────────┘
                   │
            ┌──────┴──────┐
            │  dead-drop  │
            │  MCP server │
            │  (shared)   │
            └─────────────┘
                   │
            ┌──────┴──────┐
            │    lead     │
            │  (human or  │
            │   claude)   │
            └─────────────┘
```

## Core Design Decisions

1. **Filesystem watch, not polling** — Use `watchdog` (Python) to monitor `.dead-drop/` for changes. Zero API cost when idle. Agent only wakes when a message arrives.

2. **Queue, don't interrupt** — If a message arrives while the agent is mid-task, it queues. Agent finishes current work, then checks inbox on next cycle.

3. **Sliding window context** — Use `claude -p --continue` with `--output-format stream-json` so the daemon captures all output. The daemon maintains a rolling buffer of the last N tokens (configurable, default 100k) of raw conversation history. When Claude Code's auto-compaction fires (summarizing old context), the daemon re-injects the recent buffer into the next prompt. Result: compacted summary of old work + verbatim recent history. The agent never loses what it just did.

   ```
   ┌──────────────────────────────────────────────┐
   │           Claude Code context window          │
   │                                               │
   │  [compacted summary]  [daemon's raw buffer]   │
   │   old work, lossy      last N tokens, exact   │
   └──────────────────────────────────────────────┘
   ```

   Every cycle also re-reads protocol files to compensate for any remaining compaction memory loss.

4. **Python daemon** — Process management, fswatch, logging, signal handling. One Python process per agent.

5. **Streaming to terminal** — `claude -p --output-format stream-json` for the daemon to capture history, but the daemon parses the stream and renders readable text to the terminal in real-time. Human sees clean text output, daemon sees structured JSON for the rolling buffer.

## Agent Lifecycle

```
START
  │
  ▼
IDLE (watching filesystem)
  │
  ├── message arrives ──▶ WORKING
  │                         │
  │                         ├── needs clarification ──▶ send msg to lead ──▶ IDLE
  │                         │
  │                         ├── task complete ──▶ send summary ──▶ check inbox
  │                         │                                        │
  │                         │                                        ├── more messages ──▶ WORKING
  │                         │                                        └── empty ──▶ IDLE
  │                         │
  │                         └── error/crash ──▶ log error ──▶ IDLE (retry backoff)
  │
  └── SIGTERM ──▶ graceful shutdown (wait for current task, then exit)
```

## Agent Config

Each agent is defined in a YAML config:

```yaml
# claude-swarm.yaml
dead_drop_dir: .dead-drop
project_dir: /Users/hung/projects/TTS.cpp

agents:
  opus-engineer:
    role: coder
    zone: "Zone A: Metal backend (ggml-metal.m, ggml-metal.metal)"
    system: |
      You are opus-engineer, an autonomous Metal backend engineer.
      Re-read .dead-drop/debug-protocol.md and BACKLOG.md before every task.
      NEVER use AskUserQuestion. Route all questions through dead-drop send to opus-orange.
      Write work to .dead-drop/opus-engineer/. Send ONE summary when done.
    allowed_tools: null  # all tools (default)
    model: null  # use default

  opus-q1:
    role: coder
    zone: "Zone B: Kernels and fusion (ggml-metal.metal, kernel dispatch)"
    system: |
      You are opus-q1, an autonomous kernel engineer.
      ...

  codex:
    role: reviewer
    zone: "Code review"
    system: |
      You are codex, a code reviewer.
      You receive code for review, provide feedback via dead-drop send.
      You can send fixes directly to the coder and CC opus-orange.
    allowed_tools: "Read,Glob,Grep,mcp__dead-drop__*"  # read-only + messaging
```

## CLI Interface

```bash
# Start all agents
claude-swarm start

# Start one agent
claude-swarm start opus-engineer

# Stop all agents
claude-swarm stop

# Stop one agent
claude-swarm stop opus-engineer

# Show agent status (PID, current task, last message)
claude-swarm status

# Tail an agent's live output
claude-swarm logs opus-engineer

# Send a message as lead
claude-swarm send opus-engineer "Do PERF-015"
```

## Key Implementation Details

### Filesystem Watcher
- Watch the dead-drop MCP's data directory for new messages
- Filter: only wake when a message is addressed to this agent
- Use Python `watchdog` library (cross-platform, battle-tested)
- Debounce: 1s after last file change before waking agent

### Process Management
- Each agent runs as a subprocess: `claude -p "<system + task>" --continue`
- Daemon captures stdout/stderr and logs to `.claude-swarm/logs/<agent>.log`
- Also tees to terminal when `claude-swarm logs <agent>` is running
- PID file in `.claude-swarm/pids/<agent>.pid`
- Graceful shutdown: SIGTERM → wait for claude process to finish → exit

### Context Management (Sliding Window)
- Daemon captures all `stream-json` output into a circular buffer
- Buffer holds last N tokens (default 100k, configurable per agent)
- Token counting: approximate with `len(text) / 4` (good enough for buffer sizing)
- On each cycle, if session was `--continue`'d, the daemon checks if compaction occurred (context shrank significantly between cycles)
- If compaction detected: next prompt prepends the raw buffer as a `<prior-context>` block
- Buffer is FIFO — oldest tokens drop off as new ones arrive
- The buffer stores assistant responses AND tool results (not just text output)

### The Prompt
Each cycle, the agent gets:
```
<system prompt from config>

════════════════════ RECENT HISTORY ({N} tokens) ════════════════════
The following is your raw conversation history from before context
compaction. Use it to recall what you were just working on.
Do NOT re-execute completed work.
═════════════════════════════════════════════════════════════════════

{raw rolling buffer}

═══════════════════════ END RECENT HISTORY ════════════════════════

Check your dead-drop inbox (agent name: {agent_name}).
If there are messages, pick the first one and execute the task.
If no messages, respond with IDLE.
```

### No AskUserQuestion
Agents MUST NOT use `AskUserQuestion` — it blocks waiting for terminal input, which never comes in `-p` mode. All questions go through dead-drop `send`. This is enforced in the system prompt.

### Error Recovery
- If `claude -p` exits non-zero: log error, wait 30s, retry
- If 3 consecutive failures: stop agent, alert lead via dead-drop
- If agent produces no output for 10min: timeout, kill, retry

### MCP Server Lifecycle
- The dead-drop MCP server must be running before agents start
- `claude-swarm start` checks for MCP server, starts it if needed
- MCP config must be in the project's `.claude/settings.json`

## File Structure

```
claude-swarm/
  claude_swarm/
    __init__.py
    cli.py          # click CLI (start, stop, status, logs, send)
    daemon.py       # agent daemon (watch + invoke claude -p)
    watcher.py      # filesystem watcher for dead-drop messages
    config.py       # YAML config loader
  claude-swarm.yaml.example
  requirements.txt  # click, watchdog, pyyaml
  README.md
```

## Open Questions

1. **tmux/screen integration?** — Instead of separate terminals, auto-create a tmux session with one pane per agent? `claude-swarm start --tmux` could set this up.

2. **Cost tracking** — Each `claude -p` call has a cost. Should we track cumulative spend per agent and enforce budgets?

3. **Task deduplication** — If the same message arrives twice (MCP retry, etc.), should the daemon deduplicate?

4. **Lead agent** — Should the lead also be a daemon, or always human? A lead daemon could do planning, break tasks, and assign — full autonomy.

5. **Cross-project** — Right now this is TTS.cpp-specific. Should the config support multiple projects with different agent pools?
