const POLL_INTERVAL = 3000;
let currentRoom = localStorage.getItem('lastRoom') || "";
let volumeThrottleTimer = null;
let lastArtUrl = "";
let transferMode = false;

let cacheRadio = null;
let cacheDaily = null;
let cachePlaylists = null;
let cacheTips = null;

document.addEventListener('touchmove', e => {
    if (!e.target.closest('.album-grid-container, .room-list, .ios-slider')) e.preventDefault();
}, { passive: false });

window.addEventListener('DOMContentLoaded', () => {
    fetchPlayers();
    setupEventListeners();
    
    if (currentRoom) updateStatus();
    
    // 1. Kör preload direkt vid start
    preloadAllLists(); 
    
    // 2. Uppdatera status ofta (som förut)
    setInterval(updateStatus, POLL_INTERVAL);
    
    // 3. Uppdatera cachen för alla listor var 30:e minut
    setInterval(preloadAllLists, 1800000); 
});


// === 1. API KOMMUNIKATION ===

async function sendCommand(endpoint, params = {}) {
    if (!currentRoom) return null;
    const urlParams = new URLSearchParams({ room: currentRoom, ...params });
    try {
        return await fetch(`/${endpoint}?${urlParams}`).then(r => r.text());
    } catch (e) { return null; }
}

async function updateStatus() {
    const slider = document.getElementById('volume-slider');
    if (!currentRoom || document.activeElement === slider) return;

    const [titleData, vol, artUrl, mode] = await Promise.all([
        sendCommand('title'),
        sendCommand('volume'),
        fetch(`/art?room=${encodeURIComponent(currentRoom)}`).then(r => r.text()),
        sendCommand('status')
    ]).catch(() => []);

    if (!titleData && !vol) return;

    // Artist & Titel
    const trackTitleEl  = document.getElementById('track-title');
    const trackArtistEl = document.getElementById('track-artist');
    if (titleData && titleData.includes(' - ')) {
        const [title, artist] = titleData.split(' - ');
        trackTitleEl.textContent  = title.trim();
        trackArtistEl.textContent = artist.trim();
    } else {
        trackTitleEl.textContent  = titleData || "Pausad";
        trackArtistEl.textContent = "MultiLyrion";
    }

    // Volym
    if (vol && !isNaN(vol)) {
        slider.value = vol;
    }

    // Album Art + dynamisk bakgrund
    if (artUrl && artUrl !== lastArtUrl) {
        const img = new Image();
        img.src = artUrl;
        img.onload = () => {
            document.getElementById('album-art').src = artUrl;
            document.getElementById('bg-art-blur').style.backgroundImage = `url('${artUrl}')`;
            lastArtUrl = artUrl;
        };
    }

    // Play/Pause-ikon
    const playBtn = document.getElementById('play-pause-btn');
    playBtn.innerHTML = (mode && mode.trim() === "play")
        ? '<svg viewBox="0 0 24 24"><path d="M6 19h4V5H6v14zm8-14v14h4V5h-4z"/></svg>'
        : '<svg viewBox="0 0 24 24"><path fill="currentColor" d="M8 5v14l11-7z"/></svg>';
}


// === 2. DATAHÄMTNING ===

async function preloadAllLists() {
    const now = new Date().toLocaleTimeString();
    console.log(`[${now}] Bakgrundsuppdatering av listor startad...`);
    
    try {
        const [radio, daily, playlists, tips] = await Promise.all([
            fetch('/get_radio_favorites').then(r => r.json()).catch(() => null),
            fetch('/get_daily_mixes').then(r => r.json()).catch(() => null),
            fetch('/get_playlists_with_art').then(r => r.json()).catch(() => null),
            fetch('/get_random_albums').then(r => r.json()).catch(() => null)
        ]);

        if (radio) cacheRadio = radio;
        if (daily) cacheDaily = daily;
        if (playlists) cachePlaylists = playlists;
        if (tips) cacheTips = tips;
        
        console.log(`[${now}] All data uppdaterad.`);
    } catch (e) {
        console.warn("Kunde inte uppdatera cache i bakgrunden", e);
    }
}

async function fetchPlayers() {
    try {
        const data = await fetch('/get_players').then(r => r.json());
        const roomListEl      = document.querySelector('.room-list');
        const currentRoomNameEl = document.getElementById('current-room-name');
        roomListEl.innerHTML  = '';

        if (!data?.players_loop?.length) {
            roomListEl.innerHTML = '<p style="text-align:center; color: var(--text-dim);">Inga spelare hittades.</p>';
            return;
        }

        data.players_loop.forEach((p, i) => {
            if (!currentRoom && i === 0) {
                currentRoom = p.playerid;
                localStorage.setItem('lastRoom', currentRoom);
            }
            const btn = document.createElement('button');
            btn.className = 'room-item';
            btn.dataset.id = p.playerid;
            const isCurrent = currentRoom === p.playerid;
            if (isCurrent) {
                btn.classList.add('active');
                currentRoomNameEl.textContent = capitalize(p.name);
            }
            if (transferMode && isCurrent) {
                btn.innerHTML = `${capitalize(p.name)} <span style="font-size:10px;opacity:0.5;margin-left:6px">SPELAR HÄR</span>`;
                btn.style.opacity = '0.45';
                btn.style.pointerEvents = 'none';
            } else {
                btn.textContent = capitalize(p.name);
            }
            btn.addEventListener('click', async () => {
                if (transferMode && p.playerid === currentRoom) return;
                if (transferMode) {
                    btn.textContent = "Flyttar…";
                    await fetch(`/transfer?from=${encodeURIComponent(currentRoom)}&to=${encodeURIComponent(p.playerid)}`);
                }
                document.querySelectorAll('.room-item').forEach(r => r.classList.remove('active'));
                btn.classList.add('active');
                currentRoom = p.playerid;
                localStorage.setItem('lastRoom', currentRoom);
                currentRoomNameEl.textContent = capitalize(p.name);
                lastArtUrl = "";
                setTimeout(() => {
                    closeModal(document.getElementById('room-modal'));
                    updateStatus();
                }, 150);
            });
            roomListEl.appendChild(btn);
        });
    } catch (e) {
        console.warn("Kunde inte ladda spelare", e);
        document.getElementById('current-room-name').textContent = "Nätverksfel";
    }
}

function capitalize(str) {
    return str ? str.charAt(0).toUpperCase() + str.slice(1) : str;
}

function closeModal(modal) {
    modal.classList.add('closing');
    setTimeout(() => modal.classList.remove('active', 'closing'), 350);
}

async function showGridModal(modalId, gridId, fetchUrl, itemMapper, cacheData = null) {
    const modal = document.getElementById(modalId);
    const grid  = document.getElementById(gridId);
    modal.classList.add('active');
    
    // Om cacheData är null, tömmer vi listan omedelbart och visar laddningstext
    if (!cacheData) {
        grid.innerHTML = '<div style="grid-column:1/-1;text-align:center;padding:40px;color:var(--text-dim)">Hämtar nya tips...</div>';
    } else {
        // Om vi har cache (för Radio/Daily), rendera den direkt
        render(grid, cacheData, itemMapper, modal);
    }

    try {
        const freshData = await fetch(fetchUrl).then(r => r.json());
        // Rendera den färska datan (ersätter ev. laddningstext eller gammal cache)
        render(grid, freshData, itemMapper, modal);
        
        // Uppdatera cachen i bakgrunden (utom för tips om du vill spara bandbredd)
        if (modalId === 'radio-modal') cacheRadio = freshData;
        if (modalId === 'daily-modal') cacheDaily = freshData;
        if (modalId === 'playlist-modal') cachePlaylists = freshData;
        if (modalId === 'album-modal') cacheTips = freshData;
    } catch (e) {
        if (!grid.innerHTML || grid.innerHTML.includes('Hämtar')) {
            grid.innerHTML = '<div style="grid-column:1/-1;text-align:center;padding:20px;color:red;">Kunde inte hämta tips.</div>';
        }
    }
}

// En liten helper för att slippa duplicerad kod inuti showGridModal
function render(grid, items, itemMapper, modal) {
    grid.innerHTML = '';
    items.forEach(item => {
        const { art, title, subtitle, onSelect } = itemMapper(item);
        const card = document.createElement('div');
        card.className = 'album-card-item';
        card.innerHTML = `
            <div class="album-art-placeholder">
                <img src="${art}" loading="lazy" onerror="this.src='https://via.placeholder.com/300x300/111/444?text=?'">
            </div>
            <div class="album-info">
                <span class="alb-title">${title}</span>
                <span class="alb-artist">${subtitle}</span>
            </div>`;
        card.onclick = async () => {
            await onSelect();
            closeModal(modal);
            setTimeout(updateStatus, 1000);
        };
        grid.appendChild(card);
    });
}


// === 3. EVENT LISTENERS ===

function setupEventListeners() {
    // 1. RUMSVÄLJARE & TABS
    document.getElementById('room-select-btn').addEventListener('click', () => {
        transferMode = false;
        document.getElementById('tab-select').classList.add('active');
        document.getElementById('tab-transfer').classList.remove('active');
        document.getElementById('room-modal').classList.add('active');
        fetchPlayers();
    });

    document.getElementById('tab-select').addEventListener('click', () => {
        transferMode = false;
        document.getElementById('tab-select').classList.add('active');
        document.getElementById('tab-transfer').classList.remove('active');
        fetchPlayers();
    });

    document.getElementById('tab-transfer').addEventListener('click', () => {
        transferMode = true;
        document.getElementById('tab-transfer').classList.add('active');
        document.getElementById('tab-select').classList.remove('active');
        fetchPlayers();
    });

    // 2. VOLYM (Med throttling för att inte sänka servern)
    let lastVolumeSent = null;
    document.getElementById('volume-slider').addEventListener('input', e => {
        const val = e.target.value;
        lastVolumeSent = val;
        if (!volumeThrottleTimer) {
            volumeThrottleTimer = setInterval(() => {
                if (lastVolumeSent !== null) {
                    sendCommand('set_volume', { level: lastVolumeSent });
                    lastVolumeSent = null;
                }
            }, 300);
        }
    });

    document.getElementById('volume-slider').addEventListener('change', e => {
        clearInterval(volumeThrottleTimer);
        volumeThrottleTimer = null;
        sendCommand('set_volume', { level: e.target.value });
        lastVolumeSent = null;
    });

    // 3. TRANSPORTKONTROLLER
    document.getElementById('play-pause-btn').onclick = async () => {
        await sendCommand('toggle_play_pause');
        setTimeout(updateStatus, 300);
    };

    document.getElementById('next-btn').onclick = async () => {
        await sendCommand('next');
        setTimeout(updateStatus, 500);
    };

    document.getElementById('prev-btn').onclick = async () => {
        await sendCommand('prev'); // Fixade även så denna ropar på 'prev' istället för 'next'
        setTimeout(updateStatus, 500);
    };

    // 4. MENYKNAPPAR (Med Cache-stöd för omedelbar respons)

    // RADIO (Ersätter gamla Random Album)
    document.getElementById('radio-btn').onclick = () => {
        if (!currentRoom) { alert("Välj ett rum först!"); return; }
        showGridModal('radio-modal', 'radio-grid', '/get_radio_favorites', station => ({
            art:      station.art,
            title:    station.name,
            subtitle: 'Radiokanal',
            onSelect: () => sendCommand('play_radio', { url: station.url })
        }), cacheRadio); 
    };

// TIPS PÅ ALBUM (Tvingar alltid ny hämtning)
document.getElementById('tips-album-btn').onclick = () => {
    if (!currentRoom) { alert("Välj ett rum först!"); return; }
    // Vi skickar 'null' istället för 'cacheTips' för att rensa gamla tips
    showGridModal('album-modal', 'album-grid', '/get_random_albums', album => ({
        art:      album.art,
        title:    album.title,
        subtitle: album.artist,
        onSelect: () => sendCommand('play_album', { album_id: album.id })
    }), null); 
};

    // DAILY MIXES
    document.getElementById('daily-mix-btn').onclick = () => {
        if (!currentRoom) { alert("Välj ett rum först!"); return; }
        showGridModal('daily-modal', 'daily-grid', '/get_daily_mixes', mix => ({
            art:      mix.art,
            title:    mix.title,
            subtitle: mix.description,
            onSelect: () => sendCommand('daily', { index: mix.id.split('.').pop() })
        }), cacheDaily);
    };

    // SPOTIFY LISTOR
    document.getElementById('open-playlists-btn').onclick = () => {
        if (!currentRoom) { alert("Välj ett rum först!"); return; }
        showGridModal('playlist-modal', 'playlist-list', '/get_playlists_with_art', pl => ({
            art:      pl.art,
            title:    pl.name,
            subtitle: 'Spotify Mix',
            onSelect: () => sendCommand('play_url', { url: pl.url })
        }), cachePlaylists);
    };

    // 5. STÄNG MODALER
    window.onclick = e => {
        if (e.target.classList.contains('modal')) closeModal(e.target);
    };

    document.querySelectorAll('.close-btn-large').forEach(btn => {
        btn.onclick = e => closeModal(e.target.closest('.modal'));
    });

    // 6. EDGAR RÖSTKNAPP
    setupEdgarVoice();
}

// === EDGAR VOICE ===

let edgarRecognition = null;
let edgarToastTimer  = null;

function showEdgarToast(text, persistent = false) {
    const toast = document.getElementById('edgar-toast');
    document.getElementById('edgar-toast-text').textContent = text;
    toast.classList.add('visible');
    clearTimeout(edgarToastTimer);
    if (!persistent) {
        edgarToastTimer = setTimeout(() => toast.classList.remove('visible'), 7000);
    }
}

function setupEdgarVoice() {
    const btn = document.getElementById('edgar-btn');
    const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;

    if (!SpeechRecognition) {
        btn.style.opacity = '0.4';
        btn.title = 'Röstinmatning stöds ej i denna webbläsare';
        return;
    }

    btn.addEventListener('click', () => {
        if (edgarRecognition) {
            edgarRecognition.stop();
            return;
        }

        edgarRecognition = new SpeechRecognition();
        edgarRecognition.lang = 'sv-SE';
        edgarRecognition.continuous = false;
        edgarRecognition.interimResults = false;

        edgarRecognition.onstart = () => {
            btn.classList.add('listening');
            showEdgarToast('Lyssnar...');
        };

        edgarRecognition.onresult = async (event) => {
            const transcript = event.results[0][0].transcript;
            showEdgarToast(`"${transcript}" — tänker...`, true);

            const roomName = document.getElementById('current-room-name').textContent;
            try {
                const resp = await fetch('/edgar_chat', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ message: transcript, room: currentRoom, default_room: roomName })
                });
                const data = await resp.json();
                showEdgarToast(data.reply || 'Edgar svarade inte.');
                setTimeout(updateStatus, 2000);
            } catch {
                showEdgarToast('Kunde inte nå Edgar.');
            }
        };

        edgarRecognition.onerror = (e) => {
            btn.classList.remove('listening');
            if (e.error !== 'no-speech') showEdgarToast(`Röstfel: ${e.error}`);
            edgarRecognition = null;
        };

        edgarRecognition.onend = () => {
            btn.classList.remove('listening');
            edgarRecognition = null;
        };

        edgarRecognition.start();
    });
}