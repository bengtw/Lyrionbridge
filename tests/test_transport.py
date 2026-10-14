"""Transportreglerna (12 okt 2026): en uppsättning för alla vägar.

Kör: cd ~/lms_bridge && /home/bengtw/edgar/venv/bin/python -m unittest discover -s tests -t .

pause startar aldrig, play pausar aldrig, toggle växlar, stop stoppar; utan rum gäller
pause/stop de spelare som spelar (stop även pausade) — förut den första i LMS-listan.
Lyrion är låtsad: inga anrop når riktiga spelare.
"""
import sys, os, unittest
from unittest import mock
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import lms_bridge as lb

KONT, VARD, BAD = "aa:01", "aa:02", "aa:03"
PLAYERS = [{"playerid": KONT, "name": "Kontoret"}, {"playerid": VARD, "name": "Vardagsrum"},
           {"playerid": BAD, "name": "Badrum"}]


class FakeLMS:
    def __init__(self, modes):
        self.modes = dict(modes)
        self.sent = []

    def __call__(self, mac, cmd, timeout=3, _raw=False):
        if cmd == ["mode", "?"]:
            return {"result": {"_mode": self.modes.get(mac, "stop")}}
        self.sent.append((mac, cmd))
        return {"result": {}}


class TransportTest(unittest.TestCase):
    def run_route(self, path, modes, room=None):
        fake = FakeLMS(modes)
        q = f"?room={room}" if room else ""
        with mock.patch.object(lb, "lms_json_rpc", fake), \
             mock.patch.object(lb, "get_all_players", lambda: PLAYERS):
            body = lb.app.test_client().get(path + q).get_json()
        return fake.sent, body

    def test_pause_never_starts_a_paused_player(self):
        sent, body = self.run_route("/pause", {KONT: "pause"}, room=KONT)
        self.assertEqual(sent, [])
        self.assertEqual(body["acted"], [])

    def test_pause_pauses_a_playing_player(self):
        sent, _ = self.run_route("/pause", {KONT: "play"}, room=KONT)
        self.assertEqual(sent, [(KONT, ["pause", "1"])])

    def test_play_never_pauses_a_playing_player(self):
        sent, _ = self.run_route("/play", {KONT: "play"}, room=KONT)
        self.assertEqual(sent, [])

    def test_play_resumes_paused_and_starts_stopped(self):
        self.assertEqual(self.run_route("/play", {KONT: "pause"}, room=KONT)[0], [(KONT, ["pause", "0"])])
        self.assertEqual(self.run_route("/play", {KONT: "stop"}, room=KONT)[0], [(KONT, ["play"])])

    def test_toggle_still_toggles(self):
        sent, _ = self.run_route("/toggle_play_pause", {KONT: "pause"}, room=KONT)
        self.assertEqual(sent, [(KONT, ["pause"])])

    def test_pause_without_room_pauses_only_playing(self):
        sent, _ = self.run_route("/pause", {KONT: "pause", VARD: "play"})
        self.assertEqual(sent, [(VARD, ["pause", "1"])])

    def test_stop_without_room_stops_playing_and_paused_not_first_player(self):
        sent, body = self.run_route("/stop", {VARD: "play", BAD: "pause"})
        self.assertEqual(sent, [(VARD, ["stop"]), (BAD, ["stop"])])
        self.assertEqual(body["acted"], ["Vardagsrum", "Badrum"])

if __name__ == "__main__":
    unittest.main()
