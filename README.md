# voxbridge

Dictate by voice on a Linux host and have the text typed into a Windows VM — and
into whatever window is focused inside it — as native keystrokes. Your microphone
and speech-to-text stay on the Linux side; only the finished text crosses into the
VM, where it's regenerated as real key input.

Designed to pair with [Voxtype](https://voxtype.io): Voxtype transcribes,
voxbridge delivers it to the VM.

## What it does

- Your normal Voxtype keybind records and transcribes locally; when the VMware
  window has focus, the text is routed to the VM instead of typed on the host.
- A small non-admin client inside the VM receives the text and types it into the
  foreground window via `SendInput`.
- Because it's plain keystroke injection into the focused window, it works with
  any app in the VM — whatever is in front receives the text, exactly as if you
  were typing it there yourself.

## Design

The naive approach — injecting synthetic keystrokes straight from the Linux host
into the VMware window — produces garbled text, because the host→guest
scancode/keymap translation mangles the fake key events. voxbridge instead sends
the finished **text** across the boundary and regenerates keystrokes **natively
inside Windows**, which is reliable.

```
  keybind (Hyprland)
        │
        ▼
  Voxtype (local STT) ──record start --file=<file>──► voxbridge daemon (Linux)
                                                            │  base64 over TCP
                                                            ▼
                                                  in-guest client (PowerShell)
                                                            │  SendInput
                                                            ▼
                                                 focused window in the VM
```

- The guest dials **out** to the host (no inbound rule needed in the VM) and
  authenticates with a shared token.
- The host pushes one transcription per utterance; the client types it into
  whatever is focused.
- `voxbridge-dictate` decides per keypress: VMware focused → Voxtype writes the
  text to a file the daemon watches, anything else → Voxtype's normal output.
  One keybind, no modifier-timing ambiguity.

## Setup

### 1. Host (Linux)

```bash
linux/install.sh
```

Generates a shared token, writes `~/.config/voxbridge/config.toml`, auto-detects
the VMware NAT IP, ensures the `ufw` rules, installs + starts the `voxbridge`
systemd user service, and links `voxbridge-dictate` into `~/.local/bin`.

### 2. Hyprland keybind

Point your Voxtype push-to-talk start bind at `voxbridge-dictate` (see
`voxtype/hyprland-keybind.example.conf`), keep `voxtype record stop` on release,
then `hyprctl reload`.

### 3. Guest (Windows VM) — one time

In a normal (non-admin) PowerShell window:

```powershell
iex ((New-Object Net.WebClient).DownloadString('http://<nat-ip>:8001/bootstrap.ps1'))
```

(`install.sh` prints the exact line with your detected IP.) This installs a
per-user login autostart that fetches the latest client from the host and runs it
fully hidden (via a tiny VBScript launcher under `%LOCALAPPDATA%\voxbridge` —
needed because Windows still shows a console for plain `powershell -WindowStyle
Hidden` entries in `HKCU\...\Run`), then starts it immediately. No admin
required.

## Usage

Hold your dictation keybind (e.g. **SUPER+CTRL+X**), speak, release. With the
VMware window focused the text lands in the focused window in the VM; anywhere
else it types into the Linux app as usual. If the bridge is down you get a
notification instead of a silent drop.

## Configuration

`~/.config/voxbridge/config.toml`:

| key | default | meaning |
|-----|---------|---------|
| `bind_ip` | auto (`vmnet8`) | host IP the guest dials; set to pin it |
| `tcp_port` | `5599` | keystroke channel |
| `http_port` | `8001` | serves client/bootstrap to the guest |
| `output_file` | `$XDG_RUNTIME_DIR/voxbridge.txt` | where Voxtype writes each utterance |
| `type_delay_ms` | `20` | per-char delay; raise if chars drop on a slow link |

After changing config: `systemctl --user restart voxbridge`.

## Security

- The keystroke channel requires a **shared token**
  (`~/.config/voxbridge/token`, generated at install). The guest presents it
  before the host will type anything.
- The host binds only to the **isolated VMware NAT interface**, and `ufw` scopes
  the ports to that subnet.
- Tradeoff to know: the token is embedded in the client the host serves over HTTP,
  so on the NAT network it's defense-in-depth rather than a hard secret — anything
  that can already reach the host's NAT IP could fetch it. For a single-user host
  with only this VM on the NAT, that's an acceptable boundary.

## Troubleshooting

```bash
systemctl --user status voxbridge        # is the daemon up?
journalctl --user -u voxbridge -f        # live logs: connections, utterances
```

- **Nothing types:** confirm the guest client connected (the daemon logs
  `guest authenticated`). If not, check the VM can reach `http://<nat-ip>:8001/`.
- **Characters dropped/garbled:** raise `type_delay_ms`.
- **Wrong symbols in the VM:** match the VM's keyboard layout to the target's.

## Uninstall

```bash
systemctl --user disable --now voxbridge
rm ~/.config/systemd/user/voxbridge.service ~/.config/voxbridge -rf
rm ~/.local/bin/voxbridge-dictate
```

In the VM:

```powershell
Remove-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name voxbridge
Remove-Item -Recurse "$env:LOCALAPPDATA\voxbridge"
```
