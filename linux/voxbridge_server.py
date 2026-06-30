#!/usr/bin/env python3
"""voxbridge host daemon.

Bridges Voxtype transcriptions on the Linux host into the focused window of a
Windows VM as native keystrokes.

Data path (matches the proven prototype):

    Voxtype --file=<fifo>  ->  this daemon  ->  TCP push  ->  in-guest client
                                                              (SendInput)

The guest dials *out* to us (no inbound rule needed in the VM), authenticates
with a shared token, then receives one base64-encoded utterance per line and
types it into whatever window is focused in the guest.

This daemon also serves the rendered PowerShell client/bootstrap over HTTP so
the guest can auto-fetch the latest version on login.
"""
from __future__ import annotations

import base64
import http.server
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

try:
    import tomllib  # py3.11+
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None

REPO = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = REPO / "windows"
CONFIG_PATH = Path(os.path.expanduser("~/.config/voxbridge/config.toml"))
TOKEN_PATH = Path(os.path.expanduser("~/.config/voxbridge/token"))

DEFAULTS = {
    "bind_ip": "",            # auto-detected from vmnet8 if empty
    "tcp_port": 5599,
    "http_port": 8001,
    "fifo_path": "",          # default: $XDG_RUNTIME_DIR/voxbridge.fifo
    "type_delay_ms": 20,
}


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def detect_nat_ip() -> str:
    try:
        out = subprocess.check_output(
            ["ip", "-4", "-o", "addr", "show", "dev", "vmnet8"], text=True
        )
        # find the "inet <addr>/<prefix>" token and return the bare address
        for tok in out.split():
            if tok.count(".") == 3 and "/" in tok:
                return tok.split("/")[0]
    except Exception as e:  # noqa: BLE001
        log(f"could not auto-detect vmnet8 IP: {e}")
    return "127.0.0.1"


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists() and tomllib is not None:
        with CONFIG_PATH.open("rb") as f:
            cfg.update(tomllib.load(f))
    if not cfg["bind_ip"]:
        cfg["bind_ip"] = detect_nat_ip()
    if not cfg["fifo_path"]:
        runtime = os.environ.get("XDG_RUNTIME_DIR", "/tmp")
        cfg["fifo_path"] = os.path.join(runtime, "voxbridge.fifo")
    return cfg


def load_token() -> str:
    if not TOKEN_PATH.exists():
        log(f"FATAL: token file missing ({TOKEN_PATH}); run install.sh first")
        sys.exit(1)
    return TOKEN_PATH.read_text().strip()


# ---- shared state: the single authenticated guest connection -----------------

class State:
    lock = threading.Lock()
    conn: socket.socket | None = None


def set_conn(new: socket.socket | None) -> None:
    with State.lock:
        old = State.conn
        State.conn = new
    if old is not None and old is not new:
        try:
            old.close()
        except OSError:
            pass


def send_text(text: str) -> None:
    payload = base64.b64encode(text.encode("utf-8")) + b"\n"
    with State.lock:
        conn = State.conn
    if conn is None:
        log(f"no guest connected; dropped utterance ({len(text)} chars)")
        return
    try:
        conn.sendall(payload)
        log(f"forwarded utterance ({len(text)} chars)")
    except OSError as e:  # noqa: BLE001
        log(f"send failed ({e}); clearing guest")
        set_conn(None)


# ---- FIFO reader: each Voxtype --file write is one utterance ------------------

def fifo_loop(fifo_path: str) -> None:
    if not os.path.exists(fifo_path):
        os.mkfifo(fifo_path, 0o600)
    log(f"watching FIFO {fifo_path}")
    while True:
        try:
            # open() blocks until Voxtype opens the FIFO for writing; read()
            # returns the whole transcription once Voxtype closes it.
            with open(fifo_path, "r", encoding="utf-8") as f:
                data = f.read()
        except OSError as e:  # noqa: BLE001
            log(f"FIFO error ({e}); retrying")
            time.sleep(1)
            continue
        text = data.rstrip("\n")
        if text:
            send_text(text)


# ---- TCP server: guest connects out, authenticates, then receives ------------

def watch_disconnect(conn: socket.socket) -> None:
    try:
        while conn.recv(256):
            pass
    except OSError:
        pass
    log("guest disconnected")
    with State.lock:
        if State.conn is conn:
            State.conn = None
    try:
        conn.close()
    except OSError:
        pass


def tcp_loop(bind_ip: str, port: int, token: str) -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    for attempt in range(60):
        try:
            srv.bind((bind_ip, port))
            break
        except OSError as e:  # noqa: BLE001
            log(f"bind {bind_ip}:{port} failed ({e}); retry {attempt + 1}/60")
            time.sleep(2)
    else:
        log("FATAL: could not bind TCP socket")
        sys.exit(1)
    srv.listen(1)
    log(f"listening for guest on {bind_ip}:{port}")
    while True:
        conn, addr = srv.accept()
        try:
            conn.settimeout(10)
            line = conn.makefile("rb").readline()
            conn.settimeout(None)
        except OSError:
            conn.close()
            continue
        if line.strip().decode("ascii", "replace") != token:
            log(f"rejected {addr[0]}:{addr[1]} (bad token)")
            conn.close()
            continue
        log(f"guest authenticated from {addr[0]}:{addr[1]}")
        set_conn(conn)
        threading.Thread(target=watch_disconnect, args=(conn,), daemon=True).start()


# ---- HTTP server: serve the rendered PowerShell client/bootstrap -------------

def make_http_handler(cfg: dict, token: str):
    repl = {
        "{{HOST_IP}}": cfg["bind_ip"],
        "{{TCP_PORT}}": str(cfg["tcp_port"]),
        "{{HTTP_PORT}}": str(cfg["http_port"]),
        "{{TOKEN}}": token,
        "{{DELAY}}": str(cfg["type_delay_ms"]),
    }

    def render(text: str) -> str:
        for k, v in repl.items():
            text = text.replace(k, v)
        return text

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            name = self.path.lstrip("/").split("?", 1)[0]
            if name not in ("client.ps1", "bootstrap.ps1"):
                self.send_error(404)
                return
            tmpl = TEMPLATE_DIR / f"{name}.tmpl"
            if not tmpl.exists():
                self.send_error(404)
                return
            body = render(tmpl.read_text()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):  # quiet
            pass

    return Handler


def http_loop(cfg: dict, token: str) -> None:
    handler = make_http_handler(cfg, token)
    httpd = http.server.ThreadingHTTPServer((cfg["bind_ip"], cfg["http_port"]), handler)
    log(f"serving client/bootstrap on http://{cfg['bind_ip']}:{cfg['http_port']}")
    httpd.serve_forever()


def main() -> None:
    cfg = load_config()
    token = load_token()
    log(f"voxbridge starting (delay={cfg['type_delay_ms']}ms)")
    threading.Thread(target=fifo_loop, args=(cfg["fifo_path"],), daemon=True).start()
    threading.Thread(target=http_loop, args=(cfg, token), daemon=True).start()
    tcp_loop(cfg["bind_ip"], cfg["tcp_port"], token)


if __name__ == "__main__":
    main()
