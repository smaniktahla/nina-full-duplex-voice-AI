"""Needle Voice: a conversation agent that tries Needle, then a small Qwen
model, then hands the request to the existing Hermes agent.

Settings live in a `needle_voice:` block in configuration.yaml (all keys
optional, defaults in const.DEFAULT_CONFIG). Backout: point the Assist
pipeline's conversation agent back at Hermes; or touch the passthrough flag
file to route everything to Hermes instantly; or set `enabled: false`.
"""

from __future__ import annotations

import logging
import os
import sys

from .const import DEFAULT_CONFIG, DOMAIN

_LOGGER = logging.getLogger(__name__)
PLATFORMS = ["conversation"]

try:  # voluptuous ships with Home Assistant; guarded only so the package imports in tests
    import voluptuous as vol

    _SCHEMA = vol.Schema(
        {
            vol.Optional("enabled"): bool,
            vol.Optional("needle_enabled"): bool,
            vol.Optional("qwen_fast_enabled"): bool,
            vol.Optional("hermes_fallback_enabled"): bool,
            vol.Optional("qwen_endpoint"): str,
            vol.Optional("qwen_model"): str,
            vol.Optional("qwen_timeout_seconds"): vol.All(vol.Coerce(float), vol.Range(min=0.2, max=10)),
            vol.Optional("needle_endpoint"): str,
            vol.Optional("needle_timeout_seconds"): vol.All(vol.Coerce(float), vol.Range(min=0.1, max=5)),
            vol.Optional("hermes_agent_id"): str,
            vol.Optional("allow_sensitive_intents"): bool,
            vol.Optional("hermes_follow_up_enabled"): bool,
            vol.Optional("loop_breaker_max"): vol.All(vol.Coerce(int), vol.Range(min=1, max=50)),
            vol.Optional("loop_breaker_window_seconds"): vol.All(vol.Coerce(float), vol.Range(min=10, max=3600)),
            vol.Optional("telemetry_path"): str,
            vol.Optional("lexicon_path"): str,
            vol.Optional("passthrough_flag_path"): str,
            vol.Optional("random_enabled"): bool,
            vol.Optional("rng_url"): str,
        }
    )
    CONFIG_SCHEMA = vol.Schema({DOMAIN: vol.Any(None, _SCHEMA)}, extra=vol.ALLOW_EXTRA)
except ImportError:  # pragma: no cover
    CONFIG_SCHEMA = None


def _apply_config(hass, conf: dict | None) -> dict:
    merged = {**DEFAULT_CONFIG, **(conf or {})}
    hass.data.setdefault(DOMAIN, {})["config"] = merged
    from . import needle_adapter  # package-relative copy; isolated from needle_shadow's

    needle_adapter.NEEDLE_ENDPOINT = merged["needle_endpoint"].rstrip("/")
    needle_adapter.NEEDLE_TIMEOUT_S = float(merged["needle_timeout_seconds"])
    return merged


async def async_setup(hass, config) -> bool:
    merged = _apply_config(hass, config.get(DOMAIN))
    _LOGGER.info(
        "needle_voice: config loaded (enabled=%s needle=%s qwen=%s hermes_fallback=%s qwen_endpoint=%s)",
        merged["enabled"], merged["needle_enabled"], merged["qwen_fast_enabled"],
        merged["hermes_fallback_enabled"], merged["qwen_endpoint"],
    )
    if not hass.config_entries.async_entries(DOMAIN):
        hass.async_create_task(
            hass.config_entries.flow.async_init(DOMAIN, context={"source": "import"}, data={})
        )
    return True


async def async_setup_entry(hass, entry) -> bool:
    if DOMAIN not in hass.data or "config" not in hass.data[DOMAIN]:
        _apply_config(hass, None)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass, entry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
