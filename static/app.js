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
        const response = await fetch(`/${endpoint}?${urlParams.toString()}`);
        console.log(`/${endpoint}?${urlParams.toString()}`);
        return await response.text();
    } catch (e) { return null; }
}

async function updateStatus() {
    const slider = document.getElementById('volume-slider');
    if (!currentRoom || document.activeElement === slider) return;

    try {
        const [titleData, vol, artUrl, mode] = await Promise.all([
            sendCommand('title'),
            sendCommand('status'),
            fetch(`/art?room=${encodeURIComponent(currentRoom)}`).then(r => r.text()),
            sendCommand('status_raw')
        ]);

        // Artist & Titel
        const trackTitleEl = document.getElementById('track-title');
        const trackArtistEl = document.getElementById('track-artist');
        
        if (titleData && titleData.includes(' - ')) {
            const parts = titleData.split(' - ');
            trackArtistEl.textContent = parts[1].trim(); 
            trackTitleEl.textContent = parts[0].trim();  
        } else {
            trackTitleEl.textContent = titleData || "Pausad";
            trackArtistEl.textContent = "MultiLyrion";
        }

        // Volym
        if (vol && !isNaN(vol)) {
            slider.value = vol;
            document.getElementById('vol-percentage').textContent = vol + "%";
        }

        // Album Art (Uppdaterar även den dynamiska bakgrunden)
        if (artUrl && artUrl !== lastArtUrl) {
            const artImg = document.getElementById('album-art');
            const bgArtBlur = document.getElementById('bg-art-blur');
            
            const tempImg = new Image();
            tempImg.src = artUrl;
            tempImg.onload = () => {
                artImg.src = artUrl;
                bgArtBlur.style.backgroundImage = `url('${artUrl}')`;
                lastArtUrl = artUrl;
            };
        }

        // Play/Pause Ikon
        const playBtn = document.getElementById('play-pause-btn');
        if (mode && mode.trim() === "play") {
            playBtn.innerHTML = '<svg viewBox="0 0 24 24"><path d="M6 19h4V5H6v14zm8-14v14h4V5h-4z"/></svg>';
        } else {
            playBtn.innerHTML = '<svg viewBox="0 0 24 24"><path fill="currentColor" d="M8 5v14l11-7z"/></svg>';
        }

    } catch (e) { console.warn("Poll error"); }
}

// === 2. DATAHÄMTNING (RUM & ALBUM) ===
async function fetchPlayers() {
    try {
        const response = await fetch('/get_players');
        const data = await response.json();
        
        const roomListContainer = document.querySelector('.room-list');
        const currentRoomNameEl = document.getElementById('current-room-name');
        
        roomListContainer.innerHTML = ''; 
        
        if (data && data.players_loop) { 
            data.players_loop.forEach((p, index) => {
                const btn = document.createElement('button');
                btn.className = 'room-item';
                btn.dataset.id = p.playerid; 
                btn.textContent = p.name;

                // Sätt default-rum
                if (!currentRoom && index === 0) {
                    currentRoom = p.playerid;
                    localStorage.setItem('lastRoom', currentRoom);
                    currentRoomNameEl.textContent = p.name;
                }

                // Markera valt rum
                if (currentRoom === p.playerid) {
                    btn.classList.add('active');
                    currentRoomNameEl.textContent = p.name;
                }

                // Klick-event för rum
                btn.addEventListener('click', (e) => {
                    document.querySelectorAll('.room-item').forEach(r => r.classList.remove('active'));
                    e.target.classList.add('active');
                    
                    currentRoom = p.playerid;
                    localStorage.setItem('lastRoom', currentRoom);
                    currentRoomNameEl.textContent = p.name;
                    lastArtUrl = ""; 
                    
                    setTimeout(() => {
                        document.getElementById('room-modal').classList.remove('active');
                        updateStatus();
                    }, 150);
                });

                roomListContainer.appendChild(btn);
            });
        } else {
            roomListContainer.innerHTML = '<p style="text-align:center; color: var(--text-dim);">Inga spelare hittades.</p>';
        }
    } catch (e) { 
        console.warn("Kunde inte ladda spelare", e); 
        document.getElementById('current-room-name').textContent = "Nätverksfel";
    }
}

async function showRandomSelection() {
    if (!currentRoom) {
        alert("Välj ett rum först!");
        return;
    }

    const modal = document.getElementById('album-modal');
    const grid = document.getElementById('album-grid');
    
    modal.classList.add('active');
    grid.innerHTML = '<div style="grid-column: 1 / -1; text-align:center; padding:40px; color: var(--text-dim);">Hämtar inspiration...</div>';

    try {
        const response = await fetch('/get_random_albums');
        const albums = await response.json();
        
        grid.innerHTML = ''; 

        albums.forEach(album => {
            const card = document.createElement('div');
            card.className = 'album-card-item'; 
            
            card.innerHTML = `
                <div class="album-art-placeholder">
                    <img src="${album.art}" loading="lazy" onerror="this.src='https://via.placeholder.com/300x300/111/444?text=No+Art'">
                </div>
                <div class="album-info">
                    <span class="alb-title">${album.title}</span>
                    <span class="alb-artist">${album.artist}</span>
                </div>
            `;
            
            card.onclick = async () => {
                await sendCommand('play_album', { album_id: album.id });
                modal.classList.remove('active');
                setTimeout(updateStatus, 1000);
            };

            grid.appendChild(card);
        });
    } catch (err) {
        grid.innerHTML = '<div style="grid-column: 1 / -1; text-align:center; padding:20px; color:red;">Kunde inte hämta album.</div>';
    }
}

// === 3. EVENT LISTENERS ===
function setupEventListeners() {
    
    // --- Toppmeny & Volym ---
    document.getElementById('room-select-btn').addEventListener('click', () => {
        document.getElementById('room-modal').classList.add('active');
        fetchPlayers(); 
    });

    document.getElementById('volume-slider').addEventListener('input', (e) => {
        const val = e.target.value;
        document.getElementById('vol-percentage').textContent = val + "%";
        clearTimeout(volumeThrottleTimer);
        volumeThrottleTimer = setTimeout(() => sendCommand('volume', { level: val }), 150);
    });

    // --- Transport (Play/Pause/Next/Prev) ---
    document.getElementById('play-pause-btn').onclick = async () => {
        await sendCommand('pause');
        setTimeout(updateStatus, 300);
    };

    document.getElementById('next-btn').onclick = async () => {
        await sendCommand('next');
        setTimeout(updateStatus, 500);
    };

    document.getElementById('prev-btn').onclick = async () => {
        await sendCommand('next'); // Enligt original
        setTimeout(updateStatus, 500);
    };

    // --- Bottenmeny ---
    document.getElementById('random-album-btn').onclick = async () => {
        await sendCommand('play');
        setTimeout(updateStatus, 1000);
    };

    document.getElementById('tips-album-btn').onclick = showRandomSelection;

document.getElementById('daily-mix-btn').onclick = async () => {
        const modal = document.getElementById('daily-modal');
        const grid = document.getElementById('daily-grid');
        
        modal.classList.add('active');
        grid.innerHTML = '<div style="grid-column: 1 / -1; text-align:center; padding:40px; color: var(--text-dim);">Hämtar dina mixar...</div>';
        
        try {
            const res = await fetch('/get_daily_mixes');
            const mixes = await res.json();
            grid.innerHTML = '';

            mixes.forEach(mix => {
                const card = document.createElement('div');
                card.className = 'album-card-item';
                
                // Vi använder samma kvadratiska grid som för album!
                card.innerHTML = `
                    <div class="album-art-placeholder">
                        <img src="${mix.art}" loading="lazy" onerror="this.src='https://via.placeholder.com/300x300/111/444?text=Mix'">
                    </div>
                    <div class="album-info">
                        <span class="alb-title">${mix.title}</span>
                        <span class="alb-artist">${mix.description}</span>
                    </div>
                `;
                
                card.onclick = async () => {
                    // Vi extraherar indexet (t.ex. "0" från "playlists.0")
                    const idx = mix.id.split('.').pop();
                    await sendCommand('daily', { index: idx });
                    modal.classList.remove('active');
                    setTimeout(updateStatus, 1000);
                };
                grid.appendChild(card);
            });
        } catch (e) {
            grid.innerHTML = '<div style="grid-column: 1 / -1; text-align:center; padding:20px; color:red;">Kunde inte hämta mixar.</div>';
        }
    };

    // --- Spellistor (Hämtar från nya JSON-routen) ---
    document.getElementById('open-playlists-btn').onclick = async () => {
        const modal = document.getElementById('playlist-modal');
        const list = document.getElementById('playlist-list');
        
        modal.classList.add('active');
        list.innerHTML = '<div style="grid-column: 1 / -1; text-align:center; padding:40px; color: var(--text-dim);">Laddar listor...</div>';
        
        try {
            const res = await fetch('/get_playlists_with_art');
            const playlists = await res.json(); 
            
            list.innerHTML = '';
            
            playlists.forEach(pl => {
                const card = document.createElement('div');
                card.className = 'album-card-item';
                
                card.innerHTML = `
                    <div class="album-art-placeholder">
                        <img src="${pl.art}" loading="lazy" onerror="this.src='https://via.placeholder.com/300x300/111/444?text=List'">
                    </div>
                    <div class="album-info">
                        <span class="alb-title">${pl.name}</span>
                        <span class="alb-artist">Spotify Mix</span>
                    </div>
                `;
                
                card.onclick = async () => {
                    await sendCommand('play_url', { url: pl.url });
                    modal.classList.remove('active');
                    setTimeout(updateStatus, 1000);
                };
                list.appendChild(card);
            });
        } catch (e) {
            list.innerHTML = '<div style="grid-column: 1 / -1; text-align:center; padding:20px; color:red;">Kunde inte hämta listor.</div>';
        }
    };

    // --- Stäng Modaler ---
    window.onclick = (e) => { 
        if (e.target.classList.contains('modal')) {
            e.target.classList.remove('active');
        }
    };
    
    document.querySelectorAll('.close-btn-large').forEach(btn => {
        btn.onclick = (e) => {
            e.target.closest('.modal').classList.remove('active');
        }
    });
}