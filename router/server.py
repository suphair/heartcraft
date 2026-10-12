#!/usr/bin/env python3
"""Сетевая игра Сердцекрафта: страница игры и общий мир для всех, кто вошёл.

    python3 server.py [--port 8092] [--page index.html] [--password password] [--world world.json]

Сервер мир не считает: он хранит снимок, который прислал первый вошедший
(сид, океан и блоки в том же RLE, что и сохранение игры), и поверх него -
изменённые клетки. Новичок получает снимок с изменениями, дальше все шлют
друг другу через сервер поставленные и сломанные блоки и положения героев.
Сердечки, рыбки, сундуки и время суток у каждого игрока свои.

Вход по паролю комнаты - первая строка файла --password. Мир пишется в файл
--world не чаще раза в SAVE_EVERY секунд и при остановке, поэтому переживает
перезапуск роутера.

Всё на стандартной библиотеке: WebSocket (RFC 6455) разобран здесь же -
на роутере нет pip.
"""
import argparse
import base64
import hashlib
import http.server
import json
import os
import pathlib
import signal
import struct
import sys
import threading

W, H = 1024, 256
MAX_PLAYERS = 8
MAX_FRAME = 4 * 1024 * 1024
MAX_NAME = 16
SAVE_EVERY = 10
IDLE_TIMEOUT = 120
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class Closed(Exception):
    pass


def read_exact(rfile, n):
    data = rfile.read(n)
    if len(data) != n:
        raise Closed()
    return data


def read_frame(rfile):
    """Одно сообщение клиента: (opcode, payload); продолжения склеиваются."""
    opcode, chunks = None, []
    while True:
        b0, b1 = read_exact(rfile, 2)
        n = b1 & 0x7F
        if n == 126:
            n = struct.unpack(">H", read_exact(rfile, 2))[0]
        elif n == 127:
            n = struct.unpack(">Q", read_exact(rfile, 8))[0]
        if not b1 & 0x80 or n > MAX_FRAME:
            raise Closed()
        mask = read_exact(rfile, 4)
        data = bytearray(read_exact(rfile, n))
        for i in range(n):
            data[i] ^= mask[i & 3]
        op = b0 & 0x0F
        if op >= 8:
            return op, bytes(data)
        if op:
            opcode = op
        chunks.append(bytes(data))
        if sum(map(len, chunks)) > MAX_FRAME:
            raise Closed()
        if b0 & 0x80:
            return opcode, b"".join(chunks)


def frame(opcode, payload):
    n = len(payload)
    if n < 126:
        head = struct.pack(">BB", 0x80 | opcode, n)
    elif n < 65536:
        head = struct.pack(">BBH", 0x80 | opcode, 126, n)
    else:
        head = struct.pack(">BBQ", 0x80 | opcode, 127, n)
    return head + payload


class Player:
    def __init__(self, pid, name, conn):
        self.id = pid
        self.name = name
        self.conn = conn
        self.pos = None
        self.lock = threading.Lock()

    def send(self, msg):
        data = frame(1, json.dumps(msg, ensure_ascii=False, separators=(",", ":")).encode())
        with self.lock:
            try:
                self.conn.sendall(data)
            except OSError:
                pass

    def info(self):
        return {"id": self.id, "name": self.name, "pos": self.pos}


class Room:
    def __init__(self, password, path):
        self.password = password
        self.path = path
        self.lock = threading.Lock()
        self.players = {}
        self.next_id = 1
        self.world = None
        self.changes = {}
        self.dirty = False
        if path.exists():
            d = json.loads(path.read_text(encoding="utf-8"))
            self.world = {"seed": d["seed"], "ocean": d["ocean"], "tiles": d["tiles"]}
            self.changes = {int(i): t for i, t in d["changes"]}

    def snapshot(self):
        return dict(self.world, changes=list(self.changes.items()))

    def join(self, name, conn):
        with self.lock:
            if len(self.players) >= MAX_PLAYERS:
                return None, "В игре уже %d игроков" % MAX_PLAYERS
            p = Player(self.next_id, name, conn)
            self.next_id += 1
            others = [o.info() for o in self.players.values()]
            self.players[p.id] = p
            p.send({"t": "welcome", "id": p.id, "players": others,
                    "world": self.snapshot() if self.world else None})
            self.broadcast(p, {"t": "join", "id": p.id, "name": name})
            return p, None

    def leave(self, p):
        with self.lock:
            if self.players.pop(p.id, None):
                self.broadcast(p, {"t": "leave", "id": p.id})

    def broadcast(self, sender, msg):
        for o in list(self.players.values()):
            if o is not sender:
                o.send(msg)

    def handle(self, p, msg):
        t = msg.get("t")
        with self.lock:
            if t == "pos":
                x, y, f = msg.get("x"), msg.get("y"), msg.get("f")
                if not (num(x) and num(y) and f in (-1, 1)):
                    return False
                p.pos = {"x": x, "y": y, "f": f}
                self.broadcast(p, dict(p.pos, t="pos", id=p.id))
            elif t == "set":
                x, y, b = msg.get("x"), msg.get("y"), msg.get("id")
                if not (whole(x, W) and whole(y, H) and whole(b, 256)):
                    return False
                if self.world:
                    self.changes[y * W + x] = b
                    self.dirty = True
                    self.broadcast(p, {"t": "set", "x": x, "y": y, "id": b})
            elif t == "world":
                if self.world:
                    p.send({"t": "load", "world": self.snapshot()})
                    return True
                seed, ocean, tiles = msg.get("seed"), msg.get("ocean"), msg.get("tiles")
                if not (isinstance(seed, int) and isinstance(ocean, bool) and isinstance(tiles, str)):
                    return False
                self.world = {"seed": seed, "ocean": ocean, "tiles": tiles}
                self.changes = {}
                self.dirty = True
            else:
                return False
        return True

    def save(self):
        with self.lock:
            if not self.dirty:
                return
            data = json.dumps(self.snapshot(), separators=(",", ":"))
            self.dirty = False
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(data, encoding="utf-8")
        os.replace(tmp, self.path)


def num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v < max(W, H)


def whole(v, top):
    return isinstance(v, int) and not isinstance(v, bool) and 0 <= v < top


class Handler(http.server.BaseHTTPRequestHandler):
    timeout = IDLE_TIMEOUT
    room = None
    page = None

    def do_GET(self):
        if self.path == "/ws":
            self.websocket()
        elif self.path == "/net":
            self.send_response(204)
            self.end_headers()
        elif self.path in ("/", "/index.html"):
            self.send_page()
        else:
            self.send_error(404)

    def send_page(self):
        body = self.page.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def websocket(self):
        key = self.headers.get("Sec-WebSocket-Key")
        if self.headers.get("Upgrade", "").lower() != "websocket" or not key:
            self.send_error(400)
            return
        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.wfile.flush()
        self.close_connection = True
        p = None
        try:
            p = self.hello()
            if p:
                self.serve(p)
        except (Closed, OSError, ValueError):
            pass
        finally:
            if p:
                self.room.leave(p)
            self.bye()

    def receive(self):
        while True:
            op, data = read_frame(self.rfile)
            if op == 8:
                raise Closed()
            if op == 9:
                self.connection.sendall(frame(10, data))
            elif op == 1:
                msg = json.loads(data.decode())
                if not isinstance(msg, dict):
                    raise ValueError("не объект")
                return msg

    def hello(self):
        msg = self.receive()
        name = msg.get("name")
        if msg.get("t") != "hello" or not isinstance(name, str) or not name.strip():
            return self.refuse("Нужно имя")
        if msg.get("pass") != self.room.password:
            return self.refuse("Неверный пароль")
        p, why = self.room.join(name.strip()[:MAX_NAME], self.connection)
        return p or self.refuse(why)

    def refuse(self, why):
        self.connection.sendall(frame(1, json.dumps({"t": "error", "msg": why}, ensure_ascii=False).encode()))
        return None

    def serve(self, p):
        while self.room.handle(p, self.receive()):
            pass

    def bye(self):
        try:
            self.connection.sendall(frame(8, b""))
        except OSError:
            pass

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))


def saver(room, stop):
    while not stop.wait(SAVE_EVERY):
        room.save()


def main():
    here = pathlib.Path(__file__).resolve().parent
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8092)
    parser.add_argument("--page", type=pathlib.Path, default=here / "index.html")
    parser.add_argument("--password", type=pathlib.Path, default=here / "password")
    parser.add_argument("--world", type=pathlib.Path, default=here / "world.json")
    args = parser.parse_args()
    password = args.password.read_text(encoding="utf-8").splitlines()[0].strip()
    if not password:
        sys.exit("пустой пароль в " + str(args.password))
    if not args.page.is_file():
        sys.exit("нет страницы " + str(args.page))
    Handler.room = Room(password, args.world)
    Handler.page = args.page
    server = http.server.ThreadingHTTPServer(("", args.port), Handler)
    server.daemon_threads = True
    stop = threading.Event()
    threading.Thread(target=saver, args=(Handler.room, stop), daemon=True).start()
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown).start())
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    stop.set()
    Handler.room.save()


if __name__ == "__main__":
    main()
