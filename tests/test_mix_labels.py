"""Daily Mix-etiketter följer innehållet (signaturen), inte klockan."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import lms_bridge as lb


def _item(n, img_id, artists):
    img = f"/imageproxy/https%3A%2F%2Fpickasso.spotifycdn.com%2Fimage%2Fab67c0de0000deef%2Fdt%2Fv1%2Fimg%2Fdaily%2F{n}%2F{img_id}%2Fen/image.jpg"
    return {"text": f"Daily Mix {n}\n{artists}", "icon": img,
            "params": {"item_id": f"playlists.{n - 1}"}}


class MixLabelTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db = Path(self.tmp.name) / "m.db"
        with sqlite3.connect(db) as c:
            c.execute("CREATE TABLE mix_labels (mix_id INTEGER PRIMARY KEY, label TEXT NOT NULL,"
                      " summary TEXT, cached_at INTEGER NOT NULL, sig TEXT)")
        self.p = [mock.patch.object(lb, "_SEARCH_CACHE_DB", db),
                  mock.patch.object(lb, "_get_mix_label", side_effect=lambda a: "L:" + a),
                  mock.patch.object(lb, "_get_mix_summary", side_effect=lambda a: "S " + a.split()[0])]
        self.mocks = [x.start() for x in self.p]

    def tearDown(self):
        for x in self.p:
            x.stop()
        self.tmp.cleanup()

    def test_only_changed_mix_regenerated(self):
        lb._refresh_mix_labels([_item(1, "aaa", "Front 242"), _item(3, "bbb", "Lianne La Havas")])
        self.assertEqual(self.mocks[1].call_count, 2)
        lb._refresh_mix_labels([_item(1, "aaa", "Front 242"), _item(3, "ccc", "Rob Zombie")])
        self.assertEqual(self.mocks[1].call_count, 3)          # bara mix 3 igen
        rows = lb._mix_label_rows()
        self.assertEqual(rows[2][1], "S Rob")
        self.assertEqual(rows[0][1], "S Front")

    def test_stale_summary_hidden(self):
        lb._refresh_mix_labels([_item(3, "bbb", "Lianne La Havas")])
        new = [_item(3, "ccc", "Rob Zombie")]
        with mock.patch.object(lb, "_fetch_daily_mixes_raw", return_value=new), \
             mock.patch.object(lb, "_refresh_mix_labels_async") as kick:
            out = lb.app.test_client().get("/get_daily_mixes").get_json()
        self.assertIsNone(out[0]["ai_summary"])
        self.assertIsNone(out[0]["updated"])
        self.assertIn("/daily/3/ccc/", out[0]["image_cdn"])
        kick.assert_called_once()


if __name__ == "__main__":
    unittest.main()
