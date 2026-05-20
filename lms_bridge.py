import json
import os
import re
import glob
import logging
import sqlite3
import threading
import requests
import random
import time
import urllib.parse
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, request, send_from_directory, jsonify
def _load_dotenv(path):
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, _, v = line.partition('=')
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except FileNotFoundError:
        pass

_load_dotenv(Path(__file__).parent.parent / "edgar" / ".env")

_session = requests.Session()
_session.mount('http://', requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=10))

CATEGORY_INDEX = {
    "artist":   0,
    "track":    1,
    "playlist": 2,
    "podcast":  3,
    "episode":  4,
    "user":     5,
}

app = Flask(__name__)

# --- KONFIGURATION ---
LMS_HOST = "10.0.1.132"
EDGAR_URL = "http://127.0.0.1:5015"
LMS_URL = f"http://{LMS_HOST}:9000/jsonrpc.js"
C5_IP   = "10.0.1.125"
C5_MAC  = "bb:bb:7a:f8:33:39"
DEBUG              = os.getenv("BRIDGE_DEBUG", "").lower() == "true"
LASTFM_API_KEY     = os.getenv("LAST_FM_API_KEY") or os.getenv("LASTFM_API_KEY", "9ed2b1dfa5c3f0ece0a30ec8e69b4742")
LASTFM_API_SECRET  = os.getenv("LAST_FM_API_SECRET") or os.getenv("LASTFM_API_SECRET", "")
LASTFM_SESSION_KEY = os.getenv("LASTFM_SESSION_KEY", "")
LASTFM_USERNAME    = os.getenv("LASTFM_USERNAME", "LadoCasseta")

FAVORITE_PLAYLISTS = [
    ("Background Jazz",      "spotify:playlist:37i9dQZF1DWV7EzJMK2FUI?si=e099779019b14bb7"),
    ("Chilled Classical",    "spotify:playlist:37i9dQZF1DWUvHZA1zLcjW?si=ef77a6c2ebf14473"),
    ("Soft Lounge",          "spotify:playlist:37i9dQZF1DX82pCGH5USnM?si=7752baaaeff94464"),
    ("Soul Mix",             "spotify:playlist:37i9dQZF1EQntZpEGgfBif?si=357c1eec328d4db2"),
    ("Dinner with Friends",  "spotify:playlist:37i9dQZF1DX4xuWVBs4FgJ?si=7c1574dfb25d4117"),
    ("Coffee Table Jazz",    "spotify:playlist:37i9dQZF1DWVqfgj8NZEp1?si=f90718546eb4492f")
]

_SEARCH_CACHE_DB   = Path(__file__).parent / "metadata_cache.db"
_SEARCH_TTL_TRACK  = 30 * 86400   # 30 dagar — spår ändras sällan
_SEARCH_TTL_OTHER  = 7  * 86400   # 7 dagar  — album/artist/playlist
_MIX_LABEL_TTL     = 6  * 3600    # 6 timmar — daily mixes byts ut dagligen


def _init_search_cache():
    with sqlite3.connect(_SEARCH_CACHE_DB) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS search_cache (
                query     TEXT    NOT NULL,
                type      TEXT    NOT NULL,
                limit_n   INTEGER NOT NULL,
                results   TEXT    NOT NULL,
                cached_at INTEGER NOT NULL,
                PRIMARY KEY (query, type, limit_n)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS mix_labels (
                mix_id    INTEGER PRIMARY KEY,
                label     TEXT    NOT NULL,
                cached_at INTEGER NOT NULL
            )
        """)


_init_search_cache()

_db_lock       = threading.Lock()   # serialiserar skrivningar mot metadata_cache.db
_gemini_client = None
_gemini_lock   = threading.Lock()

def _get_gemini():
    global _gemini_client
    if _gemini_client:
        return _gemini_client
    with _gemini_lock:
        if _gemini_client:
            return _gemini_client
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            return None
        from google import genai
        _gemini_client = genai.Client(api_key=api_key)
    return _gemini_client


def _get_mix_label(artists_text: str) -> str | None:
    """Frågar Gemini om 1-2 ord som sammanfattar stämningen hos dessa artister."""
    client = _get_gemini()
    if not client:
        return None
    prompt = (
        f"Artister: {artists_text}\n\n"
        "Ge ett label på 1–2 ord på svenska som hjälper användaren känna igen den här spellistan. "
        "Föredra konkreta genrebeteckningar, eror eller stilar framför abstrakta känsloord. "
        "Exempel på bra svar: 'Brittisk indie', 'EBM-klubb', 'Soulful 70s', 'Synthpop', 'Klassisk jazz'. "
        "Exempel på dåliga svar: 'Stora känslor', 'Episk känsla', 'Tidlös stämning'. "
        "Bara orden, inget annat."
    )
    try:
        resp = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
        label = resp.text.strip().strip('.')
        return label if label else None
    except Exception as e:
        logging.warning(f"[mix_labels] Gemini-fel: {e}")
        return None


def _refresh_mix_labels():
    """Genererar AI-labels för Daily Mix 1–6 och cachar i DB. Körs i bakgrund."""
    now = int(time.time())
    stale_ids = set()

    with sqlite3.connect(_SEARCH_CACHE_DB) as conn:
        rows = {r[0]: r[1] for r in conn.execute(
            "SELECT mix_id, cached_at FROM mix_labels"
        ).fetchall()}
    for mix_id in range(6):
        if mix_id not in rows or (now - rows[mix_id]) > _MIX_LABEL_TTL:
            stale_ids.add(mix_id)

    if not stale_ids:
        return

    mixes_raw = _fetch_daily_mixes_raw()
    for item in mixes_raw:
        parts = item.get('text', '').split('\n')
        title = parts[0]
        if not (title.startswith('Daily Mix ') and len(title) == 11 and '1' <= title[10] <= '6'):
            continue
        mix_id = int(title[10]) - 1
        if mix_id not in stale_ids:
            continue
        artists_text = parts[1] if len(parts) > 1 else title
        label = _get_mix_label(artists_text)
        if label:
            with _db_lock, sqlite3.connect(_SEARCH_CACHE_DB) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO mix_labels (mix_id, label, cached_at) VALUES (?,?,?)",
                    (mix_id, label, int(time.time()))
                )
            logging.info(f"[mix_labels] {title} → \"{label}\"")


def _mix_label_loop():
    time.sleep(5)
    while True:
        try:
            _refresh_mix_labels()
        except Exception as e:
            logging.warning(f"[mix_labels] refresh-fel: {e}")
        time.sleep(_MIX_LABEL_TTL)


def _search_cache_get(query: str, search_type: str, limit: int):
    ttl    = _SEARCH_TTL_TRACK if search_type == "track" else _SEARCH_TTL_OTHER
    cutoff = int(time.time()) - ttl
    with sqlite3.connect(_SEARCH_CACHE_DB) as conn:
        row = conn.execute(
            "SELECT results FROM search_cache WHERE query=? AND type=? AND limit_n=? AND cached_at>?",
            (query, search_type, limit, cutoff),
        ).fetchone()
    return json.loads(row[0]) if row else None


def _search_cache_set(query: str, search_type: str, limit: int, results: list):
    with _db_lock, sqlite3.connect(_SEARCH_CACHE_DB) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO search_cache (query, type, limit_n, results, cached_at) VALUES (?,?,?,?,?)",
            (query, search_type, limit, json.dumps(results), int(time.time())),
        )


_playlist_art_cache = []
_playlist_art_cache_time = 0
_PLAYLIST_CACHE_TTL = 3600

_player_cache = []
_player_cache_time = 0
_PLAYER_CACHE_TTL = 30

_active_players_cache = None
_active_players_cache_time = 0
_ACTIVE_PLAYERS_CACHE_TTL = 5


# --- HJÄLPFUNKTIONER ---

def _cache_valid(ts, ttl):
    return time.time() - ts < ttl

def _abs_image(url):
    """Gör relativ LMS-bild-URL absolut."""
    if url and url.startswith('/'):
        return f"http://{LMS_HOST}:9000{url}"
    return url or ""

def _act_on_active(command_fn):
    """Kör command_fn(mac) på alla spelare som för tillfället spelar. Returnerar berörda rum."""
    acted = []
    for p in get_all_players():
        mac = p.get('playerid')
        if not mac:
            continue
        res = lms_json_rpc(mac, ["status", "-", "1"])
        if res and res.get('result', {}).get('mode') == 'play':
            command_fn(mac)
            acted.append(p.get('name'))
    return acted

def set_c5_volume_upnp(volume_level):
    """Sätter volymen på Audio Pro C5 via UPnP — används när C5 kör Spotify Connect."""
    url = f"http://{C5_IP}:49152/upnp/control/rendercontrol1"
    headers = {
        'Content-Type': 'text/xml; charset="utf-8"',
        'SOAPACTION': '"urn:schemas-upnp-org:service:RenderingControl:1#SetVolume"'
    }
    body = f"""<?xml version="1.0" encoding="utf-8"?>
    <s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
        <s:Body>
            <u:SetVolume xmlns:u="urn:schemas-upnp-org:service:RenderingControl:1">
                <InstanceID>0</InstanceID>
                <Channel>Master</Channel>
                <DesiredVolume>{volume_level}</DesiredVolume>
            </u:SetVolume>
        </s:Body>
    </s:Envelope>"""
    try:
        r = _session.post(url, data=body, headers=headers, timeout=2)
        print(f"[UPNP] C5 svar: {r.status_code} {r.text[:200]}")
    except Exception as e:
        print(f"[UPNP ERROR] C5: {e}")

def lms_json_rpc(player_id, command_args, timeout=3):
    payload = {"id": 1, "method": "slim.request", "params": [player_id, command_args]}
    try:
        return _session.post(LMS_URL, json=payload, timeout=timeout).json()
    except Exception as e:
        print(f"[ERROR] LMS: {e}")
        return None

def _deferred_kick(player_mac, delay=5.0):
    """Väntar delay sekunder, kollar om spelaren faktiskt spelar — annars kickar vi igång den."""
    def _kick():
        time.sleep(delay)
        res = lms_json_rpc(player_mac, ["status", "-", 1, "tags:"])
        if res:
            mode = res.get("result", {}).get("mode")
            playlist_tracks = res.get("result", {}).get("playlist_tracks", 0)
            if mode != "play" and playlist_tracks > 0:
                print(f"[KICK] {player_mac} mode={mode!r} men {playlist_tracks} spår i kön — kickar igång")
                lms_json_rpc(player_mac, ["play"])
    threading.Thread(target=_kick, daemon=True).start()

def lms_play_stream(player_mac, play_command):
    """stop → clear → play → shuffle on (för URLs och daily mixes)"""
    lms_json_rpc(player_mac, ["stop"])
    lms_json_rpc(player_mac, ["playlist", "clear"])
    result = lms_json_rpc(player_mac, play_command)
    lms_json_rpc(player_mac, ["playlist", "shuffle", 1])
    _deferred_kick(player_mac)
    return result

def lms_play_playlist(player_mac, url):
    """Spotify playlist: shuffle off → stop → clear → play → vänta på Spotty → shuffle on → skip → play"""
    lms_json_rpc(player_mac, ["playlist", "shuffle", 0])
    lms_json_rpc(player_mac, ["stop"])
    lms_json_rpc(player_mac, ["playlist", "clear"])
    res = lms_json_rpc(player_mac, ["playlist", "play", url])
    time.sleep(2.0)
    lms_json_rpc(player_mac, ["playlist", "shuffle", 1])
    lms_json_rpc(player_mac, ["playlist", "index", "+1"])
    lms_json_rpc(player_mac, ["play"])
    _deferred_kick(player_mac)
    return res

def lms_load_album(player_mac, album_id):
    """Shuffle off → clear → load album"""
    lms_json_rpc(player_mac, ["playlist", "shuffle", 0])
    lms_json_rpc(player_mac, ["playlist", "clear"])
    return lms_json_rpc(player_mac, ["playlistcontrol", "cmd:load", f"album_id:{album_id}"])


def get_all_players():
    """Hämtar alla aktuella spelare dynamiskt från LMS, med kort TTL-cache."""
    global _player_cache, _player_cache_time
    if _player_cache and _cache_valid(_player_cache_time, _PLAYER_CACHE_TTL):
        return _player_cache
    res = lms_json_rpc("", ["players", "0", "10"])
    if res and 'result' in res:
        players = res['result'].get('players_loop', [])
        if players:
            _player_cache = players
            _player_cache_time = time.time()
    return _player_cache

def _any_player_mac():
    """Returnerar MAC-adressen för första tillgängliga spelare."""
    players = get_all_players()
    return players[0].get('playerid', '') if players else ""

def get_player_info(room_arg):
    """Matcher dynamiskt rummet mot aktiva spelare i LMS."""
    players = get_all_players()
    if not players:
        return None, "unknown"

    if not room_arg:
        return players[0].get('playerid'), players[0].get('name')

    decoded = urllib.parse.unquote(room_arg).strip().lower()

    if ":" in decoded:
        for p in players:
            if p.get('playerid', '').lower() == decoded:
                return decoded, p.get('name')
        return decoded, "unknown"

    for p in players:
        name = p.get('name', '').lower()
        if decoded in name or name in decoded:
            return p.get('playerid'), p.get('name')

    return None, decoded


def _fetch_daily_mixes_raw():
    """Hämtar råa Spotty-items från startsidan (Daily Mixes, Radar, Discovery, etc.)."""
    player_mac = _any_player_mac()
    res = lms_json_rpc(player_mac, ["spotty", "items", 0, 80, "item_id:0", "menu:1", "tags:s"], timeout=35)
    if res and 'result' in res:
        return res['result'].get('item_loop', [])
    return []


def _format_track(item, stable_uri=None):
    name = item.get('name', '')
    uri = stable_uri or (item.get('id', '') + '.0')

    title, artist, album = name, "", ""
    if ' by ' in name:
        title, rest = name.split(' by ', 1)
        if ' from ' in rest:
            artist, album = rest.split(' from ', 1)
        else:
            artist = rest

    return {
        "name": title.strip(),
        "subtitle": f"{artist.strip()} — {album.strip()}".strip(" —"),
        "uri": uri,
        "art": _abs_image(item.get('image', '')),
    }


def _get_stable_spotify_uri(item_id, player_mac):
    """Borrar ner till leaf-noden och returnerar stabil spotify://track:XXXX-URI.
    Försöker upp till 2 gånger med 8 sekunders timeout."""
    for _ in range(2):
        sub = lms_json_rpc(player_mac, ["spotty", "items", 0, 1, f"item_id:{item_id}"], timeout=8)
        if sub and 'result' in sub:
            loop = sub['result'].get('loop_loop', [])
            if loop:
                name = loop[0].get('name', '')
                if name.startswith('spotify://'):
                    return name
    return None

def _format_entry(item, search_type):
    """Formattera album/artist/playlist-träff från kategori-undermenyn."""
    name = item.get('name', '')
    uri = item.get('url') or item.get('play') or item.get('id', '')

    title, subtitle = name, ""
    if search_type == "album" and ' by ' in name:
        title, subtitle = name.split(' by ', 1)

    return {
        "name": title.strip(),
        "subtitle": subtitle.strip(),
        "uri": uri,
        "art": _abs_image(item.get('image', '')),
    }

def _query_player_status(p):
    mac = p.get('playerid')
    if not mac:
        return None
    res = lms_json_rpc(mac, ["status", "-", "1", "tags:atl"])
    if not res or 'result' not in res:
        return None
    r = res['result']
    if r.get('mode') != 'play':
        return None
    loop = r.get('playlist_loop', [])
    track = {'title': loop[0].get('title', ''), 'artist': loop[0].get('artist', '')} if loop else {}
    return {'room': p.get('name'), 'mac': mac, 'mode': 'play', 'track': track}


# --- LAST.FM LÄSNING ---

_lastfm_taste_cache      = None
_lastfm_taste_cache_time = 0
_LASTFM_TASTE_TTL        = 900  # 15 minuter

_lastfm_fail_counts: dict[str, int] = {}

def _lastfm_sig(params: dict) -> str:
    s = "".join(f"{k}{v}" for k, v in sorted(params.items()) if k != "format")
    return __import__("hashlib").md5((s + LASTFM_API_SECRET).encode()).hexdigest()

def _lastfm_post(method: str, **params) -> dict | None:
    """Autentiserat POST-anrop mot Last.fm API (kräver session key)."""
    if not (LASTFM_API_KEY and LASTFM_API_SECRET and LASTFM_SESSION_KEY):
        return None
    p = {"method": method, "api_key": LASTFM_API_KEY, "sk": LASTFM_SESSION_KEY, **params}
    p["api_sig"] = _lastfm_sig(p)
    p["format"]  = "json"
    try:
        r = _session.post("https://ws.audioscrobbler.com/2.0/", data=p, timeout=5)
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            print(f"[LastFM] {method} fel {data['error']}: {data.get('message', '')}")
            return None
        return data
    except Exception as e:
        print(f"[LastFM] {method} misslyckades: {e}")
        return None

def _lastfm_get(method, **params):
    """Läs-anrop mot Last.fm API (ingen signatur krävs)."""
    if not LASTFM_API_KEY:
        return None
    try:
        r = _session.get("https://ws.audioscrobbler.com/2.0/", params={
            "method":  method,
            "api_key": LASTFM_API_KEY,
            "format":  "json",
            **params,
        }, timeout=5)
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            _lastfm_fail_counts[method] = _lastfm_fail_counts.get(method, 0) + 1
            print(f"[LastFM] {method} API-fel {data['error']}: {data.get('message', '')} (totalt {_lastfm_fail_counts[method]}x)")
            return None
        return data
    except Exception as e:
        _lastfm_fail_counts[method] = _lastfm_fail_counts.get(method, 0) + 1
        print(f"[LastFM] {method} misslyckades: {e} (totalt {_lastfm_fail_counts[method]}x)")
        return None

def _get_lastfm_taste_profile():
    """Returnerar en dict med top_artists och recent_tracks från Last.fm, med cache."""
    global _lastfm_taste_cache, _lastfm_taste_cache_time
    if _lastfm_taste_cache and _cache_valid(_lastfm_taste_cache_time, _LASTFM_TASTE_TTL):
        return _lastfm_taste_cache

    if not (LASTFM_API_KEY and LASTFM_USERNAME):
        return None

    top_resp    = _lastfm_get("user.getTopArtists", user=LASTFM_USERNAME, period="1month", limit=10)
    recent_resp = _lastfm_get("user.getRecentTracks", user=LASTFM_USERNAME, limit=10)

    def _as_list(val):
        if isinstance(val, list):
            return val
        if isinstance(val, dict):
            return [val]
        return []

    top_artists = []
    if top_resp and "topartists" in top_resp:
        top_artists = [a["name"] for a in _as_list(top_resp["topartists"].get("artist"))]

    recent_tracks = []
    if recent_resp and "recenttracks" in recent_resp:
        for t in _as_list(recent_resp["recenttracks"].get("track")):
            if not t.get("@attr", {}).get("nowplaying"):
                recent_tracks.append(f"{t['name']} – {t['artist']['#text']}")
        recent_tracks = list(dict.fromkeys(recent_tracks))[:8]

    profile = {"top_artists": top_artists, "recent_tracks": recent_tracks}
    _lastfm_taste_cache      = profile
    _lastfm_taste_cache_time = time.time()
    return profile


# --- PWA SERVERING ---

@app.route('/')
def serve_index():
    return send_from_directory('static', 'index.html')

@app.route('/<path:path>')
def serve_static(path):
    return send_from_directory('static', path)


# --- API ENDPOINTS ---

@app.route('/get_players')
def get_players():
    res = lms_json_rpc("", ["players", "0", "10"])
    return jsonify(res['result'] if res and 'result' in res else {"players_loop": []})

@app.route('/get_playlists')
def get_playlists():
    """Textformat för ESP32 och legacy-klienter"""
    body = "\n".join(f"{name}|{url}" for name, url in FAVORITE_PLAYLISTS)
    return body, 200, {'Content-Type': 'text/plain; charset=utf-8'}

@app.route('/get_playlists_with_art')
def get_playlists_with_art():
    global _playlist_art_cache, _playlist_art_cache_time
    if _playlist_art_cache and _cache_valid(_playlist_art_cache_time, _PLAYLIST_CACHE_TTL):
        return jsonify(_playlist_art_cache)
    result = []
    for name, uri in FAVORITE_PLAYLISTS:
        art = "https://via.placeholder.com/300x300/111/444?text=List"
        try:
            r = _session.get(f"https://open.spotify.com/oembed?url={uri.split('?')[0]}", timeout=2)
            if r.status_code == 200:
                art = r.json().get('thumbnail_url', art)
        except Exception as e:
            print(f"[VARNING] Spotify-bild för {name}: {e}")
        result.append({"name": name, "url": uri, "art": art})
    _playlist_art_cache = result
    _playlist_art_cache_time = time.time()
    return jsonify(_playlist_art_cache)

@app.route('/play_url')
def play_url():
    url = request.args.get('url')
    player_mac, room_name = get_player_info(request.args.get('room'))

    if not (player_mac and url):
        return "Missing URL or Room", 400

    clean_url = url.split('?')[0].strip()

    # --- ALBUM: Shuffle AV ---
    if "spotify:album:" in clean_url:
        lms_json_rpc(player_mac, ["playlist", "shuffle", 0])
        res = lms_load_album(player_mac, clean_url)

    # --- SPELLISTOR: Shuffle PÅ, vänta på att Spotty laddar kön ---
    elif "spotify:playlist:" in clean_url:
        res = lms_play_playlist(player_mac, clean_url)

    # --- ENKLA LÅTAR: Spotty-sökresultat eller stabil spotify:// URI ---
    elif clean_url.startswith("1.0_") or "spotify:track:" in clean_url or clean_url.startswith("spotify://track:"):
        lms_json_rpc(player_mac, ["playlist", "shuffle", 0])
        lms_json_rpc(player_mac, ["stop"])
        lms_json_rpc(player_mac, ["playlist", "clear"])
        if clean_url.startswith("1.0_"):
            res = lms_json_rpc(player_mac, ["spotty", "playlist", "play", f"item_id:{clean_url}"])
        else:
            res = lms_json_rpc(player_mac, ["playlist", "play", clean_url])

    else:
        res = lms_play_stream(player_mac, ["playlist", "play", clean_url])

    return jsonify({"status": "ok", "sent_url": clean_url, "lms_response": res})

@app.route('/search_library')
def search_library():
    """Söker i det lokala Lyrion-biblioteket efter album eller artister.

    Query-parametrar:
        q      = sökterm (krävs)
        type   = 'album' (default) | 'artist' | 'track'
        limit  = antal träffar (default 20, max 100)
    """
    query = request.args.get('q', '').strip()
    if not query:
        return jsonify({"error": "Missing q parameter"}), 400

    search_type = request.args.get('type', 'album').lower()
    try:
        limit = int(request.args.get('limit', '20'))
    except ValueError:
        limit = 20
    limit = max(1, min(limit, 100))

    results = []

    if search_type == 'album':
        res = lms_json_rpc(None, [
            "albums", 0, limit,
            f"search:{query}",
            "tags:alj",
        ])
        if res and 'result' in res:
            for item in res['result'].get('albums_loop', []):
                cover_id = item.get('artwork_track_id') or item.get('id')
                results.append({
                    'id':     item.get('id'),
                    'title':  item.get('album'),
                    'artist': item.get('artist'),
                    'year':   item.get('year'),
                    'art':    f"http://{LMS_HOST}:9000/music/{cover_id}/cover.jpg"
                              if cover_id else None,
                })

    elif search_type == 'artist':
        res = lms_json_rpc(None, [
            "artists", 0, limit,
            f"search:{query}",
        ])
        if res and 'result' in res:
            for item in res['result'].get('artists_loop', []):
                results.append({
                    'id':   item.get('id'),
                    'name': item.get('artist'),
                })

    elif search_type == 'track':
        res = lms_json_rpc(None, [
            "titles", 0, limit,
            f"search:{query}",
            "tags:alt",
        ])
        if res and 'result' in res:
            for item in res['result'].get('titles_loop', []):
                results.append({
                    'id':     item.get('id'),
                    'title':  item.get('title'),
                    'artist': item.get('artist'),
                    'album':  item.get('album'),
                })
    else:
        return jsonify({"error": f"Unknown type: {search_type}"}), 400

    return jsonify({
        'query': query,
        'type': search_type,
        'items': results,
    })

@app.route('/get_artist_albums')
def get_artist_albums():
    """Alla album av en specifik artist. Tar artist_id som krävs."""
    artist_id = request.args.get('artist_id', '').strip()
    if not artist_id:
        return jsonify({"error": "Missing artist_id"}), 400

    res = lms_json_rpc(None, [
        "albums", 0, 200,
        f"artist_id:{artist_id}",
        "tags:alj",
        "sort:yearalbum",
    ])

    albums = []
    if res and 'result' in res:
        for item in res['result'].get('albums_loop', []):
            cover_id = item.get('artwork_track_id') or item.get('id')
            albums.append({
                'id':     item.get('id'),
                'title':  item.get('album'),
                'artist': item.get('artist'),
                'year':   item.get('year'),
                'art':    f"http://{LMS_HOST}:9000/music/{cover_id}/cover.jpg"
                          if cover_id else None,
            })

    return jsonify({"artist_id": artist_id, "albums": albums})


@app.route('/play_artist_random')
def play_artist_random():
    """Spelar ett slumpmässigt album av en specifik artist."""
    artist_id = request.args.get('artist_id', '').strip()
    player_mac, _ = get_player_info(request.args.get('room'))

    if not (player_mac and artist_id):
        return "Missing artist_id or room", 400

    res = lms_json_rpc(None, [
        "albums", 0, 200,
        f"artist_id:{artist_id}",
        "tags:l",
    ])

    albums = res.get('result', {}).get('albums_loop', []) if res else []
    if not albums:
        return f"Inga album hittades för artist_id:{artist_id}", 404

    target = random.choice(albums)
    lms_load_album(player_mac, target['id'])
    return jsonify({
        "played": target.get('album'),
        "artist": target.get('artist'),
        "album_id": target.get('id'),
    })

@app.route('/daily')
def play_daily():
    player_mac, _ = get_player_info(request.args.get('room'))
    idx = request.args.get('index', '0')
    if not player_mac:
        return "Error", 404
    lms_play_stream(player_mac, ["spotty", "playlist", "play", f"item_id:playlists.{idx}"])
    return f"Playing Daily Mix {idx}"

@app.route('/play_random_album')
def play_random_album():
    player_mac, _ = get_player_info(request.args.get('room'))
    if not player_mac:
        return "Error", 404
    res = lms_json_rpc("", ["albums", "0", "500", "tags:l"])
    try:
        target = random.choice(res['result']['albums_loop'])
        lms_load_album(player_mac, target['id'])
        return f"Playing: {target['album']}"
    except (KeyError, TypeError, IndexError):
        return "LMS Error", 500

@app.route('/set_volume')
def set_volume():
    level = request.args.get('level', '30')
    player_mac, room_name = get_player_info(request.args.get('room'))
    if not player_mac:
        return "Error", 404
    level_str = str(level).strip()
    if "c5" in room_name.lower():
        set_c5_volume_upnp(level_str)
    if level_str.startswith(('+', '-')):
        lms_json_rpc(player_mac, ["mixer", "volume", level_str])
    else:
        lms_json_rpc(player_mac, ["mixer", "volume", int(level_str)])
    return level_str

@app.route('/volume')
def get_volume():
    player_mac, _ = get_player_info(request.args.get('room'))
    if not player_mac:
        return "0"
    res = lms_json_rpc(player_mac, ["mixer", "volume", "?"])
    try:
        return str(res['result']['_volume'])
    except (KeyError, TypeError):
        return "30"

@app.route('/title')
def get_title():
    player_mac, _ = get_player_info(request.args.get('room'))
    if not player_mac:
        return "Välj rum"
    res = lms_json_rpc(player_mac, ["status", "-", "1", "tags:atl"])
    try:
        if res['result'].get('mode') != 'play':
            return ""
        track = res['result']['playlist_loop'][0]
        title = track.get('title') or track.get('name', 'Ingen titel')
        artist = track.get('artist', '')
        return f"{title} - {artist}" if artist else title
    except (KeyError, TypeError, IndexError):
        return ""

@app.route('/stop')
def stop_playback():
    player_mac, _ = get_player_info(request.args.get('room'))
    if not player_mac:
        return "Error", 404
    lms_json_rpc(player_mac, ["stop"])
    return "OK"

@app.route('/toggle_play_pause')
def toggle_playback():
    player_mac, room_name = get_player_info(request.args.get('room'))
    if not player_mac:
        return "Error", 404
    print(f"[TOGGLE] {room_name} ({player_mac})", flush=True)
    lms_json_rpc(player_mac, ["pause"])
    return "OK"

@app.route('/next')
def next_track():
    player_mac, _ = get_player_info(request.args.get('room'))
    if not player_mac:
        return "Error", 404
    lms_json_rpc(player_mac, ["playlist", "index", "+1"])
    return "OK"

@app.route('/art')
def get_album_art():
    player_mac, _ = get_player_info(request.args.get('room'))
    if not player_mac:
        return "/static/icon.png"
    return f"http://{LMS_HOST}:9000/music/current/cover.jpg?player={player_mac}&time={int(time.time())}"

@app.route('/status')
def get_status():
    player_mac, _ = get_player_info(request.args.get('room'))
    if not player_mac:
        return "pause"
    res = lms_json_rpc(player_mac, ["status", "-", "1"])
    try:
        return res['result']['mode']
    except (KeyError, TypeError):
        return "pause"

@app.route('/get_random_albums')
def get_random_albums():
    res = lms_json_rpc(None, ["albums", 0, 10, "sort:random", "tags:albj"])
    albums = []
    if res and 'result' in res:
        for item in res['result'].get('albums_loop', []):
            cover_id = item.get('artwork_track_id') or item.get('id')
            albums.append({
                'id':     item.get('id'),
                'title':  item.get('album'),
                'artist': item.get('artist'),
                'art':    f"http://{LMS_HOST}:9000/music/{cover_id}/cover.jpg"
            })
    return jsonify(albums)

@app.route('/get_daily_mixes')
def get_daily_mixes():
    mixes = []
    for item in _fetch_daily_mixes_raw():
        parts = item.get('text', '').split('\n')
        title = parts[0]
        if not any(x in title for x in ["Mix", "Radar", "Discovery", "daylist"]):
            continue
        raw_id = item.get('params', {}).get('item_id') or item.get('id', '0.0')
        mixes.append({
            'id':          raw_id.split('.')[-1],
            'title':       title,
            'description': parts[1] if len(parts) > 1 else "Din personliga mix",
            'art':         _abs_image(item.get('icon') or item.get('image', '')),
        })
    return jsonify(mixes)

@app.route('/get_daily_mixes_knob')
def get_daily_mixes_knob():
    """Textlista för knappen: title|label|index, en per rad, bara Daily Mix 1-6.
    label är AI-genererad stämningssummering (1-2 ord); fallback på första artisten."""
    with sqlite3.connect(_SEARCH_CACHE_DB) as conn:
        cached_labels = {r[0]: r[1] for r in conn.execute(
            "SELECT mix_id, label FROM mix_labels"
        ).fetchall()}

    lines = []
    for item in _fetch_daily_mixes_raw():
        parts = item.get('text', '').split('\n')
        title = parts[0]
        if not (title.startswith('Daily Mix ') and len(title) == 11 and '1' <= title[10] <= '6'):
            continue
        idx = int(title[10]) - 1
        if idx in cached_labels:
            label = cached_labels[idx]
        else:
            # Fallback: första artisten ur beskrivningstexten
            desc = parts[1] if len(parts) > 1 else ''
            label = desc.split(',')[0].strip() if desc else title
        lines.append(f"{title}|{label}|{idx}")
    return '\n'.join(lines), 200, {'Content-Type': 'text/plain; charset=utf-8'}

_BUTTON_PROMPTS_FILE = Path(__file__).parent / "button_prompts.json"

@app.route('/button_prompt')
def button_prompt():
    """Mappar en knapptryckning till en Edgar-prompt.
    ?id=1&room=vardagsrum — fire-and-forget till Edgar /api/chat."""
    btn_id = request.args.get('id', '').strip()
    room   = request.args.get('room', '').strip()

    try:
        prompts = json.loads(_BUTTON_PROMPTS_FILE.read_text())
    except Exception:
        return jsonify({"error": "button_prompts.json saknas eller ogiltig"}), 500

    prompt = prompts.get(btn_id)
    if not prompt:
        return jsonify({"error": f"Ingen prompt för knapp {btn_id!r}"}), 404

    payload = {
        "message":      prompt,
        "client_id":    f"remote_{room or 'default'}",
        "default_room": room or None,
    }
    try:
        requests.post(f"{EDGAR_URL}/api/chat", json=payload, timeout=60)
    except Exception as e:
        return jsonify({"error": str(e)}), 502

    return jsonify({"status": "ok", "button": btn_id, "room": room, "prompt": prompt})


@app.route('/get_radio_favorites')
def get_radio_favorites():
    res = lms_json_rpc("", ["favorites", "items", "0", "50"])
    stations = []

    if res and 'result' in res:
        for item in res['result'].get('loop_loop', []):
            fav_id = item.get('id')
            if not fav_id:
                continue
            art = _abs_image(item.get('image') or item.get('icon', '')) \
                  or "https://via.placeholder.com/300x300/111/444?text=Radio"
            stations.append({
                'id':   fav_id,
                'name': item.get('name', 'Okänd kanal'),
                'url':  fav_id,
                'art':  art
            })
        stations = sorted(stations, key=lambda x: x['name'].lower())

    return jsonify(stations)

@app.route('/play_radio')
def play_radio():
    fav_id = request.args.get('url')
    player_mac, room_name = get_player_info(request.args.get('room'))

    if not (player_mac and fav_id):
        return "Missing ID or Room", 400

    print(f"[RADIO] Försöker spela favorit-ID {fav_id} i {room_name}")
    lms_json_rpc(player_mac, ["stop"])
    lms_json_rpc(player_mac, ["playlist", "clear"])
    res = lms_json_rpc(player_mac, ["favorites", "playlist", "play", f"item_id:{fav_id}"])

    return jsonify({"status": "ok", "lms_response": res})

@app.route('/play_album')
def play_specific_album():
    album_id = request.args.get('album_id')
    player_mac, _ = get_player_info(request.args.get('room'))
    if not (player_mac and album_id):
        return "Error", 400
    lms_load_album(player_mac, album_id)
    return "OK"

@app.route('/transfer')
def transfer_playback():
    from_mac, _ = get_player_info(request.args.get('from'))
    to_mac,   _ = get_player_info(request.args.get('to'))
    if not (from_mac and to_mac) or from_mac == to_mac:
        return "Error", 400

    status = lms_json_rpc(from_mac, ["status", "0", "500", "tags:u"])
    if not status or 'result' not in status:
        return "Could not get source status", 500

    r         = status['result']
    cur_index = int(r.get('playlist_cur_index', 0))
    cur_time  = int(r.get('time', 0))
    playlist  = r.get('playlist_loop', [])

    if not playlist or cur_index >= len(playlist):
        return "No playlist on source", 400

    current_url = playlist[cur_index].get('url', '')
    if not current_url:
        return "Could not get current track URL", 400

    if to_mac == C5_MAC:
        lms_json_rpc(to_mac, ["power", 1])
        set_c5_volume_upnp(20)
        time.sleep(2.0)

    lms_json_rpc(to_mac, ["playlist", "play", current_url])
    time.sleep(0.8)
    lms_json_rpc(to_mac, ["time", cur_time])

    for track in playlist[cur_index + 1:]:
        url = track.get('url', '')
        if url:
            lms_json_rpc(to_mac, ["playlist", "add", url])

    lms_json_rpc(from_mac, ["pause", 1])
    return "OK"

@app.route('/c5_discover')
def c5_discover():
    """Hämtar UPnP device description från C5 för att hitta rätt service-URL."""
    if not DEBUG:
        return "Not available", 403
    results = {}
    for port in [49152, 1400, 8080, 80]:
        try:
            r = _session.get(f"http://{C5_IP}:{port}/description.xml", timeout=2)
            if r.status_code == 200:
                results[f"port_{port}"] = r.text[:5000]
                break
        except Exception as e:
            results[f"port_{port}"] = str(e)
    return jsonify(results)


@app.route('/spotify_search')
def spotify_search():
    """Söker i Spotify via Spotty.

    Query-parametrar:
        q      = sökterm (krävs)
        type   = 'track' | 'album' | 'artist' | 'playlist' (default 'track')
        limit  = antal träffar (default 10)

    Spotty-hierarki vi navigerar:
        item_id:1.0 + search:<q>          = träfflista (items med isaudio=1 är spår)
        item_id:1.0_<q>.N                 = gå in i kategori N (artist/album/playlist)
    """
    query = request.args.get('q', '').strip()
    if not query:
        return jsonify({"error": "Missing q parameter"}), 400

    search_type = request.args.get('type', 'track').lower()
    try:
        limit = int(request.args.get('limit', '10'))
    except ValueError:
        limit = 10
    limit = max(1, min(limit, 50))

    cached = _search_cache_get(query, search_type, limit)
    if cached is not None:
        return jsonify({"query": query, "type": search_type, "items": cached, "cached": True})

    player_mac, _ = get_player_info(request.args.get('room'))

    loop = []
    for _attempt in range(3):
        initial = lms_json_rpc(player_mac, [
            "spotty", "items", 0, 50,
            "item_id:1.0",
            f"search:{query}",
        ], timeout=10)
        if initial and 'result' in initial:
            loop = initial['result'].get('loop_loop', [])
            if loop:
                break
        if _attempt < 2:
            time.sleep(0.4 * (_attempt + 1))

    if not loop:
        return jsonify({"query": query, "type": search_type, "items": []})

    if search_type == "track":
        audio_items = [it for it in loop if it.get('isaudio') == 1][:limit]

        def _resolve_item(it):
            stable = _get_stable_spotify_uri(it.get('id', ''), player_mac)
            return _format_track(it, stable_uri=stable)

        with ThreadPoolExecutor(max_workers=3) as ex:
            formatted = list(ex.map(_resolve_item, audio_items))
    else:
        cat_idx = CATEGORY_INDEX.get(search_type)
        if cat_idx is None:
            return jsonify({"error": f"Unknown type: {search_type}"}), 400

        encoded = urllib.parse.quote(query)
        sub = lms_json_rpc(player_mac, [
            "spotty", "items", 0, limit,
            f"item_id:1.0_{encoded}.{cat_idx}",
        ])
        if not sub or 'result' not in sub:
            return jsonify({"query": query, "type": search_type, "items": []})
        formatted = [_format_entry(it, search_type) for it in sub['result'].get('loop_loop', [])[:limit]]

    items_out = [f for f in formatted if f][:limit]
    if items_out:
        _search_cache_set(query, search_type, limit, items_out)
    return jsonify({
        "query": query,
        "type": search_type,
        "items": items_out,
    })


@app.route('/active_players')
def active_players():
    """Returnerar alla spelare som för närvarande spelar musik."""
    global _active_players_cache, _active_players_cache_time
    if _active_players_cache is not None and _cache_valid(_active_players_cache_time, _ACTIVE_PLAYERS_CACHE_TTL):
        return jsonify(_active_players_cache)
    players = get_all_players()
    with ThreadPoolExecutor(max_workers=len(players) or 1) as ex:
        results = list(ex.map(_query_player_status, players))
    playing = [r for r in results if r is not None]
    _active_players_cache = playing
    _active_players_cache_time = time.time()
    return jsonify(playing)


@app.route('/stop_active')
def stop_active():
    """Stoppar alla spelare som för närvarande spelar."""
    stopped = _act_on_active(lambda mac: lms_json_rpc(mac, ["pause", 1]))
    return jsonify({'stopped': stopped})


@app.route('/next_active')
def next_active():
    """Hoppar till nästa låt på den spelare som spelar."""
    acted = _act_on_active(lambda mac: lms_json_rpc(mac, ["playlist", "index", "+1"]))
    return jsonify({'next': acted})

@app.route('/spotify_artist_top')
def spotify_artist_top():
    """Hämtar populäraste låtarna för en artist via Spotty."""
    query = request.args.get('q', '').strip()
    player_mac, _ = get_player_info(request.args.get('room'))
    if not query:
        return jsonify({"error": "Missing q"}), 400

    _spotty = 35

    search_res = lms_json_rpc(player_mac, ["spotty", "items", 0, 5, "item_id:1.0", f"search:{query}"], timeout=_spotty)
    if not search_res or 'result' not in search_res:
        return jsonify({"error": "Sökning misslyckades"}), 500

    loop = search_res['result'].get('loop_loop', [])
    artist_cat = next((it for it in loop if "Artists" in it.get('name', '')), None)
    if not artist_cat:
        return jsonify({"error": "Ingen artist-kategori hittades"}), 404

    artists = lms_json_rpc(player_mac, ["spotty", "items", 0, 1, f"item_id:{artist_cat['id']}"], timeout=_spotty)
    if not artists or not artists.get('result', {}).get('loop_loop'):
        return jsonify({"error": "Artisten hittades inte i listan"}), 404

    artist_id = artists['result']['loop_loop'][0]['id']

    # Hämta artist-menyn och hitta "Top Tracks" (eller liknande) by name
    menu_res   = lms_json_rpc(player_mac, ["spotty", "items", 0, 10, f"item_id:{artist_id}"], timeout=_spotty)
    menu_items = menu_res.get('result', {}).get('loop_loop', []) if menu_res else []
    top_item   = next(
        (it for it in menu_items if any(k in it.get('name', '') for k in ("Top", "Populär", "Popular"))),
        menu_items[0] if menu_items else None,
    )
    if not top_item:
        return jsonify({"error": "Ingen spårmeny hittades för artisten"}), 404

    tracks_res = lms_json_rpc(player_mac, ["spotty", "items", 0, 10, f"item_id:{top_item['id']}"], timeout=_spotty)
    items = tracks_res.get('result', {}).get('loop_loop', []) if tracks_res else []
    return jsonify([_format_track(it) for it in items if it.get('isaudio')])

@app.route('/spotify_artist_radio')
def spotify_artist_radio():
    """Returnerar Artist Radio-item-ID för en artist via Spotty (Spotifys egna radioalgoritm)."""
    query = request.args.get('q', '').strip()
    player_mac, _ = get_player_info(request.args.get('room'))

    if not query:
        return jsonify({"error": "Missing q"}), 400

    _spotty = 35  # Spotty-anrop kan ta lång tid när Spotify API är trög

    search_res = lms_json_rpc(player_mac, ["spotty", "items", 0, 5, "item_id:1.0", f"search:{query}"], timeout=_spotty)
    if not search_res or 'result' not in search_res:
        return jsonify({"error": "Sökning misslyckades"}), 500

    loop = search_res['result'].get('loop_loop', [])
    artist_cat = next((it for it in loop if "Artists" in it.get('name', '')), None)
    if not artist_cat:
        return jsonify({"error": "Ingen artist-kategori hittades"}), 404

    artists = lms_json_rpc(player_mac, ["spotty", "items", 0, 1, f"item_id:{artist_cat['id']}"], timeout=_spotty)
    if not artists or not artists.get('result', {}).get('loop_loop'):
        return jsonify({"error": "Artisten hittades inte"}), 404

    artist_id = artists['result']['loop_loop'][0]['id']

    root_res   = lms_json_rpc(player_mac, ["spotty", "items", 0, 20, f"item_id:{artist_id}"], timeout=_spotty)
    root_items = root_res.get('result', {}).get('loop_loop', []) if root_res else []
    radio      = next((it for it in root_items if "Radio" in it.get('name', '')), None)
    if not radio:
        return jsonify({"error": "Ingen artist-radio hittades"}), 404

    return jsonify({"id": radio['id'], "name": radio.get('name', 'Artist Radio')})

@app.route('/spotify_genres')
def spotify_genres():
    """Hämtar Spotifys genre- och stämningskategorier (t.ex. Jazz, Träning, Fokus)."""
    player_mac, _ = get_player_info(request.args.get('room'))
    # 2.2 är standard-ID för "Genres & Moods" i Spotty-browsen
    res = lms_json_rpc(player_mac, ["spotty", "items", 0, 100, "item_id:2.2"])
    items = res.get('result', {}).get('loop_loop', []) if res else []
    return jsonify([{"name": it.get('text', it.get('name')), "id": it.get('id')} for it in items])

@app.route('/spotify_genre_playlists')
def spotify_genre_playlists():
    """Hämtar spellistor för en specifik kategori (använd ID från spotify_genres)."""
    cat_id = request.args.get('id', '').strip()
    player_mac, _ = get_player_info(request.args.get('room'))
    if not cat_id:
        return jsonify({"error": "Missing id"}), 400
    res = lms_json_rpc(player_mac, ["spotty", "items", 0, 20, f"item_id:{cat_id}"])
    items = res.get('result', {}).get('loop_loop', []) if res else []
    return jsonify([_format_entry(it, "playlist") for it in items])

@app.route('/library_genres')
def library_genres():
    """Returnerar alla genrer i det lokala biblioteket."""
    res = lms_json_rpc(None, ["genres", "0", "200"])
    genres = []
    if res and 'result' in res:
        for g in res['result'].get('genres_loop', []):
            genres.append({"id": g.get("id"), "name": g.get("genre", "")})
    return jsonify(sorted(genres, key=lambda x: x["name"]))


@app.route('/library_by_genre')
def library_by_genre():
    """Returnerar slumpade spår för ett genre-ID från det lokala biblioteket."""
    genre_id = request.args.get('genre_id', '').strip()
    try:
        limit = min(int(request.args.get('limit', '30')), 100)
    except ValueError:
        limit = 30
    if not genre_id:
        return jsonify({"error": "Missing genre_id"}), 400

    res = lms_json_rpc(None, [
        "titles", "0", "9999",
        f"genre_id:{genre_id}",
        "tags:atl",
    ])
    all_tracks = []
    if res and 'result' in res:
        for t in res['result'].get('titles_loop', []):
            all_tracks.append({
                "id":     t.get("id"),
                "title":  t.get("title", ""),
                "artist": t.get("artist", ""),
                "album":  t.get("album", ""),
            })
    tracks = random.sample(all_tracks, min(limit, len(all_tracks)))
    return jsonify({"genre_id": genre_id, "tracks": tracks})


@app.route('/recent_artists')
def recent_artists():
    """Artister från de senast spelade låtarna i LMS, unika i spelordning."""
    try:
        limit = int(request.args.get('limit', '50'))
    except ValueError:
        limit = 50
    limit = max(1, min(limit, 200))

    # Hämta fler spår än vi behöver för att få tillräckligt med unika artister
    res = lms_json_rpc(None, ["tracks", 0, limit * 4, "tags:a", "sort:lastplayed"])
    artists = []
    seen = set()
    if res and 'result' in res:
        for track in res['result'].get('titles_loop', []):
            artist = track.get('artist', '').strip()
            if artist and artist.lower() not in seen:
                seen.add(artist.lower())
                artists.append(artist)
                if len(artists) >= limit:
                    break
    return jsonify({"artists": artists})


@app.route('/lastfm_tag_artists')
def lastfm_tag_artists():
    """Toppkonstnärer för en Last.fm-tagg (genre/stämning)."""
    tag = request.args.get('tag', '').strip()
    if not tag:
        return jsonify({"error": "tag saknas"}), 400
    try:
        limit = min(int(request.args.get('limit', '10')), 30)
    except ValueError:
        limit = 10
    resp = _lastfm_get("tag.getTopArtists", tag=tag, limit=limit)
    if not resp or "topartists" not in resp:
        return jsonify({"tag": tag, "artists": []})
    artists = [
        {"name": a.get("name", ""), "rank": int(a.get("@attr", {}).get("rank", 0))}
        for a in resp["topartists"].get("artist", [])
    ]
    return jsonify({"tag": tag, "artists": artists})

@app.route('/lastfm_tag_tracks')
def lastfm_tag_tracks():
    """Topplåtar för en Last.fm-tagg (genre/stämning)."""
    tag = request.args.get('tag', '').strip()
    if not tag:
        return jsonify({"error": "tag saknas"}), 400
    try:
        limit = min(int(request.args.get('limit', '15')), 30)
    except ValueError:
        limit = 15
    resp = _lastfm_get("tag.getTopTracks", tag=tag, limit=limit)
    if not resp or "tracks" not in resp:
        return jsonify({"tag": tag, "tracks": []})
    tracks = [
        {"artist": t.get("artist", {}).get("name", ""), "title": t.get("name", "")}
        for t in resp["tracks"].get("track", [])
        if t.get("artist", {}).get("name") and t.get("name")
    ]
    return jsonify({"tag": tag, "tracks": tracks})

@app.route('/lastfm_similar')
def lastfm_similar():
    """Liknande artister för en specifik artist via Last.fm."""
    artist = request.args.get('artist', '').strip()
    if not artist:
        return jsonify({"error": "artist saknas"}), 400
    try:
        limit = min(int(request.args.get('limit', '10')), 30)
    except ValueError:
        limit = 10
    resp = _lastfm_get("artist.getSimilar", artist=artist, limit=limit)
    if not resp or "similarartists" not in resp:
        return jsonify({"artist": artist, "similar": []})
    similar = [
        {"name": a.get("name", ""), "match": round(float(a.get("match", 0)) * 100)}
        for a in resp["similarartists"].get("artist", [])
    ]
    return jsonify({"artist": artist, "similar": similar})

@app.route('/lastfm_recommendations')
def lastfm_recommendations():
    """Liknande artister baserat på användarens top-artister den senaste månaden."""
    taste = _get_lastfm_taste_profile()
    if not taste or not taste["top_artists"]:
        print(f"[LastFM] lastfm_recommendations: ingen smakprofil tillgänglig — returnerar tom data")
        return jsonify({"based_on": [], "similar": []}), 200

    try:
        limit = min(int(request.args.get('limit', '5')), 20)
    except ValueError:
        limit = 5

    seen      = {a.lower() for a in taste["top_artists"]}
    similar   = []

    for artist in taste["top_artists"][:5]:
        resp = _lastfm_get("artist.getSimilar", artist=artist, limit=10)
        if not resp or "similarartists" not in resp:
            continue
        for a in resp["similarartists"].get("artist", []):
            name = a.get("name", "")
            if name.lower() not in seen:
                seen.add(name.lower())
                similar.append({
                    "name":     name,
                    "based_on": artist,
                    "match":    round(float(a.get("match", 0)) * 100),
                })
            if len(similar) >= limit * 3:
                break

    similar.sort(key=lambda x: x["match"], reverse=True)
    return jsonify({
        "based_on":   taste["top_artists"][:5],
        "similar":    similar[:limit],
    })

@app.route('/lastfm_profile')
def lastfm_profile():
    """Returnerar användarens Last.fm-smakprofil (top-artister + senast spelade)."""
    taste = _get_lastfm_taste_profile()
    if not taste:
        return jsonify({"error": "LASTFM_API_KEY eller LASTFM_USERNAME saknas"}), 503
    return jsonify(taste)

@app.route('/spy')
def spy():
    if not DEBUG:
        return "Not available", 403
    player_mac = _any_player_mac()
    return jsonify(lms_json_rpc(player_mac, ["spotty", "items", 0, 3, "item_id:0", "tags:asj"]))


PLAYLIST_DIR = "/var/lib/squeezeboxserver/playlists"

@app.route('/save_playlist', methods=['POST'])
def save_playlist():
    """Sparar en spellista som .m3u på NAS och ber LMS skanna om."""
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    uris = data.get("uris", [])
    if not name:
        return jsonify({"error": "name saknas"}), 400
    if not uris:
        return jsonify({"error": "uris saknas"}), 400

    safe_name = re.sub(r'[^\w\s\-åäöÅÄÖ]', '', name).strip()
    path = os.path.join(PLAYLIST_DIR, f"{safe_name}.m3u")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("#EXTM3U\n")
            for uri in uris:
                f.write(uri + "\n")
        lms_json_rpc("", ["rescan", "playlists"])
        return jsonify({"ok": True, "file": path, "tracks": len(uris)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/list_playlists_local')
def list_playlists_local():
    """Listar .m3u-filer från NAS-mappen direkt (fungerar även under rescan)."""
    files = glob.glob(os.path.join(PLAYLIST_DIR, "*.m3u")) + \
            glob.glob(os.path.join(PLAYLIST_DIR, "*.m3u8"))
    names = [os.path.splitext(os.path.basename(f))[0] for f in sorted(files)]
    return jsonify(names)


@app.route('/spotify_recommendations')
def spotify_recommendations():
    """Artistrekommendationer baserat på seed-artister och/eller genrer.

    Query-parametrar:
        artist  = seed-artist (kan upprepas, max 5 används)
        genre   = seed-genre/tagg (kan upprepas, max 3 används)
        limit   = antal rekommenderade artister att returnera (default 20)

    Strategi:
        - För varje seed-artist: hämta Last.fm similar artists
        - För varje seed-genre: hämta Last.fm tag top artists
        - Kombinera, deduplicera mot seeds, returnera rankad lista
    """
    seed_artists = request.args.getlist('artist')[:5]
    seed_genres  = request.args.getlist('genre')[:3]
    try:
        limit = min(int(request.args.get('limit', '20')), 50)
    except ValueError:
        limit = 20

    if not seed_artists and not seed_genres:
        return jsonify({"error": "Minst en artist eller genre krävs"}), 400

    seen    = {a.lower() for a in seed_artists}
    results = []

    for artist in seed_artists:
        resp = _lastfm_get("artist.getSimilar", artist=artist, limit=10)
        if not resp or "similarartists" not in resp:
            continue
        for a in resp["similarartists"].get("artist", []):
            name = a.get("name", "").strip()
            if not name or name.lower() in seen:
                continue
            seen.add(name.lower())
            results.append({
                "artist": name,
                "match":  round(float(a.get("match", 0)) * 100),
                "source": "lastfm_similar",
                "seed":   artist,
            })

    for genre in seed_genres:
        resp = _lastfm_get("tag.getTopArtists", tag=genre, limit=15)
        if not resp or "topartists" not in resp:
            continue
        for a in resp["topartists"].get("artist", []):
            name = a.get("name", "").strip()
            if not name or name.lower() in seen:
                continue
            seen.add(name.lower())
            results.append({
                "artist": name,
                "match":  0,
                "source": "lastfm_tag",
                "seed":   genre,
            })

    results.sort(key=lambda x: x["match"], reverse=True)
    return jsonify({"recommendations": results[:limit]})


# ---------------------------------------------------------------------------
# Spelhistorik-endpoints (delegerar till lms_logger)
# ---------------------------------------------------------------------------

def _lms_logger():
    """Lazy-importerar lms_logger från samma katalog."""
    import importlib.util, sys
    if "lms_logger" not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            "lms_logger", Path(__file__).parent / "lms_logger.py"
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        sys.modules["lms_logger"] = mod
    return sys.modules["lms_logger"]


@app.route('/recent_artists')
def recent_artists_endpoint():
    try:
        limit = int(request.args.get('limit', 40))
        days  = int(request.args.get('days', 30))
    except ValueError:
        limit, days = 40, 30
    artists = _lms_logger().recent_artists(limit=limit, days=days)
    return jsonify(artists)


@app.route('/recent_tracks')
def recent_tracks_endpoint():
    try:
        limit = int(request.args.get('limit', 100))
        days  = int(request.args.get('days', 14))
    except ValueError:
        limit, days = 100, 14
    tracks = _lms_logger().recent_tracks(limit=limit, days=days)
    return jsonify(tracks)


@app.route('/skipped_tracks')
def skipped_tracks_endpoint():
    try:
        limit = int(request.args.get('limit', 50))
        days  = int(request.args.get('days', 14))
    except ValueError:
        limit, days = 50, 14
    tracks = _lms_logger().skipped_tracks(limit=limit, days=days)
    return jsonify(tracks)


@app.route('/listening_stats')
def listening_stats_endpoint():
    try:
        days = int(request.args.get('days', 30))
    except ValueError:
        days = 30
    return jsonify(_lms_logger().listening_stats(days=days))


@app.route('/play_history_data')
def play_history_data_endpoint():
    """Returnerar komplett plays-data för history-sidan."""
    import sqlite3 as _sqlite3, time as _time
    db_path = Path(__file__).parent / "play_history.db"
    since_14 = int(_time.time()) - 14 * 86400
    since_30 = int(_time.time()) - 30 * 86400
    with _sqlite3.connect(db_path) as conn:
        conn.row_factory = _sqlite3.Row
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
    return jsonify(plays=plays, profile=profile, top_artists=top_artists, energy_dist=energy_dist)


@app.route('/like_track')
def like_track():
    """Gillar nuvarande låt: lovar på Last.fm + flaggar loved=1 i plays-tabellen."""
    room = request.args.get("room")

    # Hämta spelande spelare om inget rum angavs
    if not room:
        for p in get_all_players():
            mac = p.get("playerid")
            if not mac:
                continue
            res = lms_json_rpc(mac, ["status", "-", "1"])
            if res and res.get("result", {}).get("mode") == "play":
                room = mac
                break
        if not room:
            return jsonify({"error": "Ingen spelare spelar just nu"}), 404

    # Hämta nuvarande låt
    try:
        res    = lms_json_rpc(room, ["status", "-", "1", "tags:al"])
        result = (res or {}).get("result", {})
        track  = (result.get("playlist_loop") or [{}])[0]
        artist = track.get("artist", "").strip()
        title  = track.get("title",  "").strip()
    except Exception as e:
        return jsonify({"error": f"Kunde inte hämta låtinfo: {e}"}), 500

    if not artist or not title:
        return jsonify({"error": "Ingen låt spelar"}), 404

    # Last.fm love
    lfm_ok = False
    if LASTFM_SESSION_KEY:
        result = _lastfm_post("track.love", artist=artist, track=title)
        lfm_ok = result is not None
        print(f"[LastFM] {'♥ loved' if lfm_ok else '✗ love misslyckades'}: {artist} – {title}")
    else:
        print(f"[LastFM] Ingen session key — hoppar över Last.fm love")

    # Flagga loved=1 i plays-tabellen
    db_path = Path(__file__).parent / "play_history.db"
    db_ok = False
    try:
        with sqlite3.connect(db_path) as conn:
            # Migrera kolumn om den saknas
            cols = [r[1] for r in conn.execute("PRAGMA table_info(plays)").fetchall()]
            if "loved" not in cols:
                conn.execute("ALTER TABLE plays ADD COLUMN loved INTEGER DEFAULT 0")
            # Markera de senaste matchande raderna (samma artist+title, ej redan gillad)
            conn.execute(
                "UPDATE plays SET loved=1 WHERE lower(artist)=lower(?) AND lower(title)=lower(?) AND loved=0",
                (artist, title),
            )
            db_ok = True
    except Exception as e:
        print(f"[Like] DB-fel: {e}")

    return jsonify({"artist": artist, "title": title, "lastfm": lfm_ok, "db": db_ok})


@app.route('/skip_track')
def skip_track():
    """Flaggar nuvarande låt som skippat i plays-tabellen och hoppar till nästa."""
    room = request.args.get("room")

    if not room:
        for p in get_all_players():
            mac = p.get("playerid")
            if not mac:
                continue
            res = lms_json_rpc(mac, ["status", "-", "1"])
            if res and res.get("result", {}).get("mode") == "play":
                room = mac
                break
        if not room:
            return jsonify({"error": "Ingen spelare spelar just nu"}), 404

    # Hämta nuvarande låt innan vi hoppar
    try:
        res    = lms_json_rpc(room, ["status", "-", "1", "tags:al"])
        result = (res or {}).get("result", {})
        track  = (result.get("playlist_loop") or [{}])[0]
        artist = track.get("artist", "").strip()
        title  = track.get("title",  "").strip()
    except Exception as e:
        return jsonify({"error": f"Kunde inte hämta låtinfo: {e}"}), 500

    # Hoppa till nästa
    lms_json_rpc(room, ["playlist", "index", "+1"])

    # Flagga skipped i plays-tabellen
    db_ok = False
    if artist and title:
        db_path = Path(__file__).parent / "play_history.db"
        try:
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    "UPDATE plays SET skipped=1 WHERE lower(artist)=lower(?) AND lower(title)=lower(?) "
                    "AND id=(SELECT id FROM plays WHERE lower(artist)=lower(?) AND lower(title)=lower(?) ORDER BY ts DESC LIMIT 1)",
                    (artist, title, artist, title),
                )
            db_ok = True
            print(f"[Skip] ✗ {artist} – {title}")
        except Exception as e:
            print(f"[Skip] DB-fel: {e}")

    return jsonify({"artist": artist, "title": title, "db": db_ok})


if __name__ == '__main__':
    from werkzeug.serving import make_server

    logging.getLogger('werkzeug').setLevel(logging.ERROR)

    cert = os.path.join(os.path.dirname(__file__), 'certs', '10.0.1.132+2.pem')
    key  = os.path.join(os.path.dirname(__file__), 'certs', '10.0.1.132+2-key.pem')

    http_server  = make_server('0.0.0.0', 5000, app, threaded=True)
    https_server = make_server('0.0.0.0', 5001, app, ssl_context=(cert, key), threaded=True)

    print("--- Lyrionbridge v2: http://0.0.0.0:5000  (Edgar/intern) ---")
    print("--- Lyrionbridge v2: https://0.0.0.0:5001 (iPhone PWA)   ---")

    threading.Thread(target=http_server.serve_forever, daemon=True).start()
    threading.Thread(target=_mix_label_loop, daemon=True, name="mix-labels").start()
    https_server.serve_forever()
