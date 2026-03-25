import requests
import random
import time
import urllib.parse
from flask import Flask, request, send_from_directory, jsonify

app = Flask(__name__)

# --- KONFIGURATION ---
LMS_URL = "http://127.0.0.1:9000/jsonrpc.js"
LMS_HOST = "10.0.1.132"
C5_IP = "10.0.1.125"

PLAYERS = {
    "office": "b8:27:eb:fb:30:d9",
    "linn":   "bb:bb:4d:b0:d0:06",
    "c5":     "bb:bb:7a:f8:33:39"
}

FAVORITE_PLAYLISTS = [
    ("Background Jazz",      "spotify:playlist:37i9dQZF1DWV7EzJMK2FUI?si=e099779019b14bb7"),
    ("Chilled Classical",    "spotify:playlist:37i9dQZF1DWUvHZA1zLcjW?si=ef77a6c2ebf14473"),
    ("Soft Lounge",          "spotify:playlist:37i9dQZF1DX82pCGH5USnM?si=7752baaaeff94464"),
    ("Soul Mix",             "spotify:playlist:37i9dQZF1EQntZpEGgfBif?si=357c1eec328d4db2"),
    ("Dinner with Friends",  "spotify:playlist:37i9dQZF1DX4xuWVBs4FgJ?si=7c1574dfb25d4117"),
    ("Coffee Table Jazz",    "spotify:playlist:37i9dQZF1DWVqfgj8NZEp1?si=f90718546eb4492f")
]

PLAYLIST_CACHE = []


# --- HJÄLPFUNKTIONER ---

def lms_json_rpc(player_id, command_args):
    payload = {"id": 1, "method": "slim.request", "params": [player_id, command_args]}
    try:
        return requests.post(LMS_URL, json=payload, timeout=3).json()
    except Exception as e:
        print(f"[ERROR] LMS: {e}")
        return None

def lms_play_stream(player_mac, play_command):
    """Shuffle on → stop → clear → play (för URLs och daily mixes)"""
    lms_json_rpc(player_mac, ["playlist", "shuffle", 1])
    lms_json_rpc(player_mac, ["stop"])
    lms_json_rpc(player_mac, ["playlist", "clear"])
    return lms_json_rpc(player_mac, play_command)

def lms_load_album(player_mac, album_id):
    """Shuffle off → clear → load album"""
    lms_json_rpc(player_mac, ["playlist", "shuffle", 0])
    lms_json_rpc(player_mac, ["playlist", "clear"])
    return lms_json_rpc(player_mac, ["playlistcontrol", "cmd:load", f"album_id:{album_id}"])

def set_c5_volume_upnp(volume_level):
    url = f"http://{C5_IP}:49152/upnp/control/render_control1"
    headers = {
        'Content-Type': 'text/xml; charset="utf-8"',
        'SOAPACTION': '"urn:schemas-upnp-org:service:RenderingControl:1#SetVolume"'
    }
    body = f"""<?xml version="1.0" encoding="utf-8"?>
    <s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
        <s:Body>
            <u:SetVolume xmlns:u="urn:schemas-upnp-org:service:RenderingControl:1">
                <InstanceID>0</InstanceID><Channel>Master</Channel>
                <DesiredVolume>{volume_level}</DesiredVolume>
            </u:SetVolume>
        </s:Body>
    </s:Envelope>"""
    try:
        requests.post(url, data=body, headers=headers, timeout=2)
    except Exception as e:
        print(f"[UPNP ERROR] C5: {e}")

def get_player_info(room_arg):
    if not room_arg:
        return PLAYERS.get("office"), "office"
    decoded = urllib.parse.unquote(room_arg).strip().lower()
    if ":" in decoded:
        for name, mac in PLAYERS.items():
            if mac.lower() == decoded:
                return decoded, name
        return decoded, "unknown"
    return PLAYERS.get(decoded), decoded


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
    global PLAYLIST_CACHE
    if PLAYLIST_CACHE:
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
    return jsonify(PLAYLIST_CACHE)

@app.route('/play_url')
def play_url():
    url = request.args.get('url')
    player_mac, room_name = get_player_info(request.args.get('room'))
    if not (player_mac and url):
        return "Missing URL or Room", 400
    clean_url = url.split('?')[0].strip()
    print(f"[ACTION] Spelar: {clean_url} i {room_name}")
    res = lms_play_stream(player_mac, ["playlist", "play", clean_url])
    return jsonify({"status": "ok", "sent_url": clean_url, "lms_response": res})

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
    if room_name == "c5":
        set_c5_volume_upnp(level)
    lms_json_rpc(player_mac, ["mixer", "volume", int(level)])
    return str(level)

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
        track = res['result']['playlist_loop'][0]
        title = track.get('title') or track.get('name', 'Ingen titel')
        artist = track.get('artist', '')
        return f"{title} - {artist}" if artist else title
    except:
        return ""

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
    player_mac = list(PLAYERS.values())[0] if PLAYERS else ""
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

@app.route('/play_album')
def play_specific_album():
    album_id = request.args.get('album_id')
    player_mac, _ = get_player_info(request.args.get('room'))
    if not (player_mac and album_id):
        return "Error", 400
    lms_load_album(player_mac, album_id)
    return "OK"

@app.route('/spy')
def spy():
    player_mac = list(PLAYERS.values())[0] if PLAYERS else ""
    return jsonify(lms_json_rpc(player_mac, ["spotty", "items", 0, 3, "item_id:0", "tags:asj"]))


if __name__ == '__main__':
    print("--- Lyrionbridge v2: Startad (port 5001) ---")
    app.run(host='0.0.0.0', port=5000, debug=True)
