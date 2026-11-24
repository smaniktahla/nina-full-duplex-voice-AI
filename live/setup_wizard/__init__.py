"""Post-install configuration wizard (admin only): /setup page + /api/setup/*.

core.py   settings schema, validation, backups and atomic writes of the PRIVATE files (household.json, env)
probes.py connectivity checks for each service the voice server talks to
web.py    aiohttp routes, mounted from server.py with one line
Nothing here ever writes inside the repository: only ~/.config/nina (or $NINA_HOUSEHOLD / $NINA_ENV).
"""
