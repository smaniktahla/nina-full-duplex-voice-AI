# Migrating the running service to the repo

**Goal:** the voice server and the Needle code it imports run from a release of `main` in this repository, never from a directory someone is editing, with a one-command rollback.

## Before and after
| | Before | After |
|---|---|---|
| Code that runs | `~/claude-workspace/duplex-sim` + `~/needle-voice` (edited in place by several sessions) | `~/nina-current` -> `~/nina-releases/<sha>` (a detached worktree of a merged commit) |
| Where you edit | the same directories | `~/nina-wt/<topic>` (one git worktree per session), then PR |
| Private runtime files | `live/cert.pem`, `live/voices/` inside the code tree | `~/.local/share/nina/` (cert, key, voices, `DEPLOYED`, `server.log`) |
| Household settings | constants in code | `~/.config/nina/household.json` (private) |
| Start / stop | `nohup ./run.sh`, `pkill` | `systemctl --user ... nina-voice` (optional) or `scripts/deploy_voice.sh` |
| Rollback | none | automatic during deploy; `scripts/rollback_voice.sh [legacy]` |

`~/nina-current` initially points at the **legacy tree**, so switching on the layout changes nothing until the first real deploy. Needle for HA (`/config/custom_components/needle_voice`) is **not** part of this: the live voice server imports Needle from the same release (`needle-voice/` next to `live/`), and the HA copy is only updated by an explicit, separately approved step.

## Cutover order (do not skip steps)
1. **Merge the migration-kit PR** (this one). Nothing runs differently yet.
2. **Land in-flight work as PRs first.** Anything edited in the legacy trees since the import (today: the *responder* work: `live/responder.py`, `responder_test.py`, changes to `live/server.py`/`tools.py`, `PIPELINE.md`; and the speaker-ID assessment) must become a PR, otherwise the cutover would silently drop it. See "What each session must do".
3. **Trial the exact commit** on a spare port, without touching the live service:
   `scripts/deploy_voice.sh origin/main --trial` (Piper, always-on, port 8444; runs `scripts/smoke_voice.py`).
4. **Cut over** when nobody is connected: `scripts/deploy_voice.sh` (default ref `origin/main`). It checks CI is green for the commit, refuses if clients are connected (`--force` to override),
   builds the release, stops the old server and waits for GPU memory to free, switches `~/nina-current`, starts the new one, waits for `/api/config`, runs the smoke test (Kokoro loaded, wake words present, a typed question answered),
   and **rolls back automatically** to the previous target if any of that fails.
5. **Make it a service (do this: without it the voice server stays down after every reboot).** AI2 reboots weekly (Wed 03:00); a `nohup` server does not come back, which left voice down for ~6.5 hours on 2026-10-21. The unit waits for the local llama-server, restarts on failure and retries forever:
   ```
   mkdir -p ~/.config/systemd/user && cp live/ops/nina-voice.service ~/.config/systemd/user/ && systemctl --user daemon-reload
   sudo loginctl enable-linger salil && systemctl --user enable --now nina-voice
   ```
   (`deploy_voice.sh` and `rollback_voice.sh` use the unit automatically when it exists; logs go to `~/.local/share/nina/server.log`. Installed on AI2 2026-10-21; a `kill -9` of the server was seen to restart it.)
6. **After one stable day:** rename the legacy trees (`duplex-sim` -> `duplex-sim.legacy`, `needle-voice` -> `needle-voice.legacy`) so nobody edits them by accident. Keep them for a week as the `legacy` rollback target, then delete. (`~/nina-current` must no longer point at them first.)

## Rollback, from smallest to largest
| Situation | Command |
|---|---|
| New release fails its smoke test during deploy | automatic: previous target is restarted and the script exits non-zero |
| New release deployed fine but is wrong | `scripts/rollback_voice.sh` (previous release) |
| Anything weird, want exactly the pre-migration behaviour | `scripts/rollback_voice.sh legacy` (runs `~/claude-workspace/duplex-sim` untouched) |
| A specific commit | `scripts/rollback_voice.sh <sha>` (must be a directory in `~/nina-releases/`) |
| The tooling itself is the problem | `cd ~/claude-workspace/duplex-sim/live && ./run.sh` (the old way still works until step 6) |

Every deploy records `current`, `previous` and the time in `~/.local/share/nina/DEPLOYED`.

## What each session must do
**Every session**
- Stop editing `~/claude-workspace/duplex-sim` and `~/needle-voice` once the kit is merged. New work: `git -C ~/nina fetch && git -C ~/nina worktree add ~/nina-wt/<topic> -b s/<topic> origin/main`, edit there, `make test`, push, PR, wait for CI, squash-merge.
- Test end-to-end against a **trial instance** (`scripts/deploy_voice.sh s/<topic> --trial`, or `PORT=8445 VOICE_SLOT=-1 TTS=piper WAKE=off live/run.sh` from your worktree), never against `:8443` while someone is using it.
- Household facts (names, addresses, calendar ids, entity ids, hosts) go in `~/.config/nina/household.json`, never in code, docs or fixtures: the repository is public. Run `git grep -nE 'maniktahla|10\.10\.10\.'` before pushing.
- Merging does not deploy. Say in the PR when a deploy or a restart is needed; the deploy is a separate, explicit `scripts/deploy_voice.sh`.

**The responder session** (edits already made in the legacy tree)
1. Worktree from main (above).
2. Bring your files over with a 3-way merge, using your own backup as the base so main's scrub is kept:
   `cp ~/claude-workspace/duplex-sim/live/responder.py ~/claude-workspace/duplex-sim/live/responder_test.py live/`
   `scripts/import_from_old_tree.sh live/server.py ~/claude-workspace/duplex-sim/live/server.py.pre-responder`
   For `live/tools.py` (no backup was kept) re-apply your small change by hand on main's version: **do not copy the legacy file over main's**, it contains the un-scrubbed calendar ids, hosts and weather entities.
3. Add `responder_test.py` to CI if it needs no GPU (move it under `tests/`), run `make test`, open the PR. Keep `RESPONDER` off by default until you have trial-instance results.

**The speaker-ID session**
- Work in `s/speaker-id`; keep new code in new modules and make the hooks in `server.py` / `needle-voice/needle_voice/session.py` small and separate (those are the hot files).
- Voiceprints, embeddings and recordings never enter git (they are covered by `.gitignore` patterns for `*.npy`, `*.wav`, `speakers/`; add yours if different). The assessment document is already in the repo with names generalised.
- Evaluate on CPU or another host; AI2's GPUs have <1 GB free.

**Needle changes** (`needle-voice/`): tests in `needle-voice/tests` run in CI. Needle has two homes: this repo and the copy HA runs. A merged change reaches HA only through an explicit deploy that Salil approves.

**Session that touches production** (llama-server, Hermes, HA config, the DocMost manifest): needs Salil's OK; put the exact change and the rollback in the PR description.
