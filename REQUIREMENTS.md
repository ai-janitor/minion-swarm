# minion-swarm

Autonomous multi-agent daemon that runs Claude Code agents as background processes, coordinated through minion-comms CLI (`minion <subcommand>`).

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
            │ minion CLI  │
            │ + SQLite +  │
            │ filesystem  │
            └─────────────┘
                   │
            ┌──────┴──────┐
            │    lead     │
            │  (human or  │
            │   claude)   │
            └─────────────┘
```

## Core Design Decisions

1. **CLI-based comms, not MCP** — Agents communicate via `minion <subcommand>` CLI (Bash tool). No MCP server process, no persistent connections. Each call is stateless. Agents use `minion register`, `minion check-inbox`, `minion send`, etc. through the Bash tool.

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
# minion-swarm.yaml
project_dir: /Users/hung/projects/myproject

agents:
  opus-engineer:
    role: coder
    zone: "Zone A: backend"
    system: |
      You are opus-engineer, an autonomous backend engineer.
      NEVER use AskUserQuestion. Route all questions via: minion send --from opus-engineer --to lead --message "..."
      Send ONE summary when done.
    allowed_tools: null  # all tools (default)
    model: null  # use default

  codex:
    role: reviewer
    zone: "Code review"
    system: |
      You are codex, a code reviewer.
      Send feedback via: minion send --from codex --to <coder> --message "..."
    allowed_tools: "Read,Glob,Grep,Bash"  # read-only + CLI messaging
```

## CLI Interface

```bash
# Start all agents
minion-swarm start

# Start one agent
minion-swarm start opus-engineer

# Stop all agents
minion-swarm stop

# Stop one agent
minion-swarm stop opus-engineer

# Show agent status (PID, current task, last message)
minion-swarm status

# Tail an agent's live output
minion-swarm logs opus-engineer

# Send a message as lead
minion-swarm send opus-engineer "Do PERF-015"
```

## Key Implementation Details

### Inbox Polling
- Daemon runs `poll.sh <agent> --interval 5 --timeout 30` to block until messages arrive
- poll.sh queries SQLite directly — zero API cost when idle
- Exit codes: 0 = messages, 1 = timeout (no messages), 3 = stand_down (leader dismissed)

### Process Management
- Each agent runs as a subprocess: `claude -p "<prompt>" --output-format stream-json`
- Daemon captures stdout/stderr and logs to `.minion-swarm/logs/<agent>.log`
- Also tees to terminal when `minion-swarm logs <agent>` is running
- PID file in `.minion-swarm/pids/<agent>.pid`
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

<protocol section — how to use minion CLI>

<rules section — daemon autonomy rules>

════════════════════ RECENT HISTORY (rolling buffer) ════════════════════
(only after compaction detected)
═══════════════════════ END RECENT HISTORY ═════════════════════════════

You have new messages. Check your inbox:
  minion check-inbox --agent {agent_name}
Read and process all messages, then send results:
  minion send --from {agent_name} --to <recipient> --message "..."
```

### Boot Prompt
First invocation runs the ON STARTUP block from the crew YAML system prompt. The daemon injects:
- System prompt (identity, role, zone)
- Protocol section (how to use `minion` CLI for comms)
- Rules section (no AskUserQuestion, send results when done)
- Boot instruction: "Execute your ON STARTUP instructions now"

ON STARTUP should register, set context, check inbox, set status — all via `minion` CLI through Bash tool.

### Human-Readable Output
CLI output injected into agent context must be concise text, not raw JSON. Agents don't parse JSON — they read prompts. `minion register` and `minion cold-start` should return a compact text summary (tool list as a table, triggers as a table, playbook as bullet points) instead of verbose JSON blobs that waste context tokens. The `--human` flag or a `--agent` output mode should produce this format.

### No AskUserQuestion
Agents MUST NOT use `AskUserQuestion` — it blocks waiting for terminal input, which never comes in `-p` mode. All questions go through `minion send`. This is enforced in the system prompt.

### Error Recovery
- If `claude -p` exits non-zero: log error, wait 30s, retry
- If 3 consecutive failures: stop agent, alert lead via minion send
- If agent produces no output for 10min: timeout, kill, retry

## File Structure

```
minion-swarm/
  minion_swarm/
    __init__.py
    cli.py          # click CLI (start, stop, status, logs, send)
    daemon.py       # agent daemon (watch + invoke claude -p)
    watcher.py      # filesystem watcher for dead-drop messages
    config.py       # YAML config loader
  minion-swarm.yaml.example
  requirements.txt  # click, watchdog, pyyaml
  README.md
```

## Agent-to-CLI Communication

Agents use `minion` CLI commands via the Bash tool. No MCP server needed.

### Boot sequence (ON STARTUP)
```bash
minion register --name <agent> --class <role> --transport daemon
minion set-context --agent <agent> --context "just started"
minion check-inbox --agent <agent>
minion set-status --agent <agent> --status "ready for orders"
```

### Work cycle
```bash
minion check-inbox --agent <agent>          # read messages
# ... do the work ...
minion send --from <agent> --to <recipient> --message "done: <summary>"
```

### Protocol section injection
The daemon builds a protocol section that tells agents how to communicate. This replaces MCP tool documentation — agents learn the CLI interface from the prompt, not from tool schemas.

## Watcher as External Memory

The watcher daemon is the agent's memory that survives compaction. It captures `stream-json` output and re-injects context into the next prompt cycle. This is a general-purpose injection point — not just for tool discovery:

- **Tool catalog** — from `minion register` response, re-injected after compaction
- **Battle plan** — current session goals, re-injected so agent doesn't lose the mission
- **Zone assignment** — what files/modules the agent owns
- **Recent findings** — intel, traps, or oracle answers relevant to current task
- **Any state the watcher decides the agent needs** — codifiable, extensible

Terminal agents (human CLI) handle this themselves via `cold_start`. Daemon agents get it from the watcher. See minion-comms FRAMEWORK.md "Tool Discovery" section.

## Open Questions

1. **Cost tracking** — Each `claude -p` call has a cost. Should we track cumulative spend per agent and enforce budgets?

2. **Lead agent** — Should the lead also be a daemon, or always human? A lead daemon could do planning, break tasks, and assign — full autonomy.
