from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from collections import deque
from pathlib import Path
from typing import Iterable, List, Optional

import click

from .config import SwarmConfig, load_config
from .daemon import AgentDaemon
from .watcher import DeadDropWatcher

DEFAULT_CONFIG_PATH = str(
    Path(os.environ.get("MINION_SWARM_CONFIG", "~/.minion-swarm/minion-swarm.yaml")).expanduser()
)


def _daemon_env() -> dict:
    env = os.environ.copy()
    package_root = str(Path(__file__).resolve().parents[1])
    existing = env.get("PYTHONPATH", "").strip()
    if existing:
        env["PYTHONPATH"] = f"{package_root}{os.pathsep}{existing}"
    else:
        env["PYTHONPATH"] = package_root
    return env


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

        click.echo(f"{name}: sending SIGTERM to pid {pid}")
        os.kill(pid, signal.SIGTERM)

        deadline = time.time() + 10
        while time.time() < deadline and _is_pid_alive(pid):
            time.sleep(0.2)

        if _is_pid_alive(pid):
            click.echo(f"{name}: force killing pid {pid}")
            os.kill(pid, signal.SIGKILL)

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

    watcher = DeadDropWatcher(from_agent, cfg.dead_drop_db)
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


if __name__ == "__main__":
    main()
