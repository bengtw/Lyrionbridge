"""Vakthundens framstegsregel (12 okt 2026).

Kör: cd ~/lms_bridge && /home/bengtw/edgar/venv/bin/python -m unittest discover -s tests -t .

Verkligt fall: Melody Gardot – Quiet Fire (252 s) tog slut i vardagsrummet, nästa spår
startade aldrig, och Lyrions elapsed kröp 252 → 252,8 s under 2½ minut. Varje bråkdels
ökning räknades som framsteg, så vakthunden hoppade först efter 2 min 40 s tystnad.
Lyrion är låtsad: inga anrop når riktiga spelare.
"""
import sys, os, unittest
from unittest import mock
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import lms_bridge as lb

MAC = "bb:bb:4d:b0:d0:06"


class Sim:
    """En spelare vars status styrs av testet; registrerar vakthundens kommandon."""
    def __init__(self, duration):
        self.elapsed, self.duration, self.mode = 0.0, duration, "play"
        self.cur, self.tracks, self.sent = 0, 10, []

    def __call__(self, mac, cmd, timeout=3, _raw=False):
        if cmd[0] == "status":
            return {"result": {"mode": self.mode, "time": self.elapsed, "duration": self.duration,
                               "playlist_tracks": self.tracks, "playlist_cur_index": self.cur,
                               "playlist_loop": [{"url": f"spotify://track:{self.cur}"}]}}
        self.sent.append(cmd)
        return {"result": {}}


class WatchdogProgressTest(unittest.TestCase):
    def setUp(self):
        for d in (lb._last_active_ts, lb._last_elapsed, lb._last_track, lb._last_mode, lb._skip_recover):
            d.clear()

    def run_ticks(self, sim, steps):
        """steps: lista av elapsed-värden, ett per varv (10 s isär). Returnerar sekunden för
        vakthundens första hopp, eller None."""
        t0 = 1_000_000.0
        with mock.patch.object(lb, "lms_json_rpc", sim), \
             mock.patch.object(lb, "get_all_players", lambda: [{"playerid": MAC, "name": "Vardagsrummet"}]), \
             mock.patch.object(lb, "_record_lock_event", lambda *a, **k: None):
            for i, e in enumerate(steps):
                sim.elapsed = e
                with mock.patch.object(lb.time, "time", lambda: t0 + 10 * i):
                    lb._watchdog_tick()
                if ["playlist", "index", "+1"] in sim.sent:
                    return 10 * i
        return None

    def test_creeping_elapsed_after_track_end_is_caught_quickly(self):
        sim = Sim(duration=252)
        playing = [200 + 10 * i for i in range(6)]                 # 200 … 250
        creep = [252.0 + 0.05 * i for i in range(1, 17)]            # 252,05 … 252,8
        hit = self.run_ticks(sim, playing + creep)
        self.assertIsNotNone(hit, "vakthunden hoppade aldrig")
        self.assertLessEqual(hit - 10 * len(playing), 50)           # förr: ~160 s

    def test_creep_mid_track_counts_as_frozen(self):
        sim = Sim(duration=300)
        hit = self.run_ticks(sim, [100, 110, 120] + [120.3 + 0.2 * i for i in range(8)])
        self.assertIsNotNone(hit)

    def test_normal_playback_is_left_alone(self):
        sim = Sim(duration=300)
        self.assertIsNone(self.run_ticks(sim, [10 * i for i in range(25)]))
        self.assertEqual(sim.sent, [])

    def test_stream_without_duration_still_works(self):
        sim = Sim(duration=0)                                      # radio: längd okänd
        self.assertIsNone(self.run_ticks(sim, [5000 + 10 * i for i in range(20)]))


if __name__ == "__main__":
    unittest.main()
