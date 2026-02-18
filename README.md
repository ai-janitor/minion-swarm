# minion-swarm

Autonomous multi-agent daemon runner coordinated through dead-drop.

Supported providers:
- `claude`
- `codex`
- `opencode`
- `gemini`

## Install

Clone-based install:

```bash
git clone https://github.com/ai-janitor/minion-swarm.git
cd minion-swarm
./scripts/install.sh
```

Curl bootstrap install:

```bash
curl -fsSL https://raw.githubusercontent.com/ai-janitor/minion-swarm/main/scripts/bootstrap.sh | bash
```

Optional explicit target repo:

```bash
./scripts/install.sh --project-dir /path/to/your/repo
# or
curl -fsSL https://raw.githubusercontent.com/ai-janitor/minion-swarm/main/scripts/bootstrap.sh | bash -s -- --project-dir /path/to/your/repo
```

Config overwrite behavior:
- If config already exists, installer prompts before overwrite.
- Use `--overwrite-config` for non-interactive replacement.

Notes:
- `scripts/install.sh` is the single setup/patch path.
- `--project-dir` is optional.
- If omitted, installer keeps existing config `project_dir` or defaults to current shell directory.

What install does:
- creates `./.venv` and installs dependencies (if missing)
- creates or patches config at `~/.minion-swarm/minion-swarm.yaml`
- patches config defaults (`project_dir`, dead-drop paths, required prompt lines)
- links launchers to `~/.local/bin` (unless `--no-symlink`)

Seed config sources (if target config does not exist):
- `./minion-swarm.yaml`
- `~/.minion-swarm/minion-swarm.yaml`
- `./minion-swarm.yaml.example`

## Run

Run swarm against the repo you are currently in:

```bash
cd /path/to/repo
run-minion swarm-lead
```

If `run-minion` is not on your PATH, use:

```bash
/path/to/minion-swarm/scripts/run-minion.sh swarm-lead
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
./scripts/run-minion.sh swarm-lead
```

`scripts/run-minion.sh` always calls `scripts/install.sh --no-symlink` first, so all setup/patching is centralized in the installer.

Optional explicit args:

```bash
./scripts/run-minion.sh <agent-name> <config-path> <project-dir>
```

Compatibility:
- `scripts/run-agent.sh` still works and forwards to `scripts/run-minion.sh`.

## Config

Example file: `minion-swarm.yaml.example`

Primary config location:
- `~/.minion-swarm/minion-swarm.yaml`

Environment override:
- `MINION_SWARM_CONFIG=/path/to/file.yaml`

For Claude agents:
- `allowed_tools` maps to `claude --allowed-tools`
- `permission_mode` maps to `claude --permission-mode`

Runtime state is written under `.minion-swarm/` in each configured `project_dir`.
