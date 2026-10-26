# voxbridge

Constraints for coding agents. What this is, and how to install, configure,
and troubleshoot it: [README.md](README.md).

## Where to edit

- `linux/voxbridge_server.py` — the daemon. Python 3.11+, **stdlib only**.
- `windows/client.ps1.tmpl`, `windows/bootstrap.ps1.tmpl` — edit the templates.
  The daemon fills `{{HOST_IP}}`, `{{TCP_PORT}}`, `{{HTTP_PORT}}`, `{{TOKEN}}`,
  and `{{DELAY}}` when it serves them. Rendered `windows/*.ps1` copies are
  gitignored; do not check one in.
- `linux/install.sh` — host installer.
- `linux/voxbridge-dictate` — keybind entry point. Its output path
  (`VOXBRIDGE_OUTPUT`, default `$XDG_RUNTIME_DIR/voxbridge.txt`) has to be the
  daemon's `output_file`.
- `voxtype/hyprland-keybind.example.conf` — example trigger bind.

## Before you finish

Syntax-check the files you changed:

```bash
python3 -m py_compile linux/voxbridge_server.py
bash -n linux/install.sh
bash -n linux/voxbridge-dictate
```

## Protocol

The guest dials out and sends the shared token as its first line. The host
replies `<delay_ms> <clipboard 0|1>`. After that every line is
`<kind> <base64(UTF-8)>`:

- host → guest `T`: utterance; the client types it with `SendInput` (Unicode).
- either direction `C`: text clipboard, LF on the wire. Each side keeps the
  last synced text and drops echoes.

Change the framing in `voxbridge_server.py` and `client.ps1.tmpl` together.
The guest fetches the client again on login. A client that is already running
keeps the old framing until it restarts.

## Constraints

- **Stdlib only** on the Python side. Do not add third-party dependencies.
- **Never hardcode an IP.** Detect the host address from `vmnet8` and inject
  it at serve time.
- **No personal or sensitive data** in tracked files. This repo is public;
  keep descriptions generic.
- Match the comment density of the surrounding code.
- Never commit a token. `~/.config/voxbridge/token` and
  `~/.config/voxbridge/config.toml` live outside the repo. A copy named
  `token` or `config.toml` inside the tree is gitignored too.

## Gotchas

- TCP **5599** and HTTP **8001** bind to the VMware NAT address only, and
  `ufw` scopes them to that subnet. Do not widen the bind.
- Voxtype 1.1+ writes `--file` output with a temp file and a rename, then a
  `<path>.done` marker. The rename replaces whatever is at that path, so the
  watch target has to stay a regular file. The daemon polls
  `$XDG_RUNTIME_DIR/voxbridge.txt`, renames it aside, reads, and deletes.
  One file is one utterance.
- Restart the user service after editing `config.toml`. Keys, including
  `type_delay_ms`, are in the README.
