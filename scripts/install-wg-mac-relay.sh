#!/bin/bash
set -euo pipefail
umask 077

if [[ $# -ne 2 ]]; then
    echo "Usage: scripts/install-wg-mac-relay.sh HTTPS_RELAY_ORIGIN PRIVATE_TOKEN_FILE" >&2
    exit 2
fi
if [[ "$(uname -s)" != Darwin || "$(id -u)" == 0 ]]; then
    echo "Run this from your macOS desktop account." >&2
    exit 2
fi
visible="${MUNICH_WG_MAC_VISIBLE:-0}"
if [[ "$visible" != 0 && "$visible" != 1 ]]; then
    echo "Set MUNICH_WG_MAC_VISIBLE to 0 or 1." >&2
    exit 2
fi

repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
state_dir="$HOME/Library/Application Support/MietRadar WG"
profile="$state_dir/chrome"
venv="$state_dir/venv"
worker_dir="$state_dir/worker"
agents_dir="$HOME/Library/LaunchAgents"
label="com.mietradar.wg-sender"
domain="gui/$(id -u)"
target="$domain/$label"
plist="$agents_dir/$label.plist"

if ! /bin/launchctl print "$domain" >/dev/null 2>&1; then
    echo "Sign in to the macOS desktop before installing the sender." >&2
    exit 2
fi
if [[ -L "$state_dir" || -L "$profile" || -L "$worker_dir" || -L "$venv" || -L "$plist" ]]; then
    echo "Move symbolic links out of the MietRadar installation paths first." >&2
    exit 2
fi

bootstrap_python="$repo_dir/.venv/bin/python"
if [[ ! -x "$bootstrap_python" ]]; then
    bootstrap_python="$(command -v python3)"
fi
token_file="$("$bootstrap_python" - "$1" "$2" <<'PY'
import os
import stat
import sys
from pathlib import Path
from urllib.parse import urlsplit

try:
    origin = urlsplit(sys.argv[1])
    valid_origin = (origin.scheme == "https" and origin.hostname and origin.port in {None, 443}
                    and origin.username is None and origin.password is None
                    and origin.path in {"", "/"} and not origin.query and not origin.fragment)
except ValueError:
    valid_origin = False
if not valid_origin:
    raise SystemExit("Use the HTTPS origin of the Railway relay.")

path = Path(sys.argv[2]).expanduser()
try:
    info = path.lstat()
except OSError:
    raise SystemExit("The private relay token file could not be opened.") from None
if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
    raise SystemExit("Use a regular relay token file owned by your account with mode 0600.")
print(path.resolve())
PY
)"

mkdir -p "$state_dir" "$profile" "$agents_dir"
chmod 700 "$state_dir" "$profile"
if [[ ! -x "$venv/bin/python" ]]; then
    "$bootstrap_python" -m venv "$venv"
fi
python="$venv/bin/python"
"$python" -m pip --disable-pip-version-check install -r "$repo_dir/requirements-send.txt"

stage_dir="$(mktemp -d "$state_dir/.install.XXXXXX")"
trap 'rm -rf -- "$stage_dir"' EXIT
mkdir "$stage_dir/worker"
modules=(
    kleinanzeigen_judge listing_photos llm_personalizer munich_delivery munich_tiers outgoing_judge
    rental_availability reviewed_applications wg_delivery wg_first_message wg_mac_worker wg_reply_inbox wg_source
)
for module in "${modules[@]}"; do
    cp "$repo_dir/src/$module.py" "$stage_dir/worker/"
done
"$python" "$stage_dir/worker/wg_mac_worker.py" --help >/dev/null

log_file="$state_dir/worker.log"
if [[ -L "$log_file" ]]; then
    echo "Move the symbolic link at the sender log path first." >&2
    exit 2
fi
touch "$log_file"
chmod 600 "$log_file"

"$python" - "$stage_dir/agent.plist" "$label" "$python" "$worker_dir" "$profile" "$1" "$token_file" "$log_file" "$visible" <<'PY'
import plistlib
import sys
from pathlib import Path

plist, label, python, worker_dir, profile, relay_url, token_file, log_file, visible = sys.argv[1:]
job = {
    "Label": label,
    "ProgramArguments": [
        python, str(Path(worker_dir) / "wg_mac_worker.py"), "run",
        "--profile", profile, "--relay-url", relay_url, "--token-file", token_file,
    ],
    "WorkingDirectory": worker_dir,
    "RunAtLoad": True,
    "StandardOutPath": log_file,
    "StandardErrorPath": log_file,
}
if visible == "1":
    job["ProgramArguments"].append("--visible")
with open(plist, "wb") as stream:
    plistlib.dump(job, stream)
PY
/usr/bin/plutil -lint "$stage_dir/agent.plist" >/dev/null

bootstrap_agent() {
    local attempt error
    for attempt in 1 2 3 4 5; do
        if error="$(/bin/launchctl bootstrap "$domain" "$plist" 2>&1)"; then
            return 0
        fi
        # launchd can still be finishing a bootout when asked to load the same label.
        if [[ "$error" != *"Bootstrap failed: 5:"* || "$attempt" == 5 ]]; then
            break
        fi
        if /bin/launchctl print "$target" >/dev/null 2>&1; then
            break
        fi
        sleep 1
    done
    printf '%s\n' "$error" >&2
    return 1
}

was_running=false
if /bin/launchctl print "$target" >/dev/null 2>&1; then
    /bin/launchctl bootout "$target"
    was_running=true
    for attempt in 1 2 3 4 5; do
        if ! /bin/launchctl print "$target" >/dev/null 2>&1; then
            break
        fi
        sleep 1
    done
    if /bin/launchctl print "$target" >/dev/null 2>&1; then
        echo "macOS is still stopping the previous WG-Gesucht sender. Run the installer again shortly." >&2
        exit 1
    fi
fi
if [[ -e "$worker_dir" ]]; then
    mv "$worker_dir" "$stage_dir/previous-worker"
fi
if [[ -e "$plist" ]]; then
    mv "$plist" "$stage_dir/previous.plist"
fi
mv "$stage_dir/worker" "$worker_dir"
mv "$stage_dir/agent.plist" "$plist"

if ! bootstrap_agent; then
    mv "$worker_dir" "$stage_dir/worker"
    mv "$plist" "$stage_dir/agent.plist"
    if [[ -e "$stage_dir/previous-worker" ]]; then
        mv "$stage_dir/previous-worker" "$worker_dir"
    fi
    if [[ -e "$stage_dir/previous.plist" ]]; then
        mv "$stage_dir/previous.plist" "$plist"
        if [[ "$was_running" == true ]] && ! bootstrap_agent; then
            echo "macOS could not restore the previous WG-Gesucht sender." >&2
        fi
    fi
    echo "macOS could not start the WG-Gesucht sender." >&2
    exit 1
fi

echo "WG-Gesucht sender installed for this macOS account."
