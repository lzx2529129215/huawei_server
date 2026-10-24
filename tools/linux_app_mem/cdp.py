#!/usr/bin/env python3
"""Minimal Chrome DevTools Protocol client (no third-party packages).

The browser is started with --remote-debugging-port=PORT on a throwaway profile; this module
talks to it over a plain WebSocket (RFC 6455, client side only, text frames).
"""
import base64
import json
import os
import socket
import struct
import time
import urllib.request


class WS:
    def __init__(self, url, timeout=30):
        assert url.startswith("ws://")
        hostport, path = url[5:].split("/", 1)
        host, port = hostport.split(":")
        self.s = socket.create_connection((host, int(port)), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        self.s.sendall((f"GET /{path} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self.s.recv(4096)
            if not chunk:
                raise ConnectionError("websocket handshake failed")
            resp += chunk
        if b" 101 " not in resp.split(b"\r\n", 1)[0]:
            raise ConnectionError(resp.split(b"\r\n", 1)[0].decode())
        self.buf = resp.split(b"\r\n\r\n", 1)[1]

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.s.recv(65536)
            if not chunk:
                raise ConnectionError("websocket closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send(self, text):
        data = text.encode()
        n = len(data)
        hdr = bytearray([0x81])
        if n < 126:
            hdr.append(0x80 | n)
        elif n < 65536:
            hdr.append(0x80 | 126); hdr += struct.pack(">H", n)
        else:
            hdr.append(0x80 | 127); hdr += struct.pack(">Q", n)
        mask = os.urandom(4)
        hdr += mask
        self.s.sendall(bytes(hdr) + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def recv(self):
        parts = []
        while True:
            b0, b1 = self._read(2)
            op, n = b0 & 0x0F, b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            payload = self._read(n)
            if op == 8:
                raise ConnectionError("websocket closed by peer")
            if op == 9:                       # ping -> pong
                self.s.sendall(bytes([0x8A, 0x80 | len(payload)]) + os.urandom(4) * 0 + b"\0\0\0\0" + payload)
                continue
            parts.append(payload)
            if b0 & 0x80:
                return b"".join(parts).decode("utf-8", "replace")

    def close(self):
        try:
            self.s.close()
        except OSError:
            pass


class Session:
    """One DevTools connection (browser or page); call(method, **params) waits for the reply."""

    def __init__(self, ws_url):
        self.ws = WS(ws_url)
        self.n = 0
        self.events = []

    def call(self, method, timeout=60, **params):
        self.n += 1
        my = self.n
        self.ws.send(json.dumps({"id": my, "method": method, "params": params}))
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == my:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})
            self.events.append(msg)
            del self.events[:-200]
        raise TimeoutError(method)

    def js(self, expr, timeout=60):
        r = self.call("Runtime.evaluate", timeout=timeout, expression=expr, returnByValue=True, awaitPromise=True)
        if "exceptionDetails" in r:
            raise RuntimeError(f"js error: {r['exceptionDetails'].get('text')} in {expr[:80]}")
        return r.get("result", {}).get("value")

    def close(self):
        self.ws.close()


class Browser:
    def __init__(self, port, timeout=60):
        self.port = port
        end = time.monotonic() + timeout
        while True:
            try:
                self.version = self._get("/json/version")
                break
            except OSError:
                if time.monotonic() > end:
                    raise
                time.sleep(0.5)
        self.session = Session(self.version["webSocketDebuggerUrl"])

    def _get(self, path, method="GET"):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method)
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())

    def pages(self):
        return [t for t in self._get("/json/list") if t.get("type") == "page"]

    def page(self, target_id):
        t = next(t for t in self.pages() if t["id"] == target_id)
        return Session(t["webSocketDebuggerUrl"])

    def new_tab(self, url):
        return self.session.call("Target.createTarget", url=url)["targetId"]

    def activate(self, target_id):
        self.session.call("Target.activateTarget", targetId=target_id)

    def close(self):
        self.session.close()
