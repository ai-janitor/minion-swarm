"""spawn-crew — lead in Terminal.app, workers in tmux panes."""

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
    """Spawn a crew: lead in Terminal.app, workers in tmux panes."""
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

    if not shutil.which("tmux"):
        click.echo("Error: tmux is required for worker panes. Install with: brew install tmux", err=True)
        sys.exit(2)

    # Write patched config for minion-swarm
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

    # Write lead system prompt to file
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

    tmux_session = f"crew-{crew_name}"

    # Kill existing tmux session if any
    subprocess.run(["tmux", "kill-session", "-t", tmux_session], capture_output=True)

    click.echo(f"=== Spawning {crew_name} crew ===")
    click.echo(f"Project: {project_dir}")
    click.echo()

    # Terminal.app: lead (interactive claude session)
    click.echo(f"[{lead_name}] Interactive claude session (lead) -> Terminal.app")
    lead_cmd = f'cd {project_dir} && claude --dangerously-skip-permissions --system-prompt "$(cat {lead_prompt_file})"'
    _open_terminal(lead_name, lead_cmd)

    # tmux session: one pane per daemon agent
    # First agent creates the session, rest split into new panes
    for i, agent in enumerate(agents):
        log_file = Path(project_dir) / ".minion-swarm" / "logs" / f"{agent}.log"
        # Start daemon, then tail log. When daemon dies, show exit message.
        pane_cmd = (
            f"cd {project_dir} && "
            f"echo '=== {agent} ===' && "
            f"minion-swarm start {agent} --config {crew_config} && "
            f"tail -f {log_file}; "
            f"echo '=== {agent} exited ==='; read -p 'Press enter to close'"
        )

        if i == 0:
            # Create the tmux session with the first agent
            subprocess.run([
                "tmux", "new-session", "-d",
                "-s", tmux_session,
                "-n", agent,
                "bash", "-c", pane_cmd,
            ], check=True)
        else:
            # Split window for subsequent agents
            subprocess.run([
                "tmux", "split-window",
                "-t", tmux_session,
                "-v",
                "bash", "-c", pane_cmd,
            ], check=True)
            # Re-tile evenly after each split
            subprocess.run([
                "tmux", "select-layout", "-t", tmux_session, "tiled",
            ], capture_output=True)

    # Rename panes for identification
    for i, agent in enumerate(agents):
        subprocess.run([
            "tmux", "select-pane", "-t", f"{tmux_session}:{0}.{i}",
            "-T", agent,
        ], capture_output=True)

    # Enable pane titles
    subprocess.run([
        "tmux", "set-option", "-t", tmux_session, "pane-border-status", "top",
    ], capture_output=True)
    subprocess.run([
        "tmux", "set-option", "-t", tmux_session, "pane-border-format", " #{pane_title} ",
    ], capture_output=True)

    click.echo()
    click.echo(f"=== {crew_name} crew running ===")
    click.echo()
    click.echo(f"  Lead:    Terminal.app ({lead_name})")
    click.echo(f"  Workers: tmux session '{tmux_session}'")
    click.echo(f"  Config:  {crew_config}")
    click.echo()
    click.echo("Controls:")
    click.echo(f"  tmux attach -t {tmux_session}        # view worker panes")
    click.echo(f"  minion-swarm stop --config {crew_config}  # stop daemons")
    click.echo(f"  tmux kill-session -t {tmux_session}   # close panes")
