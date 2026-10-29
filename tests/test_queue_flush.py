"""Köer som inte spelar (29 okt 2026): rensas kl 23 och när en spelare kommer tillbaka,
inte längre efter 1 h paus/stopp.

Kör: cd ~/lms_bridge && /home/bengtw/edgar/venv/bin/python -m unittest discover -s tests -t .

Bakgrund: badrummet startade 10 jun 2026 en 20 dagar gammal kö av sig själv när spelaren
kopplade upp sig igen. Timregeln tömde samtidigt ~3 köer om dagen efter vanliga pauser.
Lyrion är låtsad: inga anrop når riktiga spelare.
"""
import sys, os, unittest, datetime
from unittest import mock
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import lms_bridge as lb

MAC = "aa:aa:aa:aa:aa:01"


def at(hour, minute=0, day=29):
    return datetime.datetime(2026, 10, day, hour, minute).timestamp()


class Player:
    def __init__(self, mode="pause", tracks=12):
        self.mode, self.tracks, self.connected, self.sent = mode, tracks, True, []
        self.queued_at = at(9)                      # när kön senast ändrades (playlist_timestamp)

    def rpc(self, mac, cmd, timeout=3, _raw=False):
        if cmd[0] == "status":
            return {"result": {"mode": self.mode, "time": 40.0, "duration": 240,
                               "playlist_tracks": self.tracks, "playlist_cur_index": 0,
                               "playlist_timestamp": self.queued_at if self.tracks else None,
                               "playlist_loop": [{"url": "spotify://track:x"}]}}
        if not _raw and lb._is_start_command(cmd):
            lb._last_start_cmd[mac] = lb.time.time()
        self.sent.append(cmd)
        if cmd == ["playlist", "clear"]:
            self.tracks = 0
        return {"result": {}}

    def listing(self):
        return [{"playerid": MAC, "name": "badrummet", "connected": 1 if self.connected else 0}]

    @property
    def cleared(self):
        return ["playlist", "clear"] in self.sent


class QueueFlushTest(unittest.TestCase):
    def setUp(self):
        for d in (lb._last_active_ts, lb._last_elapsed, lb._last_track, lb._last_mode, lb._skip_recover,
                  lb._trusted_pos, lb._jump_seen, lb._false_end_logged, lb._shadow_since,
                  lb._gone_since, lb._last_start_cmd):
            d.clear()
        lb._present = set(); lb._watchdog_started = False; lb._night_flushed = None
        self.pl = Player()

    def tick(self, t):
        with mock.patch.object(lb, "lms_json_rpc", self.pl.rpc), \
             mock.patch.object(lb, "get_all_players", self.pl.listing), \
             mock.patch.object(lb, "_record_lock_event", lambda *a, **k: None), \
             mock.patch.object(lb.time, "time", lambda: t):
            lb._watchdog_tick()

    def test_pause_survives_the_day(self):
        for m in range(0, 8 * 60, 10):            # 14:00 → 21:50, pausad hela tiden
            self.tick(at(14) + 60 * m)
        self.assertFalse(self.pl.cleared)

    def test_paused_queue_cleared_at_23(self):
        self.tick(at(22, 59))
        self.assertFalse(self.pl.cleared)
        self.tick(at(23, 0))
        self.assertTrue(self.pl.cleared)

    def test_playing_queue_not_cleared_at_23(self):
        self.pl.mode = "play"
        self.tick(at(22, 59))
        self.tick(at(23, 0))
        self.assertFalse(self.pl.cleared)

    def test_night_flush_once_per_day(self):
        self.tick(at(23, 0))
        self.pl.sent.clear(); self.pl.tracks = 5   # någon köar nytt efter rensningen
        self.tick(at(23, 30))
        self.assertFalse(self.pl.cleared)

    def test_bridge_started_after_23_flushes(self):
        self.tick(at(23, 40))
        self.assertTrue(self.pl.cleared)

    def test_reappear_after_long_absence_clears_queue(self):
        self.tick(at(12))
        self.pl.connected = False
        self.tick(at(12, 1))
        self.pl.connected = True
        self.tick(at(12, 30))
        self.assertTrue(self.pl.cleared)

    def test_reappear_playing_by_itself_is_stopped(self):
        self.tick(at(12))
        self.pl.connected = False
        self.tick(at(12, 1))
        self.pl.connected, self.pl.mode = True, "play"
        self.tick(at(12, 30))
        self.assertIn(["stop"], self.pl.sent)
        self.assertTrue(self.pl.cleared)

    def test_reappear_playing_after_own_start_is_left(self):
        self.tick(at(12))
        self.pl.connected = False
        self.tick(at(12, 1))
        self.pl.connected, self.pl.mode = True, "play"
        lb._last_start_cmd[MAC] = at(12, 29)
        self.tick(at(12, 30))
        self.assertFalse(self.pl.cleared)

    def test_reappear_with_new_queue_from_lyrion_app_is_left(self):
        # Spelaren slås på och någon väljer musik i Lyrion-appen innan vakthunden sett den:
        # ingen start via bryggan, men kön är ändrad efter att spelaren försvann.
        self.tick(at(12))
        self.pl.connected = False
        self.tick(at(12, 1))
        self.pl.connected, self.pl.mode, self.pl.queued_at = True, "play", at(12, 29)
        self.tick(at(12, 30))
        self.assertFalse(self.pl.cleared)
        self.assertNotIn(["stop"], self.pl.sent)

    def test_new_player_since_bridge_start_with_old_queue_is_cleared(self):
        saved = self.pl.listing
        self.pl.listing = lambda: []                # bryggan startar, spelaren syns inte
        self.tick(at(12))
        self.pl.listing, self.pl.mode = saved, "play"
        self.tick(at(12, 10))
        self.assertTrue(self.pl.cleared)

    def test_short_blip_is_ignored(self):
        self.tick(at(12))
        self.pl.connected = False
        self.tick(at(12, 1))
        self.pl.connected = True
        self.tick(at(12, 3))
        self.assertFalse(self.pl.cleared)

    def test_players_present_at_bridge_start_are_not_reappearing(self):
        self.tick(at(12))
        self.tick(at(12, 1))
        self.assertFalse(self.pl.cleared)


if __name__ == "__main__":
    unittest.main()
