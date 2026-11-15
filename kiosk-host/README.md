# Kiosk host configuration

Host-side setup for the two wall/desk kiosks that run the Nina kiosk client
(`../kiosk-client/`). Values in `ALL_CAPS` are placeholders: real hosts and
URLs live in the private household config, not here.

| | CN60 (GNOME / Wayland) | N5105 (XFCE / X11) |
|---|---|---|
| Browser | Chrome, `--start-fullscreen` | Chrome, `--start-fullscreen` |
| Exit fullscreen | "Full Screen" extension (swipe down, hold ESC), F11 | same |
| Screen blank | `cn60/screen-blank.py`: presence-driven (60 s after the last person seen), 5 min idle fallback | `xset` autostart (5 min), `n5105/screen-blank-5min.desktop` |
| Crash watchdog | `common/kiosk-watchdog.user.service` (user unit) | `common/kiosk-watchdog.system.service` (system unit, user `kiosk`) |

## Design rule: fullscreen, never `--kiosk`

The kiosks must switch between "locked down, clean interface" and "give me
tools to troubleshoot". `--kiosk` hides the browser UI, ignores F11 and made
leaving fullscreen impossible on the XFCE box. `--start-fullscreen` starts
clean and lets you drop out to tabs, address bar and DevTools. Install the
"Full Screen" Chrome extension (id `gmimocjjppdelmhpcmpkhekmpoddgima`) from the
Web Store into the kiosk profile; no Google sign-in is needed.

## CN60: `screen-blank`

GNOME 50 never blanked at a 300 s idle-delay (only short delays fired), so
GNOME's timer is disabled (`gsettings set org.gnome.desktop.session idle-delay 0`)
and `screen-blank.py` owns it: it polls Mutter's idle time and sets
`PowerSaveMode` 3 after `BLANK_AFTER` seconds (default 300). It wakes the panel
on real input itself (Mutter does not wake a D-Bus-forced blank). Poke it with
`touch ~/.cache/screen-poke` to wake the panel and restart the timer, e.g. from
a Nina wake word, route popup or timer alarm.

Install: copy `screen-blank.py` to `~/bin/`, `screen-blank.service` to
`~/.config/systemd/user/`, then `systemctl --user enable --now screen-blank`.

### CN60: webcam presence

`cn60/webcam-mjpeg.py` (+ `webcam-mjpeg.service`) shares the USB webcam over HTTP
(`/detect.mjpeg`, `/snapshot.jpg`, default port 8769) so Frigate and Nina can both use it.
Add it to Frigate as a detect-only camera (same pattern as the Kinect camera):

```yaml
  Office_Kiosk:
    mqtt: {enabled: false}
    ffmpeg:
      inputs:
        - {path: "http://KIOSK_HOST:8769/detect.mjpeg", input_args: preset-http-mjpeg-generic, roles: [detect]}
      hwaccel_args: []        # VAAPI fails to download frames from this MJPEG; use software decode
    detect: {enabled: true, width: 1280, height: 720, fps: 5}
    objects: {track: [person]}
    record: {enabled: false}
    snapshots: {enabled: false}
    face_recognition: {enabled: false}
```

`screen-blank.py` then subscribes to Frigate's websocket (`FRIGATE_WS`, `PRESENCE_CAMERA` in the
unit) and blanks only after `ABSENT_AFTER` s (default 60) with no person seen **and** no input,
so walking away does not blank instantly. A person appearing wakes the screen at once. If the feed
is down for 30 s it falls back to the 5-minute idle timer.

## N5105: XFCE

- Blank after 5 min: install `n5105/screen-blank-5min.desktop` in
  `~/.config/autostart/`. Wake from software with `DISPLAY=:0 xset dpms force on`.
- XFCE shows its Display dialog when the monitor re-announces itself after an
  overnight blank. Turn it off:
  `xfconf-query -c displays -p /Notify -s 0` and
  `xfconf-query -c displays -p /AutoEnableProfiles -s 0` (as the kiosk user).
- Launcher: `n5105/home-assistant-kiosk.desktop.example`.

## Crash watchdog (both)

Chrome occasionally dies with SIGILL (a deliberate `ud2` abort in a child
process, not a CPU fault). `kiosk-watchdog.py` talks to Chrome over the
loopback DevTools port, reopens a crashed tab, and writes a flight-recorder
JSON (last 120 s of page events plus CPU/memory samples) to `~/kiosk-watchdog/crashes/`.
Needs `python3-websocket` (or `pip install --user websocket-client`). Set in the
unit: `KIOSK_WD_URL` (dashboard URL for the fresh tab, required) and
`KIOSK_WD_ATQ` (task-queue base URL; empty disables the low-priority task it
files after a crash).
