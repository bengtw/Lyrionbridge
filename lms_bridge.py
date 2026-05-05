import requests
import random
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, request, send_from_directory, jsonify

_session = requests.Session()
_session.mount('http://', requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=10))

 
CATEGORY_INDEX = {
    "artist":   0,
    "album":    1,
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

C5_MAC = "bb:bb:7a:f8:33:39"

FAVORITE_PLAYLISTS = [
    ("Background Jazz",      "spotify:playlist:37i9dQZF1DWV7EzJMK2FUI?si=e099779019b14bb7"),
    ("Chilled Classical",    "spotify:playlist:37i9dQZF1DWUvHZA1zLcjW?si=ef77a6c2ebf14473"),
    ("Soft Lounge",          "spotify:playlist:37i9dQZF1DX82pCGH5USnM?si=7752baaaeff94464"),
    ("Soul Mix",             "spotify:playlist:37i9dQZF1EQntZpEGgfBif?si=357c1eec328d4db2"),
    ("Dinner with Friends",  "spotify:playlist:37i9dQZF1DX4xuWVBs4FgJ?si=7c1574dfb25d4117"),
    ("Coffee Table Jazz",    "spotify:playlist:37i9dQZF1DWVqfgj8NZEp1?si=f90718546eb4492f")
]

PLAYLIST_CACHE = []
_PLAYLIST_CACHE_TIME = 0
_PLAYLIST_CACHE_TTL = 3600

_player_cache = []
_player_cache_time = 0
_PLAYER_CACHE_TTL = 30

_active_players_cache = None
_active_players_cache_time = 0
_ACTIVE_PLAYERS_CACHE_TTL = 5


# --- HJÄLPFUNKTIONER ---

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
        r = requests.post(url, data=body, headers=headers, timeout=2)
        print(f"[UPNP] C5 svar: {r.status_code} {r.text[:200]}")
    except Exception as e:
        print(f"[UPNP ERROR] C5: {e}")

def lms_json_rpc(player_id, command_args):
    payload = {"id": 1, "method": "slim.request", "params": [player_id, command_args]}
    try:
        return _session.post(LMS_URL, json=payload, timeout=3).json()
    except Exception as e:
        print(f"[ERROR] LMS: {e}")
        return None

def lms_play_stream(player_mac, play_command):
    """stop → clear → play → shuffle on (för URLs och daily mixes)"""
    lms_json_rpc(player_mac, ["stop"])
    lms_json_rpc(player_mac, ["playlist", "clear"])
    result = lms_json_rpc(player_mac, play_command)
    lms_json_rpc(player_mac, ["playlist", "shuffle", 1])
    return result

def lms_load_album(player_mac, album_id):
    """Shuffle off → clear → load album"""
    lms_json_rpc(player_mac, ["playlist", "shuffle", 0])
    lms_json_rpc(player_mac, ["playlist", "clear"])
    return lms_json_rpc(player_mac, ["playlistcontrol", "cmd:load", f"album_id:{album_id}"])


def get_all_players():
    """Hämtar alla aktuella spelare dynamiskt från LMS, med kort TTL-cache."""
    global _player_cache, _player_cache_time
    now = time.time()
    if _player_cache and (now - _player_cache_time) < _PLAYER_CACHE_TTL:
        return _player_cache
    res = lms_json_rpc("", ["players", "0", "10"])
    if res and 'result' in res:
        players = res['result'].get('players_loop', [])
        if players:
            _player_cache = players
            _player_cache_time = now
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
        
    # Om inget rum anges, returnera första tillgängliga spelaren som fallback
    if not room_arg:
        return players[0].get('playerid'), players[0].get('name')

    decoded = urllib.parse.unquote(room_arg).strip().lower()

    # Fall 1: Vi fick in en direkt MAC-adress
    if ":" in decoded:
        for p in players:
            if p.get('playerid', '').lower() == decoded:
                return decoded, p.get('name')
        return decoded, "unknown"

    # Fall 2: Vi fick in ett rumsnamn från Edgar (t.ex. "köket")
    # Vi kollar om det namnet matchar något av namnen i LMS (t.ex. "Kök" eller "C5")
    for p in players:
        name = p.get('name', '').lower()
        if decoded in name or name in decoded:
            return p.get('playerid'), p.get('name')

    return None, decoded


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
    global PLAYLIST_CACHE, _PLAYLIST_CACHE_TIME
    if PLAYLIST_CACHE and (time.time() - _PLAYLIST_CACHE_TIME) < _PLAYLIST_CACHE_TTL:
        return jsonify(PLAYLIST_CACHE)
    result = []
    for name, uri in FAVORITE_PLAYLISTS:
        art = "https://via.placeholder.com/300x300/111/444?text=List"
        try:
            r = requests.get(f"https://open.spotify.com/oembed?url={uri.split('?')[0]}", timeout=2)
            if r.status_code == 200:
                art = r.json().get('thumbnail_url', art)
        except Exception as e:
            print(f"[VARNING] Spotify-bild för {name}: {e}")
        result.append({"name": name, "url": uri, "art": art})
    PLAYLIST_CACHE = result
    _PLAYLIST_CACHE_TIME = time.time()
    return jsonify(PLAYLIST_CACHE)

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
        
    # --- SPELLISTOR: Shuffle PÅ ---
    elif "spotify:playlist:" in clean_url:
        # Vi rensar först utan shuffle för att inte stressa Spotty
        lms_json_rpc(player_mac, ["playlist", "shuffle", 0])
        lms_json_rpc(player_mac, ["stop"])
        lms_json_rpc(player_mac, ["playlist", "clear"])
        # Spela listan
        res = lms_json_rpc(player_mac, ["playlist", "play", clean_url])
        # Vänta på att Spotty hinner ladda kön innan vi manipulerar den
        time.sleep(2.0)
        # NU slår vi på shuffle så att nästa låt blir en överraskning
        lms_json_rpc(player_mac, ["playlist", "shuffle", 1])
        # Hoppa till en slumpmässig låt och säkerställ att uppspelning startar
        lms_json_rpc(player_mac, ["playlist", "index", "+1"])
        lms_json_rpc(player_mac, ["play"])

    # --- SÖKRESULTAT / ENKLA LÅTAR ---
    elif clean_url.startswith("1.0_") or "spotify:track:" in clean_url:
        lms_json_rpc(player_mac, ["playlist", "shuffle", 0])
        lms_json_rpc(player_mac, ["stop"])
        lms_json_rpc(player_mac, ["playlist", "clear"])
        
        if clean_url.startswith("1.0_"):
            res = lms_json_rpc(player_mac, ["spotty", "playlist", "play", f"item_id:{clean_url}"])
        else:
            res = lms_json_rpc(player_mac, ["playlist", "play", clean_url, "1"])

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
    except:
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
    except:
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
    except:
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
    except:
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
    player_mac = _any_player_mac()
    res = lms_json_rpc(player_mac, ["spotty", "items", 0, 80, "item_id:0", "menu:1", "tags:s"])
    mixes = []
    if res and 'result' in res:
        for item in res['result'].get('item_loop', []):
            parts = item.get('text', '').split('\n')
            title = parts[0]
            if not any(x in title for x in ["Mix", "Radar", "Discovery", "daylist"]):
                continue
            art = item.get('icon') or item.get('image')
            if art and art.startswith('/'):
                art = f"http://{LMS_HOST}:9000{art}"
            raw_id = item.get('params', {}).get('item_id') or item.get('id', '0.0')
            mixes.append({
                'id':          raw_id.split('.')[-1],
                'title':       title,
                'description': parts[1] if len(parts) > 1 else "Din personliga mix",
                'art':         art
            })
    return jsonify(mixes)

@app.route('/get_daily_mixes_knob')
def get_daily_mixes_knob():
    """Textlista för knappen: title|desc|index, en per rad, bara Daily Mix 1-6."""
    player_mac = _any_player_mac()
    res = lms_json_rpc(player_mac, ["spotty", "items", 0, 80, "item_id:0", "menu:1", "tags:s"])
    lines = []
    if res and 'result' in res:
        for item in res['result'].get('item_loop', []):
            parts = item.get('text', '').split('\n')
            title = parts[0]
            if not (title.startswith('Daily Mix ') and len(title) == 11 and '1' <= title[10] <= '6'):
                continue
            desc = parts[1] if len(parts) > 1 else ''
            idx = int(title[10]) - 1
            lines.append(f"{title}|{desc}|{idx}")
    return '\n'.join(lines), 200, {'Content-Type': 'text/plain; charset=utf-8'}

@app.route('/get_radio_favorites')
def get_radio_favorites():
    res = lms_json_rpc("", ["favorites", "items", "0", "50"])
    stations = []
    
    if res and 'result' in res:
        items = res['result'].get('loop_loop', [])
        
        for item in items:
            fav_id = item.get('id')
            if not fav_id: continue
            
            art = item.get('image') or item.get('icon')
            if art and art.startswith('/'):
                art = f"http://{LMS_HOST}:9000{art}"
            elif not art:
                art = "https://via.placeholder.com/300x300/111/444?text=Radio"

            stations.append({
                'id': fav_id,
                'name': item.get('name', 'Okänd kanal'),
                'url': fav_id,
                'art': art
            })
        
        # Sortera listan alfabetiskt baserat på namnet
        stations = sorted(stations, key=lambda x: x['name'].lower())
            
    return jsonify(stations)

@app.route('/play_radio')
def play_radio():
    fav_id = request.args.get('url') # Detta är vårt ID, t.ex. "552e83ef.0"
    player_mac, room_name = get_player_info(request.args.get('room'))
    
    if not (player_mac and fav_id):
        return "Missing ID or Room", 400
        
    print(f"[RADIO] Försöker spela favorit-ID {fav_id} i {room_name}")
    
    # 1. Stoppa nuvarande uppspelning och rensa kön
    lms_json_rpc(player_mac, ["stop"])
    lms_json_rpc(player_mac, ["playlist", "clear"])
    
    # 2. Spela favoriten. Formatet ["favorites", "playlist", "play", "item_id:X"] 
    # är det som LMS förväntar sig för att trigga en favorit.
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
    results = {}
    for port in [49152, 1400, 8080, 80]:
        try:
            r = requests.get(f"http://{C5_IP}:{port}/description.xml", timeout=2)
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
        item_id:1                         = Search-menyn
        item_id:1.0 + search:<q>          = Ny sökning (träfflista)
          - items 0-5: kategorier (Artists, Albums, Playlists, ...)
          - items 6+:  direkta spår
        item_id:1.0_<q>.N                 = gå in i kategori N
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

    room_arg = request.args.get('room')
    if room_arg:
        player_mac, _ = get_player_info(room_arg)
        if not player_mac:
            player_mac = _any_player_mac()
    else:
        player_mac = _any_player_mac()

    # Första anropet: initiera sökningen
    initial = lms_json_rpc(player_mac, [
        "spotty", "items", 0, 50,
        "item_id:1.0",
        f"search:{query}",
    ])
 
    if not initial or 'result' not in initial:
        return jsonify({"query": query, "type": search_type, "items": []})
 
    loop = initial['result'].get('loop_loop', [])
 
    if search_type == "track":
        # Spårträffar ligger direkt i listan från index 6 och framåt
        items = [it for it in loop if it.get('isaudio') == 1]
        formatted = [_format_track(it) for it in items[:limit]]
    else:
        # För andra typer: gå in i rätt kategori-undermeny
        cat_idx = CATEGORY_INDEX.get(search_type)
        if cat_idx is None:
            return jsonify({"error": f"Unknown type: {search_type}"}), 400
 
        # Navigera in i kategorin via dess item_id
        encoded = urllib.parse.quote(query)
        category_item_id = f"1.0_{encoded}.{cat_idx}"
 
        sub = lms_json_rpc(player_mac, [
            "spotty", "items", 0, limit,
            f"item_id:{category_item_id}",
        ])
 
        if not sub or 'result' not in sub:
            return jsonify({"query": query, "type": search_type, "items": []})
 
        sub_loop = sub['result'].get('loop_loop', [])
        formatted = [_format_entry(it, search_type) for it in sub_loop[:limit]]
 
    return jsonify({
        "query": query,
        "type": search_type,
        "items": [f for f in formatted if f],
    })
 
 
def _format_track(item):
    name = item.get('name', '')
    image = item.get('image', '')
    if image.startswith('/'):
        image = f"http://{LMS_HOST}:9000{image}"

    # Vi tar det interna ID:t som Spotty ger oss (t.ex. 1.0_Enjoy...)
    uri = item.get('id', '')

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
        "art": image,
    }

def _format_entry(item, search_type):
    """Formattera album/artist/playlist-träff från kategori-undermenyn."""
    name = item.get('name', '')
    image = item.get('image', '')
    if image.startswith('/'):
        image = f"http://{LMS_HOST}:9000{image}"
 
    # Undermeny-items har ofta riktiga Spotify-URIs i 'url' eller 'play'
    uri = item.get('url') or item.get('play') or item.get('id', '')
 
    # För albums är namnet ofta 'Album by Artist'
    title, subtitle = name, ""
    if search_type == "album" and ' by ' in name:
        title, subtitle = name.split(' by ', 1)
 
    return {
        "name": title.strip(),
        "subtitle": subtitle.strip(),
        "uri": uri,
        "art": image,
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

@app.route('/active_players')
def active_players():
    """Returnerar alla spelare som för närvarande spelar musik."""
    global _active_players_cache, _active_players_cache_time
    now = time.time()
    if _active_players_cache is not None and (now - _active_players_cache_time) < _ACTIVE_PLAYERS_CACHE_TTL:
        return jsonify(_active_players_cache)
    players = get_all_players()
    with ThreadPoolExecutor(max_workers=len(players) or 1) as ex:
        results = list(ex.map(_query_player_status, players))
    playing = [r for r in results if r is not None]
    _active_players_cache = playing
    _active_players_cache_time = now
    return jsonify(playing)


@app.route('/stop_active')
def stop_active():
    """Stoppar alla spelare som för närvarande spelar."""
    stopped = []
    for p in get_all_players():
        room = p.get('name')
        mac = p.get('playerid')
        if not mac:
            continue
            
        res = lms_json_rpc(mac, ["status", "-", "1"])
        if res and res.get('result', {}).get('mode') == 'play':
            lms_json_rpc(mac, ["pause", 1])
            stopped.append(room)
    return jsonify({'stopped': stopped})


@app.route('/next_active')
def next_active():
    """Hoppar till nästa låt på den spelare som spelar."""
    acted = []
    for p in get_all_players():
        room = p.get('name')
        mac = p.get('playerid')
        if not mac:
            continue
            
        res = lms_json_rpc(mac, ["status", "-", "1"])
        if res and res.get('result', {}).get('mode') == 'play':
            lms_json_rpc(mac, ["playlist", "index", "+1"])
            acted.append(room)
    return jsonify({'next': acted})

@app.route('/spotify_artist_top')
def spotify_artist_top():
    """Hämtar populäraste låtarna för en artist via Spotty."""
    query = request.args.get('q', '').strip()
    player_mac, _ = get_player_info(request.args.get('room'))
    if not query: return jsonify({"error": "Missing q"}), 400
    
    # 1. Utför en sökning för att hitta artisten (samma logik som spotify_search)
    search_res = lms_json_rpc(player_mac, ["spotty", "items", 0, 5, "item_id:1.0", f"search:{query}"])
    if not search_res or 'result' not in search_res:
        return jsonify({"error": "Sökning misslyckades"}), 500

    # Hitta "Artists"-kategorin i sökresultatet
    loop = search_res['result'].get('loop_loop', [])
    artist_cat = next((it for it in loop if "Artists" in it.get('text', '')), None)
    if not artist_cat:
        return jsonify({"error": "Ingen artist-kategori hittades"}), 404

    # 2. Gå in i artist-kategorin och ta första artisten
    artists = lms_json_rpc(player_mac, ["spotty", "items", 0, 1, f"item_id:{artist_cat['id']}"])
    if not artists or not artists.get('result', {}).get('loop_loop'):
        return jsonify({"error": "Artisten hittades inte i listan"}), 404

    artist_id = artists['result']['loop_loop'][0]['id']

    # Topplåtar ligger oftast under index .0 i artistens meny
    tracks_res = lms_json_rpc(player_mac, ["spotty", "items", 0, 10, f"item_id:{artist_id}.0"])
    items = tracks_res.get('result', {}).get('loop_loop', []) if tracks_res else []
    return jsonify([_format_track(it) for it in items if it.get('isaudio')])

@app.route('/spotify_genres')
def spotify_genres():
    """Hämtar Spotifys genre- och stämningskategorier (t.ex. Jazz, Träning, Fokus)."""
    player_mac, _ = get_player_info(request.args.get('room'))
    # 2.2 är standard-ID för "Genres & Moods" i Spotty-browsen
    res = lms_json_rpc(player_mac, ["spotty", "items", 0, 100, "item_id:2.2"])
    items = res.get('result', {}).get('loop_loop', []) if res else []
    # Returnerar en lista med namn och ID som Edgar kan använda för att bläddra vidare
    return jsonify([{"name": it.get('text', it.get('name')), "id": it.get('id')} for it in items])

@app.route('/spotify_genre_playlists')
def spotify_genre_playlists():
    """Hämtar spellistor för en specifik kategori (använd ID från spotify_genres)."""
    cat_id = request.args.get('id', '').strip()
    player_mac, _ = get_player_info(request.args.get('room'))
    if not cat_id: return jsonify({"error": "Missing id"}), 400
    
    # Hämtar spellistorna som finns inuti kategorin
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


@app.route('/edgar_chat', methods=['POST'])
def edgar_chat():
    data = request.json or {}
    message = data.get('message', '').strip()
    if not message:
        return jsonify({"error": "Inget meddelande"}), 400
    _, room_name = get_player_info(data.get('room', ''))
    try:
        resp = requests.post(f"{EDGAR_URL}/chat", json={
            "message": message,
            "client_id": "multilyrion",
            "default_room": room_name,
        }, timeout=180)
        return jsonify(resp.json())
    except Exception as e:
        return jsonify({"error": str(e)}), 502

@app.route('/spy')
def spy():
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

    import os, re
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
    import os, glob
    files = glob.glob(os.path.join(PLAYLIST_DIR, "*.m3u")) + \
            glob.glob(os.path.join(PLAYLIST_DIR, "*.m3u8"))
    names = [os.path.splitext(os.path.basename(f))[0] for f in sorted(files)]
    return jsonify(names)


if __name__ == '__main__':
    import logging, os, threading
    from werkzeug.serving import make_server

    logging.getLogger('werkzeug').setLevel(logging.ERROR)

    cert = os.path.join(os.path.dirname(__file__), 'certs', '10.0.1.132+2.pem')
    key  = os.path.join(os.path.dirname(__file__), 'certs', '10.0.1.132+2-key.pem')

    http_server  = make_server('0.0.0.0', 5000, app, threaded=True)
    https_server = make_server('0.0.0.0', 5001, app, ssl_context=(cert, key), threaded=True)

    print("--- Lyrionbridge v2: http://0.0.0.0:5000  (Edgar/intern) ---")
    print("--- Lyrionbridge v2: https://0.0.0.0:5001 (iPhone PWA)   ---")

    threading.Thread(target=http_server.serve_forever, daemon=True).start()
    https_server.serve_forever()
