# Kiosk host configuration

Host-side setup for the two wall/desk kiosks that run the Nina kiosk client
(`../kiosk-client/`). Values in `ALL_CAPS` are placeholders: real hosts and
URLs live in the private household config, not here.

| | CN60 (GNOME / Wayland) | N5105 (XFCE / X11) |
|---|---|---|
| Browser | Chrome, `--start-fullscreen` | Chrome, `--start-fullscreen` |
| Exit fullscreen | "Full Screen" extension (swipe down, hold ESC), F11 | same |
| Screen wake/blank | `kiosk-camera-wake` (gnome backend, USB webcam) | `kiosk-camera-wake` (x11 backend, Kinect stream) |
| Crash watchdog | `common/kiosk-watchdog.user.service` (user unit) | `common/kiosk-watchdog.system.service` (system unit, user `kiosk`) |

## Design rule: fullscreen, never `--kiosk`

The kiosks must switch between "locked down, clean interface" and "give me
tools to troubleshoot". `--kiosk` hides the browser UI, ignores F11 and made
leaving fullscreen impossible on the XFCE box. `--start-fullscreen` starts
clean and lets you drop out to tabs, address bar and DevTools. Install the
"Full Screen" Chrome extension (id `gmimocjjppdelmhpcmpkhekmpoddgima`) from the
Web Store into the kiosk profile; no Google sign-in is needed.

## `kiosk-camera-wake` (both kiosks)

`common/kiosk-camera-wake.py` is camera-agnostic: each kiosk points `MOTION_URL` (in its unit) at
its own local camera MJPEG stream, and picks a screen backend with `SCREEN_BACKEND` (`gnome` =
Mutter `PowerSaveMode` on Wayland, `x11` = `xset`/`xprintidle`). No Frigate or network is involved.

One ffmpeg scales the stream to 96x54 gray at 2 fps; frames are compared (camera-wide exposure
shifts are ignored). Rules:

- screen off: wake after 2 consecutive motion frames (one noisy frame is ignored)
- screen on: every motion frame restarts the quiet timer, so someone who keeps moving stays lit
- blank after `ABSENT_AFTER` s (default 60) with no motion **and** no input, so walking away does
  not blank instantly
- camera stream down for 30 s: fall back to blanking after `BLANK_AFTER` s (default 300) of no input
- input, or `touch ~/.cache/screen-poke`, always wakes and restarts the timers. Hook this to Nina
  (wake word, route/camera popups, timer alarms).

The controller owns blanking, so the desktop's own idle blank must be off: on the CN60 GNOME's
`idle-delay` is 0 (GNOME 50 never blanked at 300 s anyway); on the N5105 the controller sets
`xset s off; xset dpms 0 0 0` at start.

### CN60 camera: `webcam-mjpeg`

`cn60/webcam-mjpeg.py` shares the USB webcam over HTTP (`/detect.mjpeg`, `/snapshot.jpg`, default
port 8769) so the controller and Nina can both use it. It passes the camera's JPEGs through
without re-encoding (about 1% CPU) and thins them to 5 fps.

### N5105 camera

Reads the Kinect RGB stream served locally on port 8768 by the existing Kinect capture service.
The older `kiosk-kinect-wake` service (Kinect motion plus a Frigate person check) is superseded.

## N5105: XFCE

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
