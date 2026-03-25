import requests
import random
import time
import urllib.parse
import os
from flask import Flask, request, send_from_directory, jsonify

app = Flask(__name__)

# --- KONFIGURATION ---
LMS_URL = "http://127.0.0.1:9000/jsonrpc.js"
C5_IP = "10.0.1.125" 

# Dina definierade rum
PLAYERS = {
    "office": "b8:27:eb:fb:30:d9", 
    "linn": "bb:bb:4d:b0:d0:06",      
    "c5": "bb:bb:7a:f8:33:39"
}

# --- DATA: SPELLISTOR ---
# En enda "sanning" för alla dina spellistor (DRY - Don't Repeat Yourself)
FAVORITE_PLAYLISTS = [
    ("Background Jazz", "spotify:playlist:37i9dQZF1DWV7EzJMK2FUI?si=e099779019b14bb7"),
    ("Chilled Classical", "spotify:playlist:37i9dQZF1DWUvHZA1zLcjW?si=ef77a6c2ebf14473"),
    ("Soft Lounge", "spotify:playlist:37i9dQZF1DX82pCGH5USnM?si=7752baaaeff94464"),
    ("Soul Mix", "spotify:playlist:37i9dQZF1EQntZpEGgfBif?si=357c1eec328d4db2"),
    ("Dinner with Friends", "spotify:playlist:37i9dQZF1DX4xuWVBs4FgJ?si=7c1574dfb25d4117"),
    ("Coffee Table Jazz", "spotify:playlist:37i9dQZF1DWVqfgj8NZEp1?si=f90718546eb4492f")
]

# Cache för spelliste-bilder så vi inte frågar Spotify varje gång
PLAYLIST_CACHE = []


# --- HJÄLPFUNKTIONER ---

def lms_json_rpc(player_id, command_args):
    """Kommunicerar med Logitech Media Server via JSON-RPC"""
    payload = {
        "id": 1, 
        "method": "slim.request", 
        "params": [player_id, command_args]
    }
    try:
        response = requests.post(LMS_URL, json=payload, timeout=3)
        return response.json()
    except Exception as e:
        print(f"[ERROR] LMS: {e}")
        return None

def set_c5_volume_upnp(volume_level):
    """Speciell volymstyrning för C5 för att trigga Spotify Connect via UPnP"""
    url = f"http://{C5_IP}:49152/upnp/control/render_control1"
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
        requests.post(url, data=body, headers=headers, timeout=2)
    except Exception as e:
        print(f"[UPNP ERROR] C5: {e}")

def get_player_info(room_arg):
    """Tolkar inkommande rumsnamn eller MAC-adress (hanterar URL-encoding)"""
    if not room_arg: 
        return PLAYERS.get("office"), "office"
    
    decoded = urllib.parse.unquote(room_arg).strip().lower()
    
    # Om det är en MAC-adress (innehåller kolon)
    if ":" in decoded:
        for name, mac in PLAYERS.items():
            if mac.lower() == decoded:
                return decoded, name
        return decoded, "unknown"
    
    # Om det är ett rumsnamn
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
    """Hämtar tillgängliga spelare från LMS för dropdown-menyn"""
    res = lms_json_rpc("", ["players", "0", "10"])
    if res and 'result' in res:
        return jsonify(res['result'])
    return jsonify({"players_loop": []})

@app.route('/get_playlists')
def get_playlists():
    """Returnerar dina kurerade favoritspellistor i textformat (för t.ex. ESP32)"""
    # Hämtar direkt från den globala variabeln FAVORITE_PLAYLISTS
    response = "\n".join([f"{name}|{url}" for name, url in FAVORITE_PLAYLISTS])
    return response, 200, {'Content-Type': 'text/plain; charset=utf-8'}

@app.route('/get_playlists_with_art')
def get_playlists_with_art():
    """Hämtar spellistor och hämtar dynamiskt omslagsbilder från Spotify (JSON för Webb)"""
    global PLAYLIST_CACHE
    
    # Om vi redan har hämtat bilderna från Spotify, returnera cachen direkt
    if PLAYLIST_CACHE:
        return jsonify(PLAYLIST_CACHE)
        
    fetched_playlists = []
    
    # Använder samma globala FAVORITE_PLAYLISTS som den vanliga /get_playlists gör
    for name, uri in FAVORITE_PLAYLISTS:
        art_url = "https://via.placeholder.com/300x300/111/444?text=List"
        
        # Tvätta URI:n från ?si= för att API:et ska bli gladare
        clean_uri = uri.split('?')[0].strip()
        
        try:
            oembed_url = f"https://open.spotify.com/oembed?url={clean_uri}"
            r = requests.get(oembed_url, timeout=2)
            
            if r.status_code == 200:
                data = r.json()
                if 'thumbnail_url' in data:
                    art_url = data['thumbnail_url']
        except Exception as e:
            print(f"[VARNING] Kunde inte hämta bild från Spotify för {name}: {e}")
            
        fetched_playlists.append({
            "name": name,
            "url": uri,
            "art": art_url
        })
        
    PLAYLIST_CACHE = fetched_playlists
    return jsonify(PLAYLIST_CACHE)

@app.route('/play_url')
def play_url():
    url = request.args.get('url')
    player_mac, room_name = get_player_info(request.args.get('room'))
    
    if player_mac and url:
        clean_url = url.split('?')[0].strip()
        print(f"[ACTION] Spelar tvättad URL: {clean_url} i {room_name}")
        
        lms_json_rpc(player_mac, ["playlist", "shuffle", 1])
        lms_json_rpc(player_mac, ["stop"])
        lms_json_rpc(player_mac, ["playlist", "clear"])
        
        res = lms_json_rpc(player_mac, ["playlist", "play", clean_url])
        return jsonify({"status": "ok", "sent_url": clean_url, "lms_response": res})
    
    return "Missing URL or Room", 400

@app.route('/daily')
def play_daily():
    player_mac, _ = get_player_info(request.args.get('room'))
    idx = request.args.get('index', '0')
    if player_mac:
        lms_json_rpc(player_mac, ["playlist", "shuffle", 1])
        lms_json_rpc(player_mac, ["stop"])
        lms_json_rpc(player_mac, ["playlist", "clear"])
        lms_json_rpc(player_mac, ["spotty", "playlist", "play", f"item_id:playlists.{idx}"])
        return f"Playing Daily Mix {idx}"
    return "Error", 404

@app.route('/play')
def play_random_album():
    player_mac, _ = get_player_info(request.args.get('room'))
    if not player_mac: return "Error", 404
    
    res = lms_json_rpc("", ["albums", "0", "500", "tags:l"])
    try:
        target = random.choice(res['result']['albums_loop'])
        lms_json_rpc(player_mac, ["playlist", "shuffle", 0])
        lms_json_rpc(player_mac, ["playlist", "clear"])
        lms_json_rpc(player_mac, ["playlistcontrol", "cmd:load", f"album_id:{target['id']}"])
        return f"Playing: {target['album']}"
    except: return "LMS Error", 500

@app.route('/volume')
def set_volume():
    room_arg = request.args.get('room')
    level = request.args.get('level', '30')
    player_mac, room_name = get_player_info(room_arg)
    
    if player_mac:
        if room_name == "c5":
            set_c5_volume_upnp(level)
        
        lms_json_rpc(player_mac, ["mixer", "volume", int(level)])
        return str(level)
    return "Error", 404

@app.route('/status')
def get_status():
    player_mac, _ = get_player_info(request.args.get('room'))
    if not player_mac: return "0"
    res = lms_json_rpc(player_mac, ["mixer", "volume", "?"])
    try: return str(res['result']['_volume'])
    except: return "30"

@app.route('/title')
def get_title():
    player_mac, _ = get_player_info(request.args.get('room'))
    if not player_mac: return "Välj rum"
    res = lms_json_rpc(player_mac, ["status", "-", "1", "tags:atl"])
    try:
        track = res['result']['playlist_loop'][0]
        title = track.get('title') or track.get('name', 'Ingen titel')
        artist = track.get('artist', '')
        return f"{title} - {artist}" if artist else title
    except: return ""

@app.route('/pause')
def pause_music():
    room_id = request.args.get('room')
    player_mac, room_name = get_player_info(room_id)
    
    if player_mac:
        print(f"[PAUSE] Skickar kommando till: {room_name} ({player_mac})", flush=True)
        lms_json_rpc(player_mac, ["pause"])
        return "OK"
    
    return "Error", 404

@app.route('/next')
def next_track():
    player_mac, _ = get_player_info(request.args.get('room'))
    if player_mac:
        lms_json_rpc(player_mac, ["playlist", "index", "+1"])
        return "OK"
    return "Error", 404

@app.route('/art')
def get_album_art():
    player_mac, _ = get_player_info(request.args.get('room'))
    if not player_mac:
        return "/static/icon.png" 
        
    lms_host = "10.0.1.132" 
    art_url = f"http://{lms_host}:9000/music/current/cover.jpg?player={player_mac}&time={int(time.time())}"
    
    return art_url

@app.route('/status_raw')
def get_status_raw():
    room_arg = request.args.get('room')
    player_mac, _ = get_player_info(room_arg)
    
    if not player_mac:
        return "pause"
        
    res = lms_json_rpc(player_mac, ["status", "-", "1"])
    
    try:
        return res['result']['mode']
    except:
        return "pause"

@app.route('/get_random_albums')
def get_random_albums():
    response = lms_json_rpc(None, ["albums", 0, 10, "sort:random", "tags:albj"])
    
    albums = []
    
    if response and 'result' in response and 'albums_loop' in response['result']:
        items = response['result']['albums_loop']
        
        for item in items:
            cover_id = item.get('artwork_track_id') or item.get('id')
            
            albums.append({
                'id': item.get('id'),
                'title': item.get('album'),
                'artist': item.get('artist'),
                'art': f"http://10.0.1.132:9000/music/{cover_id}/cover.jpg"
            })
            
    return jsonify(albums)

@app.route('/get_daily_mixes')
def get_daily_mixes():
    """Hämtar mixar och delar upp text-strängen för att visa artister"""
    LMS_BASE = "http://10.0.1.132:9000"
    player_mac = list(PLAYERS.values())[0] if PLAYERS else ""
    
    # Vi använder exakt de parametrar som fungerade i din curl
    res = lms_json_rpc(player_mac, ["spotty", "items", 0, 80, "item_id:0", "menu:1", "tags:s"])
    
    mixes = []
    if res and 'result' in res:
        # Din version använder 'item_loop'
        items = res['result'].get('item_loop', [])
        
        for item in items:
            full_text = item.get('text', '')
            
            # Dela upp strängen vid nyrad-tecknet (\n)
            parts = full_text.split('\n')
            title = parts[0] # "Daily Mix 1"
            
            # Om det finns en del två, så är det artisterna
            if len(parts) > 1:
                description = parts[1] # "Wonder Eve, Manor Blue, David Parks & Silver and more"
            else:
                description = "Din personliga mix"

            # Endast om det är en Mix eller Radar etc.
            if any(x in title for x in ["Mix", "Radar", "Discovery", "daylist"]):
                raw_id = item.get('params', {}).get('item_id') or item.get('id', '0.0')
                
                art_url = item.get('icon') or item.get('image')
                if art_url and art_url.startswith('/'):
                    art_url = LMS_BASE + art_url

                mixes.append({
                    'id': raw_id.split('.')[-1],
                    'title': title,
                    'description': description,
                    'art': art_url
                })

    return jsonify(mixes)

@app.route('/play_album')
def play_specific_album():
    album_id = request.args.get('album_id')
    room_id = request.args.get('room')
    player_mac, _ = get_player_info(room_id)
    
    if player_mac and album_id:
        lms_json_rpc(player_mac, ["playlist", "shuffle", 0])
        lms_json_rpc(player_mac, ["playlist", "clear"])
        lms_json_rpc(player_mac, ["playlistcontrol", "cmd:load", f"album_id:{album_id}"])
        return "OK"
    return "Error", 400


@app.route('/spy')
def spy():
    """Hämtar rådata från Lyrion via bridgens etablerade anslutning"""
    player_mac = list(PLAYERS.values())[0] if PLAYERS else ""
    # Vi ber om de 3 första objekten i Home-menyn med ALLA tänkbara taggar
    res = lms_json_rpc(player_mac, ["spotty", "items", 0, 3, "item_id:0", "tags:asj"])
    return jsonify(res)

if __name__ == '__main__':
    print("--- Dörrvakten Bridge: Startad ---")
    app.run(host='0.0.0.0', port=5000, debug=True)