#!/usr/bin/env python3
"""
Engångsskript för att generera en Last.fm session key.
Kör: python3 setup_lastfm_session.py
Lägg sedan till LASTFM_SESSION_KEY=<key> i edgar/.env
"""
import hashlib, json, os, sys, urllib.parse, urllib.request
from pathlib import Path

_here = Path(__file__).parent
for line in (_here.parent / "edgar" / ".env").open():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

API_KEY    = os.getenv("LAST_FM_API_KEY") or os.getenv("LASTFM_API_KEY", "9ed2b1dfa5c3f0ece0a30ec8e69b4742")
API_SECRET = os.getenv("LAST_FM_API_SECRET") or os.getenv("LASTFM_API_SECRET", "")

if not API_SECRET:
    print("Saknar LAST_FM_API_SECRET i .env")
    sys.exit(1)


def _sig(params: dict) -> str:
    s = "".join(f"{k}{v}" for k, v in sorted(params.items()))
    return hashlib.md5((s + API_SECRET).encode()).hexdigest()


def _get(params: dict) -> dict:
    p = {**params, "format": "json"}
    url = "https://ws.audioscrobbler.com/2.0/?" + urllib.parse.urlencode(p)
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read())


# Steg 1: hämta token
p = {"method": "auth.getToken", "api_key": API_KEY}
p["api_sig"] = _sig(p)
data = _get(p)
token = data["token"]

print(f"\nÖppna denna URL i webbläsaren och logga in med ditt Last.fm-konto:")
print(f"\n  https://www.last.fm/api/auth/?api_key={API_KEY}&token={token}\n")
input("Tryck Enter när du har godkänt i webbläsaren...")

# Steg 2: hämta session key
p2 = {"method": "auth.getSession", "api_key": API_KEY, "token": token}
p2["api_sig"] = _sig(p2)
data2 = _get(p2)

sk   = data2["session"]["key"]
name = data2["session"]["name"]

print(f"\nSession key för {name} genererad!")
print(f"\nLägg till i edgar/.env:\n  LASTFM_SESSION_KEY={sk}\n")
