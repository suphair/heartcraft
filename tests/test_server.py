import base64
import http.server
import json
import pathlib
import sys
import tempfile
import threading
import unittest
import unittest.mock
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "router"))
sys.path.insert(0, str(ROOT / "tests"))

import server  # noqa: E402
from wsclient import Client  # noqa: E402



def rle(tiles):
    """Blocks in the game's RLE: block id, then the run length in 7-bit pieces."""
    out, i = bytearray(), 0
    while i < len(tiles):
        run = 1
        while i + run < len(tiles) and tiles[i + run] == tiles[i]:
            run += 1
        out.append(tiles[i])
        r = run
        while r >= 128:
            out.append((r & 127) | 128)
            r >>= 7
        out.append(r)
        i += run
    return base64.b64encode(bytes(out)).decode()


# Air above row 100, stone from it down, and a pond of water hanging at x 40-59, y 60-79.
GROUND = bytearray(server.W * server.H)
for _y in range(server.H):
    for _x in range(server.W):
        GROUND[_y * server.W + _x] = 3 if _y >= 100 else 23 if 40 <= _x < 60 and 60 <= _y < 80 else 0
WORLD = {"t": "world", "seed": 42, "ocean": True, "tiles": rle(GROUND)}
INV = [[1, 5]] + [None] * 34 + [[110, 1, 30]]
CHEST = [[8, 4]] + [None] * 26
LOOK = {"c": 3, "h": 2}
NEW_HERO = {"look": None, "inv": None, "sel": 0, "x": None, "y": None, "bed": None}
MOBS = {"hearts": [[1, 10.5, 20, 1, 0, 0, 0, 0, 2]], "sheep": [[2, 30, 20.4, -1]], "fish": [[3, 50.5, 70.5, 1, 4]]}
SETTINGS = {"seed": 7, "hills": 4, "ocean": 0, "caves": 3, "ores": 2, "trees": 3, "hearts": 1}


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.data = pathlib.Path(self.dir.name) / "data"
        self.config = pathlib.Path(self.dir.name) / "worlds"
        self.config.write_text("# миры\nrose Розовая долина\n\nmint Мятный остров\n", encoding="utf-8")
        self.start()

    def start(self):
        server.Handler.log_message = lambda *args: None
        server.Handler.worlds = server.Worlds(self.config, self.data)
        server.Handler.admin = ("boss", "999")
        server.Handler.page = ROOT / "index.html"
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = "ws://127.0.0.1:%d/ws" % self.httpd.server_address[1]
        self.clients = []

    def stop(self):
        for c in self.clients:
            c.close()
        self.httpd.shutdown()
        self.httpd.server_close()

    def tearDown(self):
        self.stop()
        self.dir.cleanup()

    def save(self):
        for room in server.Handler.worlds.all():
            room.save()

    def join(self, login, code="rose"):
        c = Client(self.url)
        self.clients.append(c)
        c.send({"t": "hello", "login": login, "code": code})
        return c, c.recv()

    def synced(self, sender, watcher):
        """Waits until the server has handled everything the sender sent so far."""
        sender.send({"t": "pos", "x": 0.5, "y": 0.5, "f": 1})
        while True:
            msg = watcher.recv()
            if msg.get("t") == "pos" and msg["x"] == 0.5:
                return

    def test_serves_page(self):
        port = self.httpd.server_address[1]
        for path in ("/", "/?solo"):
            body = urllib.request.urlopen("http://127.0.0.1:%d%s" % (port, path)).read().decode()
            self.assertIn("<title>Сердцекрафт</title>", body)

    def test_wrong_code_is_refused(self):
        c, msg = self.join("Вася", "nope")
        self.assertEqual(msg, {"t": "error", "msg": "Неверный код мира"})
        self.assertIsNone(c.recv())

    def test_login_is_required(self):
        self.assertEqual(self.join("  ")[1], {"t": "error", "msg": "Нужен логин"})
        self.assertEqual(self.join("x" * 17)[1]["t"], "error")

    def test_worlds_list_is_checked(self):
        for text in ["", "ab Мир\n", "rose\n", "rose Мир\nrose Другой\n", "ро Мир\n"]:
            self.config.write_text(text, encoding="utf-8")
            with self.assertRaises(SystemExit):
                server.Worlds(self.config, self.data)

    def test_new_login_gets_new_hero_and_world_name(self):
        _, welcome = self.join("Аня")
        self.assertEqual((welcome["t"], welcome["name"], welcome["worldName"], welcome["me"], welcome["world"],
                          welcome["settings"]), ("welcome", "Аня", "Розовая долина", NEW_HERO, None, None))

    def test_first_player_shares_world_and_next_gets_it(self):
        a, _ = self.join("Аня")
        watcher, _ = self.join("Вера")
        a.send(WORLD)
        a.send({"t": "set", "x": 3, "y": 2, "id": 8})
        a.send({"t": "chest", "x": 5, "y": 6, "slots": CHEST})
        self.synced(a, watcher)
        a.send({"t": "pos", "x": 5.5, "y": 7, "f": -1})
        self.assertEqual(watcher.recv()["x"], 5.5)
        _, welcome = self.join("Боря")
        self.assertEqual(welcome["world"], {"seed": 42, "ocean": True, "tiles": WORLD["tiles"],
                                            "changes": [[2 * 1024 + 3, 8]], "chests": [[6 * 1024 + 5, CHEST]], "mobs": None})
        self.assertEqual(welcome["players"][0], {"id": 1, "name": "Аня", "look": None, "pos": {"x": 5.5, "y": 7, "f": -1}})

    def test_worlds_are_separate(self):
        a, _ = self.join("Аня", "rose")
        a.send(WORLD)
        a.send({"t": "me", "inv": INV, "sel": 2})
        b, welcome = self.join("Аня", "mint")
        self.assertEqual((welcome["world"], welcome["players"], welcome["worldName"], welcome["me"]),
                         (None, [], "Мятный остров", NEW_HERO))
        b.send(dict(WORLD, seed=7))
        b.send({"t": "set", "x": 1, "y": 1, "id": 3})
        watcher, _ = self.join("Вера", "mint")
        self.synced(b, watcher)
        self.save()
        self.assertEqual(json.loads((self.data / "rose" / "world.json").read_text())["seed"], 42)
        self.assertEqual(json.loads((self.data / "mint" / "world.json").read_text())["seed"], 7)

    def test_second_world_is_answered_with_the_shared_one(self):
        a, _ = self.join("Аня")
        b, _ = self.join("Боря")
        a.send(WORLD)
        self.assertEqual(a.recv()["t"], "join")
        b.send(dict(WORLD, seed=7))
        self.assertEqual(b.recv(), {"t": "load", "world": {"seed": 42, "ocean": True, "tiles": WORLD["tiles"],
                                                           "changes": [], "chests": [], "mobs": None}, "settings": None})

    def test_moves_blocks_chests_and_looks_reach_others_but_not_sender(self):
        a, _ = self.join("Аня")
        a.send(WORLD)
        b, _ = self.join("Боря")
        self.assertEqual(a.recv(), {"t": "join", "id": 2, "name": "Боря", "look": None})
        b.send({"t": "set", "x": 10, "y": 20, "id": 0})
        b.send({"t": "chest", "x": 4, "y": 4, "slots": CHEST})
        b.send({"t": "look", "look": LOOK})
        b.send({"t": "pos", "x": 1.25, "y": 2.5, "f": 1})
        self.assertEqual(a.recv(), {"t": "set", "x": 10, "y": 20, "id": 0})
        self.assertEqual(a.recv(), {"t": "chest", "x": 4, "y": 4, "slots": CHEST})
        self.assertEqual(a.recv(), {"t": "look", "id": 2, "look": LOOK})
        self.assertEqual(a.recv(), {"t": "pos", "id": 2, "x": 1.25, "y": 2.5, "f": 1})
        a.send({"t": "pos", "x": 3, "y": 4, "f": 1})
        self.assertEqual(b.recv(), {"t": "pos", "id": 1, "x": 3, "y": 4, "f": 1})

    def test_breaking_or_emptying_a_chest_forgets_it(self):
        a, _ = self.join("Аня")
        watcher, _ = self.join("Вера")
        a.send(WORLD)
        a.send({"t": "chest", "x": 4, "y": 4, "slots": CHEST})
        a.send({"t": "chest", "x": 5, "y": 4, "slots": CHEST})
        a.send({"t": "set", "x": 4, "y": 4, "id": 0})
        a.send({"t": "chest", "x": 5, "y": 4, "slots": [None] * 27})
        self.synced(a, watcher)
        _, welcome = self.join("Боря")
        self.assertEqual(welcome["world"]["chests"], [])

    def test_hero_is_kept_by_login(self):
        a, _ = self.join("Аня")
        watcher, _ = self.join("Вера")
        a.send(WORLD)
        a.send({"t": "look", "look": LOOK})
        a.send({"t": "me", "inv": INV, "sel": 3})
        a.send({"t": "pos", "x": 12.5, "y": 30, "f": 1})
        self.assertEqual(watcher.recv()["t"], "look")
        self.assertEqual(watcher.recv()["x"], 12.5)
        a.send_raw(8, b"")
        self.assertEqual(watcher.recv(), {"t": "leave", "id": 1})
        _, welcome = self.join("аНя")
        self.assertEqual(welcome["me"], {"look": LOOK, "inv": INV, "sel": 3, "x": 12.5, "y": 30, "bed": None})
        self.assertEqual(welcome["name"], "Аня")
        self.assertEqual(watcher.recv(), {"t": "join", "id": 3, "name": "Аня", "look": LOOK})

    def test_same_login_elsewhere_replaces_the_old_session(self):
        a, _ = self.join("Аня")
        watcher, _ = self.join("Вера")
        self.assertEqual(a.recv()["name"], "Вера")
        self.join("Аня")
        self.assertEqual(a.recv(), {"t": "error", "msg": "Вы вошли в игру с другого устройства"})
        self.assertEqual(watcher.recv(), {"t": "leave", "id": 1})
        self.assertEqual(watcher.recv(), {"t": "join", "id": 3, "name": "Аня", "look": None})

    def test_leave_is_announced(self):
        a, _ = self.join("Аня")
        b, _ = self.join("Боря")
        self.assertEqual(a.recv()["t"], "join")
        b.send_raw(8, b"")
        self.assertEqual(a.recv(), {"t": "leave", "id": 2})

    def test_bad_message_drops_only_its_sender(self):
        for bad in [{"t": "set", "x": 5000, "y": 1, "id": 1}, {"t": "me", "inv": INV[:-1], "sel": 0},
                    {"t": "me", "inv": INV, "sel": 9}, {"t": "chest", "x": 1, "y": 1, "slots": [[1]] + [None] * 26},
                    {"t": "look", "look": {"c": 8, "h": 0}}, {"t": "look", "look": {"c": 1}}, {"t": "dance"}]:
            a, _ = self.join("Аня")
            b, _ = self.join("Боря")
            self.assertEqual(a.recv()["t"], "join")
            b.send(bad)
            self.assertIsNone(b.recv(), bad)
            self.assertEqual(a.recv()["t"], "leave")
            a.close()

    def test_blocks_before_world_are_dropped(self):
        a, _ = self.join("Аня")
        watcher, _ = self.join("Вера")
        a.send({"t": "set", "x": 1, "y": 1, "id": 1})
        a.send(WORLD)
        self.synced(a, watcher)
        _, welcome = self.join("Боря")
        self.assertEqual(welcome["world"]["changes"], [])

    def test_world_is_limited_by_players(self):
        for i in range(server.MAX_PLAYERS):
            self.assertEqual(self.join("p%d" % i)[1]["t"], "welcome")
        self.assertEqual(self.join("лишний")[1], {"t": "error", "msg": "В мире уже 8 игроков"})
        self.assertNotIn("лишний", server.Handler.worlds.get("rose").accounts)
        self.assertEqual(self.join("гость", "mint")[1]["t"], "welcome")

    def test_world_and_heroes_survive_restart(self):
        a, _ = self.join("Аня")
        watcher, _ = self.join("Боря")
        a.send(WORLD)
        a.send({"t": "set", "x": 3, "y": 2, "id": 8})
        a.send({"t": "me", "inv": INV, "sel": 1})
        self.synced(a, watcher)
        self.save()
        self.assertFalse(server.Handler.worlds.get("rose").dirty)
        self.stop()
        self.start()
        _, welcome = self.join("Аня")
        self.assertEqual(welcome["world"]["changes"], [[2051, 8]])
        self.assertEqual(welcome["me"]["inv"], INV)
        self.assertEqual(welcome["players"], [])

    def test_claim_is_held_until_another_host_takes_it(self):
        path = pathlib.Path(self.dir.name) / "host"
        claim = server.Claim(path, "192.168.1.50")
        self.assertRegex(path.read_text(encoding="utf-8"), r"^192\.168\.1\.50 [0-9a-f]{16}\n$")
        self.assertTrue(claim.held())
        server.Claim(path, "192.168.1.60")
        self.assertFalse(claim.held())
        path.unlink()
        self.assertFalse(claim.held())

    def test_taken_claim_saves_worlds_and_stops_server(self):
        claim = server.Claim(pathlib.Path(self.dir.name) / "host", "192.168.1.50")
        a, _ = self.join("Аня")
        watcher, _ = self.join("Боря")
        a.send(WORLD)
        self.synced(a, watcher)
        stop = threading.Event()
        with unittest.mock.patch.object(server, "SAVE_EVERY", 0.05):
            saver = threading.Thread(target=server.saver, args=(server.Handler.worlds, stop, claim, self.httpd))
            saver.start()
            server.Claim(claim.path, "192.168.1.60")
            saver.join(5)
        stop.set()
        self.assertFalse(saver.is_alive())
        self.assertTrue((self.data / "rose" / "world.json").exists())

    def test_big_world_message_passes(self):
        a, _ = self.join("Аня")
        watcher, _ = self.join("Вера")
        tiles = rle(bytes([1, 2]) * (server.W * server.H // 2))
        a.send(dict(WORLD, tiles=tiles))
        self.synced(a, watcher)
        _, welcome = self.join("Боря")
        self.assertEqual(welcome["world"]["tiles"], tiles)

    def admin(self):
        c, state = self.join("Boss", "999")
        self.assertEqual((state["t"], state["error"]), ("admin", None))
        return c, state

    def test_admin_sees_worlds_and_heroes(self):
        a, _ = self.join("Аня")
        a.send({"t": "look", "look": LOOK})
        watcher, _ = self.join("Вера")
        self.synced(a, watcher)
        watcher.close()
        _, state = self.admin()
        rose, mint = state["worlds"]
        self.assertEqual((rose["code"], rose["name"], mint["code"], mint["heroes"]), ("rose", "Розовая долина", "mint", []))
        self.assertIn({"login": "аня", "name": "Аня", "look": LOOK, "online": True}, rose["heroes"])

    def test_admin_login_with_another_code_is_a_hero(self):
        self.assertEqual(self.join("boss", "rose")[1]["t"], "welcome")

    def test_admin_creates_worlds(self):
        c, _ = self.admin()
        c.send({"t": "create", "name": "  Горы   и  пещеры ", "code": "peaks", "settings": SETTINGS})
        state = c.recv()
        self.assertIsNone(state["error"])
        self.assertEqual(state["worlds"][-1], {"code": "peaks", "name": "Горы и пещеры", "settings": SETTINGS, "heroes": []})
        c.send({"t": "create", "name": "Без кода", "code": "", "settings": SETTINGS})
        code = c.recv()["worlds"][-1]["code"]
        self.assertRegex(code, r"^\d{6}$")
        self.assertEqual(self.config.read_text(encoding="utf-8").splitlines()[-2:], ["peaks Горы и пещеры", code + " Без кода"])
        _, welcome = self.join("Аня", "peaks")
        self.assertEqual((welcome["worldName"], welcome["settings"]), ("Горы и пещеры", SETTINGS))
        self.assertEqual(server.Worlds(self.config, self.data).get("peaks").settings, SETTINGS)

    def test_admin_create_is_checked(self):
        c, _ = self.admin()
        for name, code, settings in [("", "", SETTINGS), ("Мир", "rose", SETTINGS), ("Мир", "ко", SETTINGS),
                                     ("Мир", "", dict(SETTINGS, hills=5)), ("Мир", "", {"seed": 1})]:
            c.send({"t": "create", "name": name, "code": code, "settings": settings})
            state = c.recv()
            self.assertIsNotNone(state["error"], (name, code, settings))
            self.assertEqual(len(state["worlds"]), 2)

    def test_admin_deletes_a_world(self):
        a, _ = self.join("Аня", "mint")
        a.send(WORLD)
        watcher, _ = self.join("Вера", "mint")
        self.synced(a, watcher)
        self.save()
        c, _ = self.admin()
        c.send({"t": "delete", "code": "mint"})
        state = c.recv()
        self.assertEqual(([w["code"] for w in state["worlds"]], state["error"]), (["rose"], None))
        self.assertEqual(a.recv()["name"], "Вера")
        self.assertEqual(a.recv(), {"t": "error", "msg": "Этот мир удалён"})
        self.assertFalse((self.data / "mint").exists())
        self.assertEqual(len(list((self.data / ".deleted").iterdir())), 1)
        self.assertEqual(self.join("Аня", "mint")[1], {"t": "error", "msg": "Неверный код мира"})
        c.send({"t": "delete", "code": "rose"})
        self.assertEqual(c.recv()["error"], "Последний мир удалить нельзя")
        self.assertEqual(self.config.read_text(encoding="utf-8"), "rose Розовая долина\n")

    def test_admin_gifts_reach_heroes_online_and_later(self):
        a, _ = self.join("Аня")
        b, _ = self.join("Боря")
        self.assertEqual(a.recv()["t"], "join")
        b.send_raw(8, b"")
        self.assertEqual(a.recv()["t"], "leave")
        c, _ = self.admin()
        c.send({"t": "give", "code": "rose", "login": "аня", "id": 20, "n": 64})
        self.assertIsNone(c.recv()["error"])
        self.assertEqual(a.recv(), {"t": "gift", "items": [[20, 64]]})
        c.send({"t": "give", "code": "rose", "login": "боря", "id": 108, "n": 2})
        c.send({"t": "give", "code": "rose", "login": "боря", "id": 110, "n": 1})
        self.assertIsNone(c.recv()["error"])
        self.assertIsNone(c.recv()["error"])
        b, _ = self.join("Боря")
        self.assertEqual(b.recv(), {"t": "gift", "items": [[108, 2], [110, 1]]})
        b.close()
        _, welcome = self.join("Боря")
        self.assertEqual(welcome["t"], "welcome")
        for bad in [{"login": "нет"}, {"code": "x"}, {"n": 0}, {"n": 1000}, {"id": 300}]:
            c.send(dict({"t": "give", "code": "rose", "login": "аня", "id": 20, "n": 1}, **bad))
            self.assertIsNotNone(c.recv()["error"], bad)


    def test_look_can_change_to_the_pink_crown(self):
        a, _ = self.join("Аня")
        b, _ = self.join("Боря")
        self.assertEqual(a.recv()["t"], "join")
        b.send({"t": "look", "look": LOOK})
        b.send({"t": "look", "look": {"c": 3, "h": 5}})
        self.assertEqual(a.recv(), {"t": "look", "id": 2, "look": LOOK})
        self.assertEqual(a.recv(), {"t": "look", "id": 2, "look": {"c": 3, "h": 5}})
        b.send({"t": "look", "look": {"c": 3, "h": 6}})
        self.assertIsNone(b.recv())

    def test_players_pass_things_and_they_come_back_from_a_player_who_left(self):
        a, _ = self.join("Аня")
        b, _ = self.join("Боря")
        self.assertEqual(a.recv()["t"], "join")
        a.send({"t": "pass", "to": 2, "slot": [114, 1, 70]})
        self.assertEqual(b.recv(), {"t": "gift", "items": [[114, 1, 70]], "from": "Аня", "fromId": 1, "back": False})
        b.send({"t": "pass", "to": 1, "slot": [4, 30], "back": True})
        self.assertEqual(a.recv(), {"t": "gift", "items": [[4, 30]], "from": "Боря", "fromId": 2, "back": True})
        b.send_raw(8, b"")
        self.assertEqual(a.recv()["t"], "leave")
        a.send({"t": "pass", "to": 2, "slot": [4, 5]})
        self.assertEqual(a.recv(), {"t": "gift", "items": [[4, 5]], "returned": True})

    def test_a_bad_pass_drops_the_sender(self):
        for bad in [{"to": [1]}, {"to": 9}, {"slot": None}, {"slot": [4]}, {"slot": "x"}]:
            c, _ = self.join("Аня")
            c.send(dict({"t": "pass", "to": 1, "slot": [4, 5]}, **bad))
            self.assertIsNone(c.recv(), bad)

    def test_a_banned_login_cannot_play_anywhere_until_unbanned(self):
        a, _ = self.join("Аня")
        watcher, _ = self.join("Вера")
        self.assertEqual(a.recv()["t"], "join")
        c, state = self.admin()
        self.assertEqual(state["bans"], [])
        c.send({"t": "ban", "login": "АНЯ", "reason": "  ломала  чужие дома "})
        reply = c.recv()
        self.assertEqual((reply["error"], reply["bans"]), (None, [{"login": "АНЯ", "reason": "ломала чужие дома"}]))
        banned = {"t": "error", "msg": "Этот логин забанен: ломала чужие дома"}
        self.assertEqual(a.recv(), banned)
        self.assertEqual(watcher.recv(), {"t": "leave", "id": 1})
        for code in ("rose", "mint"):
            self.assertEqual(self.join("аня", code)[1], banned)
        for bad in [{"login": "аня"}, {"login": "Боря", "reason": "я" * 201}]:
            c.send(dict({"t": "ban"}, **bad))
            self.assertIsNotNone(c.recv()["error"], bad)
        c.send({"t": "ban", "login": "Вася"})
        self.assertEqual(c.recv()["bans"][1], {"login": "Вася", "reason": ""})
        self.assertEqual(self.join("вася")[1], {"t": "error", "msg": "Этот логин забанен"})
        self.stop()
        self.start()
        self.assertEqual(self.join("Аня")[1], banned)
        c, _ = self.admin()
        c.send({"t": "unban", "login": "аня"})
        reply = c.recv()
        self.assertEqual((reply["error"], reply["bans"]), (None, [{"login": "Вася", "reason": ""}]))
        _, welcome = self.join("Аня")
        self.assertEqual(welcome["t"], "welcome")
        c.send({"t": "unban", "login": "аня"})
        self.assertIsNotNone(c.recv()["error"])

    def test_bans_of_bare_logins_still_hold(self):
        self.stop()
        self.data.mkdir(parents=True, exist_ok=True)
        (self.data / "bans.json").write_text(json.dumps(["Аня"]), encoding="utf-8")
        self.start()
        self.assertEqual(self.join("аня")[1], {"t": "error", "msg": "Этот логин забанен"})
        _, state = self.admin()
        self.assertEqual(state["bans"], [{"login": "Аня", "reason": ""}])


    def test_admin_writes_notes_and_sees_them_marked_done_in_the_file_after_a_restart(self):
        c, state = self.admin()
        self.assertEqual(state["notes"], [])
        c.send({"t": "note", "text": "  Добавить лук  "})
        notes = c.recv()["notes"]
        self.assertEqual([(n["id"], n["text"], n["fixed"]) for n in notes], [(1, "Добавить лук", None)])
        self.assertRegex(notes[0]["at"], r"^\d{4}-\d\d-\d\d \d\d:\d\d$")
        c.send({"t": "note", "text": "Больше рыбок"})
        self.assertEqual([n["id"] for n in c.recv()["notes"]], [1, 2])
        path = self.data / "notes.json"
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        on_disk[0]["fixed"] = "2.2"
        path.write_text(json.dumps(on_disk, ensure_ascii=False), encoding="utf-8")
        c.send({"t": "state"})
        self.assertEqual([n["fixed"] for n in c.recv()["notes"]], [None, None])
        self.stop()
        self.start()
        c, state = self.admin()
        self.assertEqual([n["fixed"] for n in state["notes"]], ["2.2", None])
        c.send({"t": "note", "id": 1, "text": "Добавить лук и стрелы"})
        reply = c.recv()
        self.assertEqual((reply["error"], reply["notes"][0]["text"], reply["notes"][0]["fixed"]),
                         (None, "Добавить лук и стрелы", None))
        for bad in [{"text": " "}, {"text": "я" * 2001}, {"text": 5}, {"id": 9, "text": "x"}]:
            c.send(dict({"t": "note"}, **bad))
            self.assertIsNotNone(c.recv()["error"], bad)
        c.send({"t": "dropnote", "id": 1})
        reply = c.recv()
        self.assertEqual((reply["error"], [n["id"] for n in reply["notes"]]), (None, [2]))
        self.assertEqual([n["id"] for n in json.loads(path.read_text(encoding="utf-8"))], [2])
        c.send({"t": "dropnote", "id": 1})
        self.assertIsNotNone(c.recv()["error"])


    def mobs(self, c):
        while True:
            msg = c.recv()
            if msg["t"] == "mobs":
                return msg

    def run_mobs(self, seconds):
        room = server.Handler.worlds.get("rose")
        for _ in range(round(seconds / server.TICK)):
            room.tick(server.TICK, False)
        room.tick(0, True)

    def test_the_first_mobs_come_once_and_survive_a_restart(self):
        a, _ = self.join("Аня")
        b, _ = self.join("Боря")
        self.assertEqual(a.recv()["t"], "join")
        b.send({"t": "mobs", **MOBS})
        a.send(WORLD)
        self.synced(a, b)
        b.send({"t": "mobs", **MOBS})
        self.assertEqual(a.recv(), dict(MOBS, t="mobs"))
        a.send({"t": "mobs", **MOBS, "hearts": []})
        self.synced(a, b)
        self.save()
        self.stop()
        self.start()
        _, welcome = self.join("Аня")
        self.assertEqual(welcome["world"]["mobs"], MOBS)

    def test_the_server_moves_the_mobs_for_everyone(self):
        a, _ = self.join("Аня")
        b, _ = self.join("Боря")
        a.send(WORLD)
        a.send({"t": "mobs", **MOBS})
        self.synced(a, b)
        self.run_mobs(5)
        for c in (a, b):
            mobs = self.mobs(c)
            heart, lamb, fish = mobs["hearts"][0], mobs["sheep"][0], mobs["fish"][0]
            self.assertAlmostEqual(heart[2], 99.5, delta=0.01)
            self.assertAlmostEqual(lamb[2], 99.4, delta=0.01)
            self.assertTrue(40 <= fish[1] < 60 and 60 <= fish[2] < 80, fish)

    def test_the_mobs_stand_still_without_players(self):
        a, _ = self.join("Аня")
        a.send(WORLD)
        a.send({"t": "mobs", **MOBS})
        a.send_raw(8, b"")
        self.assertIsNone(a.recv())
        room = server.Handler.worlds.get("rose")
        while room.players:
            pass
        self.run_mobs(1)
        self.assertEqual(room.snapshot()["mobs"], MOBS)

    def test_what_the_players_do_reaches_the_mobs(self):
        a, _ = self.join("Аня")
        b, _ = self.join("Боря")
        a.send(WORLD)
        a.send({"t": "mobs", **MOBS})
        self.synced(a, b)
        b.send({"t": "mobact", "act": "kill", "id": 2})
        b.send({"t": "mobact", "act": "love", "id": 1})
        b.send({"t": "mobact", "act": "hatch", "egg": 108, "x": 10, "y": 95})
        b.send({"t": "mobact", "act": "hatch", "egg": 109, "x": 45, "y": 70})
        b.send({"t": "mobact", "act": "hatch", "egg": 109, "x": 0, "y": 70})
        b.send({"t": "mobact", "act": "kill"})
        self.synced(b, a)
        self.run_mobs(0)
        mobs = self.mobs(a)
        self.assertEqual(mobs["sheep"], [])
        self.assertEqual(mobs["hearts"][0][6], server.LOVE_TIME)
        self.assertEqual([m[0] for m in mobs["hearts"][1:]], [4])
        self.assertEqual([[f[0], f[1], f[2]] for f in mobs["fish"][1:]], [[5, 45.5, 70.5]])

    def test_a_broken_block_lets_a_mob_fall(self):
        a, _ = self.join("Аня")
        b, _ = self.join("Боря")
        a.send(WORLD)
        a.send({"t": "mobs", **MOBS})
        a.send({"t": "set", "x": 30, "y": 100, "id": 0})
        self.synced(a, b)
        with unittest.mock.patch("server.random.choice", return_value=0):
            self.run_mobs(5)
        self.assertAlmostEqual(self.mobs(b)["sheep"][0][2], 100.4, delta=0.01)

    def test_hearts_in_love_get_a_baby(self):
        hearts = [[1, 10, 99.5, 1, 0, 0, 15, 0, 0], [2, 10.3, 99.5, 1, 0, 1, 15, 0, 1]]
        herd = server.Herd(GROUND, {"hearts": hearts, "sheep": [], "fish": []})
        herd.step(server.TICK)
        baby = herd.rows()["hearts"][2]
        self.assertEqual((baby[0], baby[4]), (3, server.GROW_TIME))
        self.assertEqual([m[6] for m in herd.rows()["hearts"][:2]], [0, 0])
        for _ in range(round((server.GROW_TIME + 1) / server.TICK)):
            herd.step(server.TICK)
        self.assertEqual(herd.rows()["hearts"][2][4], 0)

    def test_bad_mobs_drop_the_sender(self):
        for bad in [{"t": "mobs", **MOBS, "fish": [[1, 2]]}, {"t": "mobs", "hearts": []},
                    {"t": "mobs", **MOBS, "sheep": [[1, 2, 3, "x"]]}, {"t": "mobact", "act": "eat", "id": 1},
                    {"t": "mobact", "act": "kill", "id": "1"}, dict(WORLD, tiles="AAE="),
                    dict(WORLD, tiles="не base64")]:
            c, _ = self.join("Аня")
            c.send(bad)
            self.assertIsNone(c.recv(), bad)

    def test_the_bed_is_kept_with_the_hero(self):
        a, _ = self.join("Аня")
        a.send({"t": "me", "inv": INV, "sel": 0, "bed": [40, 60]})
        a.send({"t": "pos", "x": 1, "y": 1, "f": 1})
        a.close()
        for _ in range(50):
            if server.Handler.worlds.get("rose").accounts["аня"].get("bed"):
                break
            threading.Event().wait(0.02)
        _, welcome = self.join("Аня")
        self.assertEqual(welcome["me"]["bed"], [40, 60])
        c, _ = self.join("Боря")
        c.send({"t": "me", "inv": INV, "sel": 0, "bed": [40, 999]})
        self.assertIsNone(c.recv())


if __name__ == "__main__":
    unittest.main()
