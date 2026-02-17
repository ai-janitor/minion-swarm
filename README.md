# claude-swarm

Autonomous multi-agent daemon runner coordinated through dead-drop.
Supported providers: `claude`, `codex`, `opencode`, `gemini`.

## Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Configure

```bash
cp claude-swarm.yaml.example claude-swarm.yaml
# edit values for your project and agents
```

Each agent can choose a provider:

```yaml
agents:
  opus-engineer:
    provider: claude
  codex:
    provider: codex
  opencode-helper:
    provider: opencode
  gemini-helper:
    provider: gemini
```

Provider command mapping:
- `claude`: `claude -p ... --output-format stream-json --continue`
- `codex`: `codex exec --json ...` and resume via `codex exec resume --last --json ...`
- `opencode`: `opencode run --format json ...` and resume via `opencode run --continue --format json ...`
- `gemini`: `gemini --prompt ... --output-format stream-json` and resume via `gemini --resume latest --prompt ... --output-format stream-json`

For Claude agents, you can set:
- `allowed_tools`: pass-through to `claude --allowed-tools`
- `permission_mode`: pass-through to `claude --permission-mode` (for autonomous daemons, `bypassPermissions` avoids interactive permission blocks)

## Run

```bash
python -m claude_swarm.cli start
python -m claude_swarm.cli status
python -m claude_swarm.cli logs opus-engineer
python -m claude_swarm.cli send opus-engineer "Do PERF-015"
python -m claude_swarm.cli stop
```

Runtime state is written under `.claude-swarm/` in the configured `project_dir`.

## One Command Runner

```bash
./run-agent.sh
```

Defaults to `opus-engineer` and `./claude-swarm.yaml`.

```bash
./run-agent.sh <agent-name> <config-path>
```

Press `Ctrl+C` to stop and clean up that agent.
Set `LEAVE_RUNNING=1` to exit logs without stopping the agent.
