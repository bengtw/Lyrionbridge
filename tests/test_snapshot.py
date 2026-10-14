"""Skyddsnätet inför uppdelningen (tools/snapshot.py) — jämförelsen ska slå larm på riktiga fel
och inte på brus. Körs utan bryggan (ingen nätverkstrafik)."""
import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import snapshot  # noqa: E402

BASE = {
    "when": "t",
    "options": {"/next": {"status": 200, "allow": ["GET", "HEAD", "OPTIONS"]},
                "/status": {"status": 200, "allow": ["GET", "HEAD", "OPTIONS"]}},
    "reads": {"status": {"path": "/status", "status": 200, "ctype": "application/json",
                         "shape": snapshot.shape({"mode": "play", "time": 1.0})},
              "rum": {"path": "/resolve_player", "status": 200, "ctype": "application/json",
                      "shape": {"name": "str"}, "value": {"name": "Köket"}}},
}


class SnapshotTest(unittest.TestCase):
    def test_identical_is_clean(self):
        self.assertEqual(snapshot.compare(BASE, copy.deepcopy(BASE)), [])

    def test_missing_route_detected_without_calling_it(self):
        new = copy.deepcopy(BASE)
        del new["options"]["/next"]
        self.assertTrue(any("VÄG BORTA /next" in d for d in snapshot.compare(BASE, new)))

    def test_method_change_detected(self):
        new = copy.deepcopy(BASE)
        new["options"]["/next"]["allow"] = ["OPTIONS", "POST"]
        self.assertTrue(any("VÄG ÄNDRAD /next" in d for d in snapshot.compare(BASE, new)))

    def test_renamed_field_detected(self):
        new = copy.deepcopy(BASE)
        new["reads"]["status"]["shape"] = snapshot.shape({"mode": "play", "elapsed": 1.0})
        self.assertTrue(any("formen ändrad" in d for d in snapshot.compare(BASE, new)))

    def test_changed_value_detected(self):
        new = copy.deepcopy(BASE)
        new["reads"]["rum"]["value"] = {"name": "Kontoret"}
        self.assertTrue(any("värdet ändrat" in d for d in snapshot.compare(BASE, new)))

    def test_cache_flag_is_noise(self):
        self.assertEqual(snapshot.shape({"a": 1, "cached": True}), snapshot.shape({"a": 2}))

    def test_every_route_is_read_or_listed(self):
        # En ny väg i lms_bridge.py måste antingen läsas eller stå i NOT_CALLED.
        self.assertEqual(snapshot.coverage(), [])


if __name__ == "__main__":
    unittest.main()
