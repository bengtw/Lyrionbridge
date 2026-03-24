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
    """Returnerar dina kurerade favoritspellistor"""
    playlists = [
        ("Background Jazz", "spotify:playlist:37i9dQZF1DWV7EzJMK2FUI?si=e099779019b14bb7"),
        ("Chilled Classical", "spotify:playlist:37i9dQZF1DWUvHZA1zLcjW?si=ef77a6c2ebf14473"),
        ("Soft Lounge", "spotify:playlist:37i9dQZF1DX82pCGH5USnM?si=7752baaaeff94464"),
        ("Soul Mix", "spotify:playlist:37i9dQZF1EQntZpEGgfBif?si=357c1eec328d4db2"),
        ("Dinner with Friends", "spotify:playlist:37i9dQZF1DX4xuWVBs4FgJ?si=7c1574dfb25d4117"),
        ("Coffee Table Jazz", "spotify:playlist:37i9dQZF1DWVqfgj8NZEp1?si=f90718546eb4492f")
    ]
    response = "\n".join([f"{name}|{url}" for name, url in playlists])
    return response, 200, {'Content-Type': 'text/plain; charset=utf-8'}

@app.route('/play_url')
def play_url():
    url = request.args.get('url')
    player_mac, room_name = get_player_info(request.args.get('room'))
    
    if player_mac and url:
        # 1. TVÄTTA URL: Ta bort allt efter "?" (si=... etc)
        # Spotty föredrar: spotify:playlist:37i9dQZF1EQntZpEGgfBif
        clean_url = url.split('?')[0].strip()
        
        print(f"[ACTION] Spelar tvättad URL: {clean_url} i {room_name}")
        
        # 2. SKICKA TILL LMS
        # Vi kör shuffle, stop, clear och play i en sekvens
        lms_json_rpc(player_mac, ["playlist", "shuffle", 1])
        lms_json_rpc(player_mac, ["stop"])
        lms_json_rpc(player_mac, ["playlist", "clear"])
        
        # Viktigt: Använd 'playlist' 'play' för URL:er
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
        # Om rummet är c5, kör vi UPnP-triggen först
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
    player_data = get_player_info(room_id)
    
    if player_data:
        player_mac, room_name = player_data
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
        return "/static/icon.png" # Fallback om inget rum är valt
        
    # LMS genererar omslag via denna URL-struktur:
    # http://[LMS-IP]:9000/music/current/cover.jpg?player=[MAC]
    # Vi mappar om 127.0.0.1 till din servers externa IP (10.0.1.132) för att iPhone ska nå den
    lms_host = "10.0.1.132" 
    art_url = f"http://{lms_host}:9000/music/current/cover.jpg?player={player_mac}&time={int(time.time())}"
    
    return art_url

@app.route('/status_raw')
def get_status_raw():
    room_arg = request.args.get('room')
    player_mac, _ = get_player_info(room_arg)
    
    if not player_mac:
        return "pause"
        
    # Vi ber LMS om status. 
    # Detta motsvarar det din ESP32 parsar.
    res = lms_json_rpc(player_mac, ["status", "-", "1"])
    
    try:
        # Här hämtar vi 'mode' direkt ur JSON-svaret
        # Det blir antingen 'play', 'pause' eller 'stop'
        return res['result']['mode']
    except:
        return "pause"


if __name__ == '__main__':
    print("--- Dörrvakten Bridge: Startad ---")
    app.run(host='0.0.0.0', port=5000, debug=True)
