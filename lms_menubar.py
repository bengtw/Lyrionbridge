#!/usr/bin/env python3
"""
LMS Menu Bar – Enkel Mac-menyrad för Lyrionbridge
──────────────────────────────────────────────────
Krav:  pip install rumps requests
Start: python3 lms_menubar.py
"""

import threading
import time

import requests
import rumps
from AppKit import NSMenuItem, NSSlider, NSView, NSMakeRect
from Foundation import NSObject
import objc

# ── Konfiguration ────────────────────────────────────────────────────────────
BRIDGE = "http://10.0.1.132:5000"   # IP till maskinen som kör lms_bridge.py
POLL   = 6   # Uppdateringsintervall i sekunder
# ─────────────────────────────────────────────────────────────────────────────

_SLIDER_PLACEHOLDER = "___vol_slider___"


def _api(path, room=None, timeout=3):
    sep = "&" if "?" in path else "?"
    url = f"{BRIDGE}{path}{sep}room={room}" if room else f"{BRIDGE}{path}"
    try:
        return requests.get(url, timeout=timeout)
    except Exception:
        return None


class SliderHandler(NSObject):
    """Objective-C target/action för NSSlider."""

    def initWithApp_(self, app):
        self = objc.super(SliderHandler, self).init()
        if self is None:
            return None
        self._app = app
        return self

    def sliderChanged_(self, sender):
        new_vol = int(sender.floatValue())
        self._app._volume = new_vol
        self._app.vol_label.title = f"🔊 Volym: {new_vol}"
        threading.Thread(
            target=lambda: _api(f"/set_volume?level={new_vol}", room=self._app._room),
            daemon=True,
        ).start()


class AlbumActionHandler(NSObject):
    """Objective-C target för album-menyval."""

    def initWithApp_(self, app):
        self = objc.super(AlbumActionHandler, self).init()
        if self is None:
            return None
        self._app = app
        return self

    def playAlbum_(self, sender):
        alb_id = sender.representedObject()
        threading.Thread(
            target=lambda: _api(f"/play_album?album_id={alb_id}", room=self._app._room),
            daemon=True,
        ).start()


class AlbumMenuDelegate(NSObject):
    """Bygger om albumlistan varje gång submenyn öppnas."""

    def initWithApp_handler_(self, app, handler):
        self = objc.super(AlbumMenuDelegate, self).init()
        if self is None:
            return None
        self._app     = app
        self._handler = handler
        return self

    def menuWillOpen_(self, menu):
        menu.removeAllItems()
        albums = _fetch_albums()
        if albums:
            for alb in albums:
                alb_id = alb.get("id", "")
                title  = alb.get("title", "Okänt album")
                artist = alb.get("artist", "")
                short  = (artist[:30] + "…") if len(artist) > 30 else artist
                label  = f"{title}  ·  {short}" if short else title
                item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                    label, "playAlbum:", ""
                )
                item.setTarget_(self._handler)
                item.setRepresentedObject_(alb_id)
                menu.addItem_(item)
        else:
            menu.addItem_(NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                "(inga album hittades)", None, ""
            ))


def _make_slider_item(handler, initial_volume):
    """Returnerar (NSMenuItem, NSSlider) med en inbäddad horisontell slider."""
    view   = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 240, 36))
    slider = NSSlider.alloc().initWithFrame_(NSMakeRect(14, 9, 212, 18))
    slider.setMinValue_(0)
    slider.setMaxValue_(100)
    slider.setFloatValue_(initial_volume)
    slider.setTarget_(handler)
    slider.setAction_("sliderChanged:")
    slider.setContinuous_(True)
    view.addSubview_(slider)

    ns_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("", None, "")
    ns_item.setView_(view)
    return ns_item, slider


def _fetch_players():
    """Hämtar spelare från /get_players. Returnerar dict {namn: playerid}."""
    r = _api("/get_players")
    if not (r and r.ok):
        return {}
    players = {}
    for p in r.json().get("players_loop", []):
        name = p.get("name", "").strip()
        mac  = p.get("playerid", "").strip()
        if name and mac:
            players[name] = mac
    return players


def _fetch_daily_mixes():
    """Hämtar Daily Mixes från /get_daily_mixes. Returnerar lista med dicts."""
    r = _api("/get_daily_mixes")
    if not (r and r.ok):
        return []
    return r.json()


def _fetch_radio():
    """Hämtar favoritkanaler från /get_radio_favorites."""
    r = _api("/get_radio_favorites")
    if not (r and r.ok):
        return []
    return r.json()


def _fetch_albums():
    """Hämtar 10 slumpmässiga album från /get_random_albums."""
    r = _api("/get_random_albums")
    if not (r and r.ok):
        return []
    return r.json()


class LMSBar(rumps.App):
    def __init__(self):
        super().__init__("♩", quit_button=None)

        self._volume  = 30
        self._playing = False

        # ── Hämta spelare från Lyrion ─────────────────────────────────────────
        self._players = _fetch_players()          # {namn: MAC}
        first_mac     = next(iter(self._players.values()), None) if self._players else None
        self._room    = first_mac or ""

        # ── Menyobjekt ───────────────────────────────────────────────────────
        self.track_item = rumps.MenuItem("Ansluter…")
        self.play_item  = rumps.MenuItem("▶  Spela",  callback=self.toggle)
        self.next_item  = rumps.MenuItem("⏭  Nästa", callback=self.skip)
        self.vol_label  = rumps.MenuItem(f"🔊 Volym: {self._volume}")

        # ── Daily Mixes-undermeny ─────────────────────────────────────────────
        self.daily_menu = rumps.MenuItem("🎵 Daily Mixes")
        mixes = _fetch_daily_mixes()
        if mixes:
            for mix in mixes:
                mix_id = mix.get("id", "0")
                title  = mix.get("title", f"Mix {mix_id}")
                desc   = mix.get("description", "")
                short  = (desc[:38] + "…") if len(desc) > 38 else desc
                label  = f"{title}  ·  {short}" if short else title

                def make_cb(idx):
                    def cb(_):
                        _api(f"/daily?index={idx}", room=self._room)
                    return cb

                self.daily_menu.add(rumps.MenuItem(label, callback=make_cb(mix_id)))
        else:
            self.daily_menu.add(rumps.MenuItem("(inga mixes hittades)"))

        # ── Radio-undermeny ───────────────────────────────────────────────────
        self.radio_menu = rumps.MenuItem("📻 Radio")
        stations = _fetch_radio()
        if stations:
            for s in stations:
                fav_id = s.get("id", "")
                label  = s.get("name", "Okänd kanal")

                def make_radio_cb(sid):
                    def cb(_):
                        _api(f"/play_radio?url={sid}", room=self._room)
                    return cb

                self.radio_menu.add(rumps.MenuItem(label, callback=make_radio_cb(fav_id)))
        else:
            self.radio_menu.add(rumps.MenuItem("(inga kanaler hittades)"))

        # ── Albumförslag-undermeny (byggs om vid varje öppning via delegat) ──
        self.album_menu = rumps.MenuItem("💿 Albumförslag")
        # Lägg till ett plachållerobjekt så att rumps skapar NSMenu-submenyn
        self.album_menu.add(rumps.MenuItem("Laddar…"))
        self._album_handler  = AlbumActionHandler.alloc().initWithApp_(self)
        self._album_delegate = AlbumMenuDelegate.alloc().initWithApp_handler_(
            self, self._album_handler
        )
        self.album_menu._menuitem.submenu().setDelegate_(self._album_delegate)

        # Rum-undermeny
        self.room_menu = rumps.MenuItem("🏠 Välj rum")
        if self._players:
            for name in self._players:
                self.room_menu.add(rumps.MenuItem(name, callback=self.set_room))
        else:
            self.room_menu.add(rumps.MenuItem("(ej ansluten)"))

        # Platshållare ersätts nedan med riktig NSSlider
        self.menu = [
            self.track_item,
            None,
            self.play_item,
            self.next_item,
            None,
            self.vol_label,
            rumps.MenuItem(_SLIDER_PLACEHOLDER),   # ← byts ut
            None,
            self.daily_menu,
            self.radio_menu,
            self.album_menu,
            None,
            self.room_menu,
            None,
            rumps.MenuItem("Avsluta", callback=rumps.quit_application),
        ]

        # Markera första rummet
        if self._players:
            first_name = next(iter(self._players))
            self.room_menu[first_name].state = 1

        # ── Injicera NSSlider ─────────────────────────────────────────────────
        self._slider_handler = SliderHandler.alloc().initWithApp_(self)
        self._ns_slider_item, self._ns_slider = _make_slider_item(
            self._slider_handler, self._volume
        )
        ns_menu = self._menu._menu
        idx = ns_menu.indexOfItemWithTitle_(_SLIDER_PLACEHOLDER)
        if idx >= 0:
            ns_menu.removeItemAtIndex_(idx)
            ns_menu.insertItem_atIndex_(self._ns_slider_item, idx)

        # ── Polling ──────────────────────────────────────────────────────────
        rumps.Timer(self.refresh, POLL).start()
        threading.Thread(target=self.refresh, daemon=True).start()

    # ── Hjälp ────────────────────────────────────────────────────────────────
    def _get(self, path):
        return _api(path, room=self._room)

    # ── Uppdatering ──────────────────────────────────────────────────────────
    def refresh(self, _=None):
        r = self._get("/title")
        if r and r.ok:
            text = r.text.strip() or "Ingen musik"
            self.track_item.title = (text[:44] + "…") if len(text) > 44 else text

        r = self._get("/status")
        if r and r.ok:
            self._playing        = r.text.strip() == "play"
            self.play_item.title = "⏸  Paus" if self._playing else "▶  Spela"
            self.title           = "♪" if self._playing else "♩"

        r = self._get("/volume")
        if r and r.ok:
            try:
                self._volume = int(float(r.text.strip()))
                self.vol_label.title = f"🔊 Volym: {self._volume}"
                self._ns_slider.setFloatValue_(self._volume)
            except ValueError:
                pass

    # ── Kontroller ───────────────────────────────────────────────────────────
    def toggle(self, _):
        self._get("/toggle_play_pause")
        time.sleep(0.4)
        self.refresh()

    def skip(self, _):
        self._get("/next")
        time.sleep(1.0)
        self.refresh()

    def set_room(self, sender):
        for item in self.room_menu.values():
            item.state = 0
        sender.state = 1
        self._room   = self._players[sender.title]
        self.refresh()


if __name__ == "__main__":
    LMSBar().run()
