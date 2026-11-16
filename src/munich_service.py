"""Run the Munich radar, optionally with its Streamlit dashboard."""

import os
from pathlib import Path
import re
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import monotonic, sleep


SRC = Path(__file__).resolve().parent
SHUTDOWN_TIMEOUT = 5
_HOST = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", re.I | re.ASCII)


def _port(value: str, name: str) -> int:
    try:
        port = int(value)
    except ValueError:
        raise ValueError(f"{name} must be an integer between 1 and 65535") from None
    if not 1 <= port <= 65535:
        raise ValueError(f"{name} must be an integer between 1 and 65535")
    return port


def _dashboard_command(*, internal_port: int | None = None) -> list[str]:
    if not os.getenv("MUNICH_DASHBOARD_PASSWORD", "").strip():
        raise ValueError("MUNICH_DASHBOARD_PASSWORD is required when the dashboard is enabled")

    port = internal_port or _port(os.getenv("PORT") or "8501", "PORT")
    address = "127.0.0.1" if internal_port else "0.0.0.0"

    command = [
        sys.executable, "-m", "streamlit", "run", str(SRC / "dashboard.py"),
        "--server.headless=true", f"--server.address={address}", f"--server.port={port}",
        "--browser.gatherUsageStats=false",
    ]
    host = (os.getenv("MUNICH_DASHBOARD_HOST") or os.getenv("RAILWAY_PUBLIC_DOMAIN") or "").strip()
    if host:
        if len(host) > 253 or not _HOST.fullmatch(host):
            raise ValueError("MUNICH_DASHBOARD_HOST must be a DNS hostname without a scheme or port")
        command.extend((f"--browser.serverAddress={host}", "--browser.serverPort=443"))
    return command


def _gateway_config(path: Path, public: int, dashboard: int, relay: int) -> Path:
    config = path / "gateway.conf"
    config.write_text(f"""
pid {path}/nginx.pid;
error_log stderr warn;
worker_processes 1;
events {{ worker_connections 256; }}
http {{
    access_log off;
    client_body_temp_path {path}/client;
    proxy_temp_path {path}/proxy;
    fastcgi_temp_path {path}/fastcgi;
    uwsgi_temp_path {path}/uwsgi;
    scgi_temp_path {path}/scgi;
    map $http_upgrade $connection_upgrade {{
        default upgrade;
        '' close;
    }}
    server {{
        listen {public};
        client_max_body_size 8m;
        proxy_http_version 1.1;
        proxy_set_header Host $http_host;
        proxy_set_header X-Forwarded-Proto $http_x_forwarded_proto;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;

        location ^~ /v1/ {{
            proxy_pass http://127.0.0.1:{relay};
            proxy_read_timeout 60s;
        }}
        location / {{
            proxy_pass http://127.0.0.1:{dashboard};
            proxy_set_header Host $http_host;
            proxy_set_header X-Forwarded-Proto $http_x_forwarded_proto;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header Upgrade $http_upgrade;
            proxy_set_header Connection $connection_upgrade;
            proxy_read_timeout 300s;
        }}
    }}
}}
""", encoding="utf-8")
    return config


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
        relay_enabled = bool(os.getenv("MUNICH_WG_RELAY_TOKEN", "").strip())
        if relay_enabled and (enabled != "true" or os.getenv("MUNICH_REVIEWED_SEND", "").strip().lower() != "true"):
            raise ValueError("The WG relay requires the dashboard and reviewed send mode")
        if enabled == "false":
            os.execv(sys.executable, worker)
        if enabled != "true":
            raise ValueError("MUNICH_DASHBOARD_ENABLED must be true or false")
        if relay_enabled:
            public_port = _port(os.getenv("PORT") or "8501", "PORT")
            relay_port = _port(os.getenv("MUNICH_WG_RELAY_PORT") or "8765", "MUNICH_WG_RELAY_PORT")
            dashboard_port = 18750
            if len({public_port, relay_port, dashboard_port}) != 3:
                raise ValueError("The public port, relay port, and internal dashboard port must differ")
            with TemporaryDirectory(prefix="mietradar-gateway-") as gateway:
                path = Path(gateway)
                config = _gateway_config(path, public_port, dashboard_port, relay_port)
                return _supervise((
                    ("Radar worker", worker),
                    ("Dashboard", _dashboard_command(internal_port=dashboard_port)),
                    ("WG delivery relay", [sys.executable, str(SRC / "wg_delivery_relay.py")]),
                    ("HTTPS gateway", ["nginx", "-g", "daemon off;", "-e", "stderr", "-p", str(path), "-c", str(config)]),
                ))
        return _supervise((("Radar worker", worker), ("Dashboard", _dashboard_command())))
    except (ValueError, OSError) as exc:
        print(f"MietRadar startup failed. {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
