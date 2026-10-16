"""Routing order: guards -> Needle -> Qwen fast tier -> Hermes.

Home Assistant-free. Everything with side effects or I/O arrives through
`IO` (injected by conversation.py, faked in tests). `handle()` never raises
and never returns an error to the user: any failure in a fast tier yields
`Outcome(route="hermes", speech=None, ...)`, and the caller hands the
original utterance to Hermes untouched.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from . import candidates as cand_mod
from . import entity_resolver, guards, policy_engine, qwen, random_ops
from .const import DEFAULT_CONFIG
from .executor import ExecutionError, PlanError, ValidatedIntent, plan
from .intents import ALL_TARGET_DOMAINS

NEEDLE_EXECUTABLE_TOOLS = ("turn_on_light", "get_sensor_state")
# Words allowed in a plain "what's the weather" question. Anything else ("tomorrow",
# "will", a place name, ...) is not the simple case and goes to Hermes.
WEATHER_WORDS = frozenset({
    "weather", "forecast", "rain", "raining", "snow", "snowing", "outside", "hows", "how", "whats", "what",
    "the", "is", "like", "today", "currently", "now", "right", "tell", "me", "give", "it", "its", "a", "s",
})
LIGHT_WORDS = frozenset({"light", "lights", "lamp", "lamps"})


@dataclass(frozen=True)
class RouterConfig:
    enabled: bool = True
    needle_enabled: bool = True
    qwen_fast_enabled: bool = True
    hermes_fallback_enabled: bool = True
    qwen_model: str = "qwen-fast"
    qwen_timeout_seconds: float = 2.0
    needle_timeout_seconds: float = 1.0
    allow_sensitive_intents: bool = False
    random_enabled: bool = True                    # coin / dice / number / pick -> the RNG service (never an LLM)

    @classmethod
    def from_dict(cls, d: dict | None) -> "RouterConfig":
        merged = {**DEFAULT_CONFIG, **(d or {})}
        return cls(**{k: merged[k] for k in cls.__dataclass_fields__})


@dataclass
class IO:
    needle_route: Callable[[str], Awaitable[Any]]                 # -> needle_adapter.RouteDecision
    qwen_post: Callable[[dict, float], Awaitable[dict]]           # (payload, timeout_s) -> parsed JSON
    execute: Callable[[Any], Awaitable[str]]                      # (Plan) -> speech
    passthrough: Callable[[], bool] = lambda: False
    rng: Callable[[str, dict], Awaitable[Any]] | None = None       # (tool, params) -> RNG service JSON, None if unreachable


@dataclass
class Outcome:
    route: str                       # "random" | "rules" | "needle" | "qwen_fast" | "hermes"
    speech: str | None               # None => caller must delegate to Hermes
    fallback_reason: str | None
    telemetry: dict


def _ms(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000, 1)


async def handle(text: str, snapshot: dict | None, cfg: RouterConfig, io: IO) -> Outcome:
    t_start = time.perf_counter()
    tel: dict[str, Any] = {
        "input": text, "guard": None, "needle": None, "needle_skipped": None,
        "retrieval": None, "qwen": None, "validation": None, "execution": None,
    }

    def hermes(reason: str) -> Outcome:
        tel["router_latency_ms"] = _ms(t_start)
        return Outcome("hermes", None, reason, tel)

    def done(route: str, speech: str) -> Outcome:
        tel["router_latency_ms"] = _ms(t_start)
        return Outcome(route, speech, None, tel)

    try:
        if not cfg.enabled:
            return hermes("disabled")
        if io.passthrough():
            return hermes("passthrough_flag")
        # ---------------- rules: randomness (coin, dice, number, pick) ----------------
        # Before the guards and the snapshot: it needs neither, and a language model must never answer it. A service failure is
        # spoken honestly (handled=True) instead of falling through to an agent that would invent a "random" result.
        if cfg.random_enabled and io.rng is not None:
            rr = await random_ops.handle(text, io.rng)
            if rr.reason != "not_random":
                tel["random"] = {"status": rr.status.value, "code": rr.telemetry.get("code")}
            if rr.handled:
                return done("random", rr.speech)
        g = guards.pre_guard(text)
        tel["guard"] = g
        if g:
            return hermes(f"guard:{g}")
        if not isinstance(snapshot, dict):
            return hermes("no_snapshot")

        # ---------------- rules: plain weather question ----------------
        weather_id = _weather_target(text, snapshot)
        if weather_id:
            out = await _run(ValidatedIntent("get_weather", weather_id, {}, "rules"), snapshot, cfg, io, tel, "rules")
            if isinstance(out, str):
                return done("rules", out)
            if out:
                return hermes(out[1])

        # ---------------- tier 1: Needle ----------------
        if cfg.needle_enabled:
            vi, skip = await _needle_tier(text, snapshot, cfg, io, tel)
            tel["needle_skipped"] = skip
            if vi is not None:
                out = await _run(vi, snapshot, cfg, io, tel, "needle")
                if out is not None:
                    return done("needle", out) if isinstance(out, str) else hermes(out[1])

        # ---------------- tier 2: Qwen fast ----------------
        if not cfg.qwen_fast_enabled:
            return hermes("qwen_disabled")
        cands = cand_mod.rank(text, snapshot, ALL_TARGET_DOMAINS)
        tel["retrieval"] = [{"id": c.id, "score": c.score} for c in cands[:5]]
        if not cands:
            return hermes("no_candidates")
        top = cand_mod.unique_top(cands)
        if top is None:
            return hermes("ambiguous_target")
        equiv = cand_mod.equivalent_candidates(top, snapshot)
        cands = cands + [x for x in equiv if x.id not in {c.id for c in cands}]

        payload = qwen.build_request(cfg.qwen_model, text, cands)
        t_q = time.perf_counter()
        try:
            resp = await asyncio.wait_for(io.qwen_post(payload, cfg.qwen_timeout_seconds), timeout=cfg.qwen_timeout_seconds)
        except asyncio.TimeoutError:
            tel["qwen"] = {"latency_ms": _ms(t_q), "raw": None, "error": "timeout"}
            return hermes("qwen_timeout")
        except Exception as e:  # noqa: BLE001
            tel["qwen"] = {"latency_ms": _ms(t_q), "raw": None, "error": type(e).__name__}
            return hermes("qwen_http_error")
        try:
            content = resp["choices"][0]["message"]["content"]
        except Exception:  # noqa: BLE001
            tel["qwen"] = {"latency_ms": _ms(t_q), "raw": None, "error": "bad_response_shape"}
            return hermes("qwen_bad_response")
        tel["qwen"] = {"latency_ms": _ms(t_q), "raw": content, "error": None}

        verdict = qwen.validate(content, utterance=text, candidates=cands, top=top,
                                allow_sensitive=cfg.allow_sensitive_intents,
                                equivalent_ids=frozenset(x.id for x in equiv))
        tel["validation"] = {"ok": verdict.ok, "reason": verdict.reason}
        if not verdict.ok:
            return hermes(f"qwen:{verdict.reason}")
        vi = ValidatedIntent(verdict.intent, verdict.target, verdict.arguments, "qwen_fast")
        out = await _run(vi, snapshot, cfg, io, tel, "qwen_fast")
        if isinstance(out, str):
            return done("qwen_fast", out)
        return hermes(out[1] if out else "plan_failed")
    except Exception as e:  # noqa: BLE001 - fail open, always
        tel["router_exception"] = f"{type(e).__name__}: {e}"
        return hermes("router_exception")


async def _run(vi: ValidatedIntent, snapshot: dict, cfg: RouterConfig, io: IO, tel: dict, tier: str):
    """Plan + execute. Returns speech (str) on success, ("fail", reason) on failure,
    or None if the plan itself was refused BEFORE any action (caller may try the next tier)."""
    try:
        p = plan(vi, snapshot, allow_sensitive=cfg.allow_sensitive_intents)
    except PlanError as e:
        tel["execution"] = {"tier": tier, "status": "plan_refused", "reason": str(e)}
        if tier == "needle":
            return None
        return ("fail", f"plan_refused:{e}")
    try:
        speech = await io.execute(p)
    except ExecutionError as e:
        tel["execution"] = {"tier": tier, "status": "execution_error", "reason": str(e), "intent": vi.intent, "target": vi.target}
        return ("fail", f"execution_error:{e}")
    except Exception as e:  # noqa: BLE001
        tel["execution"] = {"tier": tier, "status": "execution_exception", "reason": type(e).__name__}
        return ("fail", "execution_exception")
    tel["execution"] = {"tier": tier, "status": "ok", "intent": vi.intent, "target": vi.target,
                        "entities": list(p.entity_ids), "arguments": vi.arguments}
    return speech


async def _needle_tier(text: str, snapshot: dict, cfg: RouterConfig, io: IO, tel: dict):
    """Returns (ValidatedIntent | None, skip_reason | None). Never raises."""
    t0 = time.perf_counter()
    try:
        adapter = await asyncio.wait_for(io.needle_route(text), timeout=cfg.needle_timeout_seconds + 0.5)
    except asyncio.TimeoutError:
        tel["needle"] = {"status": "timeout", "latency_ms": _ms(t0)}
        return None, "needle_timeout"
    except Exception as e:  # noqa: BLE001
        tel["needle"] = {"status": "error", "latency_ms": _ms(t0), "error": type(e).__name__}
        return None, "needle_error"

    tel["needle"] = {
        "status": "error" if adapter.error else ("success" if adapter.tool_match else "declined"),
        "latency_ms": _ms(t0), "tool": adapter.tool_name, "arguments": adapter.arguments or None,
        "confidence": adapter.confidence,
    }
    if adapter.error:
        return None, "needle_error"
    if not adapter.tool_match or adapter.tool_name not in NEEDLE_EXECUTABLE_TOOLS:
        return None, "needle_no_executable_tool"

    validation = (adapter.needle_raw or {}).get("validation") if isinstance(adapter.needle_raw, dict) else None
    ungrounded = validation.get("ungrounded") if isinstance(validation, dict) else None
    res = entity_resolver.resolve(
        tool_name=adapter.tool_name, arguments=adapter.arguments, utterance=text, snapshot=snapshot,
        needle_ungrounded=ungrounded if isinstance(ungrounded, list) else [],
    )
    pol = policy_engine.evaluate_policy(adapter, policy_engine.PolicyContext(resolved_target_count=res.resolved_target_count))
    tel["needle"].update({"resolution": res.status, "policy_rule": pol.policy_rule})
    if pol.route != "fast_path_candidate" or res.status != "resolved" or not res.matched:
        return None, f"needle_policy:{pol.policy_rule}/{res.status}"

    toks = set(guards.tokens(text))
    if adapter.tool_name == "turn_on_light":
        intent, target = "turn_on", f"area:{res.matched[0]}"
        if not (toks & LIGHT_WORDS):
            return None, "needle_area_needs_light_word"
    else:
        intent = "get_temperature" if toks & {"temperature", "temp"} else "get_state"
        target = res.matched[0]
    ok, why = guards.corroborates(intent, text)
    if not ok:
        # e.g. Needle answered "turn OFF the bedroom light" with turn_on_light
        return None, f"needle_intent_not_corroborated:{why}"
    return ValidatedIntent(intent, target, {}, "needle"), None


def _weather_target(text: str, snapshot: dict) -> str | None:
    """The one exposed weather entity, if `text` is nothing but a plain weather
    question. Deterministic and read-only, so no model is involved."""
    if "get_weather" not in guards.intent_signals(text):
        return None
    if set(guards.tokens(text)) - WEATHER_WORDS:
        return None
    ids = [e["entity_id"] for e in snapshot.get("entities") or []
           if isinstance(e, dict) and str(e.get("entity_id", "")).startswith("weather.")]
    return ids[0] if len(ids) == 1 else None
