"""Speaker identification for Nina (evidence only: never routes, authorizes or calls tools).

Deliberately imports nothing: `client` is standard-library only (the voice server and CI use it without numpy/SpeechBrain), while
`identify`, `embed`, `store`, `web` and `worker` need numpy (and embed needs SpeechBrain, in the speaker-eval venv)."""
