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
    if (!currentRoom || document.activeElement.id === 'volume-slider') return;

    try {
        const [titleData, vol, artUrl, mode] = await Promise.all([
            sendCommand('title'),
            sendCommand('status'),
            fetch(`/art?room=${encodeURIComponent(currentRoom)}`).then(r => r.text()),
            sendCommand('status_raw')
        ]);

        // 1. Artist & Titel (Omvänd ordning i logiken för att matcha HTML)
        const trackTitleEl = document.getElementById('track-title');
        const trackArtistEl = document.getElementById('track-artist');
        
        if (titleData && titleData.includes(' - ')) {
            const parts = titleData.split(' - ');
            trackArtistEl.textContent = parts[1].trim(); // Artist
            trackTitleEl.textContent = parts[0].trim();  // Titel
        } else {
            trackTitleEl.textContent = titleData || "Pausad";
            trackArtistEl.textContent = "MultiLyrion";
        }

        // 2. Volym
        if (vol && !isNaN(vol)) {
            document.getElementById('volume-slider').value = vol;
            document.getElementById('vol-percentage').textContent = vol + "%";
        }

        // 3. Album Art (Blink-skydd)
        if (artUrl && artUrl !== lastArtUrl) {
            const artImg = document.getElementById('album-art');
            const tempImg = new Image();
            tempImg.src = artUrl;
            tempImg.onload = () => {
                artImg.src = artUrl;
                lastArtUrl = artUrl;
            };
        }

        // 4. Play/Pause Ikon
        const playBtn = document.getElementById('play-pause-btn');
        if (mode && mode.trim() === "play") {
            playBtn.innerHTML = '<svg viewBox="0 0 24 24"><path d="M6 19h4V5H6v14zm8-14v14h4V5h-4z"/></svg>';
        } else {
            playBtn.innerHTML = '<svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>';
        }

    } catch (e) { console.warn("Poll error"); }
}

async function fetchPlayers() {
    try {
        const response = await fetch('/get_players');
        const data = await response.json();
        const select = document.getElementById('room-select');
        select.innerHTML = '<option value="">Välj rum...</option>';
        if (data && data.players_loop) {
            data.players_loop.forEach(p => {
                const opt = document.createElement('option');
                opt.value = p.playerid; opt.textContent = p.name;
                select.appendChild(opt);
            });
            if (currentRoom) select.value = currentRoom;
        }
    } catch (e) { }
}

function setupEventListeners() {
    document.getElementById('room-select').addEventListener('change', (e) => {
        currentRoom = e.target.value;
        localStorage.setItem('lastRoom', currentRoom);
        lastArtUrl = ""; 
        updateStatus();
    });

    document.getElementById('volume-slider').addEventListener('input', (e) => {
        const val = e.target.value;
        document.getElementById('vol-percentage').textContent = val + "%";
        clearTimeout(volumeThrottleTimer);
        volumeThrottleTimer = setTimeout(() => sendCommand('volume', { level: val }), 150);
    });

    document.getElementById('play-pause-btn').onclick = async () => {
        await sendCommand('pause');
        setTimeout(updateStatus, 300);
    };

    document.getElementById('next-btn').onclick = async () => {
        await sendCommand('next');
        setTimeout(updateStatus, 500);
    };

    document.getElementById('prev-btn').onclick = async () => {
        await sendCommand('next'); // Prev mappat till nästa enligt önskemål
        setTimeout(updateStatus, 500);
    };

    document.getElementById('random-album-btn').onclick = async () => {
        await sendCommand('play');
        setTimeout(updateStatus, 1000);
    };

    document.getElementById('daily-mix-btn').onclick = async () => {
        const idx = Math.floor(Math.random() * 6);
        await sendCommand('daily', { index: idx });
        setTimeout(updateStatus, 1000);
    };

    const modal = document.getElementById('playlist-modal');
    document.getElementById('open-playlists-btn').onclick = async () => {
        modal.style.display = 'block';
        const list = document.getElementById('playlist-list');
        list.innerHTML = '<div style="text-align:center;padding:40px;opacity:0.5;">Laddar...</div>';
        const res = await fetch('/get_playlists');
        const text = await res.text();
        list.innerHTML = '';
        text.split('\n').forEach(line => {
            if (!line.trim()) return;
            const [name, url] = line.split('|');
            const btn = document.createElement('button');
            btn.className = 'playlist-item';
            btn.innerHTML = `<strong>${name}</strong>`;
            btn.onclick = async () => {
                await sendCommand('play_url', { url: url });
                modal.style.display = 'none';
                setTimeout(updateStatus, 1000);
            };
            list.appendChild(btn);
        });
    };

    document.querySelector('.close-btn-large').onclick = () => modal.style.display = 'none';
    window.onclick = (e) => { if (e.target == modal) modal.style.display = 'none'; };
}