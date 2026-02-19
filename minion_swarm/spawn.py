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


def _open_terminal_macos(title: str, cmd: str, close_on_exit: bool = False) -> None:
    # Wrap command so the terminal window closes itself when done
    if close_on_exit:
        # After the command finishes, close this Terminal.app window via AppleScript
        close_script = (
            "osascript -e "
            "'tell application \"Terminal\" to close (every window whose name contains \"${TERM_TITLE}\")' "
            "2>/dev/null"
        )
        cmd = f'TERM_TITLE="{title}" && {cmd}; {close_script}'

    escaped = cmd.replace("\\", "\\\\").replace('"', '\\"')
    script = f'''
    tell application "Terminal"
        activate
        do script "{escaped}"
        set custom title of front window to "{title}"
    end tell
    '''
    subprocess.run(["osascript", "-e", script], check=True)


def _open_terminal_linux(title: str, cmd: str, close_on_exit: bool = False) -> None:
    # When close_on_exit is False, keep the shell alive after the command
    suffix = "" if close_on_exit else "; exec bash"
    if shutil.which("gnome-terminal"):
        subprocess.Popen(["gnome-terminal", f"--title={title}", "--", "bash", "-c", f"{cmd}{suffix}"])
    elif shutil.which("xterm"):
        subprocess.Popen(["xterm", "-T", title, "-e", f"bash -c '{cmd}{suffix}'"])
    elif shutil.which("x-terminal-emulator"):
        subprocess.Popen(["x-terminal-emulator", "-T", title, "-e", f"bash -c '{cmd}{suffix}'"])
    else:
        click.echo("Error: no supported terminal emulator found", err=True)
        sys.exit(3)


def _open_terminal(title: str, cmd: str, close_on_exit: bool = False) -> None:
    system = platform.system()
    if system == "Darwin":
        _open_terminal_macos(title, cmd, close_on_exit=close_on_exit)
    elif system == "Linux":
        _open_terminal_linux(title, cmd, close_on_exit=close_on_exit)
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
    # The command monitors the daemon PID — when the daemon dies, the tail
    # exits and the shell closes (via `; exit`).
    for agent in agents:
        click.echo(f"[{agent}] Daemon + log tail")
        state_file = Path(project_dir) / ".minion-swarm" / "state" / f"{agent}.json"
        log_file = Path(project_dir) / ".minion-swarm" / "logs" / f"{agent}.log"
        agent_cmd = (
            f"cd {project_dir} && "
            f"echo '=== {agent} ===' && "
            f"minion-swarm start {agent} --config {crew_config} && "
            f"PID=$(python3 -c \"import json; print(json.load(open('{state_file}'))['pid'])\") && "
            f"tail -f {log_file} & TAIL_PID=$! && "
            f"while kill -0 $PID 2>/dev/null; do sleep 2; done && "
            f"kill $TAIL_PID 2>/dev/null; "
            f"echo '=== {agent} exited ===' && sleep 1 && exit"
        )
        _open_terminal(agent, agent_cmd, close_on_exit=True)

    click.echo()
    click.echo("=== All terminals spawned ===")
    click.echo(f"Config: {crew_config}")
    click.echo()
    click.echo("Controls:")
    click.echo(f"  minion-swarm status --config {crew_config}")
    click.echo(f"  minion-swarm stop <agent> --config {crew_config}")
    click.echo(f"  minion-swarm stop --config {crew_config}   # stop all")
