"""
lms_logger.py — Prenumererar på LMS CLI-events och loggar allt som spelas till SQLite.

Fångar både lokala filer och Spotify/Spotty-spår. Detekterar skippar
(< 40% av spårlängden spelad) och flaggar dem i databasen.

Kör som daemon:
    python lms_logger.py

Eller som systemd-service (se lms_logger.service).
"""

import json
import os
import socket
import sqlite3
import threading
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

# Ladda .env från edgar om det inte finns lokalt
_env = Path(__file__).parent / ".env"
if not _env.exists():
    _env = Path(__file__).parent.parent / "edgar" / ".env"
load_dotenv(_env)

LMS_HOST     = "10.0.1.132"
LMS_CLI_PORT = 9090
LMS_JSON_URL = f"http://{LMS_HOST}:9000/jsonrpc.js"
DB_PATH      = Path(__file__).parent / "play_history.db"

SKIP_THRESHOLD = 0.40  # Andel av spåret som måste spelas för att inte räknas som skip


# ---------------------------------------------------------------------------
# Databas
# ---------------------------------------------------------------------------

def init_db():
    with _db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS plays (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          INTEGER NOT NULL,
                player      TEXT    NOT NULL,
                artist      TEXT,
                title       TEXT,
                album       TEXT,
                duration    INTEGER,
                source      TEXT,
                spotify_uri TEXT,
                skipped     INTEGER DEFAULT 0,
                energy      REAL,
                valence     REAL,
                danceability REAL,
                tempo       REAL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ts     ON plays(ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_artist ON plays(artist)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_source ON plays(source)")
        # Migrera gamla databaser utan nya kolumner
        cols = [r[1] for r in conn.execute("PRAGMA table_info(plays)").fetchall()]
        for col, typ in [("spotify_uri","TEXT"), ("energy","REAL"), ("valence","REAL"),
                         ("danceability","REAL"), ("tempo","REAL")]:
            if col not in cols:
                conn.execute(f"ALTER TABLE plays ADD COLUMN {col} {typ}")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS track_features_cache (
                artist       TEXT    NOT NULL,
                title        TEXT    NOT NULL,
                energy       REAL    NOT NULL,
                valence      REAL    NOT NULL,
                danceability REAL    NOT NULL,
                tempo        REAL    NOT NULL,
                cached_at    INTEGER NOT NULL,
                PRIMARY KEY (artist, title)
            )
        """)


def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# LMS JSON-RPC
# ---------------------------------------------------------------------------

def _rpc(player_id, cmd):
    payload = {"id": 1, "method": "slim.request", "params": [player_id, cmd]}
    try:
        return requests.post(LMS_JSON_URL, json=payload, timeout=3).json()
    except Exception:
        return None


def _get_track(mac):
    res = _rpc(mac, ["status", "-", "1", "tags:atldu"])
    if not res or "result" not in res:
        return None
    r    = res["result"]
    loop = r.get("playlist_loop", [])
    if not loop:
        return None
    t   = loop[0]
    url = t.get("url", "")
    source = "spotify" if ("spotify" in url.lower() or "spotty" in url.lower()) else "local"
    if url.startswith("spotify://track:"):
        spotify_uri = "spotify:track:" + url.split("spotify://track:")[-1]
    elif url.startswith("spotify:track:"):
        spotify_uri = url
    else:
        spotify_uri = None
    return {
        "artist":      t.get("artist", ""),
        "title":       t.get("title") or t.get("name", ""),
        "album":       t.get("album", ""),
        "duration":    int(r.get("duration") or 0),
        "source":      source,
        "spotify_uri": spotify_uri,
    }


_player_name_cache = {}

def _player_name(mac):
    if mac in _player_name_cache:
        return _player_name_cache[mac]
    res = _rpc("", ["players", "0", "20"])
    if res and "result" in res:
        for p in res["result"].get("players_loop", []):
            _player_name_cache[p["playerid"]] = p.get("name", p["playerid"])
    return _player_name_cache.get(mac, mac)


# ---------------------------------------------------------------------------
# Spårloggning och skip-detektion
# ---------------------------------------------------------------------------

_state = {}  # mac → {ts_start, row_id, duration}
_state_lock = threading.Lock()


def _on_stop(mac):
    """Spelare stoppades — rensa state utan att markera föregående spår som skippad."""
    with _state_lock:
        _state.pop(mac, None)


def _on_newsong(mac):
    now        = int(time.time())
    track      = _get_track(mac)
    player     = _player_name(mac)

    with _db() as conn:
        # Markera föregående spår som skippad bara om det avbröts mitt i pågående spel
        # (inte om spelaren stoppades och en ny session startades senare)
        with _state_lock:
            prev = _state.get(mac)
        if prev:
            elapsed = now - prev["ts_start"]
            if prev["duration"] > 20 and elapsed < prev["duration"] * SKIP_THRESHOLD:
                conn.execute("UPDATE plays SET skipped=1 WHERE id=?", (prev["row_id"],))

        if not track:
            return

        cur    = conn.execute(
            "INSERT INTO plays (ts, player, artist, title, album, duration, source, spotify_uri) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (now, player, track["artist"], track["title"],
             track["album"], track["duration"], track["source"], track["spotify_uri"]),
        )
        row_id = cur.lastrowid

    with _state_lock:
        _state[mac] = {"ts_start": now, "row_id": row_id, "duration": track["duration"]}

    src_tag = "spotify" if track["source"] == "spotify" else "local "
    print(f"[LOG] [{src_tag}] {player}: {track['artist']} — {track['title']}")

    if track["artist"] and track["title"]:
        threading.Thread(
            target=_estimate_and_store,
            args=(row_id, track["artist"], track["title"]),
            daemon=True,
        ).start()


# ---------------------------------------------------------------------------
# Audio feature-estimering via Gemini
# ---------------------------------------------------------------------------

_gemini_lock = threading.Lock()
_gemini_client = None


def _get_gemini():
    global _gemini_client
    with _gemini_lock:
        if _gemini_client is None:
            api_key = os.environ.get("GEMINI_API_KEY")
            if not api_key:
                return None
            try:
                from google import genai
                _gemini_client = genai.Client(api_key=api_key)
            except Exception as e:
                print(f"[Features] Gemini init-fel: {e}")
                return None
        return _gemini_client


def _estimate_features_batch(tracks: list[tuple[str, str]]) -> dict:
    """
    Ber Gemini uppskatta audio features för en lista (artist, title)-tupler.
    Returnerar dict {(artist, title): {energy, valence, danceability, tempo}}.
    """
    client = _get_gemini()
    if not client or not tracks:
        return {}

    track_list = "\n".join(f"- {a} — {t}" for a, t in tracks)
    prompt = (
        "Estimate Spotify-style audio features for these tracks.\n"
        "Return ONLY a JSON array, one object per track, with fields:\n"
        "  artist, title, energy (0.0-1.0), valence (0.0-1.0), danceability (0.0-1.0), tempo (BPM integer)\n"
        "Match the order of the input list exactly.\n\n"
        f"Tracks:\n{track_list}"
    )
    try:
        resp = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
        text = resp.text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0]
        data = json.loads(text)
        return {
            (d["artist"], d["title"]): {
                "energy":       float(d["energy"]),
                "valence":      float(d["valence"]),
                "danceability": float(d["danceability"]),
                "tempo":        float(d["tempo"]),
            }
            for d in data
            if all(k in d for k in ("energy", "valence", "danceability", "tempo"))
        }
    except Exception as e:
        print(f"[Features] Batch-fel: {e}")
        return {}


def _lookup_features_cache(artist: str, title: str) -> dict | None:
    with _db() as conn:
        row = conn.execute(
            "SELECT energy, valence, danceability, tempo FROM track_features_cache WHERE artist=? AND title=?",
            (artist, title),
        ).fetchone()
    if row:
        return {"energy": row["energy"], "valence": row["valence"],
                "danceability": row["danceability"], "tempo": row["tempo"]}
    return None


def _store_features_cache(artist: str, title: str, f: dict):
    with _db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO track_features_cache "
            "(artist, title, energy, valence, danceability, tempo, cached_at) VALUES (?,?,?,?,?,?,?)",
            (artist, title, f["energy"], f["valence"], f["danceability"], f["tempo"], int(time.time())),
        )


def _estimate_and_store(row_id: int, artist: str, title: str):
    """Estimerar features för ett enskilt spår och sparar i DB. Körs i bakgrundstråd."""
    f = _lookup_features_cache(artist, title)
    if f:
        print(f"[Features] {artist} — {title}: cache-träff")
    else:
        features = _estimate_features_batch([(artist, title)])
        f = features.get((artist, title))
        if not f:
            return
        _store_features_cache(artist, title, f)
        print(f"[Features] {artist} — {title}: energy={f['energy']} valence={f['valence']} tempo={f['tempo']:.0f} BPM")
    with _db() as conn:
        conn.execute(
            "UPDATE plays SET energy=?, valence=?, danceability=?, tempo=? WHERE id=?",
            (f["energy"], f["valence"], f["danceability"], f["tempo"], row_id),
        )


def backfill_features(batch_size: int = 30):
    """Fyller på audio features för alla spår i DB som saknar dem. Körs manuellt."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT id, artist, title FROM plays WHERE energy IS NULL AND artist != '' AND title != ''",
        ).fetchall()

    if not rows:
        print("[Features] Alla spår har redan features.")
        return

    print(f"[Features] Backfill: {len(rows)} spår saknar features — söker i cache först...")
    cache_hits = 0
    needs_estimation = []
    with _db() as conn:
        for row in rows:
            f = _lookup_features_cache(row["artist"], row["title"])
            if f:
                conn.execute(
                    "UPDATE plays SET energy=?, valence=?, danceability=?, tempo=? WHERE id=?",
                    (f["energy"], f["valence"], f["danceability"], f["tempo"], row["id"]),
                )
                cache_hits += 1
            else:
                needs_estimation.append(row)

    print(f"[Features] {cache_hits} från cache, {len(needs_estimation)} behöver estimering via Gemini")
    if not needs_estimation:
        print("[Features] Backfill klar.")
        return

    print(f"[Features] Bearbetar {len(needs_estimation)} spår i batchar om {batch_size}...")
    updated = 0
    for i in range(0, len(needs_estimation), batch_size):
        batch = needs_estimation[i:i + batch_size]
        tracks = [(r["artist"], r["title"]) for r in batch]
        features = _estimate_features_batch(tracks)
        with _db() as conn:
            for row in batch:
                f = features.get((row["artist"], row["title"]))
                if f:
                    conn.execute(
                        "UPDATE plays SET energy=?, valence=?, danceability=?, tempo=? WHERE id=?",
                        (f["energy"], f["valence"], f["danceability"], f["tempo"], row["id"]),
                    )
                    _store_features_cache(row["artist"], row["title"], f)
                    updated += 1
        print(f"[Features]   {min(i + batch_size, len(needs_estimation))}/{len(needs_estimation)} klara ({updated} uppdaterade)...")
        time.sleep(1)

    print(f"[Features] Backfill klar — {cache_hits} från cache, {updated} estimerade.")


# ---------------------------------------------------------------------------
# Frågegränssnitt (används av lms_bridge.py endpoints)
# ---------------------------------------------------------------------------

def recent_artists(limit=40, days=30):
    """Returnerar de mest spelade artisterna (exkl. skippar) de senaste N dagarna."""
    since = int(time.time()) - days * 86400
    with _db() as conn:
        rows = conn.execute(
            """
            SELECT artist, COUNT(*) AS plays
            FROM plays
            WHERE ts >= ? AND skipped = 0 AND artist != ''
            GROUP BY artist
            ORDER BY plays DESC
            LIMIT ?
            """,
            (since, limit),
        ).fetchall()
    return [r["artist"] for r in rows]


def recent_tracks(limit=100, days=14):
    """Returnerar de senaste N spelade spåren (exkl. skippar)."""
    since = int(time.time()) - days * 86400
    with _db() as conn:
        rows = conn.execute(
            """
            SELECT ts, player, artist, title, album, source
            FROM plays
            WHERE ts >= ? AND skipped = 0
            ORDER BY ts DESC
            LIMIT ?
            """,
            (since, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def skipped_tracks(limit=50, days=14):
    """Returnerar spår som ofta skippas — negativ signal."""
    since = int(time.time()) - days * 86400
    with _db() as conn:
        rows = conn.execute(
            """
            SELECT artist, title, COUNT(*) AS skips
            FROM plays
            WHERE ts >= ? AND skipped = 1
            GROUP BY artist, title
            ORDER BY skips DESC
            LIMIT ?
            """,
            (since, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def listening_stats(days=30):
    """Enkel sammanfattning: antal plays, källfördelning, toppdag."""
    since = int(time.time()) - days * 86400
    with _db() as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM plays WHERE ts >= ? AND skipped = 0", (since,)
        ).fetchone()[0]
        by_source = conn.execute(
            "SELECT source, COUNT(*) AS n FROM plays WHERE ts >= ? AND skipped = 0 "
            "GROUP BY source",
            (since,),
        ).fetchall()
        top_artists = conn.execute(
            "SELECT artist, COUNT(*) AS n FROM plays WHERE ts >= ? AND skipped = 0 "
            "AND artist != '' GROUP BY artist ORDER BY n DESC LIMIT 5",
            (since,),
        ).fetchall()
    return {
        "total_plays": total,
        "by_source":   {r["source"]: r["n"] for r in by_source},
        "top_artists": [{"artist": r["artist"], "plays": r["n"]} for r in top_artists],
    }


def history_data():
    """Returnerar komplett data för history-sidan (plays, profil, top-artister, energy-distribution)."""
    import time as _time
    since_14 = int(_time.time()) - 14 * 86400
    since_30 = int(_time.time()) - 30 * 86400
    with _db() as conn:
        plays = [dict(r) for r in conn.execute(
            "SELECT ts, player, artist, title, source, energy, valence, danceability, tempo, skipped "
            "FROM plays ORDER BY ts DESC LIMIT 150"
        ).fetchall()]

        profile_row = conn.execute(
            "SELECT AVG(energy) e, AVG(valence) v, AVG(danceability) d, AVG(tempo) t, COUNT(*) n "
            "FROM plays WHERE ts >= ? AND skipped=0 AND energy IS NOT NULL",
            (since_14,)
        ).fetchone()
        profile = dict(profile_row) if profile_row and profile_row["n"] else None

        top_artists = [dict(r) for r in conn.execute(
            "SELECT artist, COUNT(*) plays, AVG(energy) avg_energy "
            "FROM plays WHERE ts >= ? AND skipped=0 AND artist != '' "
            "GROUP BY lower(artist) ORDER BY plays DESC LIMIT 20",
            (since_30,)
        ).fetchall()]

        energy_dist = [0] * 10
        for r in conn.execute(
            "SELECT energy FROM plays WHERE ts >= ? AND energy IS NOT NULL AND skipped=0",
            (since_30,)
        ).fetchall():
            bucket = min(int(r["energy"] * 10), 9)
            energy_dist[bucket] += 1

    return {
        "plays":       plays,
        "profile":     profile,
        "top_artists": top_artists,
        "energy_dist": energy_dist,
    }


def _listen():
    while True:
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.connect((LMS_HOST, LMS_CLI_PORT))
            sock.sendall(b"subscribe playlist,stop,pause\n")
            print(f"[LMS Logger] Ansluten till {LMS_HOST}:{LMS_CLI_PORT}")

            buf = ""
            while True:
                chunk = sock.recv(4096).decode("utf-8", errors="replace")
                if not chunk:
                    break
                buf += chunk
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    parts = line.split(" ")
                    mac = urllib.parse.unquote(parts[0])
                    if len(parts) >= 3 and parts[1] == "playlist" and parts[2] == "newsong":
                        threading.Thread(target=_on_newsong, args=(mac,), daemon=True).start()
                    elif len(parts) >= 2 and parts[1] == "stop":
                        threading.Thread(target=_on_stop, args=(mac,), daemon=True).start()
                    elif len(parts) >= 3 and parts[1] == "playlist" and parts[2] == "stop":
                        threading.Thread(target=_on_stop, args=(mac,), daemon=True).start()
                    elif len(parts) >= 3 and parts[1] == "pause" and parts[2] == "1":
                        threading.Thread(target=_on_stop, args=(mac,), daemon=True).start()

        except Exception as e:
            print(f"[LMS Logger] Anslutningsfel: {e} — försöker igen om 15s")
        finally:
            if sock:
                try:
                    sock.close()
                except Exception:
                    pass
        time.sleep(15)


# ---------------------------------------------------------------------------
# Startpunkt
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    init_db()
    if "--backfill" in sys.argv:
        backfill_features()
    else:
        print(f"[LMS Logger] Startar — loggar till {DB_PATH}")
        _listen()
