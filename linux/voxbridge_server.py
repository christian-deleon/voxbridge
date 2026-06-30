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
import contextlib
import http.server
import logging
import os
import socket
import subprocess
import threading
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("voxbridge")

REPO = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = REPO / "windows"
CONFIG_PATH = Path.home() / ".config" / "voxbridge" / "config.toml"
TOKEN_PATH = Path.home() / ".config" / "voxbridge" / "token"

DEFAULTS = {
    "bind_ip": "",  # auto-detected from vmnet8 if empty
    "tcp_port": 5599,
    "http_port": 8001,
    "fifo_path": "",  # default: $XDG_RUNTIME_DIR/voxbridge.fifo
    "type_delay_ms": 20,
}


@dataclass(slots=True)
class Config:
    bind_ip: str
    tcp_port: int
    http_port: int
    fifo_path: str
    type_delay_ms: int


def detect_nat_ip() -> str:
    """Return the host's VMware NAT (vmnet8) IPv4 address, or 127.0.0.1."""
    try:
        out = subprocess.check_output(
            ["ip", "-4", "-o", "addr", "show", "dev", "vmnet8"],
            text=True,
        )
    except (OSError, subprocess.SubprocessError) as e:
        logger.debug("vmnet8 not available yet: %s", e)
        return "127.0.0.1"
    # find the "inet <addr>/<prefix>" token and return the bare address
    for tok in out.split():
        if tok.count(".") == 3 and "/" in tok:
            return tok.split("/", 1)[0]
    return "127.0.0.1"


def _bindable(ip: str) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((ip, 0))
    except OSError:
        return False
    else:
        return True
    finally:
        s.close()


def resolve_bind_ip(configured: str) -> str:
    """Block until the bind address is up.

    In auto mode (empty config) this waits for the VMware NAT interface to
    appear instead of falling back to loopback -- the daemon is useless bound
    to 127.0.0.1, and at boot it can start before vmnet8 exists.
    """
    attempt = 0
    while True:
        if configured:
            candidate = configured
        else:
            candidate = detect_nat_ip()
            if candidate == "127.0.0.1":
                candidate = ""  # vmnet8 not up yet
        if candidate and _bindable(candidate):
            return candidate
        if attempt % 15 == 0:
            logger.info(
                "waiting for bind address (%s)...", configured or "vmnet8 auto-detect"
            )
        attempt += 1
        time.sleep(2)


def load_config() -> Config:
    raw = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        raw.update(tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp"))
    return Config(
        bind_ip=str(raw["bind_ip"]),  # "" = auto, resolved at startup
        tcp_port=int(raw["tcp_port"]),
        http_port=int(raw["http_port"]),
        fifo_path=str(raw["fifo_path"]) or str(runtime / "voxbridge.fifo"),
        type_delay_ms=int(raw["type_delay_ms"]),
    )


def load_token() -> str:
    if not TOKEN_PATH.exists():
        raise SystemExit(f"token file missing ({TOKEN_PATH}); run install.sh first")
    return TOKEN_PATH.read_text(encoding="utf-8").strip()


# ---- shared state: the single authenticated guest connection -----------------


class State:
    lock = threading.Lock()
    conn: socket.socket | None = None


def set_conn(new: socket.socket | None) -> None:
    with State.lock:
        old = State.conn
        State.conn = new
    if old is not None and old is not new:
        with contextlib.suppress(OSError):
            old.close()


def send_text(text: str) -> None:
    payload = base64.b64encode(text.encode("utf-8")) + b"\n"
    with State.lock:
        conn = State.conn
    if conn is None:
        logger.info("no guest connected; dropped utterance (%d chars)", len(text))
        return
    try:
        conn.sendall(payload)
    except OSError as e:
        logger.warning("send failed (%s); clearing guest", e)
        set_conn(None)
    else:
        logger.info("forwarded utterance (%d chars)", len(text))


# ---- FIFO reader: each Voxtype --file write is one utterance ------------------


def fifo_loop(fifo_path: str) -> None:
    fifo = Path(fifo_path)
    if not fifo.exists():
        os.mkfifo(fifo, 0o600)
    logger.info("watching FIFO %s", fifo)
    while True:
        try:
            # open() blocks until Voxtype opens the FIFO for writing; the read
            # returns the whole transcription once Voxtype closes it.
            data = fifo.read_text(encoding="utf-8")
        except OSError as e:
            logger.warning("FIFO error (%s); retrying", e)
            time.sleep(1)
            continue
        text = data.rstrip("\n")
        if text:
            send_text(text)


# ---- TCP server: guest connects out, authenticates, then receives ------------


def watch_disconnect(conn: socket.socket) -> None:
    with contextlib.suppress(OSError):
        while conn.recv(256):
            pass
    logger.info("guest disconnected")
    with State.lock:
        if State.conn is conn:
            State.conn = None
    with contextlib.suppress(OSError):
        conn.close()


def tcp_loop(bind_ip: str, port: int, token: str) -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    for attempt in range(60):
        try:
            srv.bind((bind_ip, port))
            break
        except OSError as e:
            logger.warning(
                "bind %s:%d failed (%s); retry %d/60", bind_ip, port, e, attempt + 1
            )
            time.sleep(2)
    else:
        raise SystemExit("could not bind TCP socket")
    srv.listen(1)
    logger.info("listening for guest on %s:%d", bind_ip, port)
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
            logger.warning("rejected %s:%d (bad token)", addr[0], addr[1])
            conn.close()
            continue
        logger.info("guest authenticated from %s:%d", addr[0], addr[1])
        set_conn(conn)
        threading.Thread(target=watch_disconnect, args=(conn,), daemon=True).start()


# ---- HTTP server: serve the rendered PowerShell client/bootstrap -------------


def make_http_handler(
    cfg: Config, token: str
) -> type[http.server.BaseHTTPRequestHandler]:
    repl = {
        "{{HOST_IP}}": cfg.bind_ip,
        "{{TCP_PORT}}": str(cfg.tcp_port),
        "{{HTTP_PORT}}": str(cfg.http_port),
        "{{TOKEN}}": token,
        "{{DELAY}}": str(cfg.type_delay_ms),
    }

    def render(text: str) -> str:
        for key, value in repl.items():
            text = text.replace(key, value)
        return text

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802  (name required by http.server)
            name = self.path.lstrip("/").split("?", 1)[0]
            if name not in ("client.ps1", "bootstrap.ps1"):
                self.send_error(404)
                return
            tmpl = TEMPLATE_DIR / f"{name}.tmpl"
            if not tmpl.exists():
                self.send_error(404)
                return
            body = render(tmpl.read_text(encoding="utf-8")).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass  # silence default per-request stderr logging

    return Handler


def http_loop(cfg: Config, token: str) -> None:
    handler = make_http_handler(cfg, token)
    httpd = http.server.ThreadingHTTPServer((cfg.bind_ip, cfg.http_port), handler)
    logger.info("serving client/bootstrap on http://%s:%d", cfg.bind_ip, cfg.http_port)
    httpd.serve_forever()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    cfg = load_config()
    token = load_token()
    cfg.bind_ip = resolve_bind_ip(cfg.bind_ip)
    logger.info("voxbridge starting on %s (delay=%dms)", cfg.bind_ip, cfg.type_delay_ms)
    threading.Thread(target=fifo_loop, args=(cfg.fifo_path,), daemon=True).start()
    threading.Thread(target=http_loop, args=(cfg, token), daemon=True).start()
    tcp_loop(cfg.bind_ip, cfg.tcp_port, token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
