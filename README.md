# minion-swarm

Autonomous multi-agent daemon runner coordinated through dead-drop.

Supported providers:
- `claude`
- `codex`
- `opencode`
- `gemini`

## Install

```bash
curl -sSL https://raw.githubusercontent.com/ai-janitor/minion-swarm/main/scripts/install.sh | bash
```

Or install directly:

```bash
pipx install git+https://github.com/ai-janitor/minion-swarm.git
minion-swarm init
```

Initialize with a specific project directory:

```bash
minion-swarm init --project-dir /path/to/your/repo
```

What install does:
- installs `minion-swarm` and `run-minion` commands via pipx/pip
- seeds config at `~/.minion-swarm/minion-swarm.yaml` from bundled example
- patches config defaults (`project_dir`, dead-drop paths, required prompt lines)

## Run

Run swarm against the repo you are currently in:

```bash
cd /path/to/repo
run-minion swarm-lead
```

Daemon controls:

```bash
minion-swarm start swarm-lead
minion-swarm status
minion-swarm logs swarm-lead --lines 0
minion-swarm stop swarm-lead
```

## One Agent Runner

```bash
run-minion swarm-lead
```

`run-minion` calls `minion-swarm init` first, so config is always seeded and patched.

Optional explicit args:

```bash
run-minion <agent-name> <config-path> <project-dir>
```

## Crews

Crews are predefined party compositions that spawn multiple agents in separate terminal windows.

```bash
# Spawn the FF1 crew (4 terminals: fighter + 3 daemons)
bash scripts/spawn-crew.sh ff1 /path/to/repo

# Controls
minion-swarm status --config ~/.minion-swarm/ff1.yaml
minion-swarm stop --config ~/.minion-swarm/ff1.yaml
```

Crew files live in `crews/`. Each `.yaml` defines a party composition. Create your own by copying an existing crew file.

| Crew | Agents | Description |
|------|--------|-------------|
| `ff1` | fighter, whitemage, blackmage, thief | FF1-themed party: coder lead + oracle + builder + recon |

## Config

Example file: `minion_swarm/data/minion-swarm.yaml.example`

Primary config location:
- `~/.minion-swarm/minion-swarm.yaml`

Environment override:
- `MINION_SWARM_CONFIG=/path/to/file.yaml`

For Claude agents:
- `allowed_tools` maps to `claude --allowed-tools`
- `permission_mode` maps to `claude --permission-mode`

Runtime state is written under `.minion-swarm/` in each configured `project_dir`.
