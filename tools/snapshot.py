#!/usr/bin/env python3
"""Ögonblicksbild av bryggan — skyddsnätet inför uppdelningen av lms_bridge.py (PLAN_MONOREPO.md steg B).

Två delar:
1. ALLA vägar (även de som spelar, pausar eller skriver) kontrolleras med OPTIONS. Flask svarar
   själv med vilka metoder vägen tar emot, utan att köra den — så /next, /play_album, /button m.fl.
   (menyradsappen, knobben, köksvredet) fångas om de försvinner eller byter metod, utan bieffekter.
2. De LÄSANDE vägarna anropas på riktigt. Svaret sparas som en "form" (nycklar och typer, rekursivt)
   och, för de stabila, som värde. Det som rör sig med musiken (vad som spelar, tid, slump) jämförs
   bara på form.

    python3 tools/snapshot.py record [fil]       # spara (default tools/snapshot.json)
    python3 tools/snapshot.py compare [fil]      # kör igen och jämför; exit 1 vid skillnad

Kör record före en flytt och compare efter. Bryggan måste vara igång (BRIDGE_URL, default
http://localhost:5000). Spotty-vägarna körs en i taget (Spotty tål högst två parallella).
"""
import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
from pathlib import Path

import requests

BASE = os.environ.get("BRIDGE_URL", "http://localhost:5000").rstrip("/")
HERE = Path(__file__).resolve().parent
BRIDGE = HERE.parent / "lms_bridge.py"
DEFAULT_FILE = HERE / "snapshot.json"

# (namn, väg, parametrar, läge). Läge: "value" = svaret ska vara identiskt, "shape" = bara
# nycklar och typer, "text" = textsvar (formen = antal rader). Parametrar kan vara en funktion
# av tidigare svar (kedjade id:n) — returnerar den None hoppas kontrollen över.
READS = [
    ("rot", "/", {}, "value"),
    ("ca-cert", "/edgar-ca.crt", {}, "value"),
    ("spelare", "/get_players", {}, "shape"),
    ("status", "/status", {}, "shape"),
    ("status kontoret", "/status", {"room": "kontoret"}, "shape"),
    ("titel kontoret", "/title", {"room": "kontoret"}, "text"),
    ("volym kontoret", "/volume", {"room": "kontoret"}, "text"),
    ("aktiva spelare", "/active_players", {}, "shape"),
    ("rum kök", "/resolve_player", {"name": "kök"}, "value"),
    ("rum vardagsrum", "/resolve_player", {"name": "vardagsrum"}, "value"),
    ("rum kontoret", "/resolve_player", {"name": "kontoret"}, "value"),
    ("daily mixes", "/get_daily_mixes", {}, "shape"),
    ("daily mixes knob", "/get_daily_mixes_knob", {}, "text"),
    ("alla mixar", "/get_all_mixes", {}, "shape"),
    ("spellistor", "/get_playlists", {}, "shape"),
    ("spellistor med omslag", "/get_playlists_with_art", {}, "shape"),
    ("lokala spellistor", "/list_playlists_local", {}, "shape"),
    ("radiofavoriter", "/get_radio_favorites", {}, "shape"),
    ("slumpade album", "/get_random_albums", {}, "shape"),
    ("biblioteksgenrer", "/library_genres", {}, "shape"),
    ("bibliotek per genre", "/library_by_genre",
     lambda r: {"genre_id": r["biblioteksgenrer"][0]["id"]} if r.get("biblioteksgenrer") else None, "shape"),
    ("sök i biblioteket", "/search_library", {"q": "Kraftwerk"}, "shape"),
    ("senaste artister", "/recent_artists", {}, "shape"),
    ("senaste låtar", "/recent_tracks", {}, "shape"),
    ("skippade låtar", "/skipped_tracks", {}, "shape"),
    ("artistens spelningar", "/artist_plays", {"artist": "Sade"}, "shape"),
    ("historikdata", "/play_history_data", {}, "shape"),
    ("lösta uri:er", "/resolved_uris", {}, "shape"),
    ("lastfm profil", "/lastfm_profile", {}, "shape"),
    ("lastfm liknande", "/lastfm_similar", {"artist": "Sade"}, "shape"),
    ("lastfm taggar", "/lastfm_artist_tags", {"artist": "Sade"}, "shape"),
    ("lastfm rekommendationer", "/lastfm_recommendations", {}, "shape"),
    ("spotify rekommendationer", "/spotify_recommendations", {"artist": "Sade"}, "shape"),
    # Spotty — en i taget
    ("spotify sök", "/spotify_search", {"q": "Kraftwerk Autobahn", "type": "track"}, "shape"),
    ("spotify toppspår", "/spotify_artist_top", {"q": "Kraftwerk"}, "shape"),
    ("spotify katalog", "/spotify_artist_catalog", {"q": "Jim Tomlinson"}, "shape"),
    ("spotify artistradio", "/spotify_artist_radio", {"q": "Kraftwerk"}, "shape"),
    ("spotify albumlåtar", "/spotify_album_tracks", {"artist": "Kraftwerk", "title": "Autobahn"}, "shape"),
    ("spotify genrer", "/spotify_genres", {}, "shape"),
    ("mixens låtar", "/get_playlist_tracks",
     lambda r: {"item_id": r["alla mixar"][0]["item_id"]}
     if isinstance(r.get("alla mixar"), list) and r["alla mixar"] and r["alla mixar"][0].get("item_id") else None,
     "shape"),
]

# Vägar som aldrig anropas med GET här (de spelar, pausar, skriver eller är felsökning) —
# de täcks av OPTIONS-kontrollen. Kontrollen nedan säger till om en ny väg dyker upp som
# varken läses eller står här.
NOT_CALLED = {
    "/button", "/cache_resolved", "/daily", "/deck_play_pause", "/delete_plays",
    "/imgproxy", "/get_artist_albums", "/spotify_genre_playlists", "/like_track", "/lms", "/love_track",
    "/mark_origin", "/mark_queue_origin", "/next", "/pause", "/play", "/play_album",
    "/play_artist_random", "/play_radio", "/play_random_album", "/play_spotify_album", "/play_url",
    "/queue-tracks", "/set_volume", "/skip_track", "/spotify/ingest",
    "/stop", "/toggle_play_pause", "/transfer",
}


def routes_in_source() -> list[str]:
    return sorted(set(re.findall(r"@app\.route\('([^']+)'", BRIDGE.read_text())))


# Fält som bara säger om svaret kom ur bryggans cache — finns andra gången, inte första.
IGNORE_KEYS = {"cached"}


def shape(v, depth=0):
    """Nycklar och typer, rekursivt. Listor: formen av de fem första elementen (unionen)."""
    if depth > 6:
        return "…"
    if isinstance(v, dict):
        return {k: shape(v[k], depth + 1) for k in sorted(v) if k not in IGNORE_KEYS}
    if isinstance(v, list):
        if not v:
            return ["tom"]
        forms = []
        for x in v[:5]:
            s = shape(x, depth + 1)
            if s not in forms:
                forms.append(s)
        return ["lista"] + forms
    if v is None:
        return "null"
    return type(v).__name__


def call(path, params):
    t = time.time()
    try:
        r = requests.get(BASE + path, params=params, timeout=150)
    except requests.RequestException as e:
        return {"status": "fel", "error": str(e)[:200], "s": round(time.time() - t, 1)}, None
    ctype = r.headers.get("Content-Type", "").split(";")[0]
    out = {"status": r.status_code, "ctype": ctype, "s": round(time.time() - t, 1)}
    body = None
    if ctype == "application/json":
        try:
            body = r.json()
        except ValueError:
            pass
    return out, (body if body is not None else r.content)


def take() -> dict:
    snap = {"base": BASE, "when": time.strftime("%Y-%m-%d %H:%M:%S"), "options": {}, "reads": {}}
    for path in routes_in_source():
        try:
            r = requests.options(BASE + path, timeout=10)
            snap["options"][path] = {"status": r.status_code,
                                     "allow": sorted(m.strip() for m in r.headers.get("Allow", "").split(",") if m.strip())}
        except requests.RequestException as e:
            snap["options"][path] = {"status": "fel", "error": str(e)[:200]}
    bodies = {}
    for name, path, params, mode in READS:
        if callable(params):
            try:
                params = params(bodies)
            except Exception:
                params = None
            if params is None:
                snap["reads"][name] = {"path": path, "skipped": "saknar kedjat värde"}
                continue
        meta, body = call(path, params)
        bodies[name] = body
        rec = {"path": path, "params": params, "mode": mode, **meta}
        if isinstance(body, (dict, list)):
            rec["shape"] = shape(body)
            if mode == "value":
                rec["value"] = body
        elif isinstance(body, (bytes, bytearray)):
            text = body.decode("utf-8", "replace")
            rec["lines"] = len(text.splitlines()) if mode == "text" else None
            if mode == "value":
                rec["sha1"] = hashlib.sha1(body).hexdigest()
        snap["reads"][name] = rec
        print(f"  {name:<26} {meta.get('status')} {meta.get('s')} s", file=sys.stderr)
    return snap


def compare(old: dict, new: dict) -> list[str]:
    diffs = []
    for path in sorted(set(old["options"]) | set(new["options"])):
        a, b = old["options"].get(path), new["options"].get(path)
        if a is None:
            diffs.append(f"NY VÄG {path} {b}")
        elif b is None:
            diffs.append(f"VÄG BORTA {path}")
        elif a != b:
            diffs.append(f"VÄG ÄNDRAD {path}: {a} → {b}")
    for name in old["reads"]:
        a, b = old["reads"][name], new["reads"].get(name)
        if b is None or "skipped" in a or "skipped" in b:
            continue
        if a.get("status") != b.get("status") or a.get("ctype") != b.get("ctype"):
            diffs.append(f"{name}: {a.get('status')} {a.get('ctype')} → {b.get('status')} {b.get('ctype')}")
            continue
        if a.get("shape") != b.get("shape"):
            diffs.append(f"{name}: formen ändrad\n    före:  {json.dumps(a.get('shape'), ensure_ascii=False)[:300]}"
                         f"\n    efter: {json.dumps(b.get('shape'), ensure_ascii=False)[:300]}")
        for k in ("value", "sha1"):
            if k in a and a.get(k) != b.get(k):
                diffs.append(f"{name}: värdet ändrat")
    return diffs


def coverage() -> list[str]:
    read_paths = {p for _, p, _, _ in READS}
    return [p for p in routes_in_source() if p not in read_paths and p not in NOT_CALLED]


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "compare"
    path = Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_FILE
    missing = coverage()
    if missing:
        print(f"OBS: vägar som varken läses eller står i NOT_CALLED: {missing}", file=sys.stderr)
    snap = take()
    if cmd == "record":
        path.write_text(json.dumps(snap, ensure_ascii=False, indent=1))
        n_ok = sum(1 for r in snap["reads"].values() if r.get("status") == 200)
        print(f"Sparat {path}: {len(snap['options'])} vägar, {n_ok}/{len(snap['reads'])} läsningar med 200")
        return 0
    old = json.loads(path.read_text())
    diffs = compare(old, snap)
    if diffs:
        print(f"{len(diffs)} skillnader mot {path} ({old['when']}):")
        for d in diffs:
            print("  " + d)
        return 1
    print(f"Inga skillnader mot {path} ({old['when']}): {len(snap['options'])} vägar, {len(snap['reads'])} läsningar")
    return 0


if __name__ == "__main__":
    sys.exit(main())
