#!/usr/bin/env python3
"""Сердцекрафт по сети: страница игры и миры, в которые входят по коду.

    python3 server.py [--port 8092] [--page index.html] [--worlds worlds] [--data data] [--admin admin]

Миры перечислены в файле --worlds, строка на мир: «код название» (код - буквы
латиницы и цифры, по нему же называется папка мира в --data). Вход - логин и код
мира. Логин - это герой в этом мире: новый логин заводит учётку, она хранит вид героя
(его выбирают при первом входе), рюкзак, выбранную ячейку пояса и место героя -
сохранений в браузере у игры нет. Своего
пароля у учётки нет, её защищает код мира.

Админ входит тем же экраном, но логином и кодом из файла --admin («логин код» одной
строкой): видит все миры с кодами и героев с их видом, создаёт миры с настройками
генерации (сид, рельеф, океан, пещеры, руды, деревья, сердечки - <data>/<код>/settings.json),
удаляет миры и дарит героям вещи. Подарок приходит сразу, если герой в игре, иначе - при входе.
Удалённый мир не стирается, а уезжает в <data>/.deleted/.

Сервер мир не считает: он хранит снимок, который прислал первый вошедший в этот мир
(свой случайный сид, океан и блоки в RLE), поверх него - изменённые клетки и
содержимое сундуков. Вошедший получает всё это, дальше игроки шлют друг другу через
сервер блоки, сундуки и положения героев. Сердечки, рыбки и время суток у каждого свои.

Мир и учётки пишутся в <data>/<код>/ не чаще раза в SAVE_EVERY секунд и при остановке.
Всё на стандартной библиотеке: WebSocket (RFC 6455) разобран здесь же - на
роутере нет pip.
"""
import argparse
import base64
import hashlib
import http.server
import json
import os
import pathlib
import re
import secrets
import signal
import struct
import sys
import threading
import time

W, H = 1024, 256
SLOTS, CHEST_SLOTS, BELT = 36, 27, 9
LOOK_COLORS, LOOK_HATS = 8, 5
CODE = r"[A-Za-z0-9]{3,32}"
MAX_WORLD_NAME = 40
MAX_GIFT = 999
# Settings of a new world: how many choices each one has; the game knows what they mean.
SETTINGS = {"seed": 2 ** 31, "hills": 5, "ocean": 2, "caves": 4, "ores": 3, "trees": 4, "hearts": 4}
MAX_PLAYERS = 8
MAX_FRAME = 4 * 1024 * 1024
MAX_LOGIN = 16
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


def text_frame(msg):
    return frame(1, json.dumps(msg, ensure_ascii=False, separators=(",", ":")).encode())


def num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v < max(W, H)


def whole(v, top):
    return isinstance(v, int) and not isinstance(v, bool) and 0 <= v < top


def look_ok(v):
    """Вид героя: номер цвета и номер украшения - сами цвета и украшения знает игра."""
    return isinstance(v, dict) and set(v) == {"c", "h"} and whole(v["c"], LOOK_COLORS) and whole(v["h"], LOOK_HATS)


def settings_ok(v):
    return isinstance(v, dict) and set(v) == set(SETTINGS) and all(whole(v[k], top) for k, top in SETTINGS.items())


def slots_ok(v, n):
    """Ячейки рюкзака или сундука: null или [id, сколько] / [id, сколько, прочность]."""
    return isinstance(v, list) and len(v) == n and all(
        s is None or (isinstance(s, list) and len(s) in (2, 3) and all(whole(x, 100000) for x in s)) for s in v)


class Player:
    def __init__(self, room, pid, key, name, account, conn):
        self.room = room
        self.id = pid
        self.key = key
        self.name = name
        self.account = account
        self.conn = conn
        self.pos = None
        self.lock = threading.Lock()

    def send(self, msg):
        data = text_frame(msg)
        with self.lock:
            try:
                self.conn.sendall(data)
            except OSError:
                pass

    def kick(self, why):
        self.send({"t": "error", "msg": why})
        try:
            self.conn.shutdown(2)
        except OSError:
            pass

    def info(self):
        return {"id": self.id, "name": self.name, "look": self.account["look"], "pos": self.pos}


class Room:
    def __init__(self, code, name, folder):
        self.code = code
        self.name = name
        self.deleted = False
        self.io = threading.Lock()
        self.world_path = folder / "world.json"
        self.accounts_path = folder / "accounts.json"
        settings_path = folder / "settings.json"
        self.settings = json.loads(settings_path.read_text(encoding="utf-8")) if settings_path.exists() else None
        self.lock = threading.Lock()
        self.players = {}
        self.next_id = 1
        self.world = None
        self.changes = {}
        self.chests = {}
        self.accounts = {}
        self.dirty = False
        if self.world_path.exists():
            d = json.loads(self.world_path.read_text(encoding="utf-8"))
            self.world = {"seed": d["seed"], "ocean": d["ocean"], "tiles": d["tiles"]}
            self.changes = {int(i): t for i, t in d["changes"]}
            self.chests = {int(i): s for i, s in d["chests"]}
        if self.accounts_path.exists():
            self.accounts = json.loads(self.accounts_path.read_text(encoding="utf-8"))

    def snapshot(self):
        return dict(self.world, changes=list(self.changes.items()), chests=list(self.chests.items()))

    def join(self, login, conn):
        """Логин без учёта регистра - один герой: «Аня» и «аня» попадают в одну учётку."""
        key = login.casefold()
        with self.lock:
            twin = next((o for o in self.players.values() if o.key == key), None)
            if twin:
                twin.kick("Вы вошли в игру с другого устройства")
                self.players.pop(twin.id)
                self.broadcast(twin, {"t": "leave", "id": twin.id})
            if len(self.players) >= MAX_PLAYERS:
                return None, "В мире уже %d игроков" % MAX_PLAYERS
            acc = self.accounts.get(key)
            if acc is None:
                acc = {"name": login, "look": None, "inv": None, "sel": 0, "x": None, "y": None}
                self.accounts[key] = acc
                self.dirty = True
            p = Player(self, self.next_id, key, acc["name"], acc, conn)
            self.next_id += 1
            others = [o.info() for o in self.players.values()]
            self.players[p.id] = p
            me = {k: acc[k] for k in ("look", "inv", "sel", "x", "y")}
            p.send({"t": "welcome", "id": p.id, "name": p.name, "worldName": self.name, "players": others, "me": me,
                    "world": self.snapshot() if self.world else None, "settings": self.settings})
            self.broadcast(p, {"t": "join", "id": p.id, "name": p.name, "look": acc["look"]})
            if acc.get("gifts"):
                p.send({"t": "gift", "items": acc.pop("gifts")})
                self.dirty = True
            return p, None

    def leave(self, p):
        with self.lock:
            if self.players.get(p.id) is p:
                del self.players[p.id]
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
                p.account["x"], p.account["y"] = x, y
                self.dirty = True
                self.broadcast(p, dict(p.pos, t="pos", id=p.id))
            elif t == "me":
                inv, sel = msg.get("inv"), msg.get("sel")
                if not (slots_ok(inv, SLOTS) and whole(sel, BELT)):
                    return False
                p.account["inv"], p.account["sel"] = inv, sel
                self.dirty = True
            elif t == "look":
                look = msg.get("look")
                if not look_ok(look):
                    return False
                p.account["look"] = look
                self.dirty = True
                self.broadcast(p, {"t": "look", "id": p.id, "look": look})
            elif t == "set":
                x, y, b = msg.get("x"), msg.get("y"), msg.get("id")
                if not (whole(x, W) and whole(y, H) and whole(b, 256)):
                    return False
                if self.world:
                    self.changes[y * W + x] = b
                    self.chests.pop(y * W + x, None)
                    self.dirty = True
                    self.broadcast(p, {"t": "set", "x": x, "y": y, "id": b})
            elif t == "chest":
                x, y, slots = msg.get("x"), msg.get("y"), msg.get("slots")
                if not (whole(x, W) and whole(y, H) and slots_ok(slots, CHEST_SLOTS)):
                    return False
                if self.world:
                    if any(slots):
                        self.chests[y * W + x] = slots
                    else:
                        self.chests.pop(y * W + x, None)
                    self.dirty = True
                    self.broadcast(p, {"t": "chest", "x": x, "y": y, "slots": slots})
            elif t == "world":
                if self.world:
                    p.send({"t": "load", "world": self.snapshot(), "settings": self.settings})
                    return True
                seed, ocean, tiles = msg.get("seed"), msg.get("ocean"), msg.get("tiles")
                if not (isinstance(seed, int) and isinstance(ocean, bool) and isinstance(tiles, str)):
                    return False
                self.world = {"seed": seed, "ocean": ocean, "tiles": tiles}
                self.changes = {}
                self.chests = {}
                self.dirty = True
            else:
                return False
        return True

    def save(self):
        with self.io:
            with self.lock:
                if not self.dirty or self.deleted:
                    return
                world = json.dumps(self.snapshot(), separators=(",", ":")) if self.world else None
                accounts = json.dumps(self.accounts, ensure_ascii=False, separators=(",", ":"))
                self.dirty = False
            self.world_path.parent.mkdir(parents=True, exist_ok=True)
            if world:
                write(self.world_path, world)
            write(self.accounts_path, accounts)

    def state(self):
        with self.lock:
            online = {p.key for p in self.players.values()}
            heroes = [{"login": k, "name": a["name"], "look": a["look"], "online": k in online}
                      for k, a in self.accounts.items()]
        return {"code": self.code, "name": self.name, "settings": self.settings, "heroes": heroes}

    def give(self, key, item, n):
        with self.lock:
            acc = self.accounts.get(key)
            if acc is None:
                return "Нет такого героя"
            p = next((o for o in self.players.values() if o.key == key), None)
            if p:
                p.send({"t": "gift", "items": [[item, n]]})
            else:
                acc.setdefault("gifts", []).append([item, n])
                self.dirty = True
        return None

    def close(self, trash):
        """Выгоняет игроков и уносит папку мира в корзину: удалённый по ошибке мир можно вернуть руками."""
        with self.io:
            with self.lock:
                self.deleted = True
                for p in list(self.players.values()):
                    p.kick("Этот мир удалён")
                self.players.clear()
            folder = self.world_path.parent
            if folder.exists():
                trash.mkdir(parents=True, exist_ok=True)
                os.replace(folder, trash / ("%s-%d" % (self.code, time.time())))


class Worlds:
    """Миры по кодам и файл их списка. Битый список роняет запуск: молча потерянный мир хуже упавшего сервера."""

    def __init__(self, config, data):
        self.config = config
        self.data = data
        self.lock = threading.Lock()
        self.rooms = {}
        for line in config.read_text(encoding="utf-8").splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            code, _, name = line.strip().partition(" ")
            if not re.fullmatch(CODE, code) or not name.strip():
                sys.exit("в %s строка не «код название»: %s" % (config, line))
            if code in self.rooms:
                sys.exit("в %s код %s повторяется" % (config, code))
            self.rooms[code] = Room(code, name.strip(), data / code)
        if not self.rooms:
            sys.exit("в %s нет ни одного мира" % config)

    def get(self, code):
        with self.lock:
            return self.rooms.get(code)

    def all(self):
        with self.lock:
            return list(self.rooms.values())

    def state(self):
        return [room.state() for room in self.all()]

    def create(self, name, code, settings):
        name = name.strip() if isinstance(name, str) else ""
        code = code.strip() if isinstance(code, str) else ""
        if not name or len(name) > MAX_WORLD_NAME:
            return "Нужно название мира, до %d букв" % MAX_WORLD_NAME
        if not settings_ok(settings):
            return "Неверные настройки мира"
        with self.lock:
            while not code:
                code = str(secrets.randbelow(900000) + 100000)
                code = "" if code in self.rooms else code
            if not re.fullmatch(CODE, code):
                return "Код мира - от 3 до 32 латинских букв и цифр"
            if code in self.rooms or (self.data / code).exists():
                return "Мир с кодом %s уже есть" % code
            (self.data / code).mkdir(parents=True)
            write(self.data / code / "settings.json", json.dumps(settings))
            self.rooms[code] = Room(code, " ".join(name.split()), self.data / code)
            self.write_config()
        return None

    def delete(self, code):
        with self.lock:
            if code not in self.rooms:
                return "Нет такого мира"
            if len(self.rooms) == 1:
                return "Последний мир удалить нельзя"
            room = self.rooms.pop(code)
            self.write_config()
        room.close(self.data / ".deleted")
        return None

    def write_config(self):
        write(self.config, "".join("%s %s\n" % (code, room.name) for code, room in self.rooms.items()))


def write(path, text):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


class Handler(http.server.BaseHTTPRequestHandler):
    timeout = IDLE_TIMEOUT
    worlds = None
    admin = None
    page = None

    def do_GET(self):
        if self.path == "/ws":
            self.websocket()
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
            if p == "admin":
                p = None
                self.admin_session()
            elif p:
                self.serve(p)
        except (Closed, OSError, ValueError):
            pass
        finally:
            if p:
                p.room.leave(p)
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
        login = msg.get("login")
        if msg.get("t") != "hello" or not isinstance(login, str) or not login.strip():
            return self.refuse("Нужен логин")
        if len(login.strip()) > MAX_LOGIN:
            return self.refuse("Логин длиннее %d букв" % MAX_LOGIN)
        if (login.strip().casefold(), msg.get("code")) == self.admin:
            return "admin"
        room = self.worlds.get(msg.get("code"))
        if not room:
            return self.refuse("Неверный код мира")
        p, why = room.join(login.strip(), self.connection)
        return p or self.refuse(why)

    def refuse(self, why):
        self.connection.sendall(text_frame({"t": "error", "msg": why}))
        return None

    def serve(self, p):
        while p.room.handle(p, self.receive()):
            pass

    def admin_session(self):
        reply = {"t": "admin", "worlds": self.worlds.state(), "error": None}
        while True:
            self.connection.sendall(text_frame(reply))
            msg = self.receive()
            t, why = msg.get("t"), None
            if t == "create":
                why = self.worlds.create(msg.get("name"), msg.get("code"), msg.get("settings"))
            elif t == "delete":
                why = self.worlds.delete(msg.get("code"))
            elif t == "give":
                room, item, n = self.worlds.get(msg.get("code")), msg.get("id"), msg.get("n")
                if not room:
                    why = "Нет такого мира"
                elif not (whole(item, 256) and whole(n, MAX_GIFT + 1) and n > 0):
                    why = "Такой подарок не выдать"
                else:
                    why = room.give(msg.get("login"), item, n)
            elif t != "state":
                return
            reply = {"t": "admin", "worlds": self.worlds.state(), "error": why}

    def bye(self):
        try:
            self.connection.sendall(frame(8, b""))
        except OSError:
            pass

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))


def saver(worlds, stop):
    while not stop.wait(SAVE_EVERY):
        for room in worlds.all():
            room.save()


def read_admin(path):
    parts = path.read_text(encoding="utf-8").split()
    if len(parts) != 2:
        sys.exit("в %s нужна одна строка «логин код»" % path)
    return parts[0].casefold(), parts[1]


def main():
    here = pathlib.Path(__file__).resolve().parent
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8092)
    parser.add_argument("--page", type=pathlib.Path, default=here / "index.html")
    parser.add_argument("--worlds", type=pathlib.Path, default=here / "worlds")
    parser.add_argument("--data", type=pathlib.Path, default=here / "data")
    parser.add_argument("--admin", type=pathlib.Path, default=here / "admin")
    args = parser.parse_args()
    if not args.page.is_file():
        sys.exit("нет страницы " + str(args.page))
    Handler.worlds = Worlds(args.worlds, args.data)
    Handler.admin = read_admin(args.admin)
    Handler.page = args.page
    server = http.server.ThreadingHTTPServer(("", args.port), Handler)
    server.daemon_threads = True
    stop = threading.Event()
    threading.Thread(target=saver, args=(Handler.worlds, stop), daemon=True).start()
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown).start())
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    stop.set()
    for room in Handler.worlds.all():
        room.save()


if __name__ == "__main__":
    main()
