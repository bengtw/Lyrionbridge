"""Vakthundens framstegsregel (12 okt 2026, mot senaste framsteget sedan 13 okt).

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
        for d in (lb._last_active_ts, lb._last_elapsed, lb._last_track, lb._last_mode, lb._skip_recover,
                  lb._trusted_pos, lb._jump_seen, lb._false_end_logged, lb._shadow_since):
            d.clear()
        p = mock.patch.object(lb, "_SHADOW_MACS", set())
        p.start(); self.addCleanup(p.stop)

    def run_ticks(self, sim, steps):
        """steps: lista av elapsed-värden, ett per varv (10 s isär). Returnerar sekunden för
        vakthundens första hopp, eller None."""
        t0 = 1_000_000.0
        with mock.patch.object(lb, "lms_json_rpc", sim), \
             mock.patch.object(lb, "get_all_players", lambda: [{"playerid": MAC, "name": "Vardagsrummet"}]), \
             mock.patch.object(lb, "_record_lock_event", lambda *a, **k: None):
            for i, e in enumerate(steps):
                if isinstance(e, tuple):          # (elapsed, spårindex) = spårbyte
                    e, sim.cur = e
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

    def test_jerky_reported_time_mid_track_is_not_a_lock(self):
        # Linn via UPnP-bryggan (13 okt 2026): tiden står nästan still i tre varv och hoppar
        # sedan ikapp. Varv mot varv såg det fruset ut (+2,5 s) → hopp mitt i låten.
        sim = Sim(duration=300)
        self.assertIsNone(self.run_ticks(sim, [0, 10, 20, 22.5, 25, 27.5, 50, 60, 70]))
        self.assertEqual(sim.sent, [])

    def test_stall_at_full_length_skips_fast(self):
        # Texas Sun (252 s): tiden når längden och kryper/står → hopp vid första varvet efter 8 s,
        # inte efter 30 s.
        sim = Sim(duration=252)
        playing = [212 + 10 * i for i in range(5)]               # 212 … 252
        hit = self.run_ticks(sim, playing + [252.3, 252.5, 252.8, 252.8])
        self.assertIsNotNone(hit)
        self.assertLessEqual(hit - 10 * (len(playing) - 1), 10)

    def test_stall_mid_track_still_waits_30s(self):
        sim = Sim(duration=300)
        hit = self.run_ticks(sim, [100, 110, 120, 120, 120, 120, 120, 120])
        self.assertIsNotNone(hit)
        self.assertGreaterEqual(hit - 20, 30)                      # fryst från t=20

    def test_normal_playback_is_left_alone(self):
        sim = Sim(duration=300)
        self.assertIsNone(self.run_ticks(sim, [10 * i for i in range(25)]))
        self.assertEqual(sim.sent, [])

    def test_stream_without_duration_still_works(self):
        sim = Sim(duration=0)                                      # radio: längd okänd
        self.assertIsNone(self.run_ticks(sim, [5000 + 10 * i for i in range(20)]))


if __name__ == "__main__":
    unittest.main()


class FalseEndTest(unittest.TestCase):
    setUp = WatchdogProgressTest.setUp
    run_ticks = WatchdogProgressTest.run_ticks

    """13 okt 2026 15:13: bryggans tid hoppade 207 → 276 (låtens längd 275) och frös,
    fast bara ~210 s spelats. Vakthunden hoppade då över låtens sista minut."""

    def test_jump_to_end_waits_for_real_length(self):
        sim = Sim(duration=275)
        playing = [167 + 10 * i for i in range(5)]                  # 167 … 207
        hit = self.run_ticks(sim, playing + [276.0] * 12)
        self.assertIsNotNone(hit, "vakthunden hoppade aldrig")
        # 207 s vid t=40 → verkligt slut t≈108, hopp tidigast 8 s efter det
        self.assertGreaterEqual(hit, 110)
        self.assertLessEqual(hit, 130)

    def test_seek_near_end_then_stall_still_skips_fast(self):
        sim = Sim(duration=261)
        steps = [5, 15, 249, 259, 261, 261, 261, 261]               # sökning, spelar, fastnar
        hit = self.run_ticks(sim, steps)
        self.assertIsNotNone(hit)
        self.assertLessEqual(hit, 70)


class ShadowTest(unittest.TestCase):
    """Skuggläge: vakthunden loggar men hoppar aldrig."""
    def setUp(self):
        WatchdogProgressTest.setUp(self)
        p = mock.patch.object(lb, "_SHADOW_MACS", {MAC})
        p.start(); self.addCleanup(p.stop)
    run_ticks = WatchdogProgressTest.run_ticks

    def test_shadow_never_skips_and_logs_resolution(self):
        sim = Sim(duration=252)
        steps = [212, 222, 232, 242, 252] + [252.0] * 6 + [(3.0, 1), 13.0]
        with self.assertLogs(level="INFO") as cm:
            hit = self.run_ticks(sim, steps)
        self.assertIsNone(hit)
        self.assertEqual(sim.sent, [])
        log = "\n".join(cm.output)
        self.assertIn("skulle ha gjort hopp", log)
        self.assertIn("löste sig själv", log)

