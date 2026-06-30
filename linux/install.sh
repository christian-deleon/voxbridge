#!/usr/bin/env bash
# voxbridge host installer. Idempotent: safe to re-run.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER="$SCRIPT_DIR/voxbridge_server.py"
CFG_DIR="$HOME/.config/voxbridge"
TOKEN_FILE="$CFG_DIR/token"
CFG_FILE="$CFG_DIR/config.toml"
UNIT="$HOME/.config/systemd/user/voxbridge.service"

msg() { printf '\033[1;36m==>\033[0m %s\n' "$1"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$1"; }

mkdir -p "$CFG_DIR" "$(dirname "$UNIT")"

# --- token ---
if [[ ! -f "$TOKEN_FILE" ]]; then
  umask 077
  openssl rand -hex 32 >"$TOKEN_FILE"
  chmod 600 "$TOKEN_FILE"
  msg "generated shared token at $TOKEN_FILE"
else
  msg "token already present"
fi

# --- config ---
if [[ ! -f "$CFG_FILE" ]]; then
  cp "$SCRIPT_DIR/config.example.toml" "$CFG_FILE"
  msg "wrote default config to $CFG_FILE"
else
  msg "config already present ($CFG_FILE)"
fi

# --- detect NAT network ---
NAT_IP="$(ip -4 -o addr show dev vmnet8 2>/dev/null | awk '{print $4}' | cut -d/ -f1 || true)"
if [[ -z "$NAT_IP" ]]; then
  warn "could not detect vmnet8 IP; is VMware running? (firewall step skipped)"
else
  NAT_SUBNET="${NAT_IP%.*}.0/24"
  msg "VMware NAT: host=$NAT_IP subnet=$NAT_SUBNET"
fi

# --- firewall (only if ufw active) ---
if command -v ufw >/dev/null && sudo -n true 2>/dev/null && [[ -n "${NAT_IP:-}" ]]; then
  if sudo ufw status 2>/dev/null | grep -q "Status: active"; then
    sudo ufw allow from "$NAT_SUBNET" to any port 5599 proto tcp comment 'voxbridge' || true
    sudo ufw allow from "$NAT_SUBNET" to any port 8001 proto tcp comment 'voxbridge' || true
    msg "ufw rules ensured for $NAT_SUBNET (5599, 8001)"
  fi
elif command -v ufw >/dev/null && [[ -n "${NAT_IP:-}" ]]; then
  warn "ufw present but no passwordless sudo; run these in a terminal:"
  echo "    sudo ufw allow from $NAT_SUBNET to any port 5599 proto tcp comment 'voxbridge'"
  echo "    sudo ufw allow from $NAT_SUBNET to any port 8001 proto tcp comment 'voxbridge'"
fi

# --- systemd user service ---
PYTHON="$(command -v python3)"
cat >"$UNIT" <<EOF
[Unit]
Description=voxbridge - voice dictation bridge to Windows VM
After=graphical-session.target

[Service]
ExecStart=$PYTHON $SERVER
Restart=always
RestartSec=2

[Install]
WantedBy=default.target
EOF
msg "installed systemd unit at $UNIT"

systemctl --user daemon-reload
systemctl --user enable --now voxbridge.service
msg "voxbridge.service enabled and started"

# --- next steps ---
HTTP_PORT=8001
cat <<EOF

$(printf '\033[1;32mvoxbridge installed.\033[0m')

Next steps:
  1. Add the Hyprland keybind:
       cat $SCRIPT_DIR/../voxtype/hyprland-keybind.example.conf >> ~/.config/hypr/bindings.conf
       hyprctl reload

  2. One-time, inside the Windows VM (non-admin PowerShell):
       iex ((New-Object Net.WebClient).DownloadString('http://${NAT_IP:-<nat-ip>}:$HTTP_PORT/bootstrap.ps1'))

  3. Hold SUPER+CTRL+SHIFT+X, speak, release -> text appears in the focused
     VM window. (SUPER+CTRL+X still dictates locally as before.)

Check status:  systemctl --user status voxbridge.service
Live logs:     journalctl --user -u voxbridge.service -f
EOF
