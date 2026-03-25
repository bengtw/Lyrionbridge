# Lyrionbridge

A lightweight Python bridge for [Lyrion Music Server](https://lyrion.org), plus a Progressive Web App for controlling playback from any device on the network.

## Features

- **REST API** wrapping the LMS JSON-RPC interface
- **Spotify Daily Mixes** via the Spotty plugin
- **Playlist management** — serve and play saved Spotify playlists
- **Random album** playback from the local library
- **Volume control** per player
- **Built-in PWA** — a mobile-optimized web app served directly by the bridge, no separate hosting needed

## Requirements

- Python 3
- [Lyrion Music Server](https://lyrion.org) with the [Spotty](https://github.com/michaelherger/Spotty-Plugin) plugin for Spotify support
- `pip install flask requests`

## Setup

1. Edit `lms_bridge.py` and update the configuration at the top:

```python
LMS_URL  = "http://127.0.0.1:9000/jsonrpc.js"
LMS_HOST = "192.168.x.x"
PLAYERS  = {
    "office": "aa:bb:cc:dd:ee:ff",
    ...
}
```

2. Start the bridge:

```bash
python3 lms_bridge.py
```

3. Open `http://<server-ip>:5000` in a browser or add it to your home screen as a PWA.

## API Endpoints

| Endpoint | Description |
|---|---|
| `GET /status` | Playback mode (`play` / `pause`) |
| `GET /title` | Current track and artist |
| `GET /art` | Album art URL |
| `GET /toggle_play_pause` | Toggle playback |
| `GET /next` | Skip to next track |
| `GET /set_volume?level=N` | Set volume (0–100) |
| `GET /play_random_album` | Play a random album |
| `GET /play_url?url=...` | Play a Spotify playlist URL |
| `GET /daily?index=N` | Play a Spotify Daily Mix |
| `GET /get_daily_mixes` | List available Daily Mixes |
| `GET /get_random_albums` | List random albums with art |
| `GET /get_players` | List connected players |

All endpoints accept an optional `?room=<name or MAC>` parameter to target a specific player.

## Related

- [WaveshareKnob](https://github.com/bengtw/WaveshareKnob) — a physical rotary controller for Lyrionbridge running on an ESP32-S3 round touch display
