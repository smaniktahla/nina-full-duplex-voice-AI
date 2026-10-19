# Nina kiosk client

Runs Nina on the kitchen kiosk as a boot-time service instead of a browser tab: it streams the microphone to the Nina voice server and plays the speech that comes
back. Same WebSocket protocol as the web page (`live/static/index.html`); the server still decides when Nina is asleep or awake and matches the wake word.
Single file `nina_kiosk.py` (numpy, sounddevice, aiohttp), no UI, reconnects forever, drops the first 2.5 s of every microphone start (the capture device's resume burst
is fully clipped).

**Install on the kiosk (Linux, PipeWire):**
1. `python3 -m venv ~/nina-kiosk/venv && ~/nina-kiosk/venv/bin/pip install -r requirements.txt`; clone this repo to `~/nina-kiosk/src`.
2. `~/.config/nina-kiosk/config.json` from `config.example.json`, and the service token in `~/.config/nina-kiosk/service_token` (chmod 600). The token is a secret: it is
   `~/.config/nina/service_token` on the voice server and grants app access (never admin). Pin the server certificate with `cert_sha256`.
3. Optional but recommended: `pipewire-echo-cancel.conf` (WebRTC echo cancellation) and restart PipeWire.
4. `nina-kiosk.service` to `~/.config/systemd/user/`, `systemctl --user enable --now nina-kiosk.service`, `loginctl enable-linger $USER`.
`nina_kiosk.py --devices` lists audio devices; `--debug` logs transcripts. Tests: `tests/test_kiosk_client.py` (no audio hardware needed).
