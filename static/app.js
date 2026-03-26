const POLL_INTERVAL = 3000;
let currentRoom = localStorage.getItem('lastRoom') || "";
let volumeThrottleTimer = null;
let lastArtUrl = "";

window.addEventListener('DOMContentLoaded', () => {
    fetchPlayers();
    setupEventListeners();
    if (currentRoom) updateStatus();
    setInterval(updateStatus, POLL_INTERVAL);
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
        document.getElementById('vol-percentage').textContent = vol + "%";
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
            btn.textContent = capitalize(p.name);
            if (currentRoom === p.playerid) {
                btn.classList.add('active');
                currentRoomNameEl.textContent = capitalize(p.name);
            }
            btn.addEventListener('click', () => {
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

// Gemensam helper för album/mix/spelliste-modaler
async function showGridModal(modalId, gridId, fetchUrl, itemMapper) {
    const modal = document.getElementById(modalId);
    const grid  = document.getElementById(gridId);
    modal.classList.add('active');
    grid.innerHTML = '<div style="grid-column:1/-1;text-align:center;padding:40px;color:var(--text-dim)">Hämtar...</div>';
    try {
        const items = await fetch(fetchUrl).then(r => r.json());
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
    } catch (e) {
        grid.innerHTML = '<div style="grid-column:1/-1;text-align:center;padding:20px;color:red;">Kunde inte hämta data.</div>';
    }
}


// === 3. EVENT LISTENERS ===

function setupEventListeners() {
    document.getElementById('room-select-btn').addEventListener('click', () => {
        document.getElementById('room-modal').classList.add('active');
        fetchPlayers();
    });

    document.getElementById('volume-slider').addEventListener('input', e => {
        document.getElementById('vol-percentage').textContent = e.target.value + "%";
        clearTimeout(volumeThrottleTimer);
        volumeThrottleTimer = setTimeout(() => sendCommand('set_volume', { level: e.target.value }), 150);
    });

    document.getElementById('play-pause-btn').onclick = async () => {
        await sendCommand('toggle_play_pause');
        setTimeout(updateStatus, 300);
    };

    document.getElementById('next-btn').onclick = async () => {
        await sendCommand('next');
        setTimeout(updateStatus, 500);
    };

    document.getElementById('prev-btn').onclick = async () => {
        await sendCommand('next');
        setTimeout(updateStatus, 500);
    };

    document.getElementById('random-album-btn').onclick = async () => {
        await sendCommand('play_random_album');
        setTimeout(updateStatus, 1000);
    };

    document.getElementById('tips-album-btn').onclick = () => {
        if (!currentRoom) { alert("Välj ett rum först!"); return; }
        showGridModal('album-modal', 'album-grid', '/get_random_albums', album => ({
            art:      album.art,
            title:    album.title,
            subtitle: album.artist,
            onSelect: () => sendCommand('play_album', { album_id: album.id })
        }));
    };

    document.getElementById('daily-mix-btn').onclick = () => {
        showGridModal('daily-modal', 'daily-grid', '/get_daily_mixes', mix => ({
            art:      mix.art,
            title:    mix.title,
            subtitle: mix.description,
            onSelect: () => sendCommand('daily', { index: mix.id.split('.').pop() })
        }));
    };

    document.getElementById('open-playlists-btn').onclick = () => {
        showGridModal('playlist-modal', 'playlist-list', '/get_playlists_with_art', pl => ({
            art:      pl.art,
            title:    pl.name,
            subtitle: 'Spotify Mix',
            onSelect: () => sendCommand('play_url', { url: pl.url })
        }));
    };

    // Stäng modaler
    window.onclick = e => {
        if (e.target.classList.contains('modal')) closeModal(e.target);
    };
    document.querySelectorAll('.close-btn-large').forEach(btn => {
        btn.onclick = e => closeModal(e.target.closest('.modal'));
    });
}
