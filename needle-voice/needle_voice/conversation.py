"""The Needle Voice conversation entity.

Structure mirrors extended_openai_conversation (the working agent on this HA
install): a ConversationEntity + AbstractConversationAgent implementing
`async_process`. Routing logic is in router.py; this file is glue.

Failure policy: nothing a fast tier does can produce an error for the user.
Any problem -> the original utterance goes to the Hermes agent unchanged, and
Hermes's ConversationResult is returned as-is.
"""

from __future__ import annotations

import inspect
import logging
import time
from typing import Any

from homeassistant.components import conversation
from homeassistant.components.conversation import (
    ConversationEntity,
    ConversationEntityFeature,
    ConversationInput,
    ConversationResult,
)
from homeassistant.const import MATCH_ALL
from homeassistant.helpers import intent

from . import candidates as cand_mod
from . import executor, needle_adapter, qwen, telemetry
from .const import DEFAULT_CONFIG, DOMAIN
from .ha_snapshot import build_entity_snapshot, read_runtime_files
from .router import IO, Outcome, RouterConfig, handle

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, config_entry, async_add_entities) -> None:
    entity = NeedleVoiceEntity(hass, config_entry)
    async_add_entities([entity])
    config_entry.async_create_background_task(hass, entity.async_warm_up(), "needle_voice_warm_up")


class NeedleVoiceEntity(ConversationEntity, conversation.AbstractConversationAgent):
    _attr_name = "Needle Voice"
    _attr_supported_features = ConversationEntityFeature.CONTROL

    def __init__(self, hass, entry) -> None:
        self.hass = hass
        self.entry = entry
        self._attr_unique_id = entry.entry_id
        self._warming = False
        self._hermes_calls: dict[str, list[float]] = {}   # device_id -> monotonic times of Hermes delegations

    @property
    def supported_languages(self):
        return MATCH_ALL

    # ------------------------------------------------------------------
    def _cfg_dict(self) -> dict:
        return self.hass.data.get(DOMAIN, {}).get("config") or dict(DEFAULT_CONFIG)

    async def _qwen_post(self, payload: dict, timeout_s: float) -> dict:
        import aiohttp
        from homeassistant.helpers.aiohttp_client import async_get_clientsession

        url = self._cfg_dict()["qwen_endpoint"].rstrip("/") + "/chat/completions"
        session = async_get_clientsession(self.hass)
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=timeout_s)) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def async_warm_up(self) -> None:
        """Prime llama-server's prompt cache with the static prefix so the first
        real request doesn't pay the cold-start cost (~2s > the 2s budget)."""
        try:
            cfg = self._cfg_dict()
            fake = [cand_mod.Candidate("light.warmup", "entity", "Warm Up", "light", None, 1.0)]
            await self._qwen_post(qwen.build_request(cfg["qwen_model"], "turn on the warm up light", fake), 10.0)
            _LOGGER.info("needle_voice: qwen warm-up done")
        except Exception as e:  # noqa: BLE001
            _LOGGER.warning("needle_voice: qwen warm-up failed (%s); first request may fall through to Hermes", type(e).__name__)

    async def _rewarm(self) -> None:
        try:
            await self.async_warm_up()
        finally:
            self._warming = False

    # ------------------------------------------------------------------
    async def async_process(self, user_input: ConversationInput) -> ConversationResult:
        t0 = time.perf_counter()
        cfg_dict = self._cfg_dict()
        cfg = RouterConfig.from_dict(cfg_dict)
        try:
            outcome = await self._route(user_input, cfg, cfg_dict)
        except Exception as e:  # noqa: BLE001 - fail open
            _LOGGER.exception("needle_voice: router failed, falling back to Hermes")
            outcome = Outcome("hermes", None, "glue_exception", {"input": user_input.text, "glue_exception": type(e).__name__})

        # A qwen timeout usually means llama-server lost its cached prompt prefix (restart, eviction).
        # Cancelled requests never finish rebuilding it, so re-prime it in the background, once at a time.
        if outcome.fallback_reason == "qwen_timeout" and not self._warming:
            self._warming = True
            self.hass.async_create_task(self._rewarm())

        hermes_ms = None
        final_ok = True
        if outcome.speech is not None:
            result = self._speech_result(user_input, outcome.speech)
        else:
            t_h = time.perf_counter()
            result, final_ok, extra = await self._delegate_to_hermes(user_input, cfg, cfg_dict)
            hermes_ms = round((time.perf_counter() - t_h) * 1000, 1)
            outcome.telemetry.update(extra)

        rec = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "conversation_id": user_input.conversation_id,
            "language": user_input.language,
            "device_id": user_input.device_id,
            "route": outcome.route,
            "fallback_reason": outcome.fallback_reason,
            "total_latency_ms": round((time.perf_counter() - t0) * 1000, 1),
            "hermes_latency_ms": hermes_ms,
            "final_success": final_ok,
            **outcome.telemetry,
        }
        self.hass.async_add_executor_job(telemetry.write, cfg_dict["telemetry_path"], rec)
        return result

    async def _route(self, user_input, cfg: RouterConfig, cfg_dict: dict) -> Outcome:
        snapshot = build_entity_snapshot(self.hass)  # on the event loop
        lexicon, passthrough = await self.hass.async_add_executor_job(
            read_runtime_files, cfg_dict["lexicon_path"], cfg_dict["passthrough_flag_path"]
        )
        if snapshot is not None:
            snapshot = cand_mod.merge_lexicon(snapshot, lexicon)

        async def needle_route(text):
            return await self.hass.async_add_executor_job(needle_adapter.route, text)

        async def run_plan(p):
            return await executor.execute(self.hass, p, user_input.context)

        io = IO(needle_route=needle_route, qwen_post=self._qwen_post, execute=run_plan, passthrough=lambda: passthrough)
        outcome = await handle(user_input.text, snapshot, cfg, io)
        if snapshot is not None:
            outcome.telemetry["snapshot"] = {"entities": len(snapshot["entities"]), "areas": len(snapshot["areas"])}
        else:
            outcome.telemetry["snapshot"] = None
        return outcome

    # ------------------------------------------------------------------
    def _speech_result(self, user_input, speech: str) -> ConversationResult:
        from homeassistant.util import ulid as ulid_util

        response = intent.IntentResponse(language=user_input.language)
        response.async_set_speech(speech)
        return ConversationResult(
            response=response,
            conversation_id=user_input.conversation_id or ulid_util.ulid_now(),
            continue_conversation=False,
        )

    def _error_result(self, user_input, message: str) -> ConversationResult:
        from homeassistant.util import ulid as ulid_util

        response = intent.IntentResponse(language=user_input.language)
        response.async_set_error(intent.IntentResponseErrorCode.UNKNOWN, message)
        return ConversationResult(
            response=response, conversation_id=user_input.conversation_id or ulid_util.ulid_now()
        )

    async def _delegate_to_hermes(self, user_input, cfg: RouterConfig, cfg_dict: dict):
        """Returns (result, success, extra_telemetry)."""
        if not cfg.hermes_fallback_enabled:
            return self._error_result(user_input, "Sorry, I couldn't do that."), False, {}

        # Circuit breaker: a stuck/looping device must not be able to drive unbounded Hermes work.
        dev = user_input.device_id or "?"
        now = time.monotonic()
        window = float(cfg_dict["loop_breaker_window_seconds"])
        recent = [t for t in self._hermes_calls.get(dev, []) if now - t < window]
        if len(recent) >= int(cfg_dict["loop_breaker_max"]):
            self._hermes_calls[dev] = recent
            _LOGGER.warning("needle_voice: loop breaker tripped for device %s (%d Hermes calls in %ds); not calling Hermes",
                            dev, len(recent), int(window))
            return self._speech_result(user_input, "Okay, I'll pause for a bit."), False, {"loop_breaker": True}
        recent.append(now)
        self._hermes_calls[dev] = recent

        wanted: dict[str, Any] = {
            "text": user_input.text,
            "conversation_id": user_input.conversation_id,
            "context": user_input.context,
            "language": user_input.language,
            "agent_id": cfg_dict["hermes_agent_id"],
            "device_id": user_input.device_id,
            "satellite_id": getattr(user_input, "satellite_id", None),
        }
        params = inspect.signature(conversation.async_converse).parameters
        kwargs = {k: v for k, v in wanted.items() if k in params}
        try:
            result = await conversation.async_converse(self.hass, **kwargs)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("needle_voice: Hermes agent call failed")
            return self._error_result(user_input, "Sorry, I couldn't reach my assistant right now."), False, {}

        extra: dict[str, Any] = {}
        if not cfg_dict["hermes_follow_up_enabled"] and getattr(result, "continue_conversation", False):
            result = self._without_follow_up(result)
            extra["follow_up_suppressed"] = True
        return result, True, extra

    @staticmethod
    def _without_follow_up(result):
        """Same result, but the satellite must not re-open its microphone afterwards."""
        try:
            result.continue_conversation = False
            return result
        except Exception:  # noqa: BLE001 - frozen/slotted result: rebuild it
            return ConversationResult(response=result.response, conversation_id=result.conversation_id,
                                      continue_conversation=False)
