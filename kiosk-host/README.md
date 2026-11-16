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

Blank and wake fade the dashboard page to/from black (a full-screen overlay driven over Chrome's
loopback DevTools port, `CDP_PORT`, default 9222) so the change is smooth; `FADE_MS` (default 1500)
sets the length, `0` disables it, and without Chrome the change is instant. The monitors expose no
backlight, so this is a page-level fade, then the panel is powered off (blank) or on first (wake).

### `kiosk-page-fade` (both kiosks)

`common/kiosk-page-fade.py` animates the switch between dashboards when the Dashboard Manager rotation
(or voice, route or manual navigation) changes dashboard. Home Assistant switches dashboards inside one
page (`history.pushState` then a `location-changed` event, no reload), so a script injected over the
loopback DevTools port (and re-injected on every page load) intercepts the event. Only a change of
dashboard (first path segment) animates; moving between views of one dashboard does not.

Settings come from the Dashboard Manager's **Kiosk Effects** card (Home Assistant, repo `ha-dashboard-manager`):
**Apply to kiosk** writes the kiosk's entry to `/config/www/kiosk_effects.json`, and each kiosk fetches
`http://HOME_ASSISTANT_HOST:8123/local/kiosk_effects.json` (set `KIOSK_EFFECTS_URL` and `KIOSK_ID`, the kiosk's
Browser Mod id, in its unit) every few seconds, so changes apply live; if Home Assistant is unreachable the last
good values are kept. The local files below are the fallback when no Dashboard Manager entry exists.

**Duplicate tabs.** Each Chrome restart on the CN60 left extra hidden Home Assistant tabs (2-4 of them) that
kept rendering dashboards in the background, loading the two-core box and making switch animations choppy
(and with no keyboard or mouse attached they cannot be closed by hand). For two minutes after each Chrome
launch, `kiosk-page-fade` closes hidden duplicate Home Assistant tabs; tabs opened later (troubleshooting)
are left alone. The cause was never pinned down (the profile's "open a specific page" start-up setting plus
the launcher URL explains one extra; the launcher now resets it, but the count still grew by one per restart).

Local setting, re-read live (no restart): `~/.config/kiosk/page-fade.json`

```json
{"mode": "fade", "seconds": 0.6, "hold_seconds": 0.5}
```

- `mode`: `none` (instant), `fade` (to black, switch while dark, back in) or `crossfade` (blend old into
  new with Chrome View Transitions)
- `seconds`: length of each fade (crossfade: the blend)
- `hold_seconds`: time allowed for the new dashboard to render after the switch

**Crossfade is not recommended on these boxes.** Both kiosks render in software (`--disable-gpu`); a
crossfade composes two full-screen layers and measured only 2-10 frames over the whole transition
(worst gap up to 3 s), i.e. choppy steps. The plain fade measured 12-15 frames. Crossfade is worth
trying on a kiosk with GPU rendering.

Wake timing comes from the same card (`blank_fade`, `wake_delay`, `wake_fade`, in seconds), with a local fallback in
`~/.config/kiosk/camera-wake.json`
(`{"fade_ms": 1500, "wake_delay_ms": 1400, "wake_fade_ms": 1000}`): after power-on the page stays black
for `wake_delay_ms` (1-3 s depending on how long the monitor takes to resync), then fades in over
`wake_fade_ms`; `fade_ms` is the blank fade-out.

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
