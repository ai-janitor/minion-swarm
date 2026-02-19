from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from collections import deque
from pathlib import Path
from typing import Iterable, List, Optional

import click
import yaml

from .config import SwarmConfig, load_config
from .daemon import AgentDaemon
from .watcher import CommsWatcher

DATA_DIR = Path(__file__).resolve().parent / "data"

DEFAULT_CONFIG_PATH = str(
    Path(os.environ.get("MINION_SWARM_CONFIG", "~/.minion-swarm/minion-swarm.yaml")).expanduser()
)


def _daemon_env() -> dict:
    """Clean environment for spawned agent subprocesses."""
    return os.environ.copy()


def _pid_path(cfg: SwarmConfig, agent_name: str) -> Path:
    return cfg.pids_dir / f"{agent_name}.pid"


def _state_path(cfg: SwarmConfig, agent_name: str) -> Path:
    return cfg.state_dir / f"{agent_name}.json"


def _log_path(cfg: SwarmConfig, agent_name: str) -> Path:
    return cfg.logs_dir / f"{agent_name}.log"


def _read_pid(path: Path) -> Optional[int]:
    if not path.exists():
        return None
    try:
        return int(path.read_text().strip())
    except ValueError:
        return None


def _is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _normalize_targets(cfg: SwarmConfig, maybe_agent: Optional[str]) -> List[str]:
    if maybe_agent:
        if maybe_agent not in cfg.agents:
            raise click.ClickException(f"Agent '{maybe_agent}' not found in config")
        return [maybe_agent]
    return list(cfg.agents.keys())


@click.group()
def cli() -> None:
    """minion-swarm daemon CLI."""


@cli.command(name="init")
@click.option("--config", "config_path", default=DEFAULT_CONFIG_PATH, show_default=True)
@click.option("--project-dir", "project_dir", default=None)
@click.option("--overwrite-config", is_flag=True, default=False)
def init_cmd(config_path: str, project_dir: Optional[str], overwrite_config: bool) -> None:
    """Seed and patch config YAML (run automatically on install)."""
    cfg_path = Path(config_path).expanduser().resolve()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)

    example_path = DATA_DIR / "minion-swarm.yaml.example"
    if not example_path.exists():
        raise click.ClickException(f"Example config not found: {example_path}")

    # Seed config from example if missing or overwrite requested
    if not cfg_path.exists() or overwrite_config:
        shutil.copy2(example_path, cfg_path)
        click.echo(f"Seeded config from: {example_path}")

    # Patch config
    raw = yaml.safe_load(cfg_path.read_text()) or {}
    if not isinstance(raw, dict):
        raise click.ClickException("Config root must be a YAML mapping")

    example = yaml.safe_load(example_path.read_text()) or {}

    # Resolve project_dir
    if project_dir:
        pd = Path(project_dir).expanduser().resolve()
        if not pd.is_dir():
            raise click.ClickException(f"Project directory not found: {pd}")
        raw["project_dir"] = str(pd)
    elif "project_dir" not in raw or not raw["project_dir"]:
        raw["project_dir"] = str(Path.cwd())

    raw.setdefault("comms_dir", raw.pop("dead_drop_dir", ".dead-drop"))
    raw.setdefault("comms_db", raw.pop("dead_drop_db", "~/.dead-drop/messages.db"))

    # Seed agents from example if missing
    if not isinstance(raw.get("agents"), dict) or not raw.get("agents"):
        example_agents = example.get("agents")
        if isinstance(example_agents, dict) and example_agents:
            raw["agents"] = example_agents
        else:
            raise click.ClickException("No agents defined and no agents in example config")

    # Inject system prompt lines for dead-drop protocol
    protocol_line = "Re-read .dead-drop/debug-protocol.md and BACKLOG.md before every task."
    lead_policy_lines = [
        "Never delegate work without a written task file in `.dead-drop/tasks/<TASK-ID>/task.md`.",
        "Capture newly discovered ideas in `.dead-drop/BACKLOG.md`.",
        "After each completed task, check backlog and assign the next written task.",
    ]

    for name, agent in list(raw.get("agents", {}).items()):
        if not isinstance(agent, dict):
            raise click.ClickException(f"Agent '{name}' config must be a mapping")

        system = str(agent.get("system", "")).strip()
        if not system:
            role = str(agent.get("role", "coder"))
            system = (
                f"You are {name} ({role}) running under minion-swarm. "
                "Check dead-drop inbox, execute tasks, and report via dead-drop."
            )

        if "debug-protocol.md" not in system or "BACKLOG.md" not in system:
            system = f"{system}\n{protocol_line}"

        if name == "swarm-lead":
            for line in lead_policy_lines:
                if line not in system:
                    system = f"{system}\n{line}"

        agent["system"] = system

    cfg_path.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=False))
    click.echo(f"Patched config: {cfg_path}")
    click.echo(f"Effective project_dir: {raw['project_dir']}")


@cli.command(name="start")
@click.argument("agent", required=False)
@click.option("--config", "config_path", default=DEFAULT_CONFIG_PATH, show_default=True)
def start_cmd(agent: Optional[str], config_path: str) -> None:
    """Start one agent (or all agents)."""
    cfg = load_config(config_path)
    cfg.ensure_runtime_dirs()

    for name in _normalize_targets(cfg, agent):
        pid_file = _pid_path(cfg, name)
        existing_pid = _read_pid(pid_file)
        if existing_pid and _is_pid_alive(existing_pid):
            click.echo(f"{name}: already running (pid {existing_pid})")
            continue

        log_file = _log_path(cfg, name)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        log_fp = log_file.open("a", encoding="utf-8")

        cmd = [
            sys.executable,
            "-m",
            "minion_swarm.cli",
            "_run-agent",
            "--config",
            str(cfg.config_path),
            "--agent",
            name,
        ]

        proc = subprocess.Popen(
            cmd,
            cwd=str(cfg.project_dir),
            stdin=subprocess.DEVNULL,
            stdout=log_fp,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
            env=_daemon_env(),
        )
        log_fp.close()

        pid_file.write_text(str(proc.pid))
        click.echo(f"{name}: started (pid {proc.pid})")


@cli.command(name="stop")
@click.argument("agent", required=False)
@click.option("--config", "config_path", default=DEFAULT_CONFIG_PATH, show_default=True)
def stop_cmd(agent: Optional[str], config_path: str) -> None:
    """Stop one agent (or all agents)."""
    cfg = load_config(config_path)
    cfg.ensure_runtime_dirs()

    targets = _normalize_targets(cfg, agent)
    for name in targets:
        pid_file = _pid_path(cfg, name)
        pid = _read_pid(pid_file)
        if not pid:
            click.echo(f"{name}: not running (no pid file)")
            continue

        if not _is_pid_alive(pid):
            click.echo(f"{name}: stale pid file (pid {pid} not alive), removing")
            pid_file.unlink(missing_ok=True)
            continue

        # Kill entire process group (daemon + child claude -p processes).
        # Daemon is started with start_new_session=True, so pgid == pid.
        click.echo(f"{name}: sending SIGTERM to process group {pid}")
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

        deadline = time.time() + 5
        while time.time() < deadline and _is_pid_alive(pid):
            time.sleep(0.2)

        if _is_pid_alive(pid):
            click.echo(f"{name}: force killing process group {pid}")
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

        pid_file.unlink(missing_ok=True)
        click.echo(f"{name}: stopped")


@cli.command(name="status")
@click.option("--config", "config_path", default=DEFAULT_CONFIG_PATH, show_default=True)
def status_cmd(config_path: str) -> None:
    """Show daemon status for all configured agents."""
    cfg = load_config(config_path)
    cfg.ensure_runtime_dirs()

    click.echo("agent\tpid\talive\tstatus\tupdated_at")
    for name in cfg.agents:
        pid = _read_pid(_pid_path(cfg, name))
        alive = bool(pid and _is_pid_alive(pid))

        status = "unknown"
        updated_at = "-"
        state_file = _state_path(cfg, name)
        if state_file.exists():
            try:
                state = json.loads(state_file.read_text())
                status = state.get("status", status)
                updated_at = state.get("updated_at", updated_at)
            except json.JSONDecodeError:
                status = "invalid-state"

        click.echo(f"{name}\t{pid or '-'}\t{alive}\t{status}\t{updated_at}")


@cli.command(name="logs")
@click.argument("agent")
@click.option("--config", "config_path", default=DEFAULT_CONFIG_PATH, show_default=True)
@click.option("--lines", default=80, show_default=True, type=int)
@click.option("--follow/--no-follow", default=True, show_default=True)
def logs_cmd(agent: str, config_path: str, lines: int, follow: bool) -> None:
    """Show (and optionally follow) one agent log."""
    cfg = load_config(config_path)
    if agent not in cfg.agents:
        raise click.ClickException(f"Unknown agent '{agent}'")

    log_file = _log_path(cfg, agent)
    if not log_file.exists():
        raise click.ClickException(f"Log file not found: {log_file}")

    with log_file.open("r", encoding="utf-8") as fp:
        if lines > 0:
            tail = deque(fp, maxlen=lines)
            for line in tail:
                click.echo(line, nl=False)

        if not follow:
            return

        while True:
            line = fp.readline()
            if line:
                click.echo(line, nl=False)
            else:
                time.sleep(0.5)


@cli.command(name="send")
@click.argument("to_agent")
@click.argument("message", nargs=-1, required=True)
@click.option("--config", "config_path", default=DEFAULT_CONFIG_PATH, show_default=True)
@click.option("--from-agent", default="lead", show_default=True)
@click.option("--cc", default=None)
def send_cmd(to_agent: str, message: Iterable[str], config_path: str, from_agent: str, cc: Optional[str]) -> None:
    """Send a dead-drop message as lead (or any sender)."""
    cfg = load_config(config_path)

    payload = " ".join(message).strip()
    if not payload:
        raise click.ClickException("Message cannot be empty")

    watcher = CommsWatcher(from_agent, cfg.comms_db)
    msg_id = watcher.send_message(from_agent, to_agent, payload, cc=cc)
    click.echo(f"sent message id={msg_id} from={from_agent} to={to_agent}")


@cli.command(name="_run-agent", hidden=True)
@click.option("--config", "config_path", required=True)
@click.option("--agent", "agent_name", required=True)
def run_agent_cmd(config_path: str, agent_name: str) -> None:
    """Internal command used by `start` to launch an agent daemon."""
    cfg = load_config(config_path)
    daemon = AgentDaemon(cfg, agent_name)
    daemon.run()


def main() -> None:
    cli()


def run_minion_main() -> None:
    """Entry point for `run-minion` console_scripts — start one agent with cleanup."""
    import atexit

    agent_name = sys.argv[1] if len(sys.argv) > 1 else "swarm-lead"
    config_path = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_CONFIG_PATH
    project_dir = sys.argv[3] if len(sys.argv) > 3 else str(Path.cwd())

    # Ensure config is seeded and patched
    ctx = click.Context(init_cmd)
    ctx.invoke(init_cmd, config_path=config_path, project_dir=project_dir, overwrite_config=False)

    cfg = load_config(config_path)
    if agent_name not in cfg.agents:
        click.echo(f"Error: Agent '{agent_name}' not found in config", err=True)
        click.echo("Available agents:", err=True)
        for name in cfg.agents:
            click.echo(f"  - {name}", err=True)
        sys.exit(2)

    leave_running = os.environ.get("LEAVE_RUNNING", "0") == "1"

    def cleanup() -> None:
        if leave_running:
            return
        subprocess.run(
            [sys.executable, "-m", "minion_swarm.cli", "stop", agent_name, "--config", config_path],
            capture_output=True,
        )

    atexit.register(cleanup)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))

    # Start, show status, follow logs
    ctx = click.Context(start_cmd)
    ctx.invoke(start_cmd, agent=agent_name, config_path=config_path)
    ctx = click.Context(status_cmd)
    ctx.invoke(status_cmd, config_path=config_path)
    ctx = click.Context(logs_cmd)
    ctx.invoke(logs_cmd, agent=agent_name, config_path=config_path, lines=0, follow=True)


if __name__ == "__main__":
    main()
