# Working on Nina (people and Claude Code sessions)

## The collision rules
1. **Never commit to `main` directly.** One branch per task, named `s/<topic>` (e.g. `s/speaker-id`, `s/fix-chunking`). Open a PR; CI must be green; squash-merge.
2. **One working tree per session.** Use git worktrees so two sessions never share files on disk:
   `git -C ~/nina worktree add ~/nina-wt/<topic> -b s/<topic> origin/main`. Edit and test in your worktree, not in `~/nina` itself.
3. **Keep PRs small and say what they touch.** The hot files are `live/server.py` and `live/tools.py`: if your change is more than a few dozen lines there,
   say so in the PR description and rebase before asking for review. Prefer adding a module over growing `server.py`.
4. **Pull before you start, rebase before you push**: `git fetch && git rebase origin/main`.

## What needs a person's OK (put it in the PR)
- Anything that restarts or reconfigures production: llama-server (`--parallel`, ctx), Hermes, Home Assistant config or lexicon, the DocMost manifest.
- Anything that executes in the house (new writable intent, new group alias) or widens what a model may do.
- Anything that stores biometrics or personal data (voiceprints, `profile.json`).

## Testing
- CI (GitHub Actions) runs Needle's unit tests, syntax/undefined-name lint, a secrets check and the pure-logic tests in `tests/`.
- Everything needing GPUs, models, HA or Hermes runs **on AI2** (`live/selftest.py`, `wakeflow_test.py`, `toolstest.py`, `ops/verify_parallel2.py`). Paste results in the PR.
- **Do not run end-to-end tests against the live port while someone is using it** (they share one llama slot and the log). Use a different `PORT` and `VOICE_SLOT`.
- Never trust the voice server's replies in a test without reading its log (`live/server.log`): the model can sound right and be wrong.

## Never commit
Private keys/certs (`*.pem`), `.env`, tokens, `profile.json`, voice recordings or embeddings, model weights, server logs. `scripts/check_no_secrets.sh` enforces the obvious ones.
The repo is **private** because `needle-voice/tests/real_snapshot.json` and `lexicon` data describe the household (device and room names).

## Deploying
Runtime flags that must survive a redeploy (e.g. `RESPONDER=1`) go in the private `~/.config/nina/env` (one `KEY=value` per line); `live/run.sh` reads it, explicit environment variables still win.

Merging does not deploy. See **docs/MIGRATION.md**: `scripts/deploy_voice.sh [ref] [--trial]` builds a release of a merged commit, switches `~/nina-current`, smoke-tests it and rolls back automatically on failure;
`scripts/rollback_voice.sh [legacy]` goes back. Needle's copy inside Home Assistant is a separate, explicitly approved step.
