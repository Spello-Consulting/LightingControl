#!/bin/bash
: '=======================================================
Application Launcher

Requires Python and UV to be installed

3/4/2026: Change default directory to the script location, and allow --homedir to override it.
3/6/2026: Better logic for finding HomeDir.
1/8/2026: Add support for 1Password op / build .env file
=========================================================='

# set -euo pipefail

PYPROJECT="pyproject.toml"

# Parse --homedir argument from any position; default to the directory containing pyproject.toml
ScriptDir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HomeDir=""
for ((i=1; i<=$#; i++)); do
  if [ "${!i}" = "--homedir" ]; then
    j=$((i+1))
    if [ $j -le $# ]; then
      HomeDir="${!j}"
      break
    fi
  fi
done

# If --homedir was not provided, locate the directory containing pyproject.toml
if [ -z "$HomeDir" ]; then
  if [ -f "$ScriptDir/$PYPROJECT" ]; then
    HomeDir="$ScriptDir"
  elif [ -f "$ScriptDir/../$PYPROJECT" ]; then
    HomeDir="$(cd "$ScriptDir/.." && pwd)"
  else
    echo "[launcher] Error: Cannot find $PYPROJECT in $ScriptDir or its parent." >&2
    exit 1
  fi
fi

# make sure HomeDir is an absolute path
HomeDir="$(cd "$HomeDir" && pwd)"

# Change to the home directory so uv commands work correctly
cd "$HomeDir" || {
  echo "[launcher] Error: Cannot change to directory $HomeDir" >&2
  exit 1
}

# Load the 1Password service account token explicitly — don't rely on shell rc files, since this script may run
# outside an interactive/login shell (cron, @reboot, etc.)
set -a
source "$HOME/.config/op/service-account-token"
set +a

# Regenerate .env from the committed template if it exists
EnvFile="$HomeDir/.env"
if [ -f "$HomeDir/.env.template" ]; then
  op inject -i "$HomeDir/.env.template" -o "$HomeDir/.env" -f
fi

# Load environment variables from .env if present (in HomeDir)
# Note: this "sources" the file, so it should contain simple KEY=VALUE lines.
if [ -f "$EnvFile" ]; then
  echo "[launcher] Loading environment from $EnvFile ..."
  set -a
  # shellcheck disable=SC1090
  . "$EnvFile"
  set +a
fi

# Get the script name from pyproject.toml
if [ -f "$PYPROJECT" ]; then
  ScriptName=$(grep -E '^launch_path *= *"' "$PYPROJECT" | head -1 | sed -E 's/^launch_path *= *"([^"]+)".*$/\1/')
else
  echo "Error: $PYPROJECT not found."
  exit 1
fi

if [ -z "$ScriptName" ]; then
  echo "Error: launch_path not defined in $PYPROJECT."
  exit 1
fi

# Find uv reliably (systemd often has a minimal PATH)
if command -v uv >/dev/null 2>&1; then
  UVCmd="$(command -v uv)"
elif [ -x "$HOME/.local/bin/uv" ]; then
  UVCmd="$HOME/.local/bin/uv"
else
  echo "[launcher] Error: 'uv' not found in PATH or at \$HOME/.local/bin/uv" >&2
  exit 1
fi

# On Raspberry Pi, enforce Python 3.13+ if requested
if [[ $(uname -m) == "armv7l" || $(uname -m) == "aarch64" ]]; then
  if ! "$UVCmd" python pin --resolved 2>/dev/null | grep -Eq '^(3\.1[3-9]|3\.[2-9][0-9]|[4-9])'; then
    echo "[launcher] Error: project must pin Python 3.13+ on Raspberry Pi. Run: uv python pin 3.13" >&2
    exit 1
  fi
fi

# Make sure deps are synced before starting
if ! "$UVCmd" sync; then
  echo "[launcher] uv sync failed — not starting app." >&2
  exit 2
fi

# Treat Ctrl-C or systemd stop (SIGTERM) as a clean, intentional shutdown
term_handler() {
  echo "[launcher] Caught termination — exiting cleanly so systemd does not restart."
  exit 0
}
trap term_handler SIGINT SIGTERM

echo "[launcher] Starting app with uv run $ScriptName from directory $HomeDir ..."
"$UVCmd" run "$ScriptName" "$@"
app_rc=$?

if [ $app_rc -eq 0 ]; then
  echo "[launcher] App exited normally (0)."
  exit 0
else
  echo "[launcher] App exited with error ($app_rc) — signaling failure so systemd restarts."
  exit $app_rc
fi
