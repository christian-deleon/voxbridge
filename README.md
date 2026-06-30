# voxbridge

Dictate by voice on a Linux host and have the text typed into a Windows VM — and
into whatever window is focused inside it — as native keystrokes. Your microphone
and speech-to-text stay on the Linux side; only the finished text crosses into the
VM, where it's regenerated as real key input.

Designed to pair with [Voxtype](https://voxtype.io): Voxtype transcribes,
voxbridge delivers it to the VM.

## What it does

- A dedicated keybind on Linux (separate from your normal Voxtype dictation)
  records and transcribes locally, then routes the text to the VM instead of
  typing it on the host.
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
  Voxtype (local STT) ──record start --file=<fifo>──► voxbridge daemon (Linux)
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

Your normal Voxtype keybind is untouched and still types into Linux apps.

## Setup

### 1. Host (Linux)

```bash
linux/install.sh
```

Generates a shared token, writes `~/.config/voxbridge/config.toml`, auto-detects
the VMware NAT IP, ensures the `ufw` rules, and installs + starts the `voxbridge`
systemd user service.

### 2. Hyprland keybind

```bash
cat voxtype/hyprland-keybind.example.conf >> ~/.config/hypr/bindings.conf
hyprctl reload
```

### 3. Guest (Windows VM) — one time

In a normal (non-admin) PowerShell window:

```powershell
iex ((New-Object Net.WebClient).DownloadString('http://<nat-ip>:8001/bootstrap.ps1'))
```

(`install.sh` prints the exact line with your detected IP.) This installs a
per-user login autostart that fetches the latest client from the host and runs it
hidden, then starts it immediately. Nothing is installed; no admin required.

## Usage

Hold the voxbridge keybind (default **SUPER+CTRL+SHIFT+X**), speak, release → the
text lands in the focused window in the VM. Your normal Voxtype keybind
(**SUPER+CTRL+X**) still dictates into Linux apps.

## Configuration

`~/.config/voxbridge/config.toml`:

| key | default | meaning |
|-----|---------|---------|
| `bind_ip` | auto (`vmnet8`) | host IP the guest dials; set to pin it |
| `tcp_port` | `5599` | keystroke channel |
| `http_port` | `8001` | serves client/bootstrap to the guest |
| `fifo_path` | `$XDG_RUNTIME_DIR/voxbridge.fifo` | where Voxtype writes |
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
```

In the VM:

```powershell
Remove-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name voxbridge
```
