"""Constants and config defaults for the Needle Voice conversation agent."""

DOMAIN = "needle_voice"

# Config comes from the `needle_voice:` block in configuration.yaml (see
# __init__.CONFIG_SCHEMA). Every key has a safe default.
DEFAULT_CONFIG = {
    "enabled": True,
    "needle_enabled": True,
    "qwen_fast_enabled": True,
    "hermes_fallback_enabled": True,
    "qwen_endpoint": "http://10.10.10.105:8082/v1",
    "qwen_model": "qwen-fast",
    "qwen_timeout_seconds": 2.0,
    "needle_endpoint": "http://10.10.10.105:8793",
    "random_enabled": True,
    # shared random-number service on AI2 (coin, dice, number, pick); "" turns the category off
    "rng_url": "http://10.10.10.226:8794",
    "needle_timeout_seconds": 1.0,
    "hermes_agent_id": "conversation.workshop_extended_openai_conversation_hermes_on_ai2",
    # unlock/open are the dangerous direction; off until explicitly enabled.
    "allow_sensitive_intents": False,
    # Hermes replies often end in a question, which keeps the satellite's mic open with no wake word;
    # it then transcribes its own voice and calls Hermes again (observed 09/24: 4 loops, ~50s of full GPU
    # load each). Off by default: after a Hermes answer the user presses the button to speak again.
    "hermes_follow_up_enabled": False,
    # backstop: refuse to call Hermes again for a device that already triggered this many calls in the window
    "loop_breaker_max": 4,
    "loop_breaker_window_seconds": 300.0,
    "telemetry_path": "/config/needle_voice/telemetry.jsonl",
    "lexicon_path": "/config/needle_voice/lexicon.json",
    # touching this file sends every request straight to Hermes, no restart needed
    "passthrough_flag_path": "/config/needle_voice/passthrough",
    # --- session lifecycle (needle_voice/session.py; used by the duplex voice assistant, not by the HA satellite path) ---
    "session_active_timeout_seconds": 25.0,
    "session_idle_timeout_seconds": 20.0,
    "session_suspend_ttl_seconds": 300.0,
    "session_wake_word": "hey_jarvis",
}
