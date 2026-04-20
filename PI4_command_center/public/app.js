const apiUrl = window.location.origin;

async function fetchNodes() {
    const container = document.getElementById('nodes-container');
    try {
        const res = await fetch(`${apiUrl}/nodes`);
        const data = await res.json();
        
        container.innerHTML = '';
        
        if (!data.nodes || Object.keys(data.nodes).length === 0) {
            container.innerHTML = '<div class="glass-card">No actuators discovered on network.</div>';
            return;
        }

        for (const [nodeId, info] of Object.entries(data.nodes)) {
            const isOnline = info.age_s < 300; // less than 5 min
            const statusClass = isOnline ? 'status-online' : 'status-offline';
            const statusText = isOnline ? 'Online' : 'Offline';
            const locationText = info.location ? `📍 ${info.location}` : 'Unmapped Node';

            const card = document.createElement('div');
            card.className = 'glass-card';
            card.innerHTML = `
                <div class="node-header">
                    <div>
                        <div class="node-title">${nodeId}</div>
                        <div class="node-location">${locationText}</div>
                        <div style="font-size: 0.8rem; color: var(--text-muted); margin-top: 4px;">IP: ${info.ip}</div>
                    </div>
                    <div class="status-badge ${statusClass}">${statusText}</div>
                </div>
                ${info.location && isOnline ? `
                <div class="node-actions">
                    <button class="action-btn" onclick="triggerAction('${info.location}', 'left_once')">Left</button>
                    <button class="action-btn" onclick="triggerAction('${info.location}', 'turn_demo')">Center/Demo</button>
                    <button class="action-btn" onclick="triggerAction('${info.location}', 'right_once')">Right</button>
                </div>
                ` : ''}
                ${!info.location && isOnline ? `
                <div style="font-size:0.85rem; color:#f39c12;">Action restricted: Node unmapped. Map to a location below.</div>
                ` : ''}
            `;
            container.appendChild(card);
        }

    } catch (err) {
        console.error(err);
        container.innerHTML = '<div class="glass-card" style="color: #ff4757;">Failed to connect to Command Center.</div>';
    }
}

async function triggerAction(location, action) {
    try {
        const res = await fetch(`${apiUrl}/trigger-location`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ location, action })
        });
        const data = await res.json();
        if (!data.ok) {
            alert(`Error: ${data.error}`);
        }
    } catch (e) {
        alert("Failed to send action");
    }
}

document.getElementById('refresh-btn').addEventListener('click', fetchNodes);

document.getElementById('map-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const nodeId = document.getElementById('node-id').value.trim();
    const location = document.getElementById('location-name').value.trim();
    
    try {
        const res = await fetch(`${apiUrl}/map-location`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ node: nodeId, location: location })
        });
        if (res.ok) {
            document.getElementById('location-name').value = '';
            document.getElementById('node-id').value = '';
            fetchNodes();
        } else {
            const data = await res.json();
            alert(`Map Failed: ${data.error}`);
        }
    } catch(err) {
        alert("Request Failed");
    }
});

// Initial load
fetchNodes();
// Refresh every 10 seconds
setInterval(fetchNodes, 10000);

// --- Voice Recording Logic ---
let mediaRecorder;
let audioChunks = [];
const recordBtn = document.getElementById('record-btn');
const recordStatus = document.getElementById('record-status');

if (navigator.mediaDevices && navigator.mediaDevices.getUserMedia) {
    let recording = false;

    const startRecording = async () => {
        try {
            const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
            mediaRecorder = new MediaRecorder(stream);
            audioChunks = [];
            
            mediaRecorder.ondataavailable = e => {
                if (e.data.size > 0) audioChunks.push(e.data);
            };
            
            mediaRecorder.onstop = async () => {
                const audioBlob = new Blob(audioChunks, { type: 'audio/webm' });
                recordStatus.innerText = "Processing voice command...";
                
                try {
                    const res = await fetch(`${apiUrl}/api/upload-audio`, {
                        method: 'POST',
                        body: audioBlob,
                        headers: {
                            'Content-Type': 'audio/webm'
                        }
                    });
                    if (res.ok) {
                        recordStatus.innerText = "Command executed successfully!";
                        setTimeout(() => recordStatus.style.display = 'none', 3000);
                    } else {
                        recordStatus.innerText = "Command failed to process.";
                    }
                } catch(e) {
                    console.error(e);
                    recordStatus.innerText = "Network error sending command.";
                }
                
                stream.getTracks().forEach(track => track.stop());
            };
            
            mediaRecorder.start();
            recording = true;
            recordBtn.style.background = '#ff4757';
            recordBtn.innerText = 'Listening (Release to Send)';
            recordStatus.innerText = "Recording...";
            recordStatus.style.display = 'block';
        } catch (err) {
            console.error(err);
            alert("Microphone access denied or unavailable (HTTPS required).");
        }
    };

    const stopRecording = () => {
        if (recording && mediaRecorder.state !== "inactive") {
            mediaRecorder.stop();
            recording = false;
            recordBtn.style.background = 'var(--primary)';
            recordBtn.innerText = '🎤 Hold to Speak Command';
        }
    };

    // Support both mouse and touch for mobile
    recordBtn.addEventListener('mousedown', startRecording);
    recordBtn.addEventListener('mouseup', stopRecording);
    recordBtn.addEventListener('mouseleave', stopRecording);
    recordBtn.addEventListener('touchstart', (e) => { e.preventDefault(); startRecording(); });
    recordBtn.addEventListener('touchend', (e) => { e.preventDefault(); stopRecording(); });
} else {
    recordBtn.innerText = "🎤 Mic Not Available (Requires HTTPS)";
    recordBtn.disabled = true;
    recordBtn.style.opacity = 0.5;
}
