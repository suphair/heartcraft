#!/usr/bin/env python3
"""Сердцекрафт по сети: страница игры и миры, в которые входят по коду.

    python3 server.py [--port 8092] [--page index.html] [--worlds worlds] [--data data] [--admin admin]
                      [--claim host]

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
Удалённый мир не стирается, а уезжает в <data>/.deleted/. Ещё админ банит логины: забаненный
не войдёт ни в один мир, пока его не разбанят, и видит причину бана; список - <data>/bans.json. И пишет замечания к игре
(<data>/notes.json): у новых и изменённых «fixed»: null. Исправленным правкой файла ставится
версия игры с исправлением, пока сервер остановлен: файл читается только при старте. К замечанию
прикрепляются фото: JPEG в <data>/photos/, его отдаёт /photos/<имя> - имя случайное, и знает его только админ.

Сердечек, овец и рыбок считает сервер (Herd - те же правила, что у игры в одиночку): пока в мире
кто-то есть, мобы ходят, а их снимок раз в MOBS_SEND_EVERY уходит всем игрокам и хранится в
world.json. Первых мобов расселяет игрок, создавший мир, - присылает их снимок, пока у мира мобов
нет. Что сделали с мобами игроки - убили, влюбили, остригли, вылупили из яйца, - приходит серверу.

Игроки передают друг другу вещи: стопка уходит адресату подарком «от игрока», а если адресат
уже вышел - возвращается отправителю.

Мир, кроме мобов, сервер не считает: он хранит снимок, который прислал первый вошедший в этот мир
(свой случайный сид, океан и блоки в RLE), поверх него - изменённые клетки и
содержимое сундуков. Вошедший получает всё это, дальше игроки шлют друг другу через
сервер блоки, сундуки и положения героев. Время суток у каждого своё.

Мир и учётки пишутся в <data>/<код>/ не чаще раза в SAVE_EVERY секунд и при остановке.

С --claim сервер - хозяин данных, лежащих на шаре роутера: пишет в файл «свой IP токен»
(по IP роутер пробрасывает на него порт), выжидает CLAIM_WAIT, чтобы прежний хозяин
сохранился и ушёл, и только потом читает миры. Увидев в файле чужой токен, сохраняет
миры и выходит с кодом REPLACED - хозяином стала другая машина.

Всё на стандартной библиотеке: WebSocket (RFC 6455) разобран здесь же - на
роутере нет pip.
"""
import argparse
import base64
import hashlib
import http.server
import json
import math
import os
import pathlib
import random
import re
import secrets
import signal
import socket
import struct
import sys
import threading
import time

W, H = 1024, 256
SLOTS, CHEST_SLOTS, BELT = 36, 27, 9
LOOK_COLORS, LOOK_HATS = 8, 6
CODE = r"[A-Za-z0-9]{3,32}"
MAX_WORLD_NAME = 40
MAX_GIFT = 999
MAX_NOTE = 2000
MAX_PHOTO = 2 * 1024 * 1024
PHOTO = r"\d+-[0-9a-f]{16}\.jpg"
MAX_BAN_REASON = 200
MOB_KINDS = {"hearts": 9, "sheep": 5, "fish": 5}
MAX_MOBS = 300
MOB_ACTS = ("kill", "love", "hatch", "shear")
# The mobs as the game moves them in the single player game: speeds, gravity, water, love and growing up.
TICK, MOBS_SEND_EVERY = 0.05, 0.3
MOB_MAX, FISH_MAX = 80, 100
MOB_SPEED, MOB_JUMP, SHEEP_SPEED, FISH_SPEED = 2, 9, 1.2, 1.5
LOVE_TIME, BREED_PAUSE, GROW_TIME, WOOL_REGROW = 15, 30, 60, 1200
WHITE_CHANCE, WHITE_PAIR_CHANCE = 0.01, 0.2
GRAV, MAX_FALL, SWIM, SWIM_FALL, WATER_GRAV = 32, 22, 5, 3, 0.3
HEART_TONES, FISH_TONES = 4, 5
HEART_EGG, FISH_EGG, SHEEP_EGG = 108, 109, 119
BEDROCK = 14
# Blocks a body passes through: air, torch, water, seaweed, ladder, bed, green torch on any side of its block;
# the wet ones are water and seaweed.
OPEN = {0, 18, 23, 24, 25, 27, 48, 49, 50, 51}
WET = {23, 24}
# Settings of a new world: how many choices each one has; the game knows what they mean.
SETTINGS = {"seed": 2 ** 31, "hills": 6, "ocean": 2, "caves": 4, "ores": 3, "trees": 4, "hearts": 4}
MAX_PLAYERS = 8
MAX_FRAME = 4 * 1024 * 1024
MAX_LOGIN = 16
SAVE_EVERY = 10
IDLE_TIMEOUT = 120
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
ROUTER = "192.168.1.1"
CLAIM_WAIT = SAVE_EVERY + 5
REPLACED = 3


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


def real(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def mobs_ok(msg):
    """Снимок мобов: по каждому виду список строк из чисел, первое - номер моба; смысл чисел знает игра."""
    return all(isinstance(msg.get(kind), list) and len(msg[kind]) <= MAX_MOBS and all(
        isinstance(row, list) and len(row) == n and all(real(x) for x in row) for row in msg[kind])
        for kind, n in MOB_KINDS.items())


def slots_ok(v, n):
    """Ячейки рюкзака или сундука: null или [id, сколько] / [id, сколько, прочность]."""
    return isinstance(v, list) and len(v) == n and all(
        s is None or (isinstance(s, list) and len(s) in (2, 3) and all(whole(x, 100000) for x in s)) for s in v)


def decode_tiles(b64):
    """Блоки мира из RLE игры: id блока, за ним длина серии по 7 бит, старший бит - «дальше ещё»."""
    s = base64.b64decode(b64, validate=True)
    tiles = bytearray(W * H)
    p = i = 0
    while i < len(s):
        block = s[i]
        i += 1
        run = shift = 0
        while True:
            if i >= len(s):
                raise ValueError("карта обрывается")
            c = s[i]
            i += 1
            run |= (c & 127) << shift
            shift += 7
            if not c & 128:
                break
        if p + run > len(tiles):
            raise ValueError("в карте лишние блоки")
        tiles[p:p + run] = bytes([block]) * run
        p += run
    if p != len(tiles):
        raise ValueError("в карте не хватает блоков")
    return tiles


class Herd:
    """Мобы мира по правилам игры (index.html, updateMob/updateSheep/updateFish): блоки - общие с комнатой."""

    def __init__(self, tiles, rows):
        self.tiles = tiles
        self.hearts = [self.heart(r[1], r[2], r[4], r[5] == 1, int(r[0]), int(r[8]) % HEART_TONES,
                                  face=-1 if r[3] < 0 else 1, love=r[6], pause=r[7]) for r in rows["hearts"]]
        # Sheep of 2.7 and older had no wool to shear: their rows are one number shorter.
        self.sheep = [self.lamb(r[1], r[2], int(r[0]), face=-1 if r[3] < 0 else 1, shorn=r[4] if len(r) > 4 else 0)
                      for r in rows["sheep"]]
        self.fish = [self.fishy(r[1], r[2], int(r[0]), int(r[4]) % FISH_TONES, -1 if r[3] < 0 else 1)
                     for r in rows["fish"]]
        self.seq = max([o["id"] + 1 for o in self.hearts + self.sheep + self.fish], default=1)

    def next_id(self):
        self.seq += 1
        return self.seq - 1

    def heart(self, x, y, grow, white, mid=None, tone=None, face=1, love=0, pause=0):
        s = 0.32 if grow > 0 else 0.5
        return {"id": self.next_id() if mid is None else mid, "x": x, "y": y, "w": s, "h": s, "vx": 0, "vy": 0,
                "ground": False, "face": face, "dir": 0, "think": 0, "love": love, "pause": pause, "grow": grow,
                "white": white, "tone": random.randrange(HEART_TONES) if tone is None else tone}

    def lamb(self, x, y, mid=None, face=1, shorn=0):
        return {"id": self.next_id() if mid is None else mid, "x": x, "y": y, "w": 0.8, "h": 0.6, "vx": 0, "vy": 0,
                "ground": False, "face": face, "dir": 0, "think": 0, "shorn": shorn}

    def fishy(self, x, y, mid=None, tone=None, face=None):
        return {"id": self.next_id() if mid is None else mid, "x": x, "y": y, "vy": 0, "think": 0,
                "face": random.choice((-1, 1)) if face is None else face,
                "tone": random.randrange(FISH_TONES) if tone is None else tone}

    def rows(self):
        return {
            "hearts": [[m["id"], round(m["x"], 2), round(m["y"], 2), m["face"], round(max(0, m["grow"]), 2),
                        1 if m["white"] else 0, round(max(0, m["love"]), 2), round(m["pause"], 2), m["tone"]]
                       for m in self.hearts],
            "sheep": [[o["id"], round(o["x"], 2), round(o["y"], 2), o["face"], round(o["shorn"], 2)] for o in self.sheep],
            "fish": [[f["id"], round(f["x"], 2), round(f["y"], 2), f["face"], f["tone"]] for f in self.fish],
        }

    def get(self, x, y):
        return self.tiles[y * W + x] if 0 <= x < W and 0 <= y < H else BEDROCK

    def wet_cell(self, x, y):
        return self.get(math.floor(x), math.floor(y)) in WET

    def wet_at(self, e):
        return self.wet_cell(e["x"] + e["w"] / 2, e["y"] + e["h"] / 2)

    def collides(self, e, x, y):
        for ty in range(math.floor(y), math.floor(y + e["h"] - 1e-9) + 1):
            for tx in range(math.floor(x), math.floor(x + e["w"] - 1e-9) + 1):
                if ty >= 0 and self.get(tx, ty) not in OPEN:
                    return True
        return False

    def move_x(self, e, dx):
        e["x"] += dx
        if not self.collides(e, e["x"], e["y"]):
            return False
        e["x"] = math.floor(e["x"] + e["w"]) - e["w"] - 1e-6 if dx > 0 else math.floor(e["x"]) + 1
        e["vx"] = 0
        return True

    def move_y(self, e, dy):
        e["y"] += dy
        if e["y"] < 0:
            e["y"] = 0
            e["vy"] = max(0, e["vy"])
        if not self.collides(e, e["x"], e["y"]):
            return
        if dy > 0:
            e["y"] = math.floor(e["y"] + e["h"]) - e["h"] - 1e-6
            e["ground"] = True
        else:
            e["y"] = math.floor(e["y"]) + 1
        e["vy"] = 0

    def walk(self, e, d, speed, dt):
        """True, когда тело упёрлось в ступеньку в один блок и может на неё запрыгнуть."""
        wet = self.wet_at(e)
        e["vx"] += (d * speed - e["vx"]) * min(1, dt * (14 if e["ground"] else 6))
        e["vy"] = min(SWIM_FALL if wet else MAX_FALL, e["vy"] + GRAV * (WATER_GRAV if wet else 1) * dt)
        was_ground = e["ground"]
        e["ground"] = False
        steps = max(1, math.ceil(max(abs(e["vx"] * dt), abs(e["vy"] * dt)) / 0.3))
        blocked = False
        for _ in range(steps):
            blocked = self.move_x(e, e["vx"] * dt / steps) or blocked
            self.move_y(e, e["vy"] * dt / steps)
        return blocked and d != 0 and (was_ground or wet) and not self.collides(e, e["x"] + d * 0.1, e["y"] - 1.05)

    def step(self, dt):
        for m in list(self.hearts):
            self.step_heart(m, dt)
        for o in self.sheep:
            self.step_sheep(o, dt)
        for f in self.fish:
            self.step_fish(f, dt)

    def step_heart(self, m, dt):
        m["think"] -= dt
        if m["think"] <= 0:
            m["think"] = 1 + random.random() * 3
            m["dir"] = random.choice((-1, 0, 0, 1))
        d = m["dir"]
        m["pause"] = max(0, m["pause"] - dt)
        if m["love"] > 0:
            m["love"] -= dt
            mate = next((o for o in self.hearts if o is not m and o["love"] > 0 and
                         math.hypot(o["x"] - m["x"], o["y"] - m["y"]) < 8), None)
            if mate:
                d = 0 if abs(mate["x"] - m["x"]) < 0.2 else (1 if mate["x"] > m["x"] else -1)
                if math.hypot(mate["x"] - m["x"], mate["y"] - m["y"]) < 0.8:
                    self.breed(m, mate)
        if m["grow"] > 0:
            m["grow"] -= dt
            if m["grow"] <= 0 and not self.grow_up(m):
                m["grow"] = 1
        if d:
            m["face"] = d
        if self.wet_at(m):
            m["vy"] = min(m["vy"], -SWIM / 2)
        if self.walk(m, d, MOB_SPEED, dt):
            m["vy"] = -MOB_JUMP

    def grow_up(self, m):
        grown = dict(m, w=0.5, h=0.5, y=m["y"] + m["h"] - 0.5, x=m["x"] + (m["w"] - 0.5) / 2)
        if self.collides(grown, grown["x"], grown["y"]):
            return False
        m.update(w=0.5, h=0.5, x=grown["x"], y=grown["y"], grow=0)
        return True

    def breed(self, a, b):
        a["love"] = b["love"] = 0
        a["pause"] = b["pause"] = BREED_PAUSE
        white = random.random() < (WHITE_PAIR_CHANCE if a["white"] and b["white"] else WHITE_CHANCE)
        baby = self.heart((a["x"] + b["x"]) / 2, 0, GROW_TIME, white)
        baby["y"] = a["y"] + a["h"] - baby["h"]
        if self.collides(baby, baby["x"], baby["y"]):
            baby["x"] = a["x"]
        self.hearts.append(baby)

    def step_sheep(self, o, dt):
        o["shorn"] = max(0, o["shorn"] - dt)
        o["think"] -= dt
        if o["think"] <= 0:
            o["think"] = 2 + random.random() * 4
            o["dir"] = random.choice((-1, 0, 0, 0, 1))
        if o["dir"]:
            o["face"] = o["dir"]
        if self.wet_at(o):
            o["vy"] = min(o["vy"], -SWIM / 2)
        if self.walk(o, o["dir"], SHEEP_SPEED, dt):
            o["vy"] = -MOB_JUMP

    def step_fish(self, f, dt):
        f["think"] -= dt
        if f["think"] <= 0:
            f["think"] = 1 + random.random() * 3
            f["vy"] = (random.random() - 0.5) * 1.2
            if random.random() < 0.3:
                f["face"] = -f["face"]
        nx = f["x"] + f["face"] * FISH_SPEED * dt
        if self.wet_cell(nx + f["face"] * 0.3, f["y"]):
            f["x"] = nx
        else:
            f["face"] = -f["face"]
        ny = f["y"] + f["vy"] * dt
        if self.wet_cell(f["x"], ny - 0.2) and self.wet_cell(f["x"], ny + 0.2):
            f["y"] = ny
        else:
            f["vy"] = -f["vy"]

    def act(self, a):
        """Что игрок сделал с мобом; невозможное (моба уже нет, мир полон) молча не происходит."""
        if a["act"] == "kill":
            for herd in (self.hearts, self.sheep, self.fish):
                herd[:] = [o for o in herd if o["id"] != a.get("id")]
        elif a["act"] == "shear":
            o = next((o for o in self.sheep if o["id"] == a.get("id")), None)
            if o and o["shorn"] <= 0:
                o["shorn"] = WOOL_REGROW
        elif a["act"] == "love":
            m = next((o for o in self.hearts if o["id"] == a.get("id")), None)
            if m and m["grow"] <= 0 and m["love"] <= 0 and m["pause"] <= 0 and len(self.hearts) < MOB_MAX:
                m["love"] = LOVE_TIME
        elif a["act"] == "hatch" and 1 <= a.get("x", -1) < W - 1 and 0 <= a.get("y", -1) < H - 1:
            if a.get("egg") == HEART_EGG and len(self.hearts) < MOB_MAX:
                self.hearts.append(self.heart(a["x"] + 0.25, a["y"] + 0.5, 0, False))
            elif a.get("egg") == FISH_EGG and len(self.fish) < FISH_MAX:
                self.fish.append(self.fishy(a["x"] + 0.5, a["y"] + 0.5))
            elif a.get("egg") == SHEEP_EGG and len(self.sheep) < MOB_MAX:
                self.sheep.append(self.lamb(a["x"] + 0.1, a["y"] + 0.39))


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
        self.tiles = None
        self.herd = None
        self.changes = {}
        self.chests = {}
        self.accounts = {}
        self.dirty = False
        if self.world_path.exists():
            d = json.loads(self.world_path.read_text(encoding="utf-8"))
            self.world = {"seed": d["seed"], "ocean": d["ocean"], "tiles": d["tiles"]}
            self.changes = {int(i): t for i, t in d["changes"]}
            self.chests = {int(i): s for i, s in d["chests"]}
            self.tiles = decode_tiles(d["tiles"])
            for i, t in self.changes.items():
                self.tiles[i] = t
            if d.get("mobs"):
                self.herd = Herd(self.tiles, d["mobs"])
        if self.accounts_path.exists():
            self.accounts = json.loads(self.accounts_path.read_text(encoding="utf-8"))

    def snapshot(self):
        return dict(self.world, changes=list(self.changes.items()), chests=list(self.chests.items()),
                    mobs=self.herd.rows() if self.herd else None)

    def tick(self, dt, send):
        with self.lock:
            if not self.players or not self.herd:
                return
            self.herd.step(dt)
            self.dirty = True
            if send:
                self.broadcast(None, dict(self.herd.rows(), t="mobs"))

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
            me = dict({k: acc[k] for k in ("look", "inv", "sel", "x", "y")}, bed=acc.get("bed"))
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
                inv, sel, bed = msg.get("inv"), msg.get("sel"), msg.get("bed")
                bed_ok = bed is None or (isinstance(bed, list) and len(bed) == 2 and whole(bed[0], W) and whole(bed[1], H))
                if not (slots_ok(inv, SLOTS) and whole(sel, BELT) and bed_ok):
                    return False
                p.account["inv"], p.account["sel"], p.account["bed"] = inv, sel, bed
                self.dirty = True
            elif t == "mobs":
                if not mobs_ok(msg):
                    return False
                if self.world and not self.herd:
                    self.herd = Herd(self.tiles, msg)
                    self.dirty = True
                    self.broadcast(p, dict(self.herd.rows(), t="mobs"))
            elif t == "mobact":
                if msg.get("act") not in MOB_ACTS or not all(real(msg.get(k, 0)) for k in ("id", "egg", "x", "y")):
                    return False
                if self.herd:
                    self.herd.act(msg)
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
                    self.tiles[y * W + x] = b
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
            elif t == "pass":
                to, slot = msg.get("to"), msg.get("slot")
                if not (whole(to, self.next_id) and slot is not None and slots_ok([slot], 1)):
                    return False
                target = self.players.get(to)
                if target and target is not p:
                    target.send({"t": "gift", "items": [slot], "from": p.name, "fromId": p.id,
                                 "back": msg.get("back") is True})
                else:
                    p.send({"t": "gift", "items": [slot], "returned": True})
            elif t == "world":
                if self.world:
                    p.send({"t": "load", "world": self.snapshot(), "settings": self.settings})
                    return True
                seed, ocean, tiles = msg.get("seed"), msg.get("ocean"), msg.get("tiles")
                if not (isinstance(seed, int) and isinstance(ocean, bool) and isinstance(tiles, str)):
                    return False
                try:
                    self.tiles = decode_tiles(tiles)
                except ValueError:
                    return False
                self.world = {"seed": seed, "ocean": ocean, "tiles": tiles}
                self.herd = None
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

    def kick_login(self, key, why):
        with self.lock:
            for p in [o for o in self.players.values() if o.key == key]:
                p.kick(why)
                del self.players[p.id]
                self.broadcast(p, {"t": "leave", "id": p.id})

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
        self.bans_path = data / "bans.json"
        self.notes_path = data / "notes.json"
        self.photos = data / "photos"
        self.notes = json.loads(self.notes_path.read_text(encoding="utf-8")) if self.notes_path.exists() else []
        bans = json.loads(self.bans_path.read_text(encoding="utf-8")) if self.bans_path.exists() else []
        # Bans of 2.1-2.2 were bare logins, without a reason.
        self.bans = [{"login": b, "reason": ""} if isinstance(b, str) else b for b in bans]
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

    def ban_list(self):
        with self.lock:
            return [dict(b) for b in self.bans]

    def banned(self, login):
        """Что сказать забаненному, или None."""
        with self.lock:
            ban = next((b for b in self.bans if b["login"].casefold() == login.casefold()), None)
        if ban is None:
            return None
        return "Этот логин забанен" + (": " + ban["reason"] if ban["reason"] else "")

    def ban(self, login, reason):
        login = login.strip() if isinstance(login, str) else ""
        reason = " ".join(reason.split()) if isinstance(reason, str) else ""
        if not login or len(login) > MAX_LOGIN:
            return "Нужен логин, до %d букв" % MAX_LOGIN
        if len(reason) > MAX_BAN_REASON:
            return "Причина бана - до %d букв" % MAX_BAN_REASON
        if self.banned(login):
            return "%s уже забанен" % login
        with self.lock:
            self.bans.append({"login": login, "reason": reason})
            self.write_bans()
        for room in self.all():
            room.kick_login(login.casefold(), self.banned(login))
        return None

    def unban(self, login):
        with self.lock:
            left = [b for b in self.bans if not isinstance(login, str) or b["login"].casefold() != login.casefold()]
            if len(left) == len(self.bans):
                return "Такого бана нет"
            self.bans = left
            self.write_bans()
        return None

    def note_list(self):
        with self.lock:
            return [dict(n) for n in self.notes]

    def note(self, nid, text):
        text = text.strip() if isinstance(text, str) else ""
        if not text or len(text) > MAX_NOTE:
            return "Нужен текст замечания, до %d букв" % MAX_NOTE
        with self.lock:
            if nid is None:
                self.notes.append({"id": max((n["id"] for n in self.notes), default=0) + 1, "text": text,
                                   "fixed": None, "at": time.strftime("%Y-%m-%d %H:%M")})
            else:
                note = next((n for n in self.notes if n["id"] == nid), None)
                if note is None:
                    return "Нет такого замечания"
                note.update(text=text, fixed=None)
            self.write_notes()
        return None

    def drop_note(self, nid):
        with self.lock:
            left = [n for n in self.notes if n["id"] != nid]
            if len(left) == len(self.notes):
                return "Нет такого замечания"
            for name in next(n for n in self.notes if n["id"] == nid).get("photos", []):
                (self.photos / name).unlink(missing_ok=True)
            self.notes = left
            self.write_notes()
        return None

    def photo(self, nid, b64):
        """Фото к замечанию: JPEG в base64, игра сама ужимает его перед отправкой."""
        try:
            jpeg = base64.b64decode(b64, validate=True) if isinstance(b64, str) else b""
        except ValueError:
            jpeg = b""
        if not jpeg.startswith(b"\xff\xd8\xff") or len(jpeg) > MAX_PHOTO:
            return "Фото - JPEG до %d МБ" % (MAX_PHOTO // 1024 // 1024)
        with self.lock:
            note = next((n for n in self.notes if n["id"] == nid), None)
            if note is None:
                return "Нет такого замечания"
            name = "%d-%s.jpg" % (note["id"], secrets.token_hex(8))
            self.photos.mkdir(parents=True, exist_ok=True)
            write(self.photos / name, jpeg)
            note.setdefault("photos", []).append(name)
            self.write_notes()
        return None

    def write_notes(self):
        self.data.mkdir(parents=True, exist_ok=True)
        write(self.notes_path, json.dumps(self.notes, ensure_ascii=False, indent=1))

    def write_bans(self):
        self.data.mkdir(parents=True, exist_ok=True)
        write(self.bans_path, json.dumps(self.bans, ensure_ascii=False))

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


def write(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


class Handler(http.server.BaseHTTPRequestHandler):
    timeout = IDLE_TIMEOUT
    worlds = None
    admin = None
    page = None

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/ws":
            self.websocket()
        elif path in ("/", "/index.html"):
            self.send_body(self.page.read_bytes(), "text/html; charset=utf-8", "no-cache")
        elif path.startswith("/photos/") and re.fullmatch(PHOTO, path[8:]) and (self.worlds.photos / path[8:]).is_file():
            self.send_body((self.worlds.photos / path[8:]).read_bytes(), "image/jpeg", "max-age=31536000, immutable")
        else:
            self.send_error(404)

    def send_body(self, body, kind, cache):
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Cache-Control", cache)
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
        ban = self.worlds.banned(login.strip())
        if ban:
            return self.refuse(ban)
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
        reply = self.admin_state(None)
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
            elif t == "ban":
                why = self.worlds.ban(msg.get("login"), msg.get("reason"))
            elif t == "unban":
                why = self.worlds.unban(msg.get("login"))
            elif t == "note":
                why = self.worlds.note(msg.get("id"), msg.get("text"))
            elif t == "dropnote":
                why = self.worlds.drop_note(msg.get("id"))
            elif t == "photo":
                why = self.worlds.photo(msg.get("id"), msg.get("data"))
            elif t != "state":
                return
            reply = self.admin_state(why)

    def admin_state(self, why):
        return {"t": "admin", "worlds": self.worlds.state(), "bans": self.worlds.ban_list(),
                "notes": self.worlds.note_list(), "error": why}

    def bye(self):
        try:
            self.connection.sendall(frame(8, b""))
        except OSError:
            pass

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))


class Claim:
    def __init__(self, path, ip):
        self.path = path
        self.mark = "%s %s\n" % (ip, secrets.token_hex(8))
        # Written in place: over SMB Windows refuses to replace a file that someone else holds open.
        path.write_text(self.mark, encoding="utf-8")

    def held(self):
        try:
            return self.path.read_text(encoding="utf-8") == self.mark
        except FileNotFoundError:
            return False


def lan_ip():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((ROUTER, 9))
        return s.getsockname()[0]


def saver(worlds, stop, claim=None, server=None):
    while not stop.wait(SAVE_EVERY):
        for room in worlds.all():
            room.save()
        if claim and not claim.held():
            server.shutdown()
            return


def herder(worlds, stop):
    """Шаг мобов во всех мирах раз в TICK; отставший шаг не длиннее TICK - как кадр игры."""
    last = sent = time.monotonic()
    while not stop.wait(TICK):
        now = time.monotonic()
        send = now - sent >= MOBS_SEND_EVERY
        if send:
            sent = now
        for room in worlds.all():
            room.tick(min(TICK, now - last), send)
        last = now


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
    parser.add_argument("--claim", type=pathlib.Path)
    args = parser.parse_args()
    if not args.page.is_file():
        sys.exit("нет страницы " + str(args.page))
    claim = None
    if args.claim:
        claim = Claim(args.claim, lan_ip())
        print("хозяин данных: " + claim.mark.strip(), flush=True)
        time.sleep(CLAIM_WAIT)
    Handler.worlds = Worlds(args.worlds, args.data)
    Handler.admin = read_admin(args.admin)
    Handler.page = args.page
    server = http.server.ThreadingHTTPServer(("", args.port), Handler)
    server.daemon_threads = True
    stop = threading.Event()
    threading.Thread(target=saver, args=(Handler.worlds, stop, claim, server), daemon=True).start()
    threading.Thread(target=herder, args=(Handler.worlds, stop), daemon=True).start()
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown).start())
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    stop.set()
    for room in Handler.worlds.all():
        room.save()
    if claim and not claim.held():
        sys.exit(REPLACED)


if __name__ == "__main__":
    main()
