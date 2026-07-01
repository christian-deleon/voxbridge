# voxbridge

Voice-dictation bridge: a Linux host (running Voxtype) types transcriptions into
the focused window of a Windows VM as native keystrokes. Two components — a Python
daemon on the Linux host (`linux/`) and a PowerShell client served to the VM
(`windows/`).

## Layout
- `linux/voxbridge_server.py` — the daemon (Python 3.11+, **stdlib only**). Reads
  the Voxtype FIFO, serves the rendered client/bootstrap over HTTP, and pushes
  transcriptions over TCP to the in-guest client.
- `windows/client.ps1.tmpl`, `windows/bootstrap.ps1.tmpl` — PowerShell templates.
  The daemon fills `{{HOST_IP}}`, `{{TCP_PORT}}`, `{{HTTP_PORT}}`, `{{TOKEN}}`,
  `{{DELAY}}` at HTTP-serve time. **Edit the `.tmpl` files — there is no checked-in
  rendered copy.**
- `linux/install.sh` — idempotent host installer (token, config, ufw, systemd user
  service).
- `voxtype/hyprland-keybind.example.conf` — the trigger keybind.

## Commands
- Syntax-check before committing: `python3 -m py_compile linux/voxbridge_server.py`
  and `bash -n linux/install.sh`
- Install / reinstall on the host: `linux/install.sh`
- Service: `systemctl --user status|restart voxbridge`
- Live logs (connections, utterances): `journalctl --user -u voxbridge -f`
- Debug the daemon in the foreground: `python3 linux/voxbridge_server.py`

## Protocol (daemon ↔ in-guest client)
The guest dials **out** to the host TCP port and sends the shared token as its
first line. The host replies with one line = the current per-char delay (so the
client picks up config changes on every reconnect), then pushes one base64(UTF-8)
line per utterance; the client decodes and types each via `SendInput` (Unicode).
If you change the framing, update **both** `voxbridge_server.py` and
`client.ps1.tmpl` together.

## Conventions
- **Stdlib only** on the Python side — do not add third-party dependencies.
- **Never hardcode an IP.** The host IP is auto-detected from `vmnet8` and injected
  into the templates at serve time.
- **No personal or sensitive data** in tracked files — this repo is intended to be
  public. Keep descriptions generic ("types into the focused window in the VM").
- Match the comment density and style of the surrounding code.

## Never commit
- `~/.config/voxbridge/token` and `~/.config/voxbridge/config.toml` live outside the
  repo and are gitignored. Never commit a token.

## Gotchas
- Ports: TCP **5599** + HTTP **8001**, bound to the VMware NAT IP only; `ufw` scopes
  them to that subnet.
- FIFO at `$XDG_RUNTIME_DIR/voxbridge.fifo`, created by the daemon. One Voxtype
  `--file` write = one utterance.
- Restart the service after editing `config.toml`.
- Raise `type_delay_ms` if characters drop on a slow link.
