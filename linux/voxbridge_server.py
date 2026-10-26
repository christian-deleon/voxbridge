#!/usr/bin/env python3
"""voxbridge host daemon.

Bridges Voxtype transcriptions on the Linux host into the focused window of a
Windows VM as native keystrokes.

Data path (matches the proven prototype):

    Voxtype --file=<file>  ->  this daemon  ->  TCP push  ->  in-guest client
                                                              (SendInput)

The guest dials *out* to us (no inbound rule needed in the VM), authenticates
with a shared token, then receives one base64-encoded utterance per line and
types it into whatever window is focused in the guest. The same connection
also carries text clipboard changes in both directions.

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
import stat
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
    "output_file": "",  # default: $XDG_RUNTIME_DIR/voxbridge.txt
    "type_delay_ms": 20,
    "clipboard_sync": True,
}

CLIP_MAX_BYTES = 1 << 20  # larger text copies are not synced


@dataclass(slots=True)
class Config:
    bind_ip: str
    tcp_port: int
    http_port: int
    output_file: str
    type_delay_ms: int
    clipboard_sync: bool


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
        output_file=str(raw["output_file"]) or str(runtime / "voxbridge.txt"),
        type_delay_ms=int(raw["type_delay_ms"]),
        clipboard_sync=bool(raw["clipboard_sync"]),
    )


def load_token() -> str:
    if not TOKEN_PATH.exists():
        raise SystemExit(f"token file missing ({TOKEN_PATH}); run install.sh first")
    return TOKEN_PATH.read_text(encoding="utf-8").strip()


# ---- shared state: the single authenticated guest connection -----------------


class State:
    lock = threading.Lock()
    send_lock = threading.Lock()  # typing and clipboard pushes share the socket
    conn: socket.socket | None = None
    # last clipboard text synced in either direction; setting one side's
    # clipboard fires the other side's watcher, and this stops the echo
    last_clip: str | None = None


def set_conn(new: socket.socket | None) -> None:
    with State.lock:
        old = State.conn
        State.conn = new
    if old is not None and old is not new:
        with contextlib.suppress(OSError):
            old.close()


def notify(message: str) -> None:
    with contextlib.suppress(OSError):
        subprocess.Popen(["notify-send", "-a", "voxbridge", "voxbridge", message])


def push(kind: str, text: str) -> bool:
    """Send one framed message ("T" = type it, "C" = set clipboard) to the guest."""
    payload = kind.encode("ascii") + b" " + base64.b64encode(text.encode("utf-8")) + b"\n"
    with State.lock:
        conn = State.conn
    if conn is None:
        return False
    try:
        with State.send_lock:
            conn.sendall(payload)
    except OSError as e:
        logger.warning("send failed (%s); clearing guest", e)
        set_conn(None)
        return False
    return True


def send_text(text: str) -> None:
    if push("T", text):
        logger.info("forwarded utterance (%d chars)", len(text))
    else:
        logger.info("no guest connected; dropped utterance (%d chars)", len(text))
        notify("Windows client not connected; dictation dropped")


# ---- clipboard sync (text only) ----------------------------------------------


def normalize_clip(data: bytes) -> str:
    return data.decode("utf-8", "replace").replace("\r\n", "\n").rstrip("\x00")


def claim_clip(text: str) -> bool:
    """Record text as the synced clipboard; False if it is already current."""
    with State.lock:
        if text == State.last_clip:
            return False
        State.last_clip = text
        return True


def host_clipboard_loop() -> None:
    # wl-paste runs the command once per clipboard change (and once at start)
    # with the content on stdin; base64 makes each change exactly one line.
    cmd = ["wl-paste", "--type", "text", "--watch", "sh", "-c", "base64 -w0; echo"]
    while True:
        try:
            with subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
            ) as proc:
                assert proc.stdout is not None
                for line in proc.stdout:
                    try:
                        data = base64.b64decode(line.strip())
                    except ValueError:
                        continue
                    text = normalize_clip(data)
                    if not text or len(data) > CLIP_MAX_BYTES or not claim_clip(text):
                        continue
                    if push("C", text):
                        logger.info("clipboard -> guest (%d chars)", len(text))
        except OSError as e:
            logger.warning("clipboard watch failed (%s)", e)
        time.sleep(2)


def set_host_clipboard(data: bytes) -> None:
    text = normalize_clip(data)
    if not text or len(data) > CLIP_MAX_BYTES or not claim_clip(text):
        return
    try:
        subprocess.run(["wl-copy"], input=text.encode("utf-8"), timeout=5, check=True)
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning("wl-copy failed (%s)", e)
    else:
        logger.info("clipboard <- guest (%d chars)", len(text))


# ---- Voxtype output file: each completed --file write is one utterance -------


def inbox_loop(output_file: str) -> None:
    # Voxtype 1.1+ writes --file output atomically (temp file + rename, which
    # also replaces a FIFO at the path) and then drops "<path>.done". So the
    # file is complete the moment it appears; a FIFO here can never work.
    inbox = Path(output_file)
    done = Path(output_file + ".done")
    claimed = Path(output_file + ".sending")
    # anything already there was dictated while the daemon was down; typing it
    # minutes later into whatever is focused would be worse than dropping it
    for stale in (inbox, done, claimed):
        with contextlib.suppress(FileNotFoundError):
            stale.unlink()
    logger.info("watching %s", inbox)
    while True:
        time.sleep(0.1)
        try:
            # rename first: a new utterance landing mid-read gets its own file
            # instead of being deleted unread
            inbox.rename(claimed)
        except FileNotFoundError:
            with contextlib.suppress(FileNotFoundError):
                done.unlink()
            continue
        except OSError as e:
            logger.warning("cannot claim %s (%s)", inbox, e)
            time.sleep(1)
            continue
        try:
            if not stat.S_ISREG(claimed.stat().st_mode):
                logger.warning("%s is not a regular file; discarding", inbox)
                continue
            text = claimed.read_text(encoding="utf-8").rstrip("\n")
        except OSError as e:
            logger.warning("cannot read %s (%s)", inbox, e)
            continue
        finally:
            with contextlib.suppress(FileNotFoundError):
                claimed.unlink()
        if text:
            send_text(text)


# ---- TCP server: guest connects out, authenticates, then receives ------------


def guest_reader(conn: socket.socket, clipboard_sync: bool) -> None:
    """Handle guest -> host messages until the connection drops."""
    with contextlib.suppress(OSError):
        for line in conn.makefile("rb"):
            kind, _, b64 = line.strip().partition(b" ")
            if kind != b"C" or not clipboard_sync:
                continue
            try:
                set_host_clipboard(base64.b64decode(b64))
            except ValueError:
                logger.warning("malformed clipboard message from guest")
    logger.info("guest disconnected")
    with State.lock:
        if State.conn is conn:
            State.conn = None
    with contextlib.suppress(OSError):
        conn.close()


def tcp_loop(cfg: Config, token: str) -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    for attempt in range(60):
        try:
            srv.bind((cfg.bind_ip, cfg.tcp_port))
            break
        except OSError as e:
            logger.warning(
                "bind %s:%d failed (%s); retry %d/60",
                cfg.bind_ip,
                cfg.tcp_port,
                e,
                attempt + 1,
            )
            time.sleep(2)
    else:
        raise SystemExit("could not bind TCP socket")
    srv.listen(1)
    logger.info("listening for guest on %s:%d", cfg.bind_ip, cfg.tcp_port)
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
        # send live runtime config (per-char delay, clipboard sync on/off) first,
        # so the client uses current values even when it auto-reconnects after
        # a config change
        try:
            conn.sendall(
                f"{cfg.type_delay_ms} {int(cfg.clipboard_sync)}\n".encode("ascii")
            )
        except OSError:
            conn.close()
            continue
        logger.info(
            "guest authenticated from %s:%d (delay=%dms, clipboard=%s)",
            addr[0],
            addr[1],
            cfg.type_delay_ms,
            "on" if cfg.clipboard_sync else "off",
        )
        # without keepalive a guest that vanished (VM suspended, network reset)
        # looks connected forever and sendall() "succeeds" into the void
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 30)
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10)
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
        set_conn(conn)
        threading.Thread(
            target=guest_reader, args=(conn, cfg.clipboard_sync), daemon=True
        ).start()


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
    threading.Thread(target=inbox_loop, args=(cfg.output_file,), daemon=True).start()
    threading.Thread(target=http_loop, args=(cfg, token), daemon=True).start()
    if cfg.clipboard_sync:
        threading.Thread(target=host_clipboard_loop, daemon=True).start()
    tcp_loop(cfg, token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
