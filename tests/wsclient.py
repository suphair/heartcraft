"""Minimal WebSocket client for tests and smoke checks against the live server."""
import base64
import json
import os
import socket
import ssl
import struct
import urllib.parse


class Client:
    def __init__(self, url, timeout=5):
        u = urllib.parse.urlsplit(url)
        port = u.port or (443 if u.scheme == "wss" else 80)
        sock = socket.create_connection((u.hostname, port), timeout=timeout)
        if u.scheme == "wss":
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=u.hostname)
        self.sock = sock
        self.buf = b""
        key = base64.b64encode(os.urandom(16)).decode()
        sock.sendall((f"GET {u.path} HTTP/1.1\r\nHost: {u.netloc}\r\nUpgrade: websocket\r\n"
                      f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        head = self.until(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise ConnectionError(head.decode(errors="replace"))

    def until(self, sep):
        while sep not in self.buf:
            self.more()
        head, self.buf = self.buf.split(sep, 1)
        return head

    def more(self):
        data = self.sock.recv(65536)
        if not data:
            raise ConnectionError("closed")
        self.buf += data

    def take(self, n):
        while len(self.buf) < n:
            self.more()
        data, self.buf = self.buf[:n], self.buf[n:]
        return data

    def send_raw(self, opcode, payload):
        mask = os.urandom(4)
        n = len(payload)
        if n < 126:
            head = struct.pack(">BB", 0x80 | opcode, 0x80 | n)
        elif n < 65536:
            head = struct.pack(">BBH", 0x80 | opcode, 0x80 | 126, n)
        else:
            head = struct.pack(">BBQ", 0x80 | opcode, 0x80 | 127, n)
        self.sock.sendall(head + mask + bytes(b ^ mask[i & 3] for i, b in enumerate(payload)))

    def send(self, msg):
        self.send_raw(1, json.dumps(msg).encode())

    def frame(self):
        b0, b1 = self.take(2)
        n = b1 & 0x7F
        if n == 126:
            n = struct.unpack(">H", self.take(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", self.take(8))[0]
        return b0 & 0x0F, self.take(n)

    def recv(self):
        """Next text message as a dict, or None when the server closed the socket."""
        while True:
            op, data = self.frame()
            if op == 8:
                return None
            if op == 1:
                return json.loads(data)

    def close(self):
        self.sock.close()
