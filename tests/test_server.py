import http.server
import json
import pathlib
import sys
import tempfile
import threading
import unittest
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "router"))
sys.path.insert(0, str(ROOT / "tests"))

import server  # noqa: E402
from wsclient import Client  # noqa: E402

WORLD = {"t": "world", "seed": 42, "ocean": True, "tiles": "AAE="}


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.world_file = pathlib.Path(self.dir.name) / "world.json"
        self.start()

    def start(self):
        server.Handler.log_message = lambda *args: None
        server.Handler.room = server.Room("secret", self.world_file)
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

    def join(self, name, password="secret"):
        c = Client(self.url)
        self.clients.append(c)
        c.send({"t": "hello", "name": name, "pass": password})
        return c, c.recv()

    def synced(self, sender, watcher):
        """Waits until the server has handled everything the sender sent so far."""
        sender.send({"t": "pos", "x": 0.5, "y": 0.5, "f": 1})
        while watcher.recv() != {"t": "pos", "id": 1, "x": 0.5, "y": 0.5, "f": 1}:
            pass

    def test_serves_page(self):
        port = self.httpd.server_address[1]
        body = urllib.request.urlopen("http://127.0.0.1:%d/" % port).read().decode()
        self.assertIn("<title>Сердцекрафт</title>", body)

    def test_answers_the_network_probe(self):
        port = self.httpd.server_address[1]
        self.assertEqual(urllib.request.urlopen("http://127.0.0.1:%d/net" % port).status, 204)

    def test_wrong_password_is_refused(self):
        c, msg = self.join("Вася", "nope")
        self.assertEqual(msg, {"t": "error", "msg": "Неверный пароль"})
        self.assertIsNone(c.recv())

    def test_empty_name_is_refused(self):
        _, msg = self.join("  ")
        self.assertEqual(msg["t"], "error")

    def test_first_player_shares_world_and_next_gets_it(self):
        a, welcome = self.join("Аня")
        self.assertEqual((welcome["t"], welcome["id"], welcome["world"], welcome["players"]), ("welcome", 1, None, []))
        watcher, _ = self.join("Вера")
        a.send(WORLD)
        a.send({"t": "set", "x": 3, "y": 2, "id": 8})
        self.synced(a, watcher)
        a.send({"t": "pos", "x": 5.5, "y": 7, "f": -1})
        self.assertEqual(watcher.recv()["x"], 5.5)
        b, welcome = self.join("Боря")
        self.assertEqual(welcome["world"], {"seed": 42, "ocean": True, "tiles": "AAE=", "changes": [[2 * 1024 + 3, 8]]})
        self.assertEqual(welcome["players"][0], {"id": 1, "name": "Аня", "pos": {"x": 5.5, "y": 7, "f": -1}})
        self.assertEqual(a.recv(), {"t": "join", "id": 2, "name": "Вера"})
        self.assertEqual(a.recv(), {"t": "join", "id": 3, "name": "Боря"})

    def test_second_world_is_answered_with_the_shared_one(self):
        a, _ = self.join("Аня")
        b, _ = self.join("Боря")
        a.send(WORLD)
        self.assertEqual(a.recv()["t"], "join")
        b.send(dict(WORLD, seed=7))
        self.assertEqual(b.recv(), {"t": "load", "world": {"seed": 42, "ocean": True, "tiles": "AAE=", "changes": []}})

    def test_moves_and_blocks_reach_others_but_not_sender(self):
        a, _ = self.join("Аня")
        a.send(WORLD)
        b, _ = self.join("Боря")
        self.assertEqual(a.recv()["t"], "join")
        b.send({"t": "set", "x": 10, "y": 20, "id": 0})
        b.send({"t": "pos", "x": 1.25, "y": 2.5, "f": 1})
        self.assertEqual(a.recv(), {"t": "set", "x": 10, "y": 20, "id": 0})
        self.assertEqual(a.recv(), {"t": "pos", "id": 2, "x": 1.25, "y": 2.5, "f": 1})
        a.send({"t": "pos", "x": 3, "y": 4, "f": 1})
        self.assertEqual(b.recv(), {"t": "pos", "id": 1, "x": 3, "y": 4, "f": 1})

    def test_leave_is_announced(self):
        a, _ = self.join("Аня")
        b, _ = self.join("Боря")
        self.assertEqual(a.recv()["t"], "join")
        b.send_raw(8, b"")
        self.assertEqual(a.recv(), {"t": "leave", "id": 2})

    def test_bad_message_drops_only_its_sender(self):
        a, _ = self.join("Аня")
        b, _ = self.join("Боря")
        self.assertEqual(a.recv()["t"], "join")
        b.send({"t": "set", "x": 5000, "y": 1, "id": 1})
        self.assertIsNone(b.recv())
        self.assertEqual(a.recv(), {"t": "leave", "id": 2})

    def test_blocks_before_world_are_dropped(self):
        a, _ = self.join("Аня")
        watcher, _ = self.join("Вера")
        a.send({"t": "set", "x": 1, "y": 1, "id": 1})
        a.send(WORLD)
        self.synced(a, watcher)
        b, welcome = self.join("Боря")
        self.assertEqual(welcome["world"]["changes"], [])

    def test_room_is_limited(self):
        for i in range(server.MAX_PLAYERS):
            self.assertEqual(self.join("p%d" % i)[1]["t"], "welcome")
        self.assertEqual(self.join("лишний")[1]["t"], "error")

    def test_world_survives_restart(self):
        a, _ = self.join("Аня")
        watcher, _ = self.join("Боря")
        a.send(WORLD)
        a.send({"t": "set", "x": 3, "y": 2, "id": 8})
        self.synced(a, watcher)
        server.Handler.room.save()
        self.assertFalse(server.Handler.room.dirty)
        self.stop()
        self.start()
        _, welcome = self.join("Вера")
        self.assertEqual(welcome["world"], {"seed": 42, "ocean": True, "tiles": "AAE=", "changes": [[2051, 8]]})
        self.assertEqual(welcome["players"], [])
        self.assertEqual(json.loads(self.world_file.read_text())["seed"], 42)

    def test_big_world_message_passes(self):
        a, _ = self.join("Аня")
        watcher, _ = self.join("Вера")
        a.send(dict(WORLD, tiles="A" * 300000))
        self.synced(a, watcher)
        _, welcome = self.join("Боря")
        self.assertEqual(len(welcome["world"]["tiles"]), 300000)


if __name__ == "__main__":
    unittest.main()
