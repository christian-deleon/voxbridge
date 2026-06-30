#!/usr/bin/env bash
# voxbridge host installer -- token, config, firewall, and systemd user service.
# Idempotent: safe to re-run.
#
# Usage: install.sh
set -Eeuo pipefail
shopt -s inherit_errexit nullglob

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_dir=$(dirname -- "$script_dir")
server=$script_dir/voxbridge_server.py
cfg_dir=$HOME/.config/voxbridge
token_file=$cfg_dir/token
cfg_file=$cfg_dir/config.toml
unit=$HOME/.config/systemd/user/voxbridge.service

if [[ -t 1 && -z ${NO_COLOR-} ]] && command -v tput >/dev/null; then
  c_info=$(tput setaf 6)
  c_warn=$(tput setaf 3)
  c_ok=$(tput setaf 2)
  c_rst=$(tput sgr0)
else
  c_info=''
  c_warn=''
  c_ok=''
  c_rst=''
fi

msg() { printf '%s==>%s %s\n' "$c_info" "$c_rst" "$*"; }
warn() { printf '%s!!%s %s\n' "$c_warn" "$c_rst" "$*" >&2; }
die() {
  printf '%s: %s\n' "${0##*/}" "$*" >&2
  exit 1
}
trap 'die "line $LINENO: $BASH_COMMAND failed (exit $?)"' ERR

command -v python3 >/dev/null || die "python3 not found"
python3=$(command -v python3)

mkdir -p -- "$cfg_dir" "$(dirname -- "$unit")"

# --- token ---
if [[ ! -f $token_file ]]; then
  (
    umask 077
    openssl rand -hex 32 >"$token_file"
  )
  msg "generated shared token at $token_file"
else
  msg "token already present"
fi

# --- config ---
if [[ ! -f $cfg_file ]]; then
  cp -- "$script_dir/config.example.toml" "$cfg_file"
  msg "wrote default config to $cfg_file"
else
  msg "config already present ($cfg_file)"
fi

# --- detect VMware NAT network ---
nat_ip=$(ip -4 -o addr show dev vmnet8 2>/dev/null | awk '{print $4}' | cut -d/ -f1) || nat_ip=
if [[ -z $nat_ip ]]; then
  warn "could not detect vmnet8 IP; is VMware running? (firewall step skipped)"
  nat_subnet=
else
  nat_subnet=${nat_ip%.*}.0/24
  msg "VMware NAT: host=$nat_ip subnet=$nat_subnet"
fi

# --- firewall (only when ufw is active and sudo is available) ---
if [[ -n $nat_subnet ]] && command -v ufw >/dev/null; then
  if sudo -n true 2>/dev/null && sudo ufw status 2>/dev/null | grep -q 'Status: active'; then
    sudo ufw allow from "$nat_subnet" to any port 5599 proto tcp comment voxbridge
    sudo ufw allow from "$nat_subnet" to any port 8001 proto tcp comment voxbridge
    msg "ufw rules ensured for $nat_subnet (5599, 8001)"
  else
    warn "ufw present but no passwordless sudo; run these in a terminal:"
    printf '    sudo ufw allow from %s to any port 5599 proto tcp comment voxbridge\n' "$nat_subnet"
    printf '    sudo ufw allow from %s to any port 8001 proto tcp comment voxbridge\n' "$nat_subnet"
  fi
fi

# --- systemd user service ---
cat >"$unit" <<EOF
[Unit]
Description=voxbridge - voice dictation bridge to a Windows VM
After=graphical-session.target

[Service]
ExecStart=$python3 $server
Restart=always
RestartSec=2

[Install]
WantedBy=default.target
EOF
msg "installed systemd unit at $unit"

systemctl --user daemon-reload
systemctl --user enable --now voxbridge.service
msg "voxbridge.service enabled and started"

# --- next steps ---
printf '\n%svoxbridge installed.%s\n\n' "$c_ok" "$c_rst"
cat <<EOF
Next steps:
  1. Add the Hyprland keybind:
       cat $repo_dir/voxtype/hyprland-keybind.example.conf >> ~/.config/hypr/bindings.conf
       hyprctl reload

  2. One-time, inside the Windows VM (non-admin PowerShell):
       iex ((New-Object Net.WebClient).DownloadString('http://${nat_ip:-<nat-ip>}:8001/bootstrap.ps1'))

  3. Hold SUPER+CTRL+SHIFT+X, speak, release -> text appears in the focused
     window in the VM. (SUPER+CTRL+X still dictates locally as before.)

Check status:  systemctl --user status voxbridge.service
Live logs:     journalctl --user -u voxbridge.service -f
EOF
