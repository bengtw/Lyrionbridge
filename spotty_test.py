#!/usr/bin/env python3
"""
Testar att söka en låt via Spotty och lägga till den i kön.
Kör: python3 spotty_test.py "Miles Davis So What"
"""

import sys
import json
import urllib.parse
import requests

LMS_HOST = "10.0.1.132"
LMS_URL  = f"http://{LMS_HOST}:9000/jsonrpc.js"
# Kontoret — byt till annan MAC om du vill testa på annat rum
PLAYER_MAC = "b8:27:eb:fb:30:d9"

def rpc(command, timeout=30):
    payload = {"id": 1, "method": "slim.request", "params": [PLAYER_MAC, command]}
    r = requests.post(LMS_URL, json=payload, timeout=timeout)
    return r.json()

def pp(label, data):
    print(f"\n=== {label} ===")
    print(json.dumps(data, indent=2, ensure_ascii=False))

def search_and_queue(query: str, dry_run: bool = True):
    print(f"\nSöker: {query!r}  (dry_run={dry_run})")

    # STEG 1: initial sökning — Spotty returnerar kategorier (Artists, Songs, Albums…)
    res1 = rpc(["spotty", "items", 0, 20, "item_id:1.0", f"search:{query}"])
    pp("STEG 1 — initial search (item_id:1.0)", res1)
    if not res1 or "result" not in res1:
        print("Fel: inget svar från LMS")
        return

    loop1 = res1["result"].get("loop_loop", [])
    print(f"\nAntal items i loop_loop: {len(loop1)}")
    for i, it in enumerate(loop1):
        print(f"  [{i}] id={it.get('id')!r:40}  isaudio={it.get('isaudio')}  name={it.get('name')!r}")

    # STEG 2: försök hitta tracks direkt (isaudio == 1)
    direct_tracks = [it for it in loop1 if it.get("isaudio") == 1]
    if direct_tracks:
        print(f"\nHittade {len(direct_tracks)} direkta spår i sökresultatet")
        track = direct_tracks[0]
        track_id = track.get("id", "")
        print(f"Första spår: {track.get('name')!r}  id={track_id!r}")

        # Spotty leaf-node: lägg till .0 om det saknas
        leaf_id = track_id if track_id.endswith(".0") else track_id + ".0"
        _try_queue(leaf_id, track.get("name", ""), dry_run)
        return

    # STEG 3: om inga direkta spår — navigera in i "Songs"-kategorin (index 1)
    encoded = urllib.parse.quote(query)
    res2 = rpc(["spotty", "items", 0, 20, f"item_id:1.0_{encoded}.1"])
    pp("STEG 3 — Songs-kategori (item_id:1.0_<q>.1)", res2)
    if not res2 or "result" not in res2:
        print("Fel: inget svar på Songs-sökning")
        return

    loop2 = res2["result"].get("loop_loop", [])
    print(f"\nAntal items i Songs-loop: {len(loop2)}")
    for i, it in enumerate(loop2):
        print(f"  [{i}] id={it.get('id')!r:50}  isaudio={it.get('isaudio')}  name={it.get('name')!r}")

    audio_items = [it for it in loop2 if it.get("isaudio") == 1]
    if not audio_items:
        # Kanske är de icke-markerade — ta första item och kolla
        print("\nINga isaudio-flaggade — testar att öppna första item")
        if loop2:
            sub_id = loop2[0].get("id", "")
            res3 = rpc(["spotty", "items", 0, 10, f"item_id:{sub_id}"])
            pp("STEG 3b — sub-item drill-down", res3)
            sub_loop = res3["result"].get("loop_loop", []) if res3 and "result" in res3 else []
            for i, it in enumerate(sub_loop):
                print(f"  [{i}] id={it.get('id')!r:50}  isaudio={it.get('isaudio')}  name={it.get('name')!r}")
            audio_items = [it for it in sub_loop if it.get("isaudio") == 1]

    if not audio_items:
        print("\nKunde inte hitta spårets leaf-node — kolla utdata ovan")
        return

    track = audio_items[0]
    track_id = track.get("id", "")
    leaf_id = track_id if track_id.endswith(".0") else track_id + ".0"
    print(f"\nValt spår: {track.get('name')!r}  leaf_id={leaf_id!r}")
    _try_queue(leaf_id, track.get("name", ""), dry_run)


def _try_queue(item_id: str, label: str, dry_run: bool):
    print(f"\n--- Köa spår ---")
    print(f"  label   = {label!r}")
    print(f"  item_id = {item_id!r}")

    if dry_run:
        print("  DRY RUN — ingen faktisk köläggning")
        return

    # Alternativ A: spotty playlist add (lägger till utan att rensa kön)
    cmd_add = ["spotty", "playlist", "add", f"item_id:{item_id}"]
    print(f"\n  Försöker: {cmd_add}")
    res_add = rpc(cmd_add)
    pp("  Svar (spotty playlist add)", res_add)

    # Alternativ B: om add inte fungerar — prova play (ersätter kön)
    # cmd_play = ["spotty", "playlist", "play", f"item_id:{item_id}"]


if __name__ == "__main__":
    query = " ".join(sys.argv[1:]) or "Miles Davis So What"
    dry = "--play" not in sys.argv
    search_and_queue(query, dry_run=dry)
