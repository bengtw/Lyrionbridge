"""
Fejk-LMS-server för testning av lyrionbridge.
Kör på port 9000 och svarar på /jsonrpc.js precis som Lyrion gör.

Starta: python fake_lms.py
Testa bryggan sedan mot 127.0.0.1:9000 (standard-URL i lms_bridge.py).
"""

import random
import time
from flask import Flask, request, jsonify, send_file
import io

app = Flask(__name__)

# --- FEJKDATA ---

FAKE_PLAYERS = [
    {"playerid": "b8:27:eb:fb:30:d9", "name": "Office",  "connected": 1, "power": 1, "model": "squeezelite"},
    {"playerid": "bb:bb:4d:b0:d0:06", "name": "Linn",    "connected": 1, "power": 1, "model": "linn"},
    {"playerid": "bb:bb:7a:f8:33:39", "name": "C5",      "connected": 1, "power": 1, "model": "squeezebox"},
]

FAKE_ALBUMS = [
    {"id": 101, "album": "Kind of Blue",          "artist": "Miles Davis",         "artwork_track_id": "1001"},
    {"id": 102, "album": "Getz/Gilberto",         "artist": "Stan Getz",           "artwork_track_id": "1002"},
    {"id": 103, "album": "Time Out",              "artist": "Dave Brubeck Quartet","artwork_track_id": "1003"},
    {"id": 104, "album": "A Love Supreme",        "artist": "John Coltrane",       "artwork_track_id": "1004"},
    {"id": 105, "album": "Mingus Ah Um",          "artist": "Charles Mingus",      "artwork_track_id": "1005"},
    {"id": 106, "album": "Blue Train",            "artist": "John Coltrane",       "artwork_track_id": "1006"},
    {"id": 107, "album": "Waltz for Debby",       "artist": "Bill Evans Trio",     "artwork_track_id": "1007"},
    {"id": 108, "album": "Giant Steps",           "artist": "John Coltrane",       "artwork_track_id": "1008"},
    {"id": 109, "album": "The Black Saint",       "artist": "Charles Mingus",      "artwork_track_id": "1009"},
    {"id": 110, "album": "Clifford Brown & Max Roach", "artist": "Clifford Brown", "artwork_track_id": "1010"},
    {"id": 111, "album": "Night Train",           "artist": "Oscar Peterson Trio", "artwork_track_id": "1011"},
    {"id": 112, "album": "Moanin'",               "artist": "Art Blakey",          "artwork_track_id": "1012"},
]

FAKE_TRACKS = [
    {"title": "So What",            "artist": "Miles Davis",          "album": "Kind of Blue",    "duration": 562},
    {"title": "The Girl from Ipanema", "artist": "Stan Getz",         "album": "Getz/Gilberto",   "duration": 296},
    {"title": "Take Five",          "artist": "Dave Brubeck Quartet", "album": "Time Out",        "duration": 324},
    {"title": "Acknowledgement",    "artist": "John Coltrane",        "album": "A Love Supreme",  "duration": 479},
    {"title": "Goodbye Pork Pie Hat","artist": "Charles Mingus",      "album": "Mingus Ah Um",    "duration": 359},
]

FAKE_DAILY_MIXES = [
    {
        "id": "0", "title": "Daily Mix 1",
        "description": "Miles Davis, John Coltrane och mer",
        "text": "Daily Mix 1\nMiles Davis, John Coltrane och mer",
        "icon": "/html/images/playlists.png",
        "params": {"item_id": "playlists.0"}
    },
    {
        "id": "1", "title": "Daily Mix 2",
        "description": "Bill Evans, Oscar Peterson och mer",
        "text": "Daily Mix 2\nBill Evans, Oscar Peterson och mer",
        "icon": "/html/images/playlists.png",
        "params": {"item_id": "playlists.1"}
    },
    {
        "id": "2", "title": "Discover Weekly",
        "description": "Din veckovisa musikupptäckt",
        "text": "Discover Weekly\nDin veckovisa musikupptäckt",
        "icon": "/html/images/playlists.png",
        "params": {"item_id": "playlists.2"}
    },
    {
        "id": "3", "title": "Release Radar",
        "description": "Nya släpp du kan gilla",
        "text": "Release Radar\nNya släpp du kan gilla",
        "icon": "/html/images/playlists.png",
        "params": {"item_id": "playlists.3"}
    },
    {
        "id": "4", "title": "Daily Mix 3",
        "description": "Norah Jones, Diana Krall och mer",
        "text": "Daily Mix 3\nNorah Jones, Diana Krall och mer",
        "icon": "/html/images/playlists.png",
        "params": {"item_id": "playlists.4"}
    },
    {
        "id": "5", "title": "Daily Mix 4",
        "description": "Radiohead, Portishead och mer",
        "text": "Daily Mix 4\nRadiohead, Portishead och mer",
        "icon": "/html/images/playlists.png",
        "params": {"item_id": "playlists.5"}
    },
    {
        "id": "6", "title": "Daily Mix 5",
        "description": "Nick Cave, Leonard Cohen och mer",
        "text": "Daily Mix 5\nNick Cave, Leonard Cohen och mer",
        "icon": "/html/images/playlists.png",
        "params": {"item_id": "playlists.6"}
    },
    {
        "id": "7", "title": "daylist • eftermiddag tisdag",
        "description": "Din personliga dagslista",
        "text": "daylist • eftermiddag tisdag\nDin personliga dagslista",
        "icon": "/html/images/playlists.png",
        "params": {"item_id": "playlists.7"}
    },
]

# Spelartillstånd per MAC
player_state = {}

def get_state(mac):
    if mac not in player_state:
        track = random.choice(FAKE_TRACKS)
        player_state[mac] = {
            "mode":   "play",
            "volume": 35,
            "track":  track,
            "time":   random.randint(30, 200),
        }
    return player_state[mac]


# --- JSONRPC-HANTERARE ---

def handle_command(player_id, args):
    if not args:
        return {}

    cmd = args[0]
    state = get_state(player_id) if player_id else {}

    # players 0 10
    if cmd == "players":
        return {
            "count": len(FAKE_PLAYERS),
            "players_loop": FAKE_PLAYERS
        }

    # albums 0 N tags:...
    if cmd == "albums":
        offset = int(args[1]) if len(args) > 1 else 0
        count  = int(args[2]) if len(args) > 2 else 10
        # sort:random
        pool = FAKE_ALBUMS[:]
        if any("sort:random" in str(a) for a in args):
            random.shuffle(pool)
        page = pool[offset:offset + count]
        return {
            "count": len(FAKE_ALBUMS),
            "albums_loop": page
        }

    # status - 1 tags:...
    if cmd == "status":
        track = state.get("track", FAKE_TRACKS[0])
        return {
            "mode":          state.get("mode", "pause"),
            "mixer volume":  state.get("volume", 30),
            "time":          state.get("time", 0),
            "playlist_loop": [{
                "title":    track["title"],
                "artist":   track["artist"],
                "album":    track["album"],
                "duration": track["duration"],
            }]
        }

    # mixer volume ?  /  mixer volume N
    if cmd == "mixer" and len(args) > 1 and args[1] == "volume":
        val = args[2] if len(args) > 2 else "?"
        if val == "?":
            return {"_volume": state.get("volume", 30)}
        else:
            state["volume"] = int(val)
            return {"_volume": state["volume"]}

    # pause  (toggle)
    if cmd == "pause":
        state["mode"] = "pause" if state.get("mode") == "play" else "play"
        return {"mode": state["mode"]}

    # stop
    if cmd == "stop":
        state["mode"] = "stop"
        return {}

    # playlist shuffle N
    if cmd == "playlist" and len(args) > 1 and args[1] == "shuffle":
        return {}

    # playlist clear
    if cmd == "playlist" and len(args) > 1 and args[1] == "clear":
        return {}

    # playlist play URL
    if cmd == "playlist" and len(args) > 1 and args[1] == "play":
        state["mode"]  = "play"
        state["track"] = {
            "title": "Streaming", "artist": "Spotify", "album": "", "duration": 0
        }
        print(f"[FAKE-LMS] Spelar URL: {args[2] if len(args) > 2 else '?'}")
        return {}

    # playlist index +1
    if cmd == "playlist" and len(args) > 1 and args[1] == "index":
        state["track"] = random.choice(FAKE_TRACKS)
        state["time"]  = 0
        return {}

    # playlistcontrol cmd:load album_id:N
    if cmd == "playlistcontrol":
        album_id = None
        for a in args:
            if str(a).startswith("album_id:"):
                album_id = int(str(a).split(":")[1])
        album = next((al for al in FAKE_ALBUMS if al["id"] == album_id), FAKE_ALBUMS[0])
        state["mode"]  = "play"
        state["track"] = {
            "title":    "Track 1",
            "artist":   album["artist"],
            "album":    album["album"],
            "duration": 240,
        }
        print(f"[FAKE-LMS] Laddar album: {album['album']} ({album_id})")
        return {}

    # spotty items  (daily mixes / spy)
    if cmd == "spotty" and len(args) > 1 and args[1] == "items":
        return {
            "count": len(FAKE_DAILY_MIXES),
            "item_loop": FAKE_DAILY_MIXES
        }

    # spotty playlist play
    if cmd == "spotty" and len(args) > 1 and args[1] == "playlist":
        item_id = next((a for a in args if str(a).startswith("item_id:")), "item_id:?")
        print(f"[FAKE-LMS] Spotty play: {item_id}")
        state["mode"]  = "play"
        state["track"] = {
            "title": "Daily Mix", "artist": "Spotify", "album": "", "duration": 0
        }
        return {}

    print(f"[FAKE-LMS] Okänt kommando: {args}")
    return {}


# --- HTTP-ENDPOINTS ---

@app.route('/jsonrpc.js', methods=['POST'])
def jsonrpc():
    body = request.get_json(force=True, silent=True) or {}
    req_id    = body.get('id', 1)
    params    = body.get('params', ['', []])
    player_id = params[0] if len(params) > 0 else ''
    args      = params[1] if len(params) > 1 else []

    result = handle_command(player_id, args)
    return jsonify({"id": req_id, "method": "slim.request", "result": result})


@app.route('/music/<track_id>/cover.jpg')
@app.route('/music/current/cover.jpg')
def fake_cover(track_id=None):
    """Returnerar en liten enkel PNG som platshållarbild."""
    # 1x1 grå PNG
    png_bytes = (
        b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01'
        b'\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00'
        b'\x00\x0cIDATx\x9cc\x88\x88\x88\x00\x00\x00\x04\x00'
        b'\x01\xa0\xdc\xd5\xc3\x00\x00\x00\x00IEND\xaeB`\x82'
    )
    return send_file(io.BytesIO(png_bytes), mimetype='image/png')


if __name__ == '__main__':
    print("=" * 50)
    print("  FAKE LMS-SERVER  (port 9000)")
    print("  Riktar sig mot lyrionbridge på port 5001")
    print("=" * 50)
    app.run(host='0.0.0.0', port=9000, debug=True)
