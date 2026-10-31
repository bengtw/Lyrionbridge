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
import datetime
import urllib.parse
from pathlib import Path
import volume_cal
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

# Logghandler så INFO faktiskt når journalen. Utan denna har root-loggern ingen
# handler → lastResort släpper bara WARNING+ (vakthundens kill-rader, QueueTracks
# PLAY/KÖ m.m. försvann tyst). Format speglar edgars _setup_logging.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logging.getLogger('werkzeug').setLevel(logging.ERROR)

_session = requests.Session()
_session.mount('http://', requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=10))

# Spottys sökmeny: 0 Artists, 1 Albums, 2 Playlists, 3 Podcasts, 4 Podcast Episodes,
# 5 Users, sedan låtarna direkt (isaudio=1). "track" stod förut på 1 (= Albums) och
# album saknades — låtsöket använder inte tabellen, så felet syntes inte (13 okt 2026).
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
# Alla adresser/sökvägar går att override:a via .env (../edgar/.env läses ovan).
LMS_HOST     = os.getenv("LMS_HOST", "10.0.1.132")
LMS_PORT     = int(os.getenv("LMS_PORT", "9000"))
LMS_HTTP     = f"http://{LMS_HOST}:{LMS_PORT}"
LMS_URL      = f"{LMS_HTTP}/jsonrpc.js"
EDGAR_URL    = os.getenv("EDGAR_URL", "http://127.0.0.1:5015")
C5_IP        = os.getenv("C5_IP",  "10.0.1.125")
C5_MAC       = os.getenv("C5_MAC", "bb:bb:7a:f8:33:39")
# C5-start (omgjord 8 okt 2026). All C5-styrning bygger på C5:ans EGEN UPnP-status
# (c5_transport_state) och tid sedan den senast spelade — aldrig på LMS:ens elapsed,
# som står på 0 i 6–9 s under en normal start och kan ticka vidare när C5 tystnat.
C5_COLD_AFTER_S   = 30 * 60   # tyst så här länge → nästa start räknas som kallstart
C5_WAKE_MAX_S     = 6.0       # väckning före play: vänta högst så här länge på svar
# 16 okt 2026: 7 av ~45 starter sedan 8 okt fastnade i TRANSITIONING och löste sig ALDRIG själva
# (stop→play vid 20 s, PLAYING 2 s senare); lyckade starter ger PLAYING efter 1–6 s (längst 12 s).
# Prövat 8 s samma dag: en gång PLAYING efter 10 s, en gång låste sig Lyrion 12 s efter stoppet
# (Spotify-strömmen laddade fortfarande) → 12 s tills vidare, och svarstiderna loggas.
C5_START_RETRY_S  = 12.0      # inte PLAYING så här länge efter play → stop→play (var 20)
C5_START_SKIP_S   = 12.0      # … fortfarande inte så här länge EFTER stop→play → nästa spår
C5_GUARD_GIVEUP_S = 45.0      # Lyrion svarar inte alls så här länge → sluta bevaka
C5_DESYNC_S       = 20.0      # LMS spelar men C5 STOPPED så här länge → logga (skuggläge)
PLAYLIST_DIR = os.getenv("LMS_PLAYLIST_DIR", "/var/lib/squeezeboxserver/playlists")
DEBUG              = os.getenv("BRIDGE_DEBUG", "").lower() == "true"
LASTFM_API_KEY     = os.getenv("LAST_FM_API_KEY") or os.getenv("LASTFM_API_KEY", "9ed2b1dfa5c3f0ece0a30ec8e69b4742")
LASTFM_API_SECRET  = os.getenv("LAST_FM_API_SECRET") or os.getenv("LASTFM_API_SECRET", "")
LASTFM_SESSION_KEY = os.getenv("LASTFM_SESSION_KEY", "")
LASTFM_USERNAME    = os.getenv("LASTFM_USERNAME", "LadoCasseta")

# Spotify Web API — user-token för recently_played-ingest (mobil-lyssning).
# client_id/secret delas med edgars spotify_api.py (SPOTIPY_*). refresh_token
# mintas en gång via edgars spotify_auth_setup.py och läggs i edgar/.env.
SPOTIFY_CLIENT_ID     = os.getenv("SPOTIPY_CLIENT_ID")     or os.getenv("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIPY_CLIENT_SECRET") or os.getenv("SPOTIFY_CLIENT_SECRET", "")
SPOTIFY_REFRESH_TOKEN = os.getenv("SPOTIFY_REFRESH_TOKEN", "")

FAVORITE_PLAYLISTS = [
    ("Background Jazz",      "spotify:playlist:37i9dQZF1DWV7EzJMK2FUI?si=e099779019b14bb7"),
    ("Chilled Classical",    "spotify:playlist:37i9dQZF1DWUvHZA1zLcjW?si=ef77a6c2ebf14473"),
    ("Soft Lounge",          "spotify:playlist:37i9dQZF1DX82pCGH5USnM?si=7752baaaeff94464"),
    ("Soul Mix",             "spotify:playlist:37i9dQZF1EQntZpEGgfBif?si=357c1eec328d4db2"),
    ("Dinner with Friends",  "spotify:playlist:37i9dQZF1DX4xuWVBs4FgJ?si=7c1574dfb25d4117"),
    ("Coffee Table Jazz",    "spotify:playlist:37i9dQZF1DWVqfgj8NZEp1?si=f90718546eb4492f")
]

# play_history.db ägs av detta repo (delas med lms_logger). Enda källan för
# sökvägen — härled den aldrig inline med Path(__file__) på nytt.
PLAY_DB            = Path(__file__).parent / "play_history.db"
_SEARCH_CACHE_DB   = Path(__file__).parent / "metadata_cache.db"
_SEARCH_TTL_TRACK  = 30 * 86400   # 30 dagar — spår ändras sällan
_SEARCH_TTL_OTHER  = 7  * 86400   # 7 dagar  — album/artist/playlist
# Daily Mix-etiketterna följer mixens INNEHÅLL, inte klockan (23 okt 2026): Spotify byter
# en mix när som helst på dygnet, och med 6 h-TTL visade Daily Mix 3 "Soulig pop med
# Lianne La Havas" i ~6 h medan Rob Zombie spelades. Nu jämförs mixens signatur
# (_mix_sig) var 15:e minut och vid uppspelning; bara ändrade mixar får nya AI-anrop.
_MIX_CHECK_INTERVAL = 15 * 60


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
                summary   TEXT,
                cached_at INTEGER NOT NULL
            )
        """)
        # Migration: lägg till summary-kolumn om tabellen redan fanns utan den.
        cols = [r[1] for r in conn.execute("PRAGMA table_info(mix_labels)").fetchall()]
        if "summary" not in cols:
            conn.execute("ALTER TABLE mix_labels ADD COLUMN summary TEXT")
        if "sig" not in cols:
            conn.execute("ALTER TABLE mix_labels ADD COLUMN sig TEXT")


_init_search_cache()

_db_lock       = threading.Lock()   # serialiserar skrivningar mot metadata_cache.db
# Spotty-browse (`spotty items`) är sessionsbundet per spelare med positionella
# item_id — samtidiga sökningar + URI-drillningar ger KORSPRAT: svaret på "ABBA
# Dancing Queen" blev The Blue Niles albumspår (68 sådana poster i sökcachen,
# skur 2026-09-15 när 11 prefetch-listor löstes i följd). Max 2 samtidiga.
_spotty_sem    = threading.Semaphore(int(os.getenv("BRIDGE_SPOTTY_CONCURRENCY", "2")))
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


def _mix_model() -> str:
    """Modell för mix-etiketter och -summeringar. 3.1 Flash Lite sedan 7 okt 2026 (var
    hårdkodad 2.5). Inga sampling-/thinking-parametrar skickas, så anropen tål även
    nyare modeller. Läses vid varje anrop så .env/systemd-ändringar slår igenom."""
    return os.environ.get("LMS_LABEL_MODEL", "gemini-3.1-flash-lite")


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
        resp = client.models.generate_content(model=_mix_model(), contents=prompt)
        label = resp.text.strip().strip('.')
        return label if label else None
    except Exception as e:
        logging.warning(f"[mix_labels] Gemini-fel: {e}")
        return None


def _get_mix_summary(artists_text: str) -> str | None:
    """Frågar Gemini om en kort, lockande beskrivning (~6–12 ord) av mixen — för
    ytor med mer plats än knappen (t.ex. dashboard-kort)."""
    client = _get_gemini()
    if not client:
        return None
    prompt = (
        f"Artister: {artists_text}\n\n"
        "Skriv en MYCKET kort, lockande beskrivning på svenska (3–6 ord, ingen "
        "fullständig mening) av den här spellistan. Nämn genre/era/stämning och "
        "gärna en tongivande artist. T.ex. 'Mörk EBM-klubb med Nitzer Ebb', "
        "'Soulig 70-tal', 'Drömsk synthpop och shoegaze'. Bara orden, inga citattecken."
    )
    try:
        resp = client.models.generate_content(model=_mix_model(), contents=prompt)
        s = resp.text.strip()
        # Gemini ger ibland flera förslag på egna rader — ta bara första raden.
        s = s.split("\n")[0].strip().strip('"').strip().rstrip(".")
        # Säkerhetsnät: håll det kort (max 7 ord).
        words = s.split()
        if len(words) > 7:
            s = " ".join(words[:7])
        return s or None
    except Exception as e:
        logging.warning(f"[mix_labels] Gemini-summary-fel: {e}")
        return None


_DAILY_IMG_RE = re.compile(r'/daily/\d+/([^/]+)/')
_mix_refresh_lock = threading.Lock()


def _image_cdn(raw_img: str) -> str:
    m = re.search(r'/imageproxy/([^/]+)/image', raw_img)
    return urllib.parse.unquote(m.group(1)) if m else (raw_img if raw_img.startswith('http') else '')


def _daily_mix_index(title: str) -> int | None:
    """0–5 för "Daily Mix 1"–"Daily Mix 6", annars None."""
    if title.startswith('Daily Mix ') and len(title) == 11 and '1' <= title[10] <= '6':
        return int(title[10]) - 1
    return None


def _mix_sig(item) -> str:
    """Signatur för en mix innehåll: id:t i omslagets adress (pickasso …/daily/<n>/<id>/en,
    byts när Spotify byter mixen) plus artistraden — samma omslag med nya artister räknas
    också som byte."""
    parts = item.get('text', '').split('\n')
    m = _DAILY_IMG_RE.search(_image_cdn(item.get('icon') or item.get('image', '')))
    return f"{m.group(1) if m else ''}|{parts[1] if len(parts) > 1 else ''}"


def _mix_label_rows() -> dict:
    """mix_id → (label, summary, sig, cached_at)."""
    with sqlite3.connect(_SEARCH_CACHE_DB) as conn:
        return {r[0]: r[1:] for r in conn.execute(
            "SELECT mix_id, label, summary, sig, cached_at FROM mix_labels"
        ).fetchall()}


def _refresh_mix_labels(mixes_raw=None):
    """Ny AI-etikett + summering för de Daily Mixar vars innehåll ändrats (eller som saknar
    en). Oförändrade mixar rörs inte. En körning åt gången; en samtidig hoppar över."""
    if not _mix_refresh_lock.acquire(blocking=False):
        return
    try:
        rows = _mix_label_rows()
        for item in (mixes_raw if mixes_raw is not None else _fetch_daily_mixes_raw()):
            parts = item.get('text', '').split('\n')
            title = parts[0]
            mix_id = _daily_mix_index(title)
            if mix_id is None:
                continue
            sig = _mix_sig(item)
            cached = rows.get(mix_id)
            # Ny om saknas, utan summering, för lång (>7 ord) eller nytt innehåll.
            if cached and cached[1] and len(cached[1].split()) <= 7 and cached[2] == sig:
                continue
            artists_text = parts[1] if len(parts) > 1 else title
            label   = _get_mix_label(artists_text)
            summary = _get_mix_summary(artists_text)
            if label or summary:
                with _db_lock, sqlite3.connect(_SEARCH_CACHE_DB) as conn:
                    conn.execute(
                        "INSERT OR REPLACE INTO mix_labels (mix_id, label, summary, sig, cached_at) VALUES (?,?,?,?,?)",
                        (mix_id, label or "", summary, sig, int(time.time()))
                    )
                why = "ny" if not cached else ("bytt innehåll" if cached[2] != sig else "omgjord")
                logging.info(f"[mix_labels] {title} ({why}) → \"{label}\" / \"{summary}\"")
    finally:
        _mix_refresh_lock.release()


def _refresh_mix_labels_async(mixes_raw=None):
    threading.Thread(target=_refresh_mix_labels, args=(mixes_raw,), daemon=True,
                     name="mix-labels-now").start()


def _mix_label_loop():
    time.sleep(5)
    while True:
        try:
            _refresh_mix_labels()
        except Exception as e:
            logging.warning(f"[mix_labels] refresh-fel: {e}")
        time.sleep(_MIX_CHECK_INTERVAL)


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

# Spelar-vakthund. "Framsteg" = mode 'play' OCH spårets elapsed-tid tickar framåt
# — en hängd spelare (typiskt vardagsrummet) kan rapportera mode 'play' men stå
# still på elapsed, så enbart mode-koll räcker inte. Vakthunden återupplivar INTE
# (ingen kick mitt i en låt — det är lika irriterande som hängningen); den dödar
# bara det som inte kommer framåt. Två trösklar beroende på läge:
#   - LÅS (mode 'play' men fryst): först hopp till nästa spår efter ~30 s (max 2 hopp,
#     en hängd Spotty-start ska inte kosta hela kön), sedan stop + rensa kö efter ~1 min.
#   - PAUS/STOPP (mode != 'play'): rörs inte under dagen. Kl 23 rensas varje kö som inte
#     spelar (_NIGHT_FLUSH_HOUR), och en spelare som varit borta minst _REAPPEAR_AFTER_S
#     rensas när den dyker upp igen (_on_reappear). Se _night_flush.
# Play-tid-kicken i _deferred_kick hanterar separat strömmar som aldrig startade.
# Allt konfigurerbart via .env.
_LOCK_KILL_SECONDS  = int(os.getenv("BRIDGE_LOCK_KILL_SECONDS", "60"))      # lås-tröskel
_LOCK_SKIP_SECONDS  = int(os.getenv("BRIDGE_LOCK_SKIP_SECONDS", "30"))      # lås: hoppa till nästa spår efter Ns
_LOCK_SKIP_MAX      = int(os.getenv("BRIDGE_LOCK_SKIP_MAX", "2"))           # max hopp innan stop + rensa
# Köer som inte spelar (29 okt 2026, ersätter "rensa efter 1 h paus/stopp"): Lyrion kan
# återuppta en gammal kö när en spelare kopplar upp sig igen eller slås på (badrummet 10 jun
# 2026: en 20 dagar gammal kö startade av sig själv). Timregeln tömde också ~3 köer om dagen
# efter vanliga pauser. Nu: rensa allt som inte spelar kl 23, och en spelare som kommer
# tillbaka efter att ha varit borta (då nådde kvällsrensningen den inte).
_NIGHT_FLUSH_HOUR   = int(os.getenv("BRIDGE_NIGHT_FLUSH_HOUR", "23"))
_REAPPEAR_AFTER_S   = int(os.getenv("BRIDGE_REAPPEAR_AFTER_S", "600"))      # borta minst så länge
_OWN_START_GRACE_S  = 120   # kön ändrad / start via bryggan så nyss → ett val, inte en gammal kö
_WATCHDOG_POLL      = int(os.getenv("BRIDGE_WATCHDOG_POLL", "10"))          # kollintervall
# Framsteg kräver att elapsed ökat minst så här mycket sedan förra varvet (ett spelande
# spår ökar ~_WATCHDOG_POLL s). 12 okt 2026: efter en låt som tagit slut kröp Lyrions
# elapsed 252 → 252,8 s på 2½ minut, och varje bråkdels ökning räknades som framsteg —
# vardagsrummet var tyst 2 min 40 s innan vakthunden reagerade.
_MIN_PROGRESS_S     = float(os.getenv("BRIDGE_MIN_PROGRESS_S", "3"))
# Häng VID LÅTSLUT (13 okt 2026): tiden har nått låtens fulla längd och står still → nästa
# spår startade aldrig. Vid ett normalt byte (Linn i flow-läge) tar nästa låt över INNAN
# tiden når längden, så läget är ett säkert tecken på häng — hoppa efter _END_STALL_SKIP_S i
# stället för _LOCK_SKIP_SECONDS. En ren tidsgräns hade inte gått: 5 % av de normala bytena
# dröjde > 10 s, och ett hopp då hade hoppat över nästa låt.
_END_STALL_SKIP_S   = float(os.getenv("BRIDGE_END_STALL_SKIP_S", "8"))
_AT_END_MARGIN_S    = 1.0
_PAST_END_MARGIN_S  = 2.0     # elapsed förbi spårets längd + marginal = spåret är slut, inget framsteg
# Falskt låtslut (13 okt 2026): UPnP-bryggans tidsräkning för Linn kan hoppa. 15:13:39
# "elapse roll-back detected" → Lyrion sade 276 av 275 s när bara ~210 s hade spelats (Lyrion
# skickade ljud till Linn i 9 s till), och regeln ovan hoppade över låtens sista minut. Därför
# en TROVÄRDIG position: elapsed flyttas bara fram med högst väggtiden + _JUMP_TOLERANCE_S
# (Linns ryckiga ikapphopp ryms). Ett större hopp (sökning eller fel) godtas först när
# tiden fortsätter framåt därifrån. Säger Lyrion "slut" före den trovärdiga positionens
# slut väntar vakthunden ut den verkliga längden innan snabbhoppet.
_JUMP_TOLERANCE_S   = 15.0
# Skuggläge (13 okt 2026): för de här spelarna loggar vakthunden vad den SKULLE ha gjort vid
# ett lås, och om det löste sig självt, men gör inget. Vardagsrummet (Linn via UPnP-bryggan):
# flera hopp den dagen visade sig vara tidsfel från bryggan medan musiken spelade, och det är
# okänt om tystnaderna vid låtslut hade släppt utan hjälp. Kommaseparerade MAC-adresser.
_SHADOW_MACS = {m.strip().lower() for m in
                os.getenv("BRIDGE_WATCHDOG_SHADOW_MACS", "bb:bb:4d:b0:d0:06").split(",") if m.strip()}
_shadow_since   = {}   # mac → (tidpunkt, åtgärd, elapsed, spår) för ett pågående skugglås
_trusted_pos    = {}   # mac → (elapsed, tidpunkt) senast trovärdiga position
_jump_seen      = {}   # mac → (elapsed, tidpunkt) för ett hopp som inte godtagits ännu
_false_end_logged = {} # mac → redan loggat "säger slut men…" för det här hänget
_last_active_ts = {}   # mac → senaste tidpunkt spelaren faktiskt gjorde framsteg
_last_elapsed   = {}   # mac → elapsed vid senast bekräftade framsteg (för att upptäcka frysning)
_last_track     = {}   # mac → nuvarande spårs identitet (url) — nytt spår = färsk frist
_c5_last_playing = 0.0   # senaste gången C5 bevisligen spelade (0 = okänt → kallstart)
_c5_desync_since = None  # när LMS-spelar-men-C5-STOPPED började (skuggläge)
_c5_woken_at     = 0.0   # senaste väckning (en start = flera kommandon → väck en gång)
_last_mode      = {}   # mac → senast sedda mode (paus/stopp → play = färsk frist)
_skip_recover   = {}   # mac → antal spårhopp vakthunden gjort mot ett lås (nollställs vid framsteg)
_gone_since     = {}   # mac → när spelaren senast försvann ur Lyrions lista (eller blev bortkopplad)
_present        = set()  # spelare som var anslutna förra varvet
_last_start_cmd = {}   # mac → senaste startkommando via bryggan (lms_json_rpc)
_night_flushed  = None   # datum för senaste kvällsrensning
_watchdog_started = False  # första varvet gjort (spelare som syns då är inte "tillbaka")


# --- HJÄLPFUNKTIONER ---

def _cache_valid(ts, ttl):
    return time.time() - ts < ttl

def _img_url(path):
    """Brygg-proxad bild-URL för en LMS-relativ sökväg — så webbklienter slipper
    nå LMS:ens :9000 direkt (mDNS/brandvägg gör ofta att den inte kan resolvas)."""
    return "/imgproxy?p=" + urllib.parse.quote(path, safe="")


def _cover_url(cover_id):
    """Proxad omslags-URL för ett artwork_track_id. Resize-formen cover_<W>x<H>_f
    (läge 'f', UTAN ändelse) är den enda som ger äkta omslag för ALLA album — även
    de vars konst LMS hämtat online (där /cover.jpg 404:ar) — och skalar ner.
    300 px fiskar upp flest omslag; 500 px får LMS att skala upp → platshållare."""
    return _img_url(f"/music/{cover_id}/cover_300x300_f")


def _abs_image(url):
    """Gör LMS-bild-URL klient-vänlig: relativa LMS-sökvägar proxas via bryggan,
    absoluta (t.ex. CDN) lämnas orörda (kan hämtas direkt av klienten)."""
    if not url:
        return ""
    if url.startswith('/'):
        return _img_url(url)
    return url


def c5_transport_state(timeout=2):
    """C5:ans EGEN transportstatus via UPnP: 'PLAYING', 'STOPPED', 'TRANSITIONING' …
    eller None om den inte svarar. Sanningen om högtalaren spelar — LMS:ens elapsed
    för C5 står på 0 i 6–9 s under en helt normal start och kan gå vidare när C5
    tystnat (mätt 8 okt 2026). RelTime rapporteras inte (alltid 00:00:00), så bara
    tillståndet går att använda."""
    url = f"http://{C5_IP}:49152/upnp/control/rendertransport1"
    body = ('<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
            '<u:GetTransportInfo xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
            '<InstanceID>0</InstanceID></u:GetTransportInfo></s:Body></s:Envelope>')
    headers = {"Content-Type": 'text/xml; charset="utf-8"',
               "SOAPACTION": '"urn:schemas-upnp-org:service:AVTransport:1#GetTransportInfo"'}
    try:
        r = _session.post(url, data=body, headers=headers, timeout=timeout)
        m = re.search(r"<CurrentTransportState>([^<]+)<", r.text)
        return m.group(1).strip() if m else None
    except Exception:
        return None


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

def _is_start_command(args) -> bool:
    """Startar kommandot uppspelning (nytt spår/ny kö/återuppta)?"""
    a = [str(x) for x in (args or [])]
    if not a:
        return False
    if a[0] == "play":
        return True
    if a[0] == "playlist" and len(a) > 1 and a[1] in ("play", "load", "index", "jump"):
        return True
    if a[0] in ("spotty", "favorites") and a[1:3] == ["playlist", "play"]:
        return True
    return a[0] == "playlistcontrol" and "cmd:load" in a


def lms_json_rpc(player_id, command_args, timeout=3, _raw=False):
    """JSON-RPC mot LMS. Startkommandon till C5 går genom C5-spärren: väckning
    vid kallstart FÖRE kommandot, startkontroll EFTER — oavsett vilken väg
    (Edgar, dashboard, transfer …) som startade. `_raw` = C5-logikens egna kommandon."""
    if not _raw and player_id and _is_start_command(command_args):
        _last_start_cmd[player_id] = time.time()
    c5_start = (not _raw and player_id == C5_MAC and _is_start_command(command_args))
    if c5_start:
        c5_wake(player_id)
    payload = {"id": 1, "method": "slim.request", "params": [player_id, command_args]}
    try:
        res = _session.post(LMS_URL, json=payload, timeout=timeout).json()
    except Exception as e:
        print(f"[ERROR] LMS: {e}")
        return None
    if c5_start:
        c5_start_guard(player_id, "brygga")
    return res

def c5_is_cold() -> bool:
    return time.time() - _c5_last_playing > C5_COLD_AFTER_S


def c5_wake(mac):
    """Före play vid KALLSTART: slå på spelaren i LMS och fråga C5 tills den svarar
    (högst C5_WAKE_MAX_S). Hypotes: C5:ans nätverk somnar efter lång tystnad och
    missar början av strömmen. Svarstiden loggas — den visar om den sov."""
    global _c5_woken_at
    if mac != C5_MAC or not c5_is_cold() or time.time() - _c5_woken_at < 60:
        return                                    # varm, eller redan väckt för den här starten
    _c5_woken_at = t0 = time.time()
    lms_json_rpc(mac, ["power", 1], _raw=True)
    state = None
    while time.time() - t0 < C5_WAKE_MAX_S:
        state = c5_transport_state(timeout=2)
        if state:
            break
        time.sleep(0.5)
    idle_min = (t0 - _c5_last_playing) / 60 if _c5_last_playing else None
    logging.info(f"[c5] kallstart (tyst {f'{idle_min:.0f} min' if idle_min else 'okänt'}): "
                 f"väckt på {time.time() - t0:.1f} s, status={state}")


_c5_guard_lock    = threading.Lock()
_c5_guard_running = False
_c5_start_t0      = 0.0      # senaste start (en ny start under bevakning förnyar fristen)
_c5_start_cold    = False


def c5_start_guard(mac, via):
    """Starta startkontrollen (en åt gången). Ny start från bryggan medan en kör → förnya fristen.
    Vakthundens upptäckt ("extern") förnyar inte: den ser samma start upp till 10 s senare, och
    16 okt gjorde det att stop→play kom efter 22 s i stället för 12."""
    global _c5_guard_running, _c5_start_t0, _c5_start_cold
    with _c5_guard_lock:
        if _c5_guard_running:
            if via == "brygga":
                _c5_start_t0 = time.time()
            return
        _c5_start_t0 = time.time()
        _c5_start_cold = c5_is_cold()
        _c5_guard_running = True
    threading.Thread(target=_c5_start_guard, args=(mac, via), daemon=True).start()


def _c5_start_guard(mac, via):
    """Efter en start: fråga C5 var 2:a s. PLAYING → klart (tiden loggas, kall/varm).
    Inte PLAYING efter C5_START_RETRY_S → stop→play; C5_START_SKIP_S därefter → nästa
    spår. Avbryter om LMS inte längre spelar (man pausade/stoppade själv) — men inte om Lyrion
    bara inte svarar (då väntar den, högst C5_GUARD_GIVEUP_S)."""
    global _c5_last_playing, _c5_guard_running
    retried, t_retry, silent_since = False, None, None
    kind = f"{'kall' if _c5_start_cold else 'varm'} start ({via})"
    try:
        while True:
            time.sleep(2.0)
            t = time.time() - _c5_start_t0
            res = lms_json_rpc(mac, ["status", "-", 1, "tags:"], _raw=True)
            if not res:
                # Inget svar ≠ stoppad (16 okt: Lyrion stod still 12 s efter stoppet, vakten gav upp
                # och vaktens play utfördes först när Lyrion vaknade).
                silent_since = silent_since or time.time()
                if time.time() - silent_since >= C5_GUARD_GIVEUP_S:
                    logging.info(f"[c5] {kind}: Lyrion har inte svarat på {C5_GUARD_GIVEUP_S:.0f} s — slutar bevaka")
                    return
                continue
            if silent_since:
                logging.info(f"[c5] {kind}: Lyrion svarade igen efter {time.time() - silent_since:.0f} s tystnad")
                silent_since = None
            if res.get("result", {}).get("mode") != "play":
                logging.info(f"[c5] {kind}: LMS spelar inte längre efter {t:.0f} s — slutar bevaka")
                return
            state = c5_transport_state()
            if state == "PLAYING":
                _c5_last_playing = time.time()
                logging.info(f"[c5] {kind}: PLAYING efter {t:.0f} s{' (efter stop→play)' if retried else ''}")
                return
            if not retried and t >= C5_START_RETRY_S:
                t1 = time.time()
                r1 = lms_json_rpc(mac, ["stop"], _raw=True)
                t2 = time.time()
                time.sleep(0.3)
                r2 = lms_json_rpc(mac, ["play"], _raw=True, timeout=15)
                t3 = time.time()
                logging.info(f"[c5] {kind}: status={state} efter {t:.0f} s — stop→play "
                             f"(stop svarade {'på %.1f s' % (t2 - t1) if r1 else 'inte'}, "
                             f"play {'på %.1f s' % (t3 - t2 - 0.3) if r2 else 'inte'})")
                retried, t_retry = True, time.time()
            elif retried and time.time() - t_retry >= C5_START_SKIP_S:
                logging.info(f"[c5] {kind}: status={state} efter {t:.0f} s — hoppar till nästa spår")
                lms_json_rpc(mac, ["playlist", "index", "+1"], _raw=True)
                return
    finally:
        with _c5_guard_lock:
            _c5_guard_running = False


def _c5_check_desync(name):
    """SKUGGLÄGE: LMS spelar och klockan tickar, men C5 själv är tyst (STOPPED).
    Hände 8 okt 2026: ljudet tog slut med spåret, LMS:ens klocka fortsatte, köket
    tystnade. Loggar "skulle ha hoppat" efter C5_DESYNC_S — åtgärdar inget ännu."""
    global _c5_last_playing, _c5_desync_since
    state = c5_transport_state()
    if state == "PLAYING":
        _c5_last_playing = time.time()
        _c5_desync_since = None
        return
    if state is None:
        return                                    # svarar inte — ingen slutsats
    _c5_desync_since = _c5_desync_since or time.time()
    gap = time.time() - _c5_desync_since
    if C5_DESYNC_S <= gap < C5_DESYNC_S + _WATCHDOG_POLL:
        logging.info(f"[c5] SKUGGA: {name} LMS spelar men C5 säger {state} i {gap:.0f} s — skulle ha hoppat till nästa spår")


def _deferred_kick(player_mac, delay=5.0, attempts=4):
    """Efter ett play-kommando: säkerställ att uppspelningen faktiskt kom igång,
    annars knuffa. Gör upp till `attempts` försök med `delay`s mellanrum — Spotty
    cold-start kan behöva mer än en knuff (men hela serien ryms inom vakthundens
    60s-lås-fönster). Fel-lägen:
      - mode != 'play' → spelaren startade aldrig → ['play'].
      - mode == 'play' men elapsed ≈ 0 → strömmen hängde vid start. ESKALERANDE
        omladdning: först en mjuk knuff (['playlist','jump',cur_index]), sedan —
        om strömmen fortfarande är fryst — en hård omstart (stop → play). En
        UPnP-renderare (t.ex. Audio Pro C5) tolkar ofta 'playlist jump' till SAMMA
        index som en no-op, så den mjuka knuffen räcker inte alltid; stop→play
        tvingar en färsk ström. Avbryter så fort elapsed tickar."""
    # C5 (UPnP) sköts av en egen startkontroll som frågar högtalaren själv —
    # den generiska kicken läser LMS:ens elapsed, som inte går att lita på för C5.
    if player_mac == C5_MAC:
        return   # startkontrollen startades redan av C5-spärren i lms_json_rpc
    def _kick():
        frozen_kicks = 0   # antal knuffar mot en fryst-vid-start-ström
        for _ in range(attempts):
            time.sleep(delay)
            res = lms_json_rpc(player_mac, ["status", "-", 1, "tags:"])
            if not res:
                continue
            r = res.get("result", {})
            mode = r.get("mode")
            playlist_tracks = r.get("playlist_tracks", 0)
            elapsed = r.get("time") or 0
            if playlist_tracks <= 0:
                return
            if mode == "play" and elapsed > 1.0:
                return  # spelar och tickar — klart
            if mode == "pause":
                return  # MEDVETEN paus (t.ex. användaren) → knuffa inte igång igen.
                        # Bara 'stop' (startade aldrig) eller fryst 'play' ska knuffas.
            cur_index = r.get("playlist_cur_index", 0)
            if mode != "play":
                logging.info(f"[kick] {player_mac} mode={mode!r}, {playlist_tracks} spår — startar (play)")
                lms_json_rpc(player_mac, ["play"])
            elif frozen_kicks == 0:
                logging.info(f"[kick] {player_mac} 'play' men fryst på elapsed≈{elapsed} — mjuk knuff (playlist jump {cur_index})")
                lms_json_rpc(player_mac, ["playlist", "jump", cur_index])
                frozen_kicks += 1
            else:
                logging.info(f"[kick] {player_mac} 'play' fortfarande fryst (elapsed≈{elapsed}) — hård omstart (stop→play)")
                lms_json_rpc(player_mac, ["stop"])
                time.sleep(0.3)
                lms_json_rpc(player_mac, ["play"])
                frozen_kicks += 1
    threading.Thread(target=_kick, daemon=True).start()

# Ingen shuffle (13 okt 2026). Den infördes när Daily Mix var det enda knapparna nådde, för
# att det inte skulle bli samma ordning varje gång. Nu ska det som spelas vara det som visas
# (dashboardens låtlista, "starta från den här låten"), och Spotify har redan ordnat mixarna.
# Shuffle stängs uttryckligen av: en spelare minns läget från förra kön.

def lms_play_stream(player_mac, play_command):
    """shuffle av → stop → clear → play (för URLs och daily mixes)"""
    lms_json_rpc(player_mac, ["playlist", "shuffle", 0])
    lms_json_rpc(player_mac, ["stop"])
    lms_json_rpc(player_mac, ["playlist", "clear"])
    result = lms_json_rpc(player_mac, play_command)
    _deferred_kick(player_mac)
    return result

def lms_play_playlist(player_mac, url):
    """Spotify playlist: shuffle av → stop → clear → play, i spellistans ordning."""
    lms_json_rpc(player_mac, ["playlist", "shuffle", 0])
    lms_json_rpc(player_mac, ["stop"])
    lms_json_rpc(player_mac, ["playlist", "clear"])
    res = lms_json_rpc(player_mac, ["playlist", "play", url])
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
    res = lms_json_rpc("", ["players", "0", "50"])
    if res and 'result' in res:
        players = res['result'].get('players_loop', [])
        if players:
            _player_cache = players
            _player_cache_time = time.time()
    return _player_cache

def _is_phone(mac):
    """Är spelaren Edgar-appen i en telefon? Känns igen på modellnamnet, se volume_cal."""
    mac = str(mac or "").lower()
    for p in get_all_players():
        if p.get('playerid', '').lower() == mac:
            return volume_cal.is_phone(p.get('modelname'))
    return False

def _any_player_mac():
    """Returnerar MAC-adressen för första tillgängliga spelare."""
    players = get_all_players()
    return players[0].get('playerid', '') if players else ""

def _strip_definite(s: str) -> str:
    """Tar bort svenska bestämd-form-ändelser: vardagsrummet → vardagsrum."""
    for suf in ("mmet", "mmen", "met", "men", "et", "en", "t", "n"):
        if s.endswith(suf) and len(s) - len(suf) >= 3:
            return s[:-len(suf)]
    return s


# Enheters fasta rumsnamn → LMS-spelarens namn. Köksvredet skickar "c5", men spelaren
# heter "Köket" (döptes om), så "c5" matchade ingen spelare och gav 404.
_ROOM_ALIASES = {"c5": "köket"}


def get_player_info(room_arg):
    """Matcher dynamiskt rummet mot aktiva spelare i LMS."""
    players = get_all_players()
    if not players:
        return None, "unknown"

    if not room_arg:
        return players[0].get('playerid'), players[0].get('name')

    decoded = urllib.parse.unquote(room_arg).strip().lower()
    decoded = _ROOM_ALIASES.get(decoded, decoded)

    if ":" in decoded:
        for p in players:
            if p.get('playerid', '').lower() == decoded:
                return decoded, p.get('name')
        return decoded, "unknown"

    hints = {decoded, _strip_definite(decoded)}
    for p in players:
        name = p.get('name', '').lower()
        pid  = p.get('playerid', '').lower()
        if decoded == name or decoded == pid:
            return p.get('playerid'), p.get('name')
    for p in players:
        name = p.get('name', '').lower()
        if any(h in name or name in h for h in hints):
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

    raw_img = item.get('image', '')
    m = re.search(r'/imageproxy/([^/]+)/image', raw_img)
    return {
        "name": title.strip(),
        "subtitle": f"{artist.strip()} — {album.strip()}".strip(" —"),
        "uri": uri,
        "art": _abs_image(raw_img),
        # Direkt CDN-URL (som _format_entry) — dashboarden på Edgars origin kan inte använda /imgproxy
        "image_cdn": urllib.parse.unquote(m.group(1)) if m else (raw_img if raw_img.startswith('http') else ''),
    }


# Ord som matchar nästan vad som helst — "the" i "Berlin The Metro" matchade
# "The Blue Nile" och släppte igenom korsprat.
_SEARCH_STOPWORDS = {"the", "and", "feat", "featuring", "with", "for", "you", "your", "från", "och", "med"}


def _search_matches_query(query: str, loop: list) -> bool:
    """True om minst en träff (namn eller undertext) delar ett ord (>2 tecken) med
    frågan. Kategorirader (Artists/Albums/…) räknas inte som träffar."""
    words = {w.strip(".,'\"()-") for w in query.lower().split()} - _SEARCH_STOPWORDS
    words = {w for w in words if len(w) > 2}
    audio = [it for it in loop
             if str(it.get('isaudio', '')) == '1' or it.get('type') in ('audio', 'track')]
    if not words or not audio:
        return True   # inget att bedöma — "inga spårträffar" är ett äkta svar, inte korsprat
    for it in audio:
        text = f"{it.get('name', '')} {it.get('line2', '')} {it.get('subtitle', '')} {it.get('artist', '')}".lower()
        if any(w in text for w in words):
            return True
    return False


def _get_stable_spotify_uri(item_id, player_mac):
    """Borrar ner till leaf-noden och returnerar stabil spotify://track:XXXX-URI.
    Försöker upp till 2 gånger med 8 sekunders timeout."""
    for _ in range(2):
        with _spotty_sem:
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

    # Spotty lindar Spotify-CDN-bilder i LMS imageproxy
    # (/imageproxy/https%3A%2F%2Fi.scdn.co%2F.../image.png). Plocka ut den direkta
    # CDN-URL:en — den kan hämtas direkt av vilken klient som helst (publik HTTPS).
    raw_img = item.get('image', '')
    m = re.search(r'/imageproxy/([^/]+)/image', raw_img)
    image_cdn = urllib.parse.unquote(m.group(1)) if m else (raw_img if raw_img.startswith('http') else '')

    return {
        "name": title.strip(),
        "subtitle": subtitle.strip(),
        "uri": uri,
        "art": _abs_image(raw_img),
        "image_cdn": image_cdn,
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


def _init_lock_events():
    """Skapar lock_events-tabellen i play_history.db (delas med lms_logger).
    En rad per gång vakthunden dödar en spelare — forensik för 'varför dör saker'."""
    try:
        with sqlite3.connect(PLAY_DB) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS lock_events (
                    ts              INTEGER NOT NULL,
                    player          TEXT,
                    mac             TEXT,
                    model           TEXT,
                    event           TEXT,     -- 'lock' | 'skip' | 'shadow' | 'night' (kl 23) | 'reappear' | 'idle' (före 29 okt)
                    mode            TEXT,
                    froze_at        REAL,     -- elapsed-tid där den fastnade (0 = startade aldrig)
                    frozen_for      INTEGER,  -- sekunder utan framsteg innan vi dödade
                    playlist_tracks INTEGER,
                    cur_index       INTEGER,
                    signalstrength  INTEGER,
                    artist          TEXT,
                    title           TEXT,
                    url             TEXT,
                    source          TEXT      -- URL-schema: spotify / file / http ...
                )
            """)
    except Exception as e:
        logging.warning(f"[watchdog] kunde inte skapa lock_events: {e}")


_init_lock_events()


def _init_resolved_tracks():
    """Persistent (artist, title) → upplöst spår-id. Skrivs vid VARJE lyckad
    upplösning (inte bara vid play, som plays.spotify_uri). Den stabila
    spotify://-URI:n är permanent, så ingen TTL — bara invalidering vid play-fel."""
    try:
        with sqlite3.connect(PLAY_DB) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS resolved_tracks (
                    artist_norm TEXT NOT NULL,
                    title_norm  TEXT NOT NULL,
                    source      TEXT NOT NULL,   -- 'spotify' | 'lms'
                    track_id    TEXT NOT NULL,   -- spotify://track:XXX eller lms track_id
                    resolved_at INTEGER NOT NULL,
                    PRIMARY KEY (artist_norm, title_norm)
                )
            """)
    except Exception as e:
        logging.warning(f"[cache] kunde inte skapa resolved_tracks: {e}")


_init_resolved_tracks()


def _invalidate_resolved(artist: str, title: str):
    """Tar bort en cachad upplösning (self-healing: spåret gick inte att spela,
    så låt det re-resolvas nästa gång)."""
    a, t = (artist or "").strip().lower(), (title or "").strip().lower()
    if not (a and t):
        return
    try:
        with sqlite3.connect(PLAY_DB, timeout=5) as conn:
            conn.execute("DELETE FROM resolved_tracks WHERE artist_norm=? AND title_norm=?", (a, t))
    except Exception as e:
        logging.warning(f"[cache] invalidate fel: {e}")


def _record_lock_event(p, r, event, froze_at, frozen_for):
    """Fångar tillståndet vid ett vakthund-dödande: rik journal-rad + DB-rad.
    Anropas före stop/clear så vi ser vad som faktiskt hängde."""
    mac   = p.get('playerid', '')
    name  = p.get('name', mac)
    model = p.get('model', '')
    # Hämta rik spårinfo (URL behövs för att se om det alltid är spotify://).
    artist = title = url = ""
    try:
        rich = lms_json_rpc(mac, ["status", "-", 1, "tags:alu"])
        loop = (rich or {}).get("result", {}).get("playlist_loop", [])
        if loop:
            t = loop[0]
            artist, title, url = t.get("artist", ""), t.get("title", ""), t.get("url", "")
    except Exception:
        pass
    source = url.split(":", 1)[0] if "://" in url else (url.split(":", 1)[0] if ":" in url else "")
    mode   = r.get("mode")
    pl_tracks = r.get("playlist_tracks", 0)
    cur_index = r.get("playlist_cur_index", 0)
    signal    = r.get("signalstrength", 0)

    logging.info(
        f"[watchdog] LOCK-EVENT {name} ({model}) event={event} "
        f"froze_at={froze_at}s frozen_for={frozen_for}s source={source!r} "
        f"signal={signal} spår={pl_tracks}@{cur_index} «{artist} – {title}»"
    )
    try:
        with sqlite3.connect(PLAY_DB, timeout=5) as conn:
            conn.execute(
                "INSERT INTO lock_events (ts, player, mac, model, event, mode, froze_at, "
                "frozen_for, playlist_tracks, cur_index, signalstrength, artist, title, url, source) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (int(time.time()), name, mac, model, event, mode, froze_at, frozen_for,
                 pl_tracks, cur_index, signal, artist, title, url, source),
            )
    except Exception as e:
        logging.warning(f"[watchdog] kunde inte logga lock_event: {e}")


def _num(v, default=0.0) -> float:
    """LMS JSON-RPC returnerar ibland numeriska fält som strängar ("3", "12.5") —
    beroende på spelartyp. Jämförelser mellan str och tal kraschade vakthunden
    var 10:e s från 1 sep 2026 tills detta infördes."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _forget_player_state(mac):
    for d in (_last_elapsed, _last_track, _skip_recover, _trusted_pos):
        d.pop(mac, None)


def _flush_queue(p, r, event, why, now):
    """Stop + rensa kön. Klockan nollställs så att spelaren får färsk frist efteråt."""
    mac = p.get('playerid')
    logging.info(f"[watchdog] {p.get('name', mac)} ({mac}) {why}, "
                 f"{int(_num(r.get('playlist_tracks')))} spår — rensar kö + stop")
    _record_lock_event(p, r, event=event, froze_at=_num(r.get('time')),
                       frozen_for=int(now - _last_active_ts.get(mac, now)))
    lms_json_rpc(mac, ["stop"])
    lms_json_rpc(mac, ["playlist", "clear"])
    _last_active_ts[mac] = now
    _forget_player_state(mac)


def _on_reappear(p, r, away_s, now):
    """En spelare som varit borta har kopplat upp sig igen. Lyrion kan då återuppta den gamla
    kön, så den rensas, utom när kön är ett nytt val: Lyrions playlist_timestamp (när kön
    senast ändrades) ligger efter att spelaren försvann, oavsett varifrån valet kom (Lyrion-
    appen, :9000, Spotify Connect). En start via bryggan räknas också som ett val."""
    mac = p.get('playerid')
    if int(_num(r.get('playlist_tracks'))) <= 0:
        return
    since = now - away_s if away_s is not None else now - _OWN_START_GRACE_S
    if _num(r.get('playlist_timestamp')) >= since or now - _last_start_cmd.get(mac, 0) < _OWN_START_GRACE_S:
        return
    away = f"borta {away_s / 60:.0f} min" if away_s is not None else "ny sedan bryggan startade"
    how = "spelar den gamla kön av sig själv" if r.get('mode') == 'play' else f"läge {r.get('mode')!r}"
    _flush_queue(p, r, "reappear", f"tillbaka ({away}), {how}", now)


def _night_flush(players, statuses, now):
    """Kl _NIGHT_FLUSH_HOUR: rensa varje kö som inte spelar, så att inget väcks till liv i natt.
    Bortkopplade spelare som Lyrion fortfarande listar får också sin kö rensad."""
    n = 0
    for p in players:
        mac = p.get('playerid')
        if not mac:
            continue
        if mac in statuses:
            r = statuses[mac]
            if r.get('mode') == 'play' or int(_num(r.get('playlist_tracks'))) <= 0:
                continue
            _flush_queue(p, r, "night", f"kvällsrensning (läge {r.get('mode')!r})", now)
        else:
            logging.info(f"[watchdog] kvällsrensning: {p.get('name', mac)} bortkopplad — rensar kön")
            lms_json_rpc(mac, ["playlist", "clear"])
        n += 1
    logging.info(f"[watchdog] kvällsrensning klar: {n} köer rensade")


def _watchdog_tick():
    """Ett varv: döda spelare som inte kommer framåt (lås: mode 'play' men fryst, efter
    _LOCK_KILL_SECONDS), rensa köer som inte spelar kl _NIGHT_FLUSH_HOUR och spelare som
    kommer tillbaka efter att ha varit borta. Ingen återupplivning. Anropas av
    _watchdog_loop var _WATCHDOG_POLL:e sekund."""
    global _c5_last_playing, _night_flushed, _present, _watchdog_started
    now = time.time()
    seen = set()
    statuses = {}
    players = get_all_players()
    for p in players:
        mac = p.get('playerid')
        if not mac or str(p.get('connected', 1)) == '0':
            continue
        seen.add(mac)
        res = lms_json_rpc(mac, ["status", "-", 1, "tags:ud"])
        if not res or 'result' not in res:
            continue
        r = res['result']
        statuses[mac] = r
        if mac not in _present:
            gone = _gone_since.pop(mac, None)
            if gone is not None and now - gone >= _REAPPEAR_AFTER_S:
                _on_reappear(p, r, now - gone, now)
                continue
            if gone is None and _watchdog_started:
                _on_reappear(p, r, None, now)       # ny spelare sedan bryggan startade
                continue
        mode = r.get('mode')
        playlist_tracks = int(_num(r.get('playlist_tracks')))
        elapsed = _num(r.get('time'))
        name = p.get('name', mac)
        loop = r.get('playlist_loop', [])
        track_key = (loop[0].get('url') or loop[0].get('id')) if loop else None

        # Elapsed vid senast BEKRÄFTADE framsteg — inte förra varvet (13 okt 2026). Linn i
        # vardagsrummet (UPnP-bryggan, flow-läge) rapporterar tiden ryckvis: den står still
        # 3–9 s och hoppar sedan ikapp (uppmätt 61,8 → 64,9 → 65,0 … → 74,0). Jämfört varv
        # för varv gav det tre varv i rad under _MIN_PROGRESS_S mitt i en spelande låt, och
        # vakthunden hoppade till nästa (Luther Vandross vid 202 av 230 s, Heatwave vid 250
        # av 303 s m.fl.). Mot senaste framsteget räknas ikapphoppet; en krypning vid
        # låtslut (252 → 252,8) når aldrig upp till tröskeln och fångas som förut.
        prev_elapsed = _last_elapsed.get(mac)
        # Paus/stopp → play (återupptagen, samma spår) = färsk frist. Annars räknades
        # pausen som lås: 8 okt 2026 "C5 låst 541s" → stop→play + hopp en sekund efter
        # att man tryckt play.
        prev_mode = _last_mode.get(mac)
        _last_mode[mac] = mode
        resumed = mode == 'play' and prev_mode is not None and prev_mode != 'play'
        # Nytt spår laddat → färsk frist. Annars dödas en kallstartande ström direkt
        # om spelaren stått idle/pausad länge innan (stale _last_active_ts-klocka).
        prev_track = _last_track.get(mac)
        _last_track[mac] = track_key
        track_changed = track_key is not None and track_key != prev_track
        # C5 startad UTANFÖR bryggan (Lyrion-appen m.m.): starta startkontrollen här.
        # Väckning i förväg går inte — starten har redan skett. Naturliga spårbyten
        # under pågående spel räknas inte (C5 sågs spela nyss).
        if (mac == C5_MAC and mode == 'play' and (resumed or track_changed)
                and now - _c5_last_playing > 15):
            c5_start_guard(mac, "extern")

        # Framsteg = spelar OCH elapsed har ökat MÄRKBART (minst _MIN_PROGRESS_S) och ligger
        # inte förbi spårets slut. En backning, frysning eller krypning är inget framsteg.
        duration = _num(r.get('duration'))
        past_end = duration > 0 and elapsed > duration + _PAST_END_MARGIN_S
        making_progress = mode == 'play' and not past_end and (
            prev_elapsed is None or elapsed - prev_elapsed >= _MIN_PROGRESS_S)
        if (making_progress or prev_elapsed is None or elapsed < prev_elapsed
                or track_changed or resumed):
            _last_elapsed[mac] = elapsed          # ny baslinje (backning = sökning/nytt spår)

        # Trovärdig position: rimlig ökning (eller backning/nytt spår) flyttar den, ett hopp
        # längre än väggtiden gör det inte förrän nästa rimliga steg därifrån.
        tp = _trusted_pos.get(mac)
        if tp is None or track_changed or resumed or mode != 'play' or elapsed < tp[0]:
            _trusted_pos[mac] = (elapsed, now)
        elif elapsed - tp[0] < _MIN_PROGRESS_S:
            pass                                  # står still/kryper: positionen och dess tid står kvar
        elif elapsed - tp[0] <= (now - tp[1]) + _JUMP_TOLERANCE_S:
            _trusted_pos[mac] = (elapsed, now)
        elif _jump_seen.get(mac) is not None and elapsed - _jump_seen[mac][0] >= _MIN_PROGRESS_S:
            _trusted_pos[mac] = (elapsed, now)    # sökning: tiden går vidare efter hoppet
        if _trusted_pos[mac][1] == now or elapsed - _trusted_pos[mac][0] < _MIN_PROGRESS_S:
            _jump_seen.pop(mac, None)
        elif _jump_seen.get(mac) is None or elapsed < _jump_seen[mac][0]:
            _jump_seen[mac] = (elapsed, now)

        if mac in _shadow_since and (making_progress or track_changed or resumed or mode != 'play'):
            t0, what, el0, tr0 = _shadow_since.pop(mac)
            how = ("spårbyte" if track_changed else "framsteg" if making_progress
                   else "återupptagen" if resumed else f"läge {mode}")
            logging.info(f"[watchdog] SKUGGA {name}: löste sig själv efter {now - t0:.0f} s ({how}) "
                         f"— skulle ha gjort {what} vid {el0:.0f} s")
        if making_progress:
            _last_active_ts[mac] = now
            _skip_recover.pop(mac, None)
            if mac == C5_MAC:
                _c5_check_desync(name)
            continue
        if track_changed or resumed:
            _last_active_ts[mac] = now
            continue

        baseline = _last_active_ts.get(mac)
        if baseline is None:
            # Aldrig sedd aktiv ännu — starta klockan nu, agera inte retroaktivt.
            _last_active_ts[mac] = now
            continue

        if playlist_tracks <= 0:
            continue

        frozen_for = now - baseline
        # Lås (rapporterar 'play' men står still) är en trasig ström → döda snabbt.
        # Paus/stopp rörs inte här — se _night_flush och _on_reappear.
        is_lock = mode == 'play'
        if not is_lock:
            continue

        # C5: LMS-klockan står still, men det betyder inget förrän C5 själv bekräftar.
        # Säger C5 PLAYING spelar den — rör inget och starta om frysklockan. Annars
        # behandlas C5 som alla andra spelare nedan (hopp, sedan stop + rensa).
        if is_lock and mac == C5_MAC and c5_transport_state() == "PLAYING":
            _c5_last_playing = now
            _last_active_ts[mac] = now
            continue

        # HOPP-RÄDDNING (alla spelare; C5 bara när den själv sagt att den inte spelar): ett lås
        # med fler spår i kön är oftast EN hängd strömstart — typiskt Spotty-helpern
        # som aldrig levererar nästa spår (verkligt fall 2026-09-15: Goldfrapp spelade
        # klart, spotty-x86_64 för spår 2 hängde tyst i 75 s, vakthunden kastade 17
        # köade spår). Hoppa över det hängda spåret i stället; först när hoppen inte
        # ger framsteg faller vi till stop + rensa nedan. Hoppet byter url → track_changed
        # → färsk frist, så nästa spår får egen _LOCK_SKIP_SECONDS-frist.
        cur_index = int(_num(r.get('playlist_cur_index')))
        has_next  = cur_index < playlist_tracks - 1
        at_end = duration > 0 and elapsed >= duration - _AT_END_MARGIN_S
        skip_after = _END_STALL_SKIP_S if at_end else _LOCK_SKIP_SECONDS
        if at_end and is_lock:
            # Kan låten verkligen vara slut? Annars vänta ut den verkliga längden.
            t_el, t_ts = _trusted_pos.get(mac, (elapsed, now))
            real_end = t_ts + max(0.0, duration - t_el)
            if now < real_end + _END_STALL_SKIP_S:
                if not _false_end_logged.get(mac):
                    logging.info(f"[watchdog] {name} säger slut ({elapsed:.0f}/{duration:.0f} s) men "
                                 f"trovärdig position är {t_el + (now - t_ts):.0f} s — väntar ut låten")
                    _false_end_logged[mac] = True
                continue
        _false_end_logged.pop(mac, None)
        if is_lock and mac.lower() in _SHADOW_MACS and frozen_for >= min(skip_after, _LOCK_KILL_SECONDS):
            if mac not in _shadow_since:
                what = "hopp" if has_next else "stop + rensa"
                _shadow_since[mac] = (now - frozen_for, what, elapsed, track_key)
                logging.info(f"[watchdog] SKUGGA {name}: står still {int(frozen_for)}s vid {elapsed:.0f}/{duration:.0f} s "
                             f"på spår {cur_index + 1}/{playlist_tracks} — skulle ha gjort {what}, gör inget")
                _record_lock_event(p, r, event="shadow", froze_at=elapsed, frozen_for=int(frozen_for))
            continue
        if is_lock and has_next and frozen_for >= skip_after:
            n = _skip_recover.get(mac, 0)
            if n < _LOCK_SKIP_MAX:
                logging.info(
                    f"[watchdog] {name} låst {int(frozen_for)}s på spår {cur_index + 1}/{playlist_tracks} "
                    f"— hoppar till nästa (försök {n + 1}/{_LOCK_SKIP_MAX})"
                )
                _record_lock_event(p, r, event="skip", froze_at=elapsed, frozen_for=int(frozen_for))
                lms_json_rpc(mac, ["playlist", "index", "+1"])
                _skip_recover[mac] = n + 1
                _last_active_ts[mac] = now
                continue

        if frozen_for >= _LOCK_KILL_SECONDS:
            _flush_queue(p, r, "lock", f"låst ström i {int(frozen_for)}s", now)

    # Kvällsrensning en gång per dygn (även om bryggan startar efter klockslaget).
    today = datetime.datetime.fromtimestamp(now)
    if today.hour >= _NIGHT_FLUSH_HOUR and _night_flushed != today.date():
        _night_flushed = today.date()
        _night_flush(players, statuses, now)
    _watchdog_started = True

    # Spelare som försvunnit: notera när, så att återkomsten kan rensas.
    for mac in _present - seen:
        _gone_since.setdefault(mac, now)
    _present = seen

    # Glöm spelare som försvunnit (avstängda) så dictarna inte växer obegränsat.
    for mac in list(_last_active_ts.keys()):
        if mac not in seen:
            _last_active_ts.pop(mac, None)
            _last_elapsed.pop(mac, None)
            _last_track.pop(mac, None)
            _skip_recover.pop(mac, None)
            _trusted_pos.pop(mac, None)
            _jump_seen.pop(mac, None)
            _shadow_since.pop(mac, None)


def _watchdog_loop():
    time.sleep(_WATCHDOG_POLL)
    while True:
        try:
            _watchdog_tick()
        except Exception as e:
            logging.warning(f"[watchdog] fel: {e}")
        time.sleep(_WATCHDOG_POLL)


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
            logging.warning("[LastFM] %s fel %s: %s", method, data['error'], data.get('message', ''))
            return None
        return data
    except Exception as e:
        logging.warning("[LastFM] %s misslyckades: %s", method, e)
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
            logging.warning("[LastFM] %s API-fel %s: %s (totalt %dx)", method, data['error'], data.get('message', ''), _lastfm_fail_counts[method])
            return None
        return data
    except Exception as e:
        _lastfm_fail_counts[method] = _lastfm_fail_counts.get(method, 0) + 1
        logging.warning("[LastFM] %s misslyckades: %s (totalt %dx)", method, e, _lastfm_fail_counts[method])
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


# --- ROT + ENSTAKA FIL ---
# Multilyrion-PWA:n (static/index.html + app.js) togs bort sep 2026 — dashboarden
# på muzak:5015/dashboard gör samma sak bättre, och alla data-endpoints den använde
# används ändå av dashboarden. Bryggan är sedan dess ett rent API.

@app.route('/')
def serve_index():
    return ("LMS-bryggan — API, inget UI här.\n"
            "Musikgränssnittet finns på http://muzak.local:5015/dashboard\n", 200,
            {"Content-Type": "text/plain; charset=utf-8"})

@app.route('/edgar-ca.crt')
def serve_ca():
    """Rot-certifikatet för handpåläggning på nya enheter. Hade tidigare bara en
    URL via den generella static-routen, som försvann med PWA:n."""
    return send_from_directory('static', 'edgar-ca.crt')


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

    # --- SPELLISTOR: i spellistans ordning (ingen shuffle sedan 13 okt) ---
    elif "spotify:playlist:" in clean_url:
        res = lms_play_playlist(player_mac, clean_url)
        _mark_queue_origin(player_mac, "spotify_playlist")

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
                    'art':    _cover_url(cover_id)
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
                'art':    _cover_url(cover_id)
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
    _mark_queue_origin(player_mac, "daily_mix")   # Spotify valde spåren — inte 'manual'
    _refresh_mix_labels_async()   # har mixen bytts syns det när låten börjar
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

def _set_player_volume(player_mac, level):
    """Sätter volymen på en spelare. C5:an har en EGEN volym vid sidan av Lyrions
    och måste sättas över UPnP också — styrt på MAC, inte namn (spelaren döptes om
    från "C5" till "Köket" och namnstyrningen slutade då fungera tyst)."""
    level_str = str(level).strip()
    if player_mac == C5_MAC:
        # C5:ans UPnP tar bara absoluta nivåer och tolkar "+10" som 10: "höj volymen" i
        # köket sänkte C5:an till 10 (uppmätt 12 okt 2026, "+1" → 1). Räkna fram nivån
        # ur Lyrions volym och sätt BÅDA absolut.
        if level_str.startswith(('+', '-')):
            res = lms_json_rpc(player_mac, ["mixer", "volume", "?"])
            try:
                current = int(float(res['result']['_volume']))
            except (KeyError, TypeError, ValueError):
                current = 30
            level_str = str(max(0, min(100, current + int(level_str))))
        set_c5_volume_upnp(level_str)
    if level_str.startswith(('+', '-')):
        lms_json_rpc(player_mac, ["mixer", "volume", level_str])
    else:
        lms_json_rpc(player_mac, ["mixer", "volume", int(level_str)])
    return level_str


@app.route('/set_volume')
def set_volume():
    level = request.args.get('level', '30')
    player_mac, room_name = get_player_info(request.args.get('room'))
    if not player_mac:
        return "Error", 404
    return _set_player_volume(player_mac, level)

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

# ── Transport: EN uppsättning regler för alla vägar (12 okt 2026) ─────────────
# Edgar, dashboarden, köksvredet, Pi:ns F3 och menyradsappen hade var sina tolkningar:
# "paus" var en växling som startade en pausad spelare, och Edgars "spela" på en
# spelande spelare pausade den. Nu:
#   toggle — växlar (bara för knappar som SER ut som play/paus)
#   pause  — pausar en spelande spelare, startar ALDRIG något
#   play   — återupptar en pausad / startar en stoppad, pausar ALDRIG
#   stop   — stoppar
# Utan rum: pause/stop gäller alla spelare som spelar (stop även pausade); förut tog
# get_player_info(None) den FÖRSTA spelaren i LMS-listan, oavsett om den spelade.
# Rösten har en egen regel ovanpå (ett tyst rum → de som spelar) i Edgars
# tool_music_control, eftersom modellen ofta lägger till rum som aldrig sagts.

def _player_mode(mac) -> str | None:
    res = lms_json_rpc(mac, ["mode", "?"], _raw=True)
    try:
        return res['result']['_mode']
    except (KeyError, TypeError):
        return None


def transport(mac, action) -> str | None:
    """Kör en transporthandling enligt reglerna ovan. Returnerar läget efteråt-ish
    ('play'/'pause'/'stop') eller None om inget gjordes."""
    if action == "toggle":
        lms_json_rpc(mac, ["pause"])
        return "toggle"
    if action == "stop":
        lms_json_rpc(mac, ["stop"])
        return "stop"
    mode = _player_mode(mac)
    if action == "pause":
        if mode != "play":
            return None                      # pausad/stoppad: rör inte (växling hade startat)
        lms_json_rpc(mac, ["pause", "1"])
        return "pause"
    if action == "play":
        if mode == "play":
            return None                      # spelar redan: en växling hade pausat
        lms_json_rpc(mac, ["pause", "0"] if mode == "pause" else ["play"])
        return "play"
    raise ValueError(f"okänd transporthandling: {action}")


def _active_macs(include_paused: bool) -> list[tuple[str, str]]:
    out = []
    for p in get_all_players():
        mac = p.get('playerid')
        if mac and _player_mode(mac) in (("play", "pause") if include_paused else ("play",)):
            out.append((mac, p.get('name')))
    return out


def _transport_route(action):
    room = (request.args.get('room') or "").strip()
    if not room and action in ("pause", "stop"):
        targets = _active_macs(include_paused=(action == "stop"))
    else:
        mac, name = get_player_info(room)
        if not mac:
            return jsonify({"error": f"okänt rum: {room}"}), 404
        targets = [(mac, name)]
    acted = [name for mac, name in targets if transport(mac, action)]
    logging.info(f"[transport] {action} {room or '(alla aktiva)'} → {acted or 'inget att göra'}")
    return jsonify({"action": action, "acted": acted})


@app.route('/stop')
def stop_playback():
    return _transport_route("stop")

@app.route('/pause')
def pause_playback():
    return _transport_route("pause")

@app.route('/play')
def play_playback():
    return _transport_route("play")

@app.route('/toggle_play_pause')
def toggle_playback():
    return _transport_route("toggle")

@app.route('/next')
def next_track():
    player_mac, _ = get_player_info(request.args.get('room'))
    if not player_mac:
        return "Error", 404
    lms_json_rpc(player_mac, ["playlist", "index", "+1"])
    return "OK"

@app.route('/imgproxy')
def imgproxy():
    """Proxar en LMS-bild (sökväg måste börja med /) så webbklienter aldrig behöver
    nå LMS:ens :9000 direkt. Begränsad till LMS_HTTP → ingen öppen proxy."""
    from flask import Response
    p = urllib.parse.unquote(request.args.get('p', ''))
    if not p.startswith('/'):
        return "", 400
    try:
        r = _session.get(f"{LMS_HTTP}{p}", timeout=6)
    except Exception:
        return "", 502
    if not r.ok:
        return "", 404
    return Response(
        r.content,
        content_type=r.headers.get('Content-Type', 'image/jpeg'),
        headers={'Cache-Control': 'public, max-age=86400'},
    )

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
    res = lms_json_rpc(None, ["albums", 0, 40, "sort:random", "tags:albj"])
    albums = []
    if res and 'result' in res:
        for item in res['result'].get('albums_loop', []):
            # BARA artwork_track_id ger äkta omslag; album-id/imageproxy ger en fast
            # platshållare för ALLA. Saknas artwork_track_id → inget omslag (glyf).
            cover_id = item.get('artwork_track_id')
            albums.append({
                'id':       item.get('id'),
                'title':    item.get('album'),
                'artist':   item.get('artist'),
                'cover_id': cover_id or "",
                'art':      _cover_url(cover_id) if cover_id else ""
            })
    return jsonify(albums)

@app.route('/get_daily_mixes')
def get_daily_mixes():
    # AI-summeringar (samma som knappens label men längre) cachade i mix_labels. En summering
    # visas bara om den skrevs för mixens NUVARANDE innehåll — annars None (skärmarna faller
    # tillbaka på description) och en omgörning startas i bakgrunden.
    try:
        rows = _mix_label_rows()
    except Exception:
        rows = {}

    raw = _fetch_daily_mixes_raw()
    mixes, changed = [], False
    for item in raw:
        parts = item.get('text', '').split('\n')
        title = parts[0]
        if not any(x in title for x in ["Mix", "Radar", "Discovery", "daylist"]):
            continue
        raw_id = item.get('params', {}).get('item_id') or item.get('id', '0.0')
        raw_img = item.get('icon') or item.get('image', '')
        mid = raw_id.split('.')[-1]
        ai_summary, updated = None, None
        idx = _daily_mix_index(title)
        if idx is not None:
            cached = rows.get(idx)
            if cached and cached[2] == _mix_sig(item):
                ai_summary, updated = cached[1], cached[3]
            else:
                changed = True
        mixes.append({
            'id':          mid,
            'title':       title,
            'description': parts[1] if len(parts) > 1 else "Din personliga mix",
            'ai_summary':  ai_summary or None,
            'art':         _abs_image(raw_img),
            'image_cdn':   _image_cdn(raw_img),
            'updated':     updated,   # unix-tid då ai_summary skrevs (None = saknas/gammal)
        })
    if changed:
        _refresh_mix_labels_async(raw)
    return jsonify(mixes)


_ERA_RE   = __import__('re').compile(r"^\d{2,4}s$", __import__('re').I)
_GENRE_STEMS = {
    "pop", "rock", "jazz", "soul", "hip hop", "hip-hop", "electronic", "dance",
    "house", "techno", "metal", "punk", "folk", "country", "indie", "alternative",
    "grunge", "synthpop", "new wave", "workout", "mellow", "nu metal", "screamo",
    "classic rock", "art pop", "soft pop", "indie pop", "alternative rock",
    "180 bpm", "workout pop", "mellow rock", "focus techno", "walking workout",
    "chill upbeat", "chill", "happy", "upbeat", "sad", "focus", "party", "r&b",
}

def _classify_mix(title: str) -> str:
    t = title.strip()
    tl = t.lower()
    if tl.startswith("daily mix"):
        return "daily_mix"
    if "radar" in tl:
        return "radar"
    if "daylist" in tl:
        return "daylist"
    stem = tl.removesuffix(" mix").strip()
    if _ERA_RE.match(stem):
        return "era_mix"
    if stem in _GENRE_STEMS or any(g in stem for g in _GENRE_STEMS):
        return "genre_mix"
    return "artist_mix"


def _pivot_artist(title: str) -> str | None:
    """Extraherar artistnamn ur 'Artist Mix'-titel."""
    if _classify_mix(title) != "artist_mix":
        return None
    return title.removesuffix(" Mix").strip()


@app.route('/get_all_mixes')
def get_all_mixes():
    """Alla Spotty-spellistor med full item_id och typ-klassificering."""
    mixes = []
    for item in _fetch_daily_mixes_raw():
        parts = item.get('text', '').split('\n')
        title = parts[0]
        if not any(x in title for x in ["Mix", "Radar", "daylist"]):
            continue
        raw_id = item.get('params', {}).get('item_id') or item.get('id', '0.0')
        mixes.append({
            'item_id':      raw_id,
            'id':           raw_id.split('.')[-1],
            'title':        title,
            'description':  parts[1] if len(parts) > 1 else "",
            'art':          _abs_image(item.get('icon') or item.get('image', '')),
            'mix_type':     _classify_mix(title),
            'pivot_artist': _pivot_artist(title),
        })
    return jsonify(mixes)


@app.route('/get_playlist_tracks')
def get_playlist_tracks():
    """Hämtar spår ur en Spotty-spellista via item_id."""
    item_id = request.args.get('item_id', '').strip()
    limit   = min(int(request.args.get('limit', 30)), 50)
    if not item_id:
        return jsonify([])
    player_mac = _any_player_mac()
    res = lms_json_rpc(player_mac, ["spotty", "items", 0, limit, f"item_id:{item_id}"], timeout=35)
    if not res or 'result' not in res:
        return jsonify([])
    items = res['result'].get('loop_loop', [])
    return jsonify([_format_track(it) for it in items if it.get('isaudio') == 1])


@app.route('/get_daily_mixes_knob')
def get_daily_mixes_knob():
    """Textlista för knappen: title|label|index, en per rad, bara Daily Mix 1-6.
    label är AI-genererad stämningssummering (1-2 ord); fallback på första artisten."""
    rows = _mix_label_rows()

    raw = _fetch_daily_mixes_raw()
    lines, changed = [], False
    for item in raw:
        parts = item.get('text', '').split('\n')
        title = parts[0]
        idx = _daily_mix_index(title)
        if idx is None:
            continue
        cached = rows.get(idx)
        if cached and cached[0] and cached[2] == _mix_sig(item):
            label = cached[0]
        else:
            changed = True
            # Fallback: första artisten ur beskrivningstexten
            desc = parts[1] if len(parts) > 1 else ''
            label = desc.split(',')[0].strip() if desc else title
        lines.append(f"{title}|{label}|{idx}")
    if changed:
        _refresh_mix_labels_async(raw)
    return '\n'.join(lines), 200, {'Content-Type': 'text/plain; charset=utf-8'}

_BUTTONS_FILE    = Path(__file__).parent / "buttons.json"
_BUTTON_REPEAT_S = 20      # samma musikval i samma rum inom N s = dubbeltryck, ignoreras
_button_last: dict[tuple[str, str, str], float] = {}
_button_lock = threading.Lock()


def _button_action(room: str, btn_id: str, press: str):
    """Slår upp handlingen i buttons.json: rummets egen karta, annars "default"."""
    cfg = json.loads(_BUTTONS_FILE.read_text())
    mapping = cfg.get(room) or cfg.get("default") or {}
    return (mapping.get(btn_id) or {}).get(press)


def _run_button_action(action, room: str, player_mac: str):
    """play_pause | next | {"daily": N} | {"warm": "önskemål"} | {"prompt": "Edgar-prompt"}"""
    if action == "play_pause":
        transport(player_mac, "toggle")
    elif action == "next":
        lms_json_rpc(player_mac, ["playlist", "index", "+1"])
    elif isinstance(action, dict) and "daily" in action:
        lms_play_stream(player_mac, ["spotty", "playlist", "play", f"item_id:playlists.{int(action['daily'])}"])
        _mark_queue_origin(player_mac, "daily_mix")
        _refresh_mix_labels_async()
    elif isinstance(action, dict) and "warm" in action:
        # Bästa FÖRVÄRMDA DJ-listan för önskemålet — Edgar svarar direkt, spelar i bakgrunden
        requests.post(f"{EDGAR_URL}/api/warm_button", json={"query": action["warm"], "room": room}, timeout=5)
    elif isinstance(action, dict) and "prompt" in action:
        requests.post(f"{EDGAR_URL}/api/chat", timeout=60, json={
            "message": action["prompt"], "client_id": f"remote_{room}", "default_room": room})
    else:
        logging.warning(f"[button] okänd handling: {action!r}")


@app.route('/button')
def button():
    """Fysiska knappar utan skärm (köksvredet). Enheten skickar bara VILKEN knapp och
    HUR den trycktes — vad det betyder står i buttons.json, så ändringar kräver ingen
    omflashning. ?room=c5&id=1&press=short|double|long. Svarar direkt."""
    room   = request.args.get('room', '').strip()
    btn_id = request.args.get('id', '').strip()
    press  = request.args.get('press', 'short').strip()

    try:
        action = _button_action(room, btn_id, press)
    except Exception:
        return jsonify({"error": "buttons.json saknas eller ogiltig"}), 500
    if not action:
        logging.info(f"[button] {room} knapp {btn_id} {press}: ingen handling")
        return jsonify({"status": "unmapped", "room": room, "button": btn_id, "press": press})

    player_mac, _ = get_player_info(room)
    if not player_mac:
        return "Error", 404

    # Musikval (allt utom play/paus och nästa): enheten saknar återkoppling, så ett
    # andra tryck medan listan laddas är vanligt och skulle starta en till.
    if isinstance(action, dict):
        now = time.time()
        with _button_lock:
            if now - _button_last.get((room, btn_id, press), 0) < _BUTTON_REPEAT_S:
                logging.info(f"[button] {room} knapp {btn_id} {press}: dubbeltryck ignorerat")
                return jsonify({"status": "ignored", "room": room, "button": btn_id, "press": press})
            _button_last[(room, btn_id, press)] = now

    logging.info(f"[button] {room} knapp {btn_id} {press}: {action!r}")

    def _run():
        try:
            _run_button_action(action, room, player_mac)
        except Exception as e:
            logging.warning(f"[button] {room} knapp {btn_id} {press} misslyckades: {e}")

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"status": "ok", "room": room, "button": btn_id, "press": press, "action": action})


@app.route('/get_radio_favorites')
def get_radio_favorites():
    res = lms_json_rpc("", ["favorites", "items", "0", "50"])
    stations = []

    if res and 'result' in res:
        for item in res['result'].get('loop_loop', []):
            fav_id = item.get('id')
            if not fav_id:
                continue
            raw_img = item.get('image') or item.get('icon', '')
            m = re.search(r'/imageproxy/([^/]+)/image', raw_img)
            image_cdn = urllib.parse.unquote(m.group(1)) if m else (raw_img if raw_img.startswith('http') else '')
            art = _abs_image(raw_img) or "https://via.placeholder.com/300x300/111/444?text=Radio"
            stations.append({
                'id':        fav_id,
                'name':      item.get('name', 'Okänd kanal'),
                'url':       fav_id,
                'art':       art,
                'image_cdn': image_cdn,
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

    # Volymen normaliseras FÖRE uppspelningen: rummen har olika och ojämna skalor
    # (volume_cal, uppmätt på gehör 12 okt 2026), och mottagarrummet kan stå högt
    # sedan sist — sätts nivån efteråt hinner det smälla till. Omätta rum ger None
    # och lämnas i fred. Här, i bryggan, eftersom ALLA flyttar passerar den här vägen.
    # Telefonen (Edgar-appen) spelar på 100 och styrs av telefonens systemvolym — dess
    # volym får aldrig räknas om till ett rum. Se volume_cal.transfer_volume.
    from_phone = _is_phone(from_mac)
    to_phone   = _is_phone(to_mac)
    to_cur = None
    if from_phone:
        cur = lms_json_rpc(to_mac, ["mixer", "volume", "?"])
        try:
            to_cur = cur['result']['_volume']
        except (KeyError, TypeError):
            pass
    tgt_vol = volume_cal.transfer_volume(from_mac, to_mac, r.get('mixer volume'), to_cur,
                                         from_is_phone=from_phone, to_is_phone=to_phone)

    if to_mac == C5_MAC:
        lms_json_rpc(to_mac, ["power", 1])
    if tgt_vol is not None:
        _set_player_volume(to_mac, tgt_vol)     # sätter även C5:ans egen volym
        logging.info(f"[transfer] volym {r.get('mixer volume')} → {tgt_vol} ({from_mac} → {to_mac}"
                     f"{', från telefonen, mål stod på ' + str(to_cur) if from_phone else ''}"
                     f"{', till telefonen' if to_phone else ''})")
    elif to_mac == C5_MAC:
        set_c5_volume_upnp(20)                  # gammalt beteende när rummet saknar mätning
    if to_mac == C5_MAC:
        time.sleep(2.0)

    lms_json_rpc(to_mac, ["playlist", "play", current_url])
    time.sleep(0.8)
    # C5 (UPnP) klarar INTE att söka in i en färsk Spotty-ström → den fastnar i en
    # om-seek-loop (t.ex. 0:36→0:39→0:36). Starta spåret från början på C5; söker
    # bara på spelare som faktiskt hanterar det.
    if to_mac != C5_MAC:
        lms_json_rpc(to_mac, ["time", cur_time])

    for track in playlist[cur_index + 1:]:
        url = track.get('url', '')
        if url:
            lms_json_rpc(to_mac, ["playlist", "add", url])

    lms_json_rpc(from_mac, ["pause", 1])
    # C5 kan behöva knuffas igång — samma eskalerande stop→play som övriga
    # uppspelningsvägar, annars hänger öppnaren tills vakthunden dödar den.
    if to_mac == C5_MAC:
        _deferred_kick(to_mac)
    return "OK"


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
        with _spotty_sem:
            initial = lms_json_rpc(player_mac, [
                "spotty", "items", 0, 50,
                "item_id:1.0",
                f"search:{query}",
            ], timeout=10)
        if initial and 'result' in initial:
            loop = initial['result'].get('loop_loop', [])
            # Korsprat-vakt: ett svar där INGEN träff delar ett ord med frågan är
            # inte ett sökresultat utan någon annan meny (typiskt ett albums spår-
            # lista). Kasta och försök igen — och cacha aldrig ett sådant svar.
            if loop and not _search_matches_query(query, loop):
                logging.warning(f"[spotify_search] korsprat för {query!r}: {loop[0].get('name','')[:40]!r} — försöker igen")
                loop = []
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


@app.route('/resolve_player')
def resolve_player_endpoint():
    """Löser upp ett rumsnamn eller MAC-adress till spelaren's playerid och namn.
    ?name=vardagsrum → {"playerid": "aa:bb:...", "name": "Vardagsrum"}
    Utan ?name returneras första spelaren (fallback — aktiv-spelare-logik sitter i edgar).
    """
    name = request.args.get('name', '').strip() or None
    mac, player_name = get_player_info(name)
    if mac:
        return jsonify({"playerid": mac, "name": player_name})
    return jsonify({"error": f"Hittade inte spelaren '{name}'"})



def _resolve_spotify_artist_id(player_mac, query, timeout=35):
    """Söker en artist via Spotty och borrar ner till dess stabila artist-item_id.
    Returnerar (artist_id, None) vid träff, annars (None, (response, status)) som
    anroparen kan returnera direkt. Delas av spotify_artist_top/_radio."""
    search_res = lms_json_rpc(player_mac, ["spotty", "items", 0, 5, "item_id:1.0", f"search:{query}"], timeout=timeout)
    if not search_res or 'result' not in search_res:
        return None, (jsonify({"error": "Sökning misslyckades"}), 500)

    loop = search_res['result'].get('loop_loop', [])
    artist_cat = next((it for it in loop if "Artists" in it.get('name', '')), None)
    if not artist_cat:
        return None, (jsonify({"error": "Ingen artist-kategori hittades"}), 404)

    artists = lms_json_rpc(player_mac, ["spotty", "items", 0, 1, f"item_id:{artist_cat['id']}"], timeout=timeout)
    if not artists or not artists.get('result', {}).get('loop_loop'):
        return None, (jsonify({"error": "Artisten hittades inte"}), 404)

    return artists['result']['loop_loop'][0]['id'], None


@app.route('/spotify_artist_top')
def spotify_artist_top():
    """Hämtar populäraste låtarna för en artist via Spotty."""
    query = request.args.get('q', '').strip()
    player_mac, _ = get_player_info(request.args.get('room'))
    if not query:
        return jsonify({"error": "Missing q"}), 400

    _spotty = 35
    artist_id, err = _resolve_spotify_artist_id(player_mac, query, _spotty)
    if err:
        return err

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

# ── Artistens katalog (Edgars titelrättning, 14 okt 2026) ─────────────────────
# Toppspåren gav titelrättningen bara hits: en påhittad titel blev artistens mest spelade
# låt ("Jim Tomlinson – So Maco" → "So Nice"). Upptäckter ska inte styras av Spotifys
# popularitet (användaren), så katalogen tar också albumen och singlarna. Ett anrop per
# album → svaret sparas i ett dygn, och Spotty får högst _spotty_sem frågor åt gången.
_catalog_cache: dict = {}
_CATALOG_TTL = 86400
_TRACK_NO_RE = re.compile(r"^\d+\.\s+")
# Livealbum, liveversioner och remixer är dåliga ersättare för en studiolåt (Depeche Modes
# åtta senaste "album" var livealbum, och "Remixes 81>04" kom med).
_LIVE_RE = re.compile(r"\blive\b|\bunplugged\b|in concert|\bremix(es)?\b|\bmixes\b", re.I)


def _spread(items, n):
    """n poster jämnt fördelade över listan (hela karriären, inte bara de senaste)."""
    if len(items) <= n:
        return items
    step = len(items) / n
    return [items[int(i * step)] for i in range(n)]


def _spotty_items(player_mac, item_id, limit, timeout=35):
    with _spotty_sem:
        res = lms_json_rpc(player_mac, ["spotty", "items", 0, limit, f"item_id:{item_id}"], timeout=timeout)
    return res.get('result', {}).get('loop_loop', []) if res else []


@app.route('/spotify_artist_catalog')
def spotify_artist_catalog():
    """Artistens låtar: toppspår + album (högst ?albums=8) + singlar/EP (högst ?singles=6),
    i formatet från /spotify_artist_top ({name, subtitle, …}), utan dubbletter. Livealbum
    och liveversioner hoppas över, och albumen sprids över hela karriären."""
    query = request.args.get('q', '').strip()
    if not query:
        return jsonify({"error": "Missing q"}), 400
    n_albums = min(int(request.args.get('albums', 8)), 20)
    n_singles = min(int(request.args.get('singles', 6)), 20)
    key = (query.lower(), n_albums, n_singles)
    hit = _catalog_cache.get(key)
    if hit and time.time() - hit[0] < _CATALOG_TTL:
        return jsonify(hit[1])
    player_mac, _ = get_player_info(request.args.get('room'))
    with _spotty_sem:
        artist_id, err = _resolve_spotify_artist_id(player_mac, query, 35)
    if err:
        return err
    menu = _spotty_items(player_mac, artist_id, 10)
    by_name = {it.get('name', ''): it for it in menu}
    out, seen = [], set()

    def add(items):
        for it in items:
            if not it.get('isaudio'):   # även låtar har hasitems=1 i Spotty — läs bara låtlistor hit
                continue
            t = _format_track(dict(it, name=_TRACK_NO_RE.sub("", it.get('name', ''))))
            k = t['name'].lower()
            if t['name'] and k not in seen and not _LIVE_RE.search(t['name']):
                seen.add(k)
                out.append(t)

    top = next((it for n, it in by_name.items() if any(w in n for w in ("Top", "Populär", "Popular"))), None)
    if top:
        add(_spotty_items(player_mac, top['id'], 10))
    for label, n in (("Albums", n_albums), ("Singles", n_singles)):
        sec = next((it for nm, it in by_name.items() if nm.startswith(label)), None)
        if not sec or not n:
            continue
        albums = [al for al in _spotty_items(player_mac, sec['id'], 100)
                  if not _LIVE_RE.search((al.get('name') or '').split(' by ')[0])]
        for alb in _spread(albums, n):
            add(_spotty_items(player_mac, alb['id'], 50))
    _catalog_cache[key] = (time.time(), out)
    return jsonify(out)


# ── Spotify-album (dashboardens sök, 13 okt 2026) ─────────────────────────────
# Spotty ger inga stabila album-URI:er, bara positionsberoende item_id
# (1.0_<sökterm>.1.N). Albumet identifieras därför med titel + artist, och varje
# uppspelning gör en FÄRSK sökning och kontrollerar namnet innan den spelar — ett
# cachat id kan inte spela fel album om Spotify ändrat ordningen.

def _album_norm(s):
    return re.sub(r"\W+", " ", (s or "").lower()).strip()


def _find_spotify_album(player_mac, title, artist):
    """Färsk Spotty-sökning på "<titel> <artist>" → (item_id, Spottys albumtitel) för
    albumet vars namn stämmer, annars (None, None)."""
    query = f"{title} {artist}".strip()
    encoded = urllib.parse.quote(query)
    with _spotty_sem:
        lms_json_rpc(player_mac, ["spotty", "items", 0, 1, "item_id:1.0", f"search:{query}"], timeout=35)
        sub = lms_json_rpc(player_mac, ["spotty", "items", 0, 20, f"item_id:1.0_{encoded}.1"], timeout=35)
    want_t, want_a = _album_norm(title), _album_norm(artist)
    for it in (sub or {}).get('result', {}).get('loop_loop', []):
        name = it.get('name', '')
        t, _, a = name.rpartition(' by ')
        if not t:
            t, a = name, ""
        if _album_norm(t) == want_t and (not want_a or want_a in _album_norm(a) or _album_norm(a) in want_a):
            return it.get('id'), t
    return None, None


@app.route('/spotify_album_tracks')
def spotify_album_tracks():
    """Låtarna i ett Spotify-album: title=, artist=. [{title, artist}] i albumets ordning."""
    title, artist = request.args.get('title', '').strip(), request.args.get('artist', '').strip()
    if not title:
        return jsonify({"error": "Missing title"}), 400
    player_mac = _any_player_mac()
    item_id, spotty_title = _find_spotify_album(player_mac, title, artist)
    if not item_id:
        return jsonify({"error": "Albumet hittades inte på Spotify"}), 404
    with _spotty_sem:
        res = lms_json_rpc(player_mac, ["spotty", "items", 0, 100, f"item_id:{item_id}"], timeout=35)
    tracks = []
    suffix = f" from {spotty_title}".lower()   # Spottys stavning ("Songs From The Big Chair")
    for it in (res or {}).get('result', {}).get('loop_loop', []):
        if not it.get('isaudio'):
            continue
        # "3. Appetite - 2007 Remastered Version by Prefab Sprout from Steve McQueen"
        name = re.sub(r"^\d+\.\s*", "", it.get('name', ''))
        if name.lower().endswith(suffix):
            name = name[:-len(suffix)]
        t, _, a = name.rpartition(' by ')
        tracks.append({"title": (t or name).strip(), "artist": (a if t else "").strip()})
    return jsonify(tracks)


@app.route('/play_spotify_album')
def play_spotify_album():
    """Spelar ett Spotify-album: title=, artist=, room=. Färsk sökning + namnkontroll."""
    title, artist = request.args.get('title', '').strip(), request.args.get('artist', '').strip()
    player_mac, _ = get_player_info(request.args.get('room'))
    if not (player_mac and title):
        return jsonify({"error": "Missing title or room"}), 400
    item_id, _ = _find_spotify_album(player_mac, title, artist)
    if not item_id:
        return jsonify({"error": "Albumet hittades inte på Spotify"}), 404
    lms_play_stream(player_mac, ["spotty", "playlist", "play", f"item_id:{item_id}"])
    return jsonify({"status": "ok", "item_id": item_id})


@app.route('/spotify_artist_radio')
def spotify_artist_radio():
    """Returnerar Artist Radio-item-ID för en artist via Spotty (Spotifys egna radioalgoritm)."""
    query = request.args.get('q', '').strip()
    player_mac, _ = get_player_info(request.args.get('room'))
    if not query:
        return jsonify({"error": "Missing q"}), 400

    _spotty = 35  # Spotty-anrop kan ta lång tid när Spotify API är trög
    artist_id, err = _resolve_spotify_artist_id(player_mac, query, _spotty)
    if err:
        return err

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


@app.route('/lastfm_artist_tags')
def lastfm_artist_tags():
    """Toptaggar för en artist via Last.fm artist.getTopTags."""
    artist = request.args.get('artist', '').strip()
    if not artist:
        return jsonify({"error": "artist saknas"}), 400
    limit = min(int(request.args.get('limit', '5')), 10)
    cached = _search_cache_get(artist, "lastfm_artist_tags", limit)
    if cached is not None:
        return jsonify({"artist": artist, "tags": cached, "cached": True})
    resp = _lastfm_get("artist.getTopTags", artist=artist, autocorrect=1)
    if not resp or "toptags" not in resp:
        return jsonify({"artist": artist, "tags": []})
    tags = [
        {"tag": t.get("name", "").lower(), "count": int(t.get("count", 0))}
        for t in resp["toptags"].get("tag", [])[:limit]
        if t.get("name") and int(t.get("count", 0)) > 5
    ]
    _search_cache_set(artist, "lastfm_artist_tags", limit, tags)
    return jsonify({"artist": artist, "tags": tags})



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
        logging.info("[LastFM] lastfm_recommendations: ingen smakprofil tillgänglig — returnerar tom data")
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



@app.route('/play_history_data')
def play_history_data_endpoint():
    """Returnerar komplett plays-data för history-sidan."""
    import sqlite3 as _sqlite3, time as _time
    db_path = PLAY_DB
    since_14 = int(_time.time()) - 14 * 86400
    since_30 = int(_time.time()) - 30 * 86400
    with _sqlite3.connect(db_path) as conn:
        conn.row_factory = _sqlite3.Row
        plays = [dict(r) for r in conn.execute(
            "SELECT id, ts, player, artist, title, source, energy, valence, danceability, tempo, skipped "
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


@app.route('/artist_plays')
def artist_plays_endpoint():
    """Alla spel för en given artist (case-insensitiv exakt match på artistnamn).
    Returnerar enskilda rader (med id för riktad radering) + en sammanfattning
    (antal, första/senaste spel). Används av history-sidans artist-uppslagning."""
    import sqlite3 as _sqlite3
    artist = (request.args.get("artist") or "").strip()
    if not artist:
        return jsonify({"error": "artist krävs"}), 400
    db_path = PLAY_DB
    with _sqlite3.connect(db_path) as conn:
        conn.row_factory = _sqlite3.Row
        rows = [dict(r) for r in conn.execute(
            "SELECT id, ts, player, artist, title, album, source, skipped, origin "
            "FROM plays WHERE lower(artist) = lower(?) ORDER BY ts DESC",
            (artist,)
        ).fetchall()]
        summ = conn.execute(
            "SELECT COUNT(*) n, MIN(ts) first_ts, MAX(ts) last_ts "
            "FROM plays WHERE lower(artist) = lower(?)",
            (artist,)
        ).fetchone()
    return jsonify(
        artist=artist,
        plays=rows,
        count=summ[0],
        first_ts=summ[1],
        last_ts=summ[2],
    )


@app.route('/delete_plays', methods=['POST'])
def delete_plays_endpoint():
    """Raderar spel ur historiken. Body antingen {artist: "..."} (alla spel för
    artisten, case-insensitivt) eller {ids: [1,2,...]} (specifika rader).
    Returnerar {ok, deleted}. Oåterkalleligt — frontend bekräftar före anrop."""
    import sqlite3 as _sqlite3
    data   = request.get_json(silent=True) or {}
    artist = (data.get("artist") or "").strip()
    ids    = data.get("ids") or []
    if not artist and not ids:
        return jsonify({"error": "artist eller ids krävs"}), 400
    db_path = PLAY_DB
    try:
        with _sqlite3.connect(db_path, timeout=5) as conn:
            if artist:
                cur = conn.execute(
                    "DELETE FROM plays WHERE lower(artist) = lower(?)", (artist,))
            else:
                ids = [int(i) for i in ids]
                ph = ",".join("?" * len(ids))
                cur = conn.execute(
                    f"DELETE FROM plays WHERE id IN ({ph})", ids)
            deleted = cur.rowcount
        logging.info("[history] raderade %d spel (%s)",
                     deleted, f"artist={artist}" if artist else f"{len(ids)} id:n")
        return jsonify({"ok": True, "deleted": deleted})
    except Exception as e:
        logging.warning("[history] delete_plays fel: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route('/resolved_uris')
def resolved_uris():
    """Returnerar upplösta spår-id:n för (artist, title)-par. Edgar förvärmer sin
    in-memory track_cache med detta vid start. Två källor unionas:
      - resolved_tracks: alla upplösningar (spotify + lms), den dedikerade cachen
      - plays.spotify_uri: spår som faktiskt spelats (historik före cachen fanns)
    Format: [{artist, title, source, id}]. 'uri' behålls som alias för bakåtkompat."""
    db_path = PLAY_DB
    if not db_path.exists():
        return jsonify([])
    out = []
    try:
        with sqlite3.connect(db_path) as conn:
            # plays FÖRST — resolved_tracks läggs sist så den (auktoritativa, ev.
            # nyss re-resolvade) cachen vinner i edgars last-write-wins-laddning.
            for a, t, u in conn.execute("""
                SELECT DISTINCT lower(artist), lower(title), spotify_uri
                FROM plays
                WHERE spotify_uri IS NOT NULL AND spotify_uri != ''
                  AND artist IS NOT NULL AND title IS NOT NULL
            """).fetchall():
                out.append({"artist": a, "title": t, "source": "spotify", "id": u, "uri": u})
            try:
                for a, t, s, i in conn.execute(
                    "SELECT artist_norm, title_norm, source, track_id FROM resolved_tracks"
                ).fetchall():
                    out.append({"artist": a, "title": t, "source": s, "id": i, "uri": i})
            except sqlite3.OperationalError:
                pass  # tabellen finns inte än
    except Exception as e:
        logging.warning("[resolved_uris] DB-fel: %s", e)
    return jsonify(out)


@app.route('/cache_resolved', methods=['POST'])
def cache_resolved():
    """Persisterar en upplösning så den överlever omstart. Anropas av edgar vid
    varje lyckad (artist, title) → (source, id)-upplösning."""
    data     = request.get_json(silent=True) or {}
    artist   = (data.get("artist") or "").strip().lower()
    title    = (data.get("title")  or "").strip().lower()
    source   = (data.get("source") or "").strip()
    track_id = (data.get("id") or data.get("track_id") or "").strip()
    if not (artist and title and source and track_id):
        return jsonify({"error": "artist, title, source, id krävs"}), 400
    try:
        with sqlite3.connect(PLAY_DB, timeout=5) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO resolved_tracks "
                "(artist_norm, title_norm, source, track_id, resolved_at) VALUES (?,?,?,?,?)",
                (artist, title, source, track_id, int(time.time())),
            )
        return jsonify({"ok": True})
    except Exception as e:
        logging.warning(f"[cache] /cache_resolved fel: {e}")
        return jsonify({"error": str(e)}), 500


def _write_pending_origins(rows: list[tuple]) -> int:
    """rows = (artist_lower, title_lower, origin, ts, ctx_json). Tål logger utan context-kolumn."""
    if not rows:
        return 0
    with sqlite3.connect(PLAY_DB) as conn:
        try:
            conn.executemany(
                "INSERT INTO pending_origins (artist_lower, title_lower, origin, ts, context) "
                "VALUES (?,?,?,?,?)", rows,
            )
        except sqlite3.OperationalError:
            conn.executemany(
                "INSERT INTO pending_origins (artist_lower, title_lower, origin, ts) "
                "VALUES (?,?,?,?)", [r[:4] for r in rows],
            )
    return len(rows)


def _mark_queue_origin(player_mac: str, origin: str, delays=(4, 20)):
    """Taggar det som ligger i spelarens kö med ett ursprung — i bakgrunden, efter
    att en spellista/mix laddats. Läser kön två gånger (Spotty fyller på progressivt)
    och unionar. Används för Daily Mix, Spotify-spellistor och Artist Radio, som
    annars loggas som 'manual' (= aktivt val, 3× smakvikt) fast Spotify valde spåren.
    Läsa kön slår Spotty-browse: samma väg för alla laddningar och inget korsprat."""
    def _run():
        seen: set[tuple] = set()
        for i, d in enumerate(delays):
            time.sleep(d if i == 0 else d - delays[i - 1])
            res = lms_json_rpc(player_mac, ["status", 0, 200, "tags:al"], timeout=15)
            loop = ((res or {}).get('result') or {}).get('playlist_loop', [])
            now = int(time.time())
            rows = []
            for t in loop:
                a, ti = (t.get('artist') or '').strip().lower(), (t.get('title') or '').strip().lower()
                if a and ti and (a, ti) not in seen:
                    seen.add((a, ti))
                    rows.append((a, ti, origin, now, None))
            try:
                n = _write_pending_origins(rows)
                if n:
                    logging.info(f"[mark_queue_origin] {origin}: {n} spår taggade (läsning {i + 1})")
            except Exception as e:
                logging.warning(f"[mark_queue_origin] DB-fel: {e}")
    threading.Thread(target=_run, daemon=True, name=f"mark-{origin}").start()


@app.route('/mark_queue_origin', methods=['POST'])
def mark_queue_origin():
    """Edgar-anrop efter en playlist-laddning den gjort själv (t.ex. Artist Radio via
    direkt LMS-kommando): {room, origin}."""
    data = request.get_json(force=True, silent=True) or {}
    origin = (data.get("origin") or "").strip()
    player_mac, _ = get_player_info(data.get("room"))
    if not origin or not player_mac:
        return jsonify({"error": "origin och room krävs"}), 400
    _mark_queue_origin(player_mac, origin)
    return jsonify({"ok": True})


@app.route('/lms', methods=['POST'])
def lms_passthrough():
    """Genomsläpp för LMS JSON-RPC: {player, command, timeout?} → LMS-svaret.
    Edgars alla LMS-kommandon går hit i stället för direkt till Lyrion, så att
    spärrarna i lms_json_rpc (C5:ans väckning och startvakt) gäller för varje väg
    (12 okt 2026 — dashboardens föregående/hoppa, genrelistor, Lyrion-spellistor
    och Artist Radio startade förut köket utan dem)."""
    data = request.get_json(force=True, silent=True) or {}
    command = data.get("command")
    if not isinstance(command, list) or not command:
        return jsonify({"error": "command (lista) krävs"}), 400
    try:
        timeout = min(float(data.get("timeout") or 12), 60)
    except (TypeError, ValueError):
        timeout = 12
    res = lms_json_rpc(data.get("player") or "", command, timeout=timeout)
    if res is None:
        return jsonify({"error": "LMS svarade inte"}), 502
    return jsonify(res)


@app.route('/mark_origin', methods=['POST'])
def mark_origin():
    """Skriver köade spår till pending_origins-tabellen så lms_logger kan märka
    framtida plays med ursprung (t.ex. 'nydj'). Skickas från edgar när låtar köas."""
    data = request.get_json(force=True, silent=True) or {}
    origin = (data.get("origin") or "").strip()
    tracks = data.get("tracks") or []
    if not origin or not tracks:
        return jsonify({"error": "origin och tracks krävs"}), 400
    # Genererande kontext (mood/prompt_type/energy) — valfritt, mest för nydj.
    ctx = data.get("context") or None
    ctx_json = json.dumps(ctx, ensure_ascii=False) if isinstance(ctx, dict) and ctx else None
    now = int(time.time())
    rows = [
        (t["artist"].lower(), t["title"].lower(), origin, now, ctx_json)
        for t in tracks
        if t.get("artist") and t.get("title")
    ]
    if not rows:
        return jsonify({"written": 0})
    try:
        return jsonify({"written": _write_pending_origins(rows)})
    except Exception as e:
        logging.warning("[mark_origin] DB-fel: %s", e)
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Spotify recently_played-ingest (mobil-lyssning → play_history)
# ---------------------------------------------------------------------------
# Edgar "beställer" var 3:e timme (POST /spotify/ingest); bryggan hämtar
# användarens senaste Spotify-plays via Web API och skriver in dem som plays
# med origin='spotify_mobile'. Fångar lyssnandet utanför hemmet (~30%) som
# annars är osynligt för LMS-historiken. Neutral smaksignal (1× via _TASTE_W).
# played_seconds/interrupted_by_next lämnas NULL (Spotify ger ingen engagemangs-
# data), men skipped=0 sätts: recently_played innehåller bara FAKTISKT spelade
# spår, och skipped=0 krävs för att de ska räknas i smak-queries (alla filtrerar
# skipped=0). skipped rör binär skip-mätning, inte completion-ratio (den läser
# played_seconds, som förblir NULL).

_spotify_user_token = {"token": None, "expires_at": 0}
_spotify_token_lock = threading.Lock()


def _spotify_access_token():
    """Refresh-token-grant → access token, cachat tills strax före utgång."""
    if not (SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET and SPOTIFY_REFRESH_TOKEN):
        return None
    import base64
    with _spotify_token_lock:
        now = time.time()
        if _spotify_user_token["token"] and now < _spotify_user_token["expires_at"] - 60:
            return _spotify_user_token["token"]
        creds = base64.b64encode(
            f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}".encode()).decode()
        try:
            r = _session.post(
                "https://accounts.spotify.com/api/token",
                headers={"Authorization": f"Basic {creds}"},
                data={"grant_type": "refresh_token", "refresh_token": SPOTIFY_REFRESH_TOKEN},
                timeout=10,
            )
            r.raise_for_status()
            data = r.json()
            _spotify_user_token["token"]      = data["access_token"]
            _spotify_user_token["expires_at"] = now + data.get("expires_in", 3600)
            return _spotify_user_token["token"]
        except Exception as e:
            logging.warning("[spotify-ingest] token-fel: %s", e)
            return None


def _played_at_to_unix(s: str):
    """ISO8601 ('2026-06-20T14:32:11.123Z') → unix-sekunder (int) eller None."""
    try:
        from datetime import datetime
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


def _ensure_spotify_ingest_schema(conn):
    """Idempotent — pollern är skribent och ska aldrig falla på saknad kolumn/tabell,
    oavsett om lms_logger.init_db hunnit migrera play_history.db."""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(plays)").fetchall()]
    if "spotify_context_type" not in cols:
        conn.execute("ALTER TABLE plays ADD COLUMN spotify_context_type TEXT")
    conn.execute("CREATE TABLE IF NOT EXISTS spotify_ingest_state "
                 "(key TEXT PRIMARY KEY, value TEXT)")


def _already_logged_home(conn, uri, artist, title, ts, window=600):
    """True om spåret redan loggats av lms_logger som en HEM-play nära i tid (±window s).
    Spotty bygger på librespot → registrerar sig som Spotify Connect-enhet → hem-plays
    dyker upp i recently_played. Utan denna spärr skulle de dubbelräknas (en gång som
    manual/nydj av lms_logger, en gång som spotify_mobile här). Matchar på stabil URI
    eller artist+titel; ignorerar våra egna tidigare spotify_mobile-rader."""
    lo, hi = ts - window, ts + window
    if uri:
        if conn.execute(
            "SELECT 1 FROM plays WHERE spotify_uri=? AND ts BETWEEN ? AND ? "
            "AND (origin IS NULL OR origin != 'spotify_mobile') LIMIT 1",
            (uri, lo, hi)).fetchone():
            return True
    if artist and title:
        if conn.execute(
            "SELECT 1 FROM plays WHERE lower(artist)=lower(?) AND lower(title)=lower(?) "
            "AND ts BETWEEN ? AND ? AND (origin IS NULL OR origin != 'spotify_mobile') LIMIT 1",
            (artist, title, lo, hi)).fetchone():
            return True
    return False


def _spotify_feature_worker(rows):
    """Estimerar audio-features sekventiellt för nya spotify-plays (cache + Gemini) —
    samma väg som lms_logger använder för LMS-spår. Sekventiellt = ingen Gemini-storm."""
    ll = _lms_logger()
    for row_id, artist, title in rows:
        try:
            ll._estimate_and_store(row_id, artist, title)
        except Exception as e:
            logging.warning("[spotify-ingest] feature-fel %s—%s: %s", artist, title, e)


@app.route('/spotify/ingest', methods=['POST'])
def spotify_ingest():
    """Hämtar Spotifys recently_played och skriver nya plays (origin='spotify_mobile').
    Dedupe via persistent cursor (senaste played_at). Anropas av edgar var 3:e timme."""
    if not SPOTIFY_REFRESH_TOKEN:
        return jsonify({"error": "SPOTIFY_REFRESH_TOKEN saknas — kör spotify_auth_setup.py"}), 400
    token = _spotify_access_token()
    if not token:
        return jsonify({"error": "kunde inte hämta access token"}), 502

    db_path = PLAY_DB
    with sqlite3.connect(db_path) as conn:
        _ensure_spotify_ingest_schema(conn)
        cur_row = conn.execute(
            "SELECT value FROM spotify_ingest_state WHERE key='last_played_at_ms'"
        ).fetchone()
    cursor_ms = int(cur_row[0]) if cur_row and cur_row[0] else None

    params = {"limit": 50}
    if cursor_ms:
        params["after"] = cursor_ms
    try:
        r = _session.get(
            "https://api.spotify.com/v1/me/player/recently-played",
            headers={"Authorization": f"Bearer {token}"},
            params=params, timeout=15,
        )
        r.raise_for_status()
        items = r.json().get("items", [])
    except Exception as e:
        logging.warning("[spotify-ingest] hämtningsfel: %s", e)
        return jsonify({"error": str(e)}), 502

    cap_count = len(items)
    items = list(reversed(items))  # Spotify ger nyast först → kronologisk för insert
    inserted = []
    home_dupes = 0
    max_ms = cursor_ms or 0
    with sqlite3.connect(db_path) as conn:
        _ensure_spotify_ingest_schema(conn)
        for it in items:
            tr = it.get("track") or {}
            if not tr:
                continue
            ts = _played_at_to_unix(it.get("played_at") or "")
            if ts is None:
                continue
            played_ms = ts * 1000
            if cursor_ms and played_ms <= cursor_ms:
                continue  # redan ingestat (belt-and-suspenders utöver after-param)
            artist   = (tr.get("artists") or [{}])[0].get("name", "") or ""
            title    = tr.get("name", "") or ""
            album    = (tr.get("album") or {}).get("name", "") or ""
            duration = int((tr.get("duration_ms") or 0) / 1000)
            uri      = tr.get("uri") or None     # 'spotify:track:XXXX' — matchar lagrat format
            ctx      = it.get("context") or {}
            ctx_type = ctx.get("type") if isinstance(ctx, dict) else None
            # Hoppa om redan loggat som hem-play (Spotty via Connect → undvik dubbelräkning).
            # Cursorn flyttas ändå förbi spåret nedan, så det re-prövas inte.
            if _already_logged_home(conn, uri, artist, title, ts):
                home_dupes += 1
                if played_ms > max_ms:
                    max_ms = played_ms
                continue
            cur = conn.execute(
                "INSERT INTO plays (ts, player, artist, title, album, duration, source, "
                "spotify_uri, origin, skipped, spotify_context_type) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (ts, "spotify-mobil", artist, title, album, duration, "spotify",
                 uri, "spotify_mobile", 0, ctx_type),
            )
            if artist and title:
                inserted.append((cur.lastrowid, artist, title))
            if played_ms > max_ms:
                max_ms = played_ms
        if max_ms and max_ms != (cursor_ms or 0):
            conn.execute(
                "INSERT OR REPLACE INTO spotify_ingest_state (key, value) "
                "VALUES ('last_played_at_ms', ?)", (str(max_ms),),
            )

    hit_cap = cap_count >= 50
    if inserted:
        threading.Thread(target=_spotify_feature_worker, args=(inserted,),
                         daemon=True, name="spotify-features").start()
    if hit_cap:
        logging.warning("[spotify-ingest] 50-taks-cap nådd — kan ha clippat plays "
                        "(>50 sedan förra hämtning). Överväg tätare intervall.")
    logging.info("[spotify-ingest] %d nya plays, %d hem-dubbletter hoppade (cap=%s)",
                 len(inserted), home_dupes, hit_cap)
    return jsonify({"ingested": len(inserted), "home_dupes": home_dupes,
                    "hit_cap": hit_cap, "cursor_ms": max_ms})


def _first_playing_mac():
    """MAC för första spelaren i mode 'play', annars None. Används av
    like_track/skip_track för att hitta aktiv spelare när inget rum angavs."""
    for p in get_all_players():
        mac = p.get("playerid")
        if not mac:
            continue
        res = lms_json_rpc(mac, ["status", "-", "1"])
        if res and res.get("result", {}).get("mode") == "play":
            return mac
    return None


@app.route('/like_track')
def like_track():
    """Gillar nuvarande låt: lovar på Last.fm + flaggar loved=1 i plays-tabellen."""
    room = request.args.get("room")

    # Hämta spelande spelare om inget rum angavs
    if not room:
        room = _first_playing_mac()
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
        if lfm_ok:
            logging.info("[LastFM] ♥ loved: %s – %s", artist, title)
        else:
            logging.warning("[LastFM] love misslyckades: %s – %s", artist, title)
    else:
        logging.warning("[LastFM] Ingen session key — hoppar över Last.fm love")

    # Flagga loved=1 i plays-tabellen
    db_path = PLAY_DB
    db_ok = False
    try:
        with sqlite3.connect(db_path) as conn:
            # 'loved'-kolumnen ägs av lms_logger.init_db() — antas finnas här.
            # Markera de senaste matchande raderna (samma artist+title, ej redan gillad)
            conn.execute(
                "UPDATE plays SET loved=1 WHERE lower(artist)=lower(?) AND lower(title)=lower(?) AND loved=0",
                (artist, title),
            )
            db_ok = True
    except Exception as e:
        print(f"[Like] DB-fel: {e}")

    return jsonify({"artist": artist, "title": title, "lastfm": lfm_ok, "db": db_ok})


@app.route('/mark_current_choice', methods=['POST'])
def mark_current_choice():
    """{room}: låten som spelar blir ett eget val (origin 'manual', 3× smakvikt).
    Edgar Musiks "mixa vidare" på låten som redan spelar (15 okt 2026, användarens beslut):
    att mixa från en låt är ett aktivt val, som att välja den ur en lista. Spelningen hade
    redan loggats med ett annat ursprung (DJ, graf, Daily Mix …), så raden märks om i
    efterhand. Egna val och mobilens spelningar rörs inte."""
    data = request.get_json(silent=True) or {}
    mac, name = get_player_info(data.get("room"))
    if not mac:
        return jsonify({"error": "Okänt rum"}), 404
    try:
        res = lms_json_rpc(mac, ["status", "-", "1", "tags:a"])
        track = ((res or {}).get("result", {}).get("playlist_loop") or [{}])[0]
        artist, title = (track.get("artist") or "").strip(), (track.get("title") or "").strip()
    except Exception as e:
        return jsonify({"error": f"Kunde inte hämta låtinfo: {e}"}), 500
    if not artist or not title:
        return jsonify({"error": "Ingen låt spelar"}), 404
    with sqlite3.connect(PLAY_DB, timeout=10) as conn:
        row = conn.execute(
            "SELECT id, origin FROM plays WHERE lower(player)=lower(?) AND lower(artist)=lower(?) "
            "AND lower(title)=lower(?) AND ts > strftime('%s','now') - 7200 ORDER BY ts DESC LIMIT 1",
            (name, artist, title)).fetchone()
        if not row:
            return jsonify({"ok": False, "error": "Spelningen finns inte i historiken än"}), 404
        if row[1] in ("manual", "spotify_mobile"):
            return jsonify({"ok": True, "changed": False, "origin": row[1]})
        conn.execute("UPDATE plays SET origin='manual' WHERE id=?", (row[0],))
    logging.info("[val] %s – %s i %s: %s → manual (mixa vidare)", artist, title, name, row[1])
    return jsonify({"ok": True, "changed": True, "was": row[1]})


@app.route('/love_track', methods=['POST'])
def love_track():
    """Gillar ett spår som INTE spelas just nu: Last.fm love + loved=1 i plays.
    Body: {artist, title} eller {artist, titles: [...]}. Utan titel gillas
    artistens senast spelade spår. Samma effekt som /like_track, men riktad mot
    historiken — används av history-vyns 'antecknad artist → gillad'."""
    import sqlite3 as _sqlite3
    data   = request.get_json(silent=True) or {}
    artist = (data.get("artist") or "").strip()
    titles = [t.strip() for t in (data.get("titles") or []) if (t or "").strip()]
    if data.get("title"):
        titles.append(str(data["title"]).strip())
    if not artist:
        return jsonify({"error": "artist krävs"}), 400

    db_path = PLAY_DB
    if not titles:
        # Ingen titel angiven → ta artistens senaste spel ur historiken.
        try:
            with _sqlite3.connect(db_path, timeout=5) as conn:
                row = conn.execute(
                    "SELECT title FROM plays WHERE lower(artist) = lower(?) "
                    "ORDER BY ts DESC LIMIT 1", (artist,)
                ).fetchone()
            if row and row[0]:
                titles = [row[0]]
        except Exception as e:
            logging.warning("[Love] kunde inte slå upp senaste spår: %s", e)
    if not titles:
        return jsonify({"error": f"Hittade inga spel för {artist}"}), 404

    # Last.fm love per titel (tyst hoppad om sessionsnyckel saknas)
    lastfm = []
    for t in titles:
        ok = False
        if LASTFM_SESSION_KEY:
            ok = _lastfm_post("track.love", artist=artist, track=t) is not None
            logging.info("[LastFM] %s love: %s – %s", "♥" if ok else "✗", artist, t)
        lastfm.append({"title": t, "ok": ok})

    # loved=1 på matchande rader (samma villkor som /like_track)
    loved_rows = 0
    try:
        with _sqlite3.connect(db_path, timeout=5) as conn:
            for t in titles:
                cur = conn.execute(
                    "UPDATE plays SET loved=1 WHERE lower(artist)=lower(?) "
                    "AND lower(title)=lower(?) AND loved=0", (artist, t))
                loved_rows += cur.rowcount
    except Exception as e:
        logging.warning("[Love] DB-fel: %s", e)
        return jsonify({"error": str(e)}), 500

    logging.info("[Love] %s: %d rader loved=1 (%d titlar)", artist, loved_rows, len(titles))
    return jsonify({"ok": True, "artist": artist, "titles": titles,
                    "loved_rows": loved_rows, "lastfm": lastfm})



@app.route('/skip_track')
def skip_track():
    """Hoppar till nästa låt. Skip-FLAGGNING sker INTE här — lms_logger äger numera
    skip-/engagemangsdetektionen (debounce på stop + tröskel i _on_newsong). Tidigare
    dubbelskrev denna endpoint skipped=1 oberoende av lms_logger, vilket gav motstridig
    data (skipped=1 med interrupted_by_next=0 som föll ur completion-mätningen)."""
    room = request.args.get("room")

    if not room:
        room = _first_playing_mac()
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

    # Hoppa till nästa — newsong-eventet får lms_logger att registrera skippen
    # konsekvent (interrupted_by_next=1 + completion-ratio via tröskel). Ingen
    # skip-skrivning här (se docstring).
    lms_json_rpc(room, ["playlist", "index", "+1"])
    print(f"[Skip] → nästa ({artist} – {title})")

    return jsonify({"artist": artist, "title": title})


def _playlist_count(mac):
    """Aktuellt antal spår i en spelares kö."""
    res = lms_json_rpc(mac, ["status", "-", 1, "tags:"])
    return (res or {}).get("result", {}).get("playlist_tracks", 0) or 0


def _wait_for_count(mac, want, timeout=2.0):
    """Pollar tills kön har minst `want` spår eller timeout. Spotty löser URI:er
    asynkront — räkningen uppdateras först när spåret faktiskt landat."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _playlist_count(mac) >= want:
            return True
        time.sleep(0.25)
    return False


def _add_spotify_verified(mac, track_id, action, want, retries=3):
    """Lägger till ett Spotify-spår och verifierar att det faktiskt landade i kön.
    Under Spotty cold-start/throttle slänger LMS tyst URI:er den inte hinner lösa
    (se CLAUDE.md) — vi bekräftar via playlist_tracks och försöker om vid behov.

    `action` är 'play' (första spåret: clear+spela) eller 'add' (append). Retry på
    'add' kan i sällsynta fall ge en dubblett om Spotty landar precis efter timeout,
    men ett extra spår är bättre än ett tappat."""
    # Acceptera BÅDA Spotify-URI-formaten: spotify://track:XXX (färska sökningar) och
    # spotify:track:XXX (det format LMS lagrar för spelade spår → plays.spotify_uri →
    # warm-cachen). Normalisera till dubbel-slash som 'playlist add' vill ha. Bara äkta
    # positionella item_id:n (1.0_…) går till spotty item_id-grenen.
    # (Tidigare föll spotify:track: tyst till item_id-grenen → varje cache-träff på ett
    #  spelat spår TAPPADes.)
    if track_id.startswith("spotify:"):
        uri = track_id if track_id.startswith("spotify://") else "spotify://" + track_id[len("spotify:"):]
        cmd = ["playlist", action, uri]
    else:
        cmd = ["spotty", "playlist", action, f"item_id:{track_id}"]
    for _ in range(retries):
        lms_json_rpc(mac, cmd, timeout=8)
        if _wait_for_count(mac, want, timeout=2.0):
            return True
    return _playlist_count(mac) >= want


@app.route('/queue-tracks', methods=['POST'])
def queue_tracks():
    """Köar lösta spår till en spelare. Hanterar Spotty-throttle/cold-start internt
    genom att verifiera att varje spår landat innan nästa läggs till.

    Body: {
      "room":   "spelarnamn eller MAC",
      "tracks": [{"source": "spotify|lms", "id": "...", "artist": "...", "title": "..."}],
      "mode":   "load|add"   // load = power+clear+spela första, add = lägg till i kö
    }
    """
    data   = request.get_json(silent=True) or {}
    room   = data.get("room")
    tracks = data.get("tracks", [])
    mode   = data.get("mode", "add")

    if not tracks:
        return jsonify({"error": "Inga spår"}), 400

    mac, _ = get_player_info(room)
    if not mac:
        return jsonify({"error": f"Spelare hittades inte: {room}"}), 404

    if mode == "load":
        lms_json_rpc(mac, ["power", "1"])
        lms_json_rpc(mac, ["playlist", "shuffle", "0"])
        lms_json_rpc(mac, ["playlist", "clear"])

    # Spåra faktiskt landat antal — ett tappat spår ökar inte räkningen, så nästa
    # spår siktar fortfarande mot rätt index.
    landed = 0 if mode == "load" else _playlist_count(mac)

    queued, dropped = [], []
    for i, track in enumerate(tracks):
        source   = track.get("source", "lms")
        track_id = track.get("id", "")
        first    = (mode == "load" and i == 0)
        want     = landed + 1
        try:
            if source == "spotify":
                action = "play" if first else "add"
                ok = _add_spotify_verified(mac, track_id, action, want)
            else:
                cmd_type = "load" if first else "add"
                lms_json_rpc(mac, ["playlistcontrol", f"cmd:{cmd_type}", f"track_id:{track_id}"])
                ok = _wait_for_count(mac, want, timeout=1.5)

            landed = _playlist_count(mac)   # resynka mot verkligheten
            if ok:
                queued.append({"artist": track.get("artist", ""), "title": track.get("title", "")})
                logging.info(f"[QueueTracks] {'PLAY' if first else 'KÖ  '} {source:7s} "
                             f"{track.get('artist','?')} – {track.get('title','?')}")
            else:
                dropped.append({"artist": track.get("artist", ""), "title": track.get("title", "")})
                logging.warning(f"[QueueTracks] TAPPAD (Spotty löste inte efter retries) {source:7s} "
                                f"{track.get('artist','?')} – {track.get('title','?')}")
                _invalidate_resolved(track.get("artist", ""), track.get("title", ""))
        except Exception as e:
            logging.error(f"[QueueTracks] Fel: {track.get('artist')} – {track.get('title')}: {e}")

    # Vid load: säkerställ att openern faktiskt börjar spela (Spotty cold-start kan
    # hänga på elapsed=0 fast mode='play'). _deferred_kick knuffar med 'playlist jump'
    # inom vakthundens 60s-fönster. Streaming-vägen anropade aldrig detta förut.
    if mode == "load" and queued:
        _deferred_kick(mac)

    return jsonify({"queued": len(queued), "dropped": len(dropped), "tracks": queued})


if __name__ == '__main__':
    from werkzeug.serving import make_server

    logging.getLogger('werkzeug').setLevel(logging.ERROR)

    # Certifikaten ligger utanför repot sedan 14 okt 2026 (privata nycklar hör inte hemma i git).
    _certs = os.getenv("BRIDGE_CERT_DIR", os.path.expanduser("~/.edgar/certs/bridge"))
    cert = os.getenv("BRIDGE_CERT", os.path.join(_certs, '10.0.1.132+2.pem'))
    key  = os.getenv("BRIDGE_KEY",  os.path.join(_certs, '10.0.1.132+2-key.pem'))

    _http_port  = int(os.getenv("BRIDGE_PORT", "5000"))
    _https_port = int(os.getenv("BRIDGE_HTTPS_PORT", "5001"))
    http_server  = make_server('0.0.0.0', _http_port,  app, threaded=True)
    https_server = make_server('0.0.0.0', _https_port, app, ssl_context=(cert, key), threaded=True)

    print(f"--- Lyrionbridge v2: http://0.0.0.0:{_http_port}  (Edgar/intern) ---")
    print(f"--- Lyrionbridge v2: https://0.0.0.0:{_https_port} (iPhone PWA)   ---")

    threading.Thread(target=http_server.serve_forever, daemon=True).start()
    threading.Thread(target=_mix_label_loop, daemon=True, name="mix-labels").start()
    threading.Thread(target=_watchdog_loop, daemon=True, name="watchdog").start()
    https_server.serve_forever()
