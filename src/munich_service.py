"""Run the Munich radar, optionally with its Streamlit dashboard."""

import os
from pathlib import Path
import re
import signal
import subprocess
import sys
from time import monotonic, sleep


SRC = Path(__file__).resolve().parent
SHUTDOWN_TIMEOUT = 5
_HOST = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", re.I | re.ASCII)


def _dashboard_command() -> list[str]:
    if not os.getenv("MUNICH_DASHBOARD_PASSWORD", "").strip():
        raise ValueError("MUNICH_DASHBOARD_PASSWORD is required when the dashboard is enabled")

    value = os.getenv("PORT") or "8501"
    try:
        port = int(value)
    except ValueError:
        raise ValueError("PORT must be an integer between 1 and 65535") from None
    if not 1 <= port <= 65535:
        raise ValueError("PORT must be an integer between 1 and 65535")

    command = [
        sys.executable, "-m", "streamlit", "run", str(SRC / "dashboard.py"),
        "--server.headless=true", "--server.address=0.0.0.0", f"--server.port={port}",
        "--browser.gatherUsageStats=false",
    ]
    host = (os.getenv("MUNICH_DASHBOARD_HOST") or os.getenv("RAILWAY_PUBLIC_DOMAIN") or "").strip()
    if host:
        if len(host) > 253 or not _HOST.fullmatch(host):
            raise ValueError("MUNICH_DASHBOARD_HOST must be a DNS hostname without a scheme or port")
        command.extend((f"--browser.serverAddress={host}", "--browser.serverPort=443"))
    return command


def _signal_group(process: subprocess.Popen, signum: signal.Signals) -> None:
    try:
        os.killpg(process.pid, signum)
    except ProcessLookupError:
        pass


def _stop_children(children: list[subprocess.Popen]) -> None:
    # An exited worker may still have Chrome descendants in its process group.
    for process in children:
        _signal_group(process, signal.SIGTERM)
    deadline = monotonic() + SHUTDOWN_TIMEOUT
    try:
        for process in children:
            process.wait(timeout=max(0, deadline - monotonic()))
    except subprocess.TimeoutExpired:
        pass
    finally:
        for process in children:
            _signal_group(process, signal.SIGKILL)
        for process in children:
            process.wait()


def _supervise(services: tuple[tuple[str, list[str]], ...]) -> int:
    stop_requested = False

    def stop_services(_signum, _frame):
        nonlocal stop_requested
        stop_requested = True

    signals = (signal.SIGTERM, signal.SIGINT)
    handlers = {signum: signal.signal(signum, stop_services) for signum in signals}
    children = []
    try:
        for label, command in services:
            if stop_requested:
                return 0
            children.append((label, subprocess.Popen(command, start_new_session=True)))
        while not stop_requested:
            for label, process in children:
                exit_code = process.poll()
                if exit_code is not None:
                    print(f"{label} exited unexpectedly ({exit_code})", file=sys.stderr)
                    return 1
            sleep(0.25)
        return 0
    finally:
        _stop_children([process for _, process in children])
        for signum, handler in handlers.items():
            signal.signal(signum, handler)


def main() -> int:
    worker = [sys.executable, str(SRC / "munich_radar.py"), "watch"]
    try:
        enabled = os.getenv("MUNICH_DASHBOARD_ENABLED", "false").strip().lower()
        if enabled == "false":
            os.execv(sys.executable, worker)
        if enabled != "true":
            raise ValueError("MUNICH_DASHBOARD_ENABLED must be true or false")
        return _supervise((("Radar worker", worker), ("Dashboard", _dashboard_command())))
    except (ValueError, OSError) as exc:
        print(f"MietRadar startup failed. {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
