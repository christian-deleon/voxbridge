# voxbridge

Voice-dictation bridge: a Linux host (running Voxtype) types transcriptions into
the focused window of a Windows VM as native keystrokes. Two components — a Python
daemon on the Linux host (`linux/`) and a PowerShell client served to the VM
(`windows/`).

## Layout
- `linux/voxbridge_server.py` — the daemon (Python 3.11+, **stdlib only**). Picks
  up Voxtype's output file, serves the rendered client/bootstrap over HTTP, and
  pushes transcriptions over TCP to the in-guest client.
- `windows/client.ps1.tmpl`, `windows/bootstrap.ps1.tmpl` — PowerShell templates.
  The daemon fills `{{HOST_IP}}`, `{{TCP_PORT}}`, `{{HTTP_PORT}}`, `{{TOKEN}}`,
  `{{DELAY}}` at HTTP-serve time. **Edit the `.tmpl` files — there is no checked-in
  rendered copy.**
- `linux/install.sh` — idempotent host installer (token, config, ufw, systemd user
  service, `~/.local/bin/voxbridge-dictate` link).
- `linux/voxbridge-dictate` — keybind entry point; routes dictation to the
  output file only when a VMware window has focus.
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
first line. The host replies with one line `<delay_ms> <clipboard 0|1>` (so the
client picks up config changes on every reconnect). After that every line is
`<kind> <base64(UTF-8)>`:
- host → guest `T`: utterance; the client types it via `SendInput` (Unicode).
- host → guest `C` / guest → host `C`: text clipboard, LF line endings on the
  wire. Each side keeps the last synced text to suppress echoes.

If you change the framing, update **both** `voxbridge_server.py` and
`client.ps1.tmpl` together. The client is re-fetched on each guest login, so a
running client keeps the old framing until it restarts.

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
- Voxtype 1.1+ writes `--file` output atomically (temp + rename) then a
  `<path>.done` marker, so **a FIFO cannot work** — it gets replaced by a regular
  file. The daemon polls `$XDG_RUNTIME_DIR/voxbridge.txt`, renames it aside,
  reads, deletes. One file = one utterance.
- Restart the service after editing `config.toml`.
- Raise `type_delay_ms` if characters drop on a slow link.
