"""spawn-crew — open OS terminal windows for a crew of agents."""

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

import click
import yaml


CREW_SEARCH_PATHS = [
    Path(__file__).resolve().parent / "data" / "crews",
    Path.home() / ".minion-swarm" / "crews",
]


def _find_crew(name: str) -> Path:
    for d in CREW_SEARCH_PATHS:
        candidate = d / f"{name}.yaml"
        if candidate.is_file():
            return candidate
    available = []
    for d in CREW_SEARCH_PATHS:
        if d.is_dir():
            available.extend(p.stem for p in d.glob("*.yaml"))
    click.echo(f"Error: crew '{name}' not found", err=True)
    click.echo(f"Searched: {', '.join(str(d) for d in CREW_SEARCH_PATHS)}", err=True)
    if available:
        click.echo(f"Available: {', '.join(sorted(set(available)))}", err=True)
    sys.exit(1)


def _open_terminal_macos(title: str, cmd: str) -> None:
    # Escape for AppleScript string literal
    escaped = cmd.replace("\\", "\\\\").replace('"', '\\"')
    script = f'''
    tell application "Terminal"
        activate
        do script "{escaped}"
        set custom title of front window to "{title}"
    end tell
    '''
    subprocess.run(["osascript", "-e", script], check=True)


def _open_terminal_linux(title: str, cmd: str) -> None:
    if shutil.which("gnome-terminal"):
        subprocess.Popen(["gnome-terminal", f"--title={title}", "--", "bash", "-c", f"{cmd}; exec bash"])
    elif shutil.which("xterm"):
        subprocess.Popen(["xterm", "-T", title, "-e", cmd])
    elif shutil.which("x-terminal-emulator"):
        subprocess.Popen(["x-terminal-emulator", "-T", title, "-e", cmd])
    else:
        click.echo("Error: no supported terminal emulator found", err=True)
        sys.exit(3)


def _open_terminal(title: str, cmd: str) -> None:
    system = platform.system()
    if system == "Darwin":
        _open_terminal_macos(title, cmd)
    elif system == "Linux":
        _open_terminal_linux(title, cmd)
    else:
        click.echo(f"Error: unsupported OS: {system}", err=True)
        sys.exit(3)


@click.command()
@click.argument("crew_name")
@click.argument("project_dir", default=".")
def main(crew_name: str, project_dir: str) -> None:
    """Open OS terminal windows for each member of a crew."""
    project_dir = str(Path(project_dir).resolve())
    crew_file = _find_crew(crew_name)

    with open(crew_file) as f:
        crew_cfg = yaml.safe_load(f)

    lead_cfg = crew_cfg.get("lead")
    if not lead_cfg:
        click.echo(f"Error: no 'lead' defined in {crew_file}", err=True)
        sys.exit(1)

    lead_name = lead_cfg["name"]
    lead_system = lead_cfg["system"].strip()

    agents = list(crew_cfg.get("agents", {}).keys())
    if not agents:
        click.echo(f"Error: no agents defined in {crew_file}", err=True)
        sys.exit(1)

    # Write patched config for minion-swarm (daemons only — lead is interactive)
    config_dir = Path.home() / ".minion-swarm"
    config_dir.mkdir(parents=True, exist_ok=True)
    crew_config = config_dir / f"{crew_name}.yaml"

    crew_cfg["project_dir"] = project_dir
    with open(crew_config, "w") as f:
        yaml.dump(crew_cfg, f, default_flow_style=False)

    # Seed runtime dirs
    if shutil.which("minion-swarm"):
        subprocess.run(
            ["minion-swarm", "init", "--config", str(crew_config), "--project-dir", project_dir],
            capture_output=True,
        )

    # Write lead system prompt to a file (shell quoting is unreliable for multi-line prompts)
    prompt_dir = config_dir / "prompts"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    lead_prompt_file = prompt_dir / f"{crew_name}-{lead_name}.txt"
    with open(lead_prompt_file, "w") as f:
        f.write(lead_system)

    # Clear old daemon logs
    logs_dir = Path(project_dir) / ".minion-swarm" / "logs"
    if logs_dir.is_dir():
        for log_file in logs_dir.glob("*.log"):
            log_file.write_text("")

    click.echo(f"=== Spawning {crew_name} crew ===")
    click.echo(f"Project: {project_dir}")
    click.echo()

    # Terminal 1: lead (interactive claude session)
    click.echo(f"[{lead_name}] Interactive claude session (lead)")
    lead_cmd = f'cd {project_dir} && claude --dangerously-skip-permissions --system-prompt "$(cat {lead_prompt_file})"'
    _open_terminal(lead_name, lead_cmd)

    # Terminals 2-N: daemon agents with log tailing
    for agent in agents:
        click.echo(f"[{agent}] Daemon + log tail")
        agent_cmd = (
            f"cd {project_dir} && "
            f"echo '=== {agent} ===' && "
            f"minion-swarm start {agent} --config {crew_config} && "
            f"exec minion-swarm logs {agent} --config {crew_config} --lines 0"
        )
        _open_terminal(agent, agent_cmd)

    click.echo()
    click.echo("=== All terminals spawned ===")
    click.echo(f"Config: {crew_config}")
    click.echo()
    click.echo("Controls:")
    click.echo(f"  minion-swarm status --config {crew_config}")
    click.echo(f"  minion-swarm stop <agent> --config {crew_config}")
    click.echo(f"  minion-swarm stop --config {crew_config}   # stop all")
