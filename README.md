# Lyrionbridge

A Python bridge for [Lyrion Music Server](https://lyrion.org) that acts as the music data layer for the [Edgar](https://github.com/bengtw/edgar) AI assistant. Also serves a Progressive Web App for direct playback control.

## Components

| File | Role |
|------|------|
| `lms_bridge.py` | Flask app (port 5000/5001) — REST API wrapping LMS JSON-RPC and Spotify via Spotty |
| `lms_logger.py` | Daemon subscribing to LMS CLI events, logging plays to SQLite, estimating audio features via Gemini |
| `play_history.db` | SQLite — play log, skip flags, audio features cache, track features cache |
| `metadata_cache.db` | SQLite — Spotify search result cache + AI-generated Daily Mix labels |
| `button_prompts.json` | Maps button IDs to Edgar prompts for physical remotes |

## Features

- **Playback control** — play, pause, next, volume, transfer between players
- **Search** — local library and Spotify via Spotty, with persistent disk cache
- **Playlists** — serve and play saved `.m3u` playlists
- **Player discovery** — list connected players and active playback state
- **Play history API** — recent tracks, artists, skipped tracks and listening stats for Edgar
- **Audio features** — energy/valence/danceability/tempo estimated via Gemini, cached to avoid repeat API calls
- **Spotify caching** — search results cached on disk (30 days for tracks, 7 days for albums/artists/playlists)
- **Daily Mix labels** — AI-generated 1–2 word Swedish style labels per Daily Mix (e.g. "EBM-klubb", "80-talssynth"), refreshed every 6 hours
- **Button prompts** — physical remotes with no screen can trigger Edgar prompts via `/button_prompt`
- **PWA** — mobile-optimized web app served directly by the bridge

## Requirements

```bash
pip install flask requests google-genai
```

`lms_bridge.py` reads `GEMINI_API_KEY` from `../edgar/.env` for Daily Mix label generation.
`lms_logger.py` uses the same key for audio feature estimation.

## Setup

Update the configuration at the top of `lms_bridge.py`:

```python
LMS_HOST = "192.168.x.x"
```

Start the bridge:

```bash
python3 lms_bridge.py
```

Start the play logger as a daemon (or use the included systemd service):

```bash
python3 lms_logger.py
# or: sudo systemctl enable --now lms_logger
```

Open `http://<server-ip>:5000` in a browser or add it to your home screen as a PWA.

## API Endpoints

### Playback

| Endpoint | Description |
|----------|-------------|
| `GET /toggle_play_pause` | Toggle playback |
| `GET /next_active` | Skip to next track on active player |
| `GET /stop_active` | Stop all active players |
| `GET /set_volume?level=N` | Set volume (0–100) |
| `GET /play_url?url=...` | Play a Spotify URI or stream URL |
| `GET /play_random_album` | Play a random album from the local library |
| `GET /transfer_playback?to=...&from=...` | Transfer playback between players |

### Discovery

| Endpoint | Description |
|----------|-------------|
| `GET /get_players` | List all connected players |
| `GET /active_players` | List players currently playing |
| `GET /get_playlists_with_art` | List saved playlists with artwork |
| `GET /get_daily_mixes` | List Spotify Daily Mixes |
| `GET /get_daily_mixes_knob` | Daily Mix 1–6 as `title\|AI-label\|index` lines for display devices |
| `GET /button_prompt?id=N&room=...` | Trigger an Edgar prompt mapped to button N (see `button_prompts.json`) |

### Search

| Endpoint | Description |
|----------|-------------|
| `GET /search_library?q=...&type=track\|album\|artist` | Search the local LMS library |
| `GET /spotify_search?q=...&type=track\|album\|artist\|playlist` | Search Spotify via Spotty (disk-cached) |
| `GET /spotify_artist_top?q=...` | Top tracks for an artist |
| `GET /spotify_artist_radio?q=...` | Artist radio item ID |
| `GET /spotify_genres` | List Spotify genre/mood categories |
| `GET /spotify_genre_playlists?id=...` | Playlists for a genre category |

### Play history (used by Edgar)

| Endpoint | Description |
|----------|-------------|
| `GET /recent_artists?limit=N&days=N` | Most-played artists, excluding skips |
| `GET /recent_tracks?limit=N&days=N` | Recently played tracks, excluding skips |
| `GET /skipped_tracks?limit=N&days=N` | Tracks skipped most often |
| `GET /listening_stats?days=N` | Play count, source breakdown, top artists |
| `GET /play_history_data` | Full history page payload (plays, profile, top artists, energy distribution) |

All playback endpoints accept an optional `?room=<name or MAC>` parameter to target a specific player.

## Backfill audio features

To estimate features for all tracks in the play history that are missing them:

```bash
python3 lms_logger.py --backfill
```

## Related

- [Edgar](https://github.com/bengtw/edgar) — the AI assistant this bridge serves
- [WaveshareKnob](https://github.com/bengtw/WaveshareKnob) — a physical rotary controller running on an ESP32-S3 round touch display
