#!/usr/bin/env python3
"""Discord Gateway websocket client - stdlib only (socket + ssl), no deps.

ARCHITECTURE.md par. 3.4, Phase 5: replace the ~3s REST poll with a pushed
event stream so a message from the phone becomes an inbox envelope instantly.

Why hand-rolled instead of `websockets`/`discord.py`:

api.py is stdlib-only on purpose and the workspace travels as a zip to machines
that fill their own .env. A pip dependency *on the transport* means a machine
where the install silently failed is a machine with no bus at all - the one
component that must work before anything else can report that anything is wrong.
The subset of RFC 6455 + the Gateway protocol actually needed here is small:
connect, IDENTIFY, heartbeat, RESUME, MESSAGE_CREATE. Voice, sharding, compression
and rate-limit buckets are all out of scope, so the usual reasons to take a
library do not apply.

**The gateway is never the authority.** It supplies speed; REST supplies
completeness. watchdog.py keeps polling every mapped channel on a slow
reconciliation pass with `after=<lastId>`, so anything this client drops - a
missed frame, a bad resume, a bug in the code below - costs latency, not a
message. If the socket cannot be established at all, the watchdog simply keeps
its old 3-second poll and says so. Any change here must preserve that property.

Threading: run() owns one socket in one daemon thread and pushes message
payloads onto a queue.Queue. Nothing else in the watchdog becomes concurrent -
the main loop drains the queue, which is why "only the inner loop changes".
"""
import base64
import hashlib
import json
import os
import queue
import socket
import ssl
import struct
import threading
import time
import urllib.parse

GATEWAY_URL = "wss://gateway.discord.gg/?v=10&encoding=json"
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"   # RFC 6455 sec 1.3

# GUILDS (1<<0) | GUILD_MESSAGES (1<<9) | MESSAGE_CONTENT (1<<15).
# MESSAGE_CONTENT is PRIVILEGED: it must be ticked in the Developer Portal
# (Bot -> Privileged Gateway Intents) or IDENTIFY is closed with 4014. Without
# it messages arrive with an empty `content`, which for this bus means every
# envelope is blank - worse than not connecting, so we ask for it and treat a
# refusal as fatal-but-harmless (permanent REST fallback, loudly logged).
INTENTS = (1 << 0) | (1 << 9) | (1 << 15)

# Close codes Discord will never let us recover from by retrying. Reconnecting
# on these is a hot loop against the API for no benefit.
FATAL_CLOSE = {
    4004: "authentication failed - DISCORD_BOT_TOKEN is wrong",
    4010: "invalid shard",
    4011: "sharding required (this guild is too large for one connection)",
    4012: "invalid API version",
    4013: "invalid intents",
    4014: ("disallowed intents - tick 'MESSAGE CONTENT INTENT' in the Discord "
           "Developer Portal (Bot -> Privileged Gateway Intents)"),
}

OP_DISPATCH, OP_HEARTBEAT, OP_IDENTIFY = 0, 1, 2
OP_RESUME, OP_RECONNECT, OP_INVALID_SESSION = 6, 7, 9
OP_HELLO, OP_HEARTBEAT_ACK = 10, 11

WS_TEXT, WS_BINARY, WS_CLOSE, WS_PING, WS_PONG = 0x1, 0x2, 0x8, 0x9, 0xA


class GatewayClosed(Exception):
    """The socket ended. `code` is the websocket close code if we got one."""
    def __init__(self, msg, code=None):
        super().__init__(msg)
        self.code = code


# --- RFC 6455 framing ---------------------------------------------------------
# Kept as free functions so the suite can exercise them without a socket.

def accept_key(client_key):
    """The server's expected Sec-WebSocket-Accept for our key (RFC 6455 sec 4.1)."""
    digest = hashlib.sha1((client_key + WS_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def encode_frame(opcode, payload, mask=None):
    """One FIN frame. Client frames MUST be masked - an unmasked client frame is
    a protocol error and Discord drops the connection."""
    if mask is None:
        mask = os.urandom(4)
    head = bytes([0x80 | opcode])
    n = len(payload)
    if n < 126:
        head += bytes([0x80 | n])
    elif n < 65536:
        head += bytes([0x80 | 126]) + struct.pack(">H", n)
    else:
        head += bytes([0x80 | 127]) + struct.pack(">Q", n)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return head + mask + masked


def parse_frame(buf):
    """Parse one frame from the front of `buf`.

    Returns (fin, opcode, payload, consumed) or None when `buf` does not yet
    hold a whole frame. Nothing is consumed until a frame is complete, which is
    what makes a read timeout mid-frame harmless: the bytes stay in the buffer
    and the next call resumes exactly where this one stopped.
    """
    if len(buf) < 2:
        return None
    b0, b1 = buf[0], buf[1]
    fin = bool(b0 & 0x80)
    opcode = b0 & 0x0F
    masked = bool(b1 & 0x80)
    n = b1 & 0x7F
    off = 2
    if n == 126:
        if len(buf) < off + 2:
            return None
        n = struct.unpack(">H", buf[off:off + 2])[0]
        off += 2
    elif n == 127:
        if len(buf) < off + 8:
            return None
        n = struct.unpack(">Q", buf[off:off + 8])[0]
        off += 8
    key = b""
    if masked:                      # servers do not mask, but be correct anyway
        if len(buf) < off + 4:
            return None
        key = buf[off:off + 4]
        off += 4
    if len(buf) < off + n:
        return None
    payload = buf[off:off + n]
    if masked:
        payload = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
    return fin, opcode, payload, off + n


class WebSocket:
    """A client websocket good enough for one JSON stream."""

    def __init__(self, url, timeout=30):
        self.url = url
        self.timeout = timeout
        self.sock = None
        self._buf = b""

    def connect(self):
        u = urllib.parse.urlsplit(self.url)
        host = u.hostname
        port = u.port or (443 if u.scheme == "wss" else 80)
        path = (u.path or "/") + (f"?{u.query}" if u.query else "")

        raw = socket.create_connection((host, port), timeout=self.timeout)
        if u.scheme == "wss":
            ctx = ssl.create_default_context()
            raw = ctx.wrap_socket(raw, server_hostname=host)
        self.sock = raw

        key = base64.b64encode(os.urandom(16)).decode("ascii")
        req = (f"GET {path} HTTP/1.1\r\n"
               f"Host: {host}\r\n"
               "Upgrade: websocket\r\n"
               "Connection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\n"
               "Sec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(req.encode("ascii"))

        # Read headers only - anything after the blank line is already websocket
        # data and must stay in the buffer.
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise GatewayClosed("connection closed during handshake")
            head += chunk
            if len(head) > 65536:
                raise GatewayClosed("handshake response too large")
        head, _, rest = head.partition(b"\r\n\r\n")
        self._buf = rest
        lines = head.decode("latin-1").split("\r\n")
        if "101" not in lines[0]:
            raise GatewayClosed(f"handshake refused: {lines[0]}")
        got = ""
        for ln in lines[1:]:
            k, _, v = ln.partition(":")
            if k.strip().lower() == "sec-websocket-accept":
                got = v.strip()
        if got != accept_key(key):
            raise GatewayClosed("handshake accept key mismatch")

    def send_text(self, text):
        self.sock.sendall(encode_frame(WS_TEXT, text.encode("utf-8")))

    def recv_json(self, timeout):
        """Next JSON message, or None if `timeout` passed with none complete.

        Control frames are answered here and never surface to the caller.
        """
        deadline = time.monotonic() + timeout
        frags, frag_op = [], None
        while True:
            got = parse_frame(self._buf)
            if got is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self.sock.settimeout(remaining)
                try:
                    chunk = self.sock.recv(65536)
                except socket.timeout:
                    return None
                if not chunk:
                    raise GatewayClosed("connection closed by peer")
                self._buf += chunk
                continue

            fin, opcode, payload, consumed = got
            self._buf = self._buf[consumed:]

            if opcode == WS_CLOSE:
                code = struct.unpack(">H", payload[:2])[0] if len(payload) >= 2 else None
                reason = payload[2:].decode("utf-8", "replace")
                raise GatewayClosed(f"closed by server: {code} {reason}".strip(), code)
            if opcode == WS_PING:
                self.sock.sendall(encode_frame(WS_PONG, payload))
                continue
            if opcode == WS_PONG:
                continue

            if opcode in (WS_TEXT, WS_BINARY):
                frags, frag_op = [payload], opcode
            elif opcode == 0x0:                     # continuation
                frags.append(payload)
            if fin and frag_op is not None:
                data = b"".join(frags)
                frags, frag_op = [], None
                try:
                    return json.loads(data.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as e:
                    raise GatewayClosed(f"undecodable frame: {e}")

    def close(self):
        try:
            if self.sock:
                self.sock.sendall(encode_frame(WS_CLOSE, struct.pack(">H", 1000)))
        except OSError:
            pass
        try:
            if self.sock:
                self.sock.close()
        except OSError:
            pass
        self.sock = None


# --- the Discord protocol on top ----------------------------------------------

class Gateway:
    """Runs one Gateway connection in a daemon thread.

    Public surface, all the watchdog needs:
      .start()      - spawn the thread
      .events       - queue.Queue of MESSAGE_CREATE payloads (REST-shaped)
      .connected    - bool, live socket that has finished READY/RESUMED
      .fatal        - str or None; set once we know retrying cannot help
      .stop()       - ask the thread to wind down
    """

    def __init__(self, token, log=print, intents=INTENTS, url=GATEWAY_URL,
                 events=None, ws_factory=WebSocket):
        self.token = token
        self.log = log
        self.intents = intents
        self.url = url
        self.events = events if events is not None else queue.Queue()
        self.ws_factory = ws_factory

        self.connected = False
        self.fatal = None
        self.last_event_at = 0.0        # monotonic; watchdog reports it as health
        self.reconnects = 0

        self._stop = threading.Event()
        self._thread = None
        self._ws = None
        self._session_id = None
        self._resume_url = None
        self._seq = None

    # -- lifecycle --

    def start(self):
        self._thread = threading.Thread(target=self._run_forever, name="gateway",
                                        daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        ws = self._ws
        if ws:
            ws.close()

    def _run_forever(self):
        backoff = 1.0
        while not self._stop.is_set():
            try:
                self._session()
                backoff = 1.0                    # a clean session resets the ramp
            except GatewayClosed as e:
                self.connected = False
                if self._stop.is_set():
                    return
                if e.code in FATAL_CLOSE:
                    # Retrying cannot fix a wrong token or an unticked intent.
                    # Say exactly what to do and stop; the watchdog stays on REST.
                    self.fatal = FATAL_CLOSE[e.code]
                    self.log(f"gateway: {self.fatal} - staying on REST polling")
                    return
                self.log(f"gateway: {e} - reconnecting in {backoff:.0f}s")
            except (OSError, ssl.SSLError, ValueError) as e:
                self.connected = False
                # stop() closes the socket out from under a blocked recv, which
                # surfaces as WinError 10038. That is us shutting down, not a
                # fault - saying "reconnecting" here would be a lie in the log.
                if self._stop.is_set():
                    return
                self.log(f"gateway: {type(e).__name__}: {e} - reconnecting in {backoff:.0f}s")
            except Exception as e:                # never let the thread die quietly
                self.connected = False
                self.log(f"gateway: unexpected {type(e).__name__}: {e} - "
                         f"reconnecting in {backoff:.0f}s")
            finally:
                self.connected = False
                if self._ws:
                    self._ws.close()
                    self._ws = None
            if self._stop.wait(backoff):
                return
            self.reconnects += 1
            backoff = min(backoff * 2, 60.0)     # 1,2,4..60s - never a hot loop

    # -- one connection --

    def _session(self):
        resuming = bool(self._session_id and self._seq is not None)
        url = self._resume_url if resuming and self._resume_url else self.url
        if resuming and "?" not in url:
            url += "?v=10&encoding=json"
        self._ws = self.ws_factory(url)
        self._ws.connect()

        hello = self._ws.recv_json(timeout=30)
        if not hello or hello.get("op") != OP_HELLO:
            raise GatewayClosed(f"expected HELLO, got {hello!r}")
        interval = hello["d"]["heartbeat_interval"] / 1000.0

        if resuming:
            self._send({"op": OP_RESUME, "d": {"token": self.token,
                                               "session_id": self._session_id,
                                               "seq": self._seq}})
        else:
            self._identify()

        # Discord asks for the first heartbeat after interval*jitter; sending it
        # immediately is also legal and one less thing to get wrong on a resume.
        next_beat = time.monotonic() + interval
        awaiting_ack = False

        while not self._stop.is_set():
            wait = max(0.0, min(next_beat - time.monotonic(), 1.0))
            msg = self._ws.recv_json(timeout=wait)

            if msg is not None:
                if msg.get("s") is not None:
                    self._seq = msg["s"]
                op = msg.get("op")
                if op == OP_HEARTBEAT_ACK:
                    awaiting_ack = False
                elif op == OP_HEARTBEAT:
                    self._send({"op": OP_HEARTBEAT, "d": self._seq})
                    next_beat = time.monotonic() + interval
                elif op == OP_RECONNECT:
                    raise GatewayClosed("server asked us to reconnect")
                elif op == OP_INVALID_SESSION:
                    # d=false means the session cannot be resumed: forget it so
                    # the next attempt IDENTIFYs fresh instead of looping.
                    if not msg.get("d"):
                        self._session_id = self._seq = self._resume_url = None
                    raise GatewayClosed("session invalidated")
                elif op == OP_DISPATCH:
                    self._dispatch(msg)

            now = time.monotonic()
            if now >= next_beat:
                if awaiting_ack:
                    # A zombie connection: the socket is open, nothing is coming
                    # through it. Dropping it is the documented cure and is why
                    # the bus does not go quietly deaf.
                    raise GatewayClosed("heartbeat not acknowledged - zombie connection")
                self._send({"op": OP_HEARTBEAT, "d": self._seq})
                awaiting_ack = True
                next_beat = now + interval

    def _identify(self):
        self._send({
            "op": OP_IDENTIFY,
            "d": {
                "token": self.token,
                "intents": self.intents,
                "properties": {"os": "windows", "browser": "omnius", "device": "omnius"},
                # Presence is a free health indicator: the bot shows online in the
                # member list exactly while this socket is up, so "is the bus
                # alive?" is answerable from the phone without any command.
                "presence": {"status": "online", "afk": False,
                             "since": None,
                             "activities": [{"name": "the fleet", "type": 3}]},
            },
        })

    def _dispatch(self, msg):
        t = msg.get("t")
        d = msg.get("d") or {}
        if t == "READY":
            self._session_id = d.get("session_id")
            self._resume_url = d.get("resume_gateway_url")
            self.connected = True
            user = (d.get("user") or {}).get("username", "?")
            self.log(f"gateway: connected as {user} (session {str(self._session_id)[:8]}…)")
        elif t == "RESUMED":
            self.connected = True
            self.log("gateway: resumed - no messages missed")
        elif t == "MESSAGE_CREATE":
            self.last_event_at = time.monotonic()
            # The payload is the same shape REST returns, so the watchdog can
            # hand it to the existing handle_message() untouched.
            self.events.put(d)

    def _send(self, payload):
        self._ws.send_text(json.dumps(payload))
