#!/usr/bin/env python3
"""Live duplex voice prototype (service side). One command: ./run.sh   -> https://<host>:8443/

Reuses the simulator's controller pieces (PromptPolicy / system_prompt / parse_out / TtsModel, backchannel logic) and its best
prompt-only configuration: append-only history + state tags, silence-probability gate 0.3, speak-gate 0.95.

Browser <-> server (WebSocket /ws):
  browser -> server  binary: 16 kHz mono int16 PCM, ~40 ms frames          text JSON: {t: stop|reset|hello}
  server  -> browser binary: [u32 LE sample rate][int16 PCM] TTS audio      text JSON: stt / state / assistant / cancel / duck / log
"""
import asyncio
import json
import os
import re
import ssl
import struct
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import aiohttp
from aiohttp import web

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
from duplex.hybrid import is_backchannel, norm_words           # noqa: E402
from duplex.llama import Llama, prob_of                       # noqa: E402
from duplex.loop import PromptPolicy, TtsModel, THINK_PREFIX, chat, is_filler, parse_out, system_prompt  # noqa: E402
NEEDLE_DIR = os.environ.get("NEEDLE_DIR") or next((p for p in (os.path.join(HERE, "..", "needle-voice"), os.path.expanduser("~/needle-voice")) if os.path.isdir(p)), "")
sys.path.insert(0, NEEDLE_DIR)
from needle_voice import telemetry as needle_telemetry         # noqa: E402  Needle owns the session lifecycle + its event log
from needle_voice.session import ACTIVE, DORMANT, IDLE, SUSPENDED, SessionConfig, SessionManager  # noqa: E402
from stt_stream import StreamingSTT                           # noqa: E402
from wake import PHRASE_WAKE, WAKE_TOKENS, WORDS as WAKE_WORDS, WakeDetector            # noqa: E402
import tools as tools_mod                                                                  # noqa: E402
from tools import ask_hermes, calendar, ha_say, hermes_key, weather, web_search          # noqa: E402

CFG = dict(
    llama="http://localhost:8080", tick_ms=600, sentinel="~", gate_lo=0.3, gate_hi=0.95,
    stt_model=os.environ.get("STT_MODEL", "base.en"), voice=os.path.join(HERE, "voices", "en_US-lessac-medium.onnx"),
    port=int(os.environ.get("PORT", "8443")), temp=0.2,
)
BARGE = re.compile(r"\b(wait|stop|hold on|hang on|pause|never ?mind|shut up|be quiet|enough|cancel that)\b", re.I)
LIVE_NOTE = ("\nThis is a live spoken conversation with one person. Speak naturally and helpfully in plain spoken English (no markdown, "
             "no lists). Keep each spoken chunk to one natural clause.\n"
             "TOOLS (they run in the background; keep talking while they work). ROUTING: anything about the home (lights, switches, "
             "temperatures, what's on, the time) or current weather -> '@ha <request in plain words>'; weather forecasts -> "
             "'@weather today' / '@weather tomorrow' / '@weather week'; the user's calendar -> '@calendar today' / '@calendar tomorrow' / "
             "'@calendar week'. These three answer in under a second: NEVER use @search or @hermes for weather, calendar or home questions. "
             "@ha is ONLY for devices, sensors, the time and current weather: never use it for personal facts or general questions. Only if those fail, escalate. For the rest: "
             "(a) @search <query> looks something up on the web and returns in a few seconds: use it for anything current or that you "
             "are unsure of (news, prices, recent facts, weather, scores). "
             "(b) @hermes <task> hands a harder job to a deeper-thinking agent that can take 30 seconds to several minutes: use it for "
             "research, analysis, multi-step or homelab tasks. Write the tag after a very short spoken acknowledgement, e.g. "
             "'Let me check. @search who won the world series'. Results arrive later as [search #n done: ...] or [hermes #n done: ...]; "
             "then tell the user the answer briefly in your own spoken words, using ONLY facts present in the result: if it lacks a detail the user asked about, say it wasn't in the result (offer to dig deeper with @hermes) rather than filling in from memory. A home result starts with [DONE] (it happened, verified) or [NOT DONE] (it did NOT happen or is unconfirmed): say only what that sentence says, never claim an action you were not told was done, never re-issue it yourself, and relay a question like 'Did you mean...?' to the user in full. Never invent results and never claim to have searched "
             "without the tag. Do not use a tool for chit-chat or things you know well.\n"
             "LONG-TERM MEMORY: when the user tells you a lasting personal fact (address, family names, preferences, routines), say a short "
             "acknowledgement then '@remember <the fact as a complete sentence>'. Only what the user actually said. '@forget <words>' removes it. "
             "A statement about the user ('My address is...') is NOT a question: acknowledge it, don't answer as if asked. Wait for the user to "
             "finish their sentence before replying.\n"
             "Pending tasks appear as {pending: #id kind \"task\" ...}. If the user refines, narrows or changes what they want from a pending "
             "task, do NOT start a new tool: say a short acknowledgement then '@revise #<id> <the refinement in your own words>' "
             "(for a search give the full new query). If they drop it (never mind / cancel that), say so then '@cancel #<id>'.")
TOOL_RE = re.compile(r"@(search|hermes|ha|weather|calendar|revise|cancel|remember|forget)\b[:\s]*(.*)$", re.S)
SESSION_LOG = os.environ.get("SESSION_LOG", os.path.expanduser("~/.local/share/needle_voice/sessions.jsonl"))
BAN_TOKENS = ["...", "\u2026", "-", "_", "~"]                 # NOT . , ! ? : forced speech must be able to finish its sentence
DANGLING = re.compile(r"(?:\b(?:a|an|the|to|of|in|on|at|for|with|and|but|or|if|is|are|was|were|be|my|your|his|her|its|our|their|that|this|it's|i'm|you're|about|from|into|than|as|by|so)|\d|,)\s*$", re.I)   # ends mid-phrase
DANGER = re.compile(r"\b(bleach|ammonia|fire|gas leak|knife|electric|plug|burn|choke|poison|danger|hurt|poisonous|toxic)\b", re.I)
PROFILE = os.environ.get("PROFILE", os.path.expanduser("~/.local/share/needle_voice/profile.json"))


def load_facts():
    try:
        return list(json.load(open(PROFILE)).get("facts", []))
    except Exception:                                                     # noqa: BLE001
        return []


def save_facts(facts):
    os.makedirs(os.path.dirname(PROFILE), exist_ok=True)
    json.dump({"facts": facts}, open(PROFILE, "w"), indent=1)
    os.chmod(PROFILE, 0o600)


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


class Shared:
    """Loaded once: whisper, piper, tokenizer ids, executors."""

    def __init__(self):
        from faster_whisper import WhisperModel
        from tts_engine import Tts
        log("loading whisper", CFG["stt_model"])
        self.whisper = WhisperModel(CFG["stt_model"], device="cpu", compute_type="int8", cpu_threads=4)
        self.stt_pool = ThreadPoolExecutor(max_workers=1)
        log("loading TTS", os.environ.get("TTS", "kokoro"))
        self.tts_engine = Tts(here=HERE)
        log("TTS ready:", self.tts_engine.kind, self.tts_engine.device, "rate", self.tts_engine.rate)
        self.tts_pool = ThreadPoolExecutor(max_workers=1)
        self.sample_rate = self.tts_engine.rate
        ll = Llama(CFG["llama"])
        self.sid = ll.tokenize(CFG["sentinel"])[0]
        import urllib.request as _u
        self.eos = json.load(_u.urlopen(_u.Request(CFG["llama"] + "/tokenize", data=json.dumps({"content": "<|im_end|>", "parse_special": True}).encode(),
                                                  headers={"Content-Type": "application/json"}), timeout=10))["tokens"][0]
        try:
            # Pinning to a fixed slot is wrong: another client may already hold it and our request would queue behind it. Unpinned, the
            # server gives us an idle slot and prompt-cache affinity keeps us on our own one.
            self.voice_slot = int(os.environ.get("VOICE_SLOT", "-1"))
        except Exception:                                                        # noqa: BLE001
            self.voice_slot = -1
        try:
            n_slots = len(ll.get_json("/slots"))
        except Exception:                                                        # noqa: BLE001
            n_slots = 0
        log("llama-server has %d slot(s); voice requests are %s" % (n_slots, ("pinned to slot %d" % self.voice_slot) if self.voice_slot >= 0 else "unpinned (the server assigns an idle slot)"))
        self.ban = sorted({self.sid} | {ll.tokenize(t)[0] for t in BAN_TOKENS if len(ll.tokenize(t)) == 1})
        import numpy as np
        self.whisper.transcribe(np.zeros(16000, np.float32), language="en")      # warm-up
        try:
            hermes_key()
        except Exception as e:                                                       # noqa: BLE001
            log("hermes key unavailable (hermes tool will fail):", e)


class Session:
    def __init__(self, ws, sh: Shared, http: aiohttp.ClientSession):
        self.ws, self.sh, self.http = ws, sh, http
        self.loop = asyncio.get_running_loop()
        self.t0 = self.loop.time()
        self.stt = StreamingSTT(shared_model=sh.whisper, executor=sh.stt_pool, on_event=self.on_stt,
                                sens=float(os.environ.get("SENS", "0.4")))
        self.tts = TtsModel()
        self.tts.WPS = 2.6
        self.reset_conv()
        self.gen = 0
        self.llm_task = None
        self.duck = False
        self.tasks = []
        self.state = "LISTENING"
        self.barge_fired = False
        self.tts_jobs = []
        self.jobs, self.job_n, self.job_tasks = [], 0, set()
        self.voice = self.sh.tts_engine.default_voice
        wake = os.environ.get("WAKE", "hey_jarvis")
        self.sess = SessionManager(SessionConfig(wake_enabled=wake != "off", wake_word=wake if wake != "off" else "hey_jarvis",
                                                  active_timeout_s=float(os.environ.get("TIMEOUT", "25")),
                                                  idle_timeout_s=float(os.environ.get("IDLE_TIMEOUT", "20")),
                                                  suspend_ttl_s=float(os.environ.get("SUSPEND_TTL", "300"))), emit=self.on_session_event)
        self.slept_at = time.monotonic()
        self.cont_n = 0
        self.last_chunk = ""
        self.wake_det, self.dismiss_buf, self.wake_grace_until, self.wake_clean = None, "", 0.0, True
        self.closed = False
        self.sbuf, self.sbuf_timer, self.resp_started = "", None, False

    def now(self):
        return (self.loop.time() - self.t0) * 1000

    def reset_conv(self):
        self.pol = PromptPolicy("append_state", CFG["sentinel"], CFG["tick_ms"])
        facts = load_facts()
        self.pol.sys = chat("system", system_prompt(CFG["sentinel"], CFG["tick_ms"], True) + LIVE_NOTE
                            + (("\nWHAT YOU KNOW ABOUT THE USER (long-term memory, use it; don't recite it unprompted):\n- " + "\n- ".join(facts)) if facts else ""))
        self.user_buf, self.last_word_ms, self.note_once, self.answer_open = "", 0.0, "", False

    async def send(self, **m):
        try:
            await self.ws.send_str(json.dumps(m))
        except Exception:
            pass

    def set_state(self, s):
        if s != self.state:
            log("STATE", self.state, "->", s)
            self.state = s
            asyncio.ensure_future(self.send(t="state", state=s))

    # ------------------------------------------------------------ STT callbacks (event loop thread)
    def on_stt(self, kind, text):
        if kind == "wake_check":
            if self.sess.asleep and self.phrase_mode:
                if re.search(PHRASE_WAKE[self.sess.cfg.wake_word], text.strip(), re.I):
                    log("WAKE PHRASE heard")
                    self.stt.listen_only = False
                    asyncio.ensure_future(self.activate("wake_phrase"))
                else:
                    log("(speech while asleep: not the wake phrase)")        # the text itself is never logged or kept
            return
        if self.sess.asleep:
            return
        if kind == "speech_start":
            return
        asyncio.ensure_future(self.send(t="stt", kind=kind, text=text))
        if kind == "partial":
            log("STT partial:", text)
            speaking = self.tts.speaking(self.now()) or self.llm_task is not None
            tail = " ".join(text.split()[-6:])
            if speaking and not self.barge_fired and BARGE.search(tail):
                self.barge_fired = True
                self.interrupt("barge-in keyword: %r" % BARGE.search(tail).group(0))
        elif kind == "final":
            log("STT final:  ", text)
            if len(text.split()) >= 2:
                self.sess.on_user_utterance(text)
            self.barge_fired = False
            if self.duck:
                self.duck = False
                asyncio.ensure_future(self.send(t="duck", on=False))

    # ------------------------------------------------------------ barge-in / cancellation
    def interrupt(self, why):
        log("INTERRUPT:", why)
        self.gen += 1                                   # everything generated before now is obsolete
        if self.llm_task and not self.llm_task.done():
            self.llm_task.cancel()
        for j in self.tts_jobs:
            j.cancel()
        self.tts_jobs = []
        self.sbuf, self.resp_started = "", False
        if self.sbuf_timer:
            self.sbuf_timer.cancel()
        self.tts.cancel(self.now())
        self.answer_open = False
        self.note_once = "{you were interrupted and your speech was cancelled; do not resume it unless asked}"
        self.stt.boost = 1.0
        asyncio.ensure_future(self.send(t="cancel", why=why))
        self.set_state("INTERRUPTED")

    # ------------------------------------------------------------ TTS
    def speak(self, text, final=False):
        """Queue assistant words. The first chunk of a response is spoken at once; later chunks are joined into clauses so the
        voice doesn't stutter (flush on punctuation, >=6 words, a silent tick, or 0.9 s)."""
        now = self.now()
        self.sess.engage("assistant_spoke")
        self.tts.say(now, text)                                  # state/tag accounting happens immediately
        self.sbuf = (self.sbuf + " " + text).strip()
        if self.sbuf_timer:
            self.sbuf_timer.cancel()
        if (not self.resp_started) or final or re.search(r"[,.;:?!][\"')]*$", self.sbuf) or len(self.sbuf.split()) >= 6:
            self.flush_speech()
        else:
            self.sbuf_timer = self.loop.call_later(0.9, self.flush_speech)

    def flush_speech(self):
        if self.sbuf_timer:
            self.sbuf_timer.cancel()
            self.sbuf_timer = None
        text, self.sbuf = self.sbuf, ""
        if not text:
            return
        self.resp_started = True
        gen = self.gen
        q = asyncio.Queue()

        def synth():
            try:
                for pcm in self.sh.tts_engine.synth(text, self.voice):
                    self.loop.call_soon_threadsafe(q.put_nowait, pcm)
            finally:
                self.loop.call_soon_threadsafe(q.put_nowait, None)

        async def pump():
            self.loop.run_in_executor(self.sh.tts_pool, synth)
            while True:
                b = await q.get()
                if b is None or gen != self.gen:
                    break
                try:
                    await self.ws.send_bytes(struct.pack("<I", self.sh.sample_rate) + b)
                except Exception:
                    break

        self.tts_jobs.append(asyncio.ensure_future(pump()))
        self.tts_jobs = [j for j in self.tts_jobs if not j.done()]

    # ------------------------------------------------------------ LLM
    async def llm(self, prompt, forced, cont=False):
        body = {"prompt": prompt, "n_predict": 110 if forced else 30, "n_probs": 8, "temperature": CFG["temp"], "cache_prompt": True,
                "stream": True, "stop": ["<|im_end|>"]}
        if cont:
            # finish the unfinished sentence in ONE call: the model must not be able to end its turn after a single word (it did, one
            # word per tick), so ban end-of-turn and stop at the sentence end instead
            body.update(n_predict=40, stop=["\n", ".", "!", "?"])
        if self.sh.voice_slot >= 0:
            body["id_slot"] = self.sh.voice_slot
        if forced:
            body["logit_bias"] = [[i, False] for i in self.sh.ban] + ([[self.sh.eos, False]] if cont else [])
        text, first_top, t0, ttft, stop_word = [], None, time.perf_counter(), None, ""
        async with self.http.post(CFG["llama"] + "/completion", json=body) as r:
            async for raw in r.content:
                line = raw.decode().strip()
                if not line.startswith("data:"):
                    continue
                d = json.loads(line[5:])
                if d.get("content"):
                    ttft = ttft or time.perf_counter() - t0
                    text.append(d["content"])
                if first_top is None and d.get("completion_probabilities"):
                    first_top = [(x["id"], x["logprob"]) for x in d["completion_probabilities"][0].get("top_logprobs", [])]
                if d.get("stop") and d.get("stopping_word") in (".", "!", "?"):
                    stop_word = d["stopping_word"]
        out = "".join(text).rstrip()
        return {"text": out + stop_word if out else out, "first_top": first_top or [], "ms": (time.perf_counter() - t0) * 1000}

    async def say_result(self, events):
        """Tool results are spoken by a clean one-shot request, NOT by the tick-format history: that history teaches the model to emit a few
        words and end its turn, which is what chopped results into 'You have a tour at / twelve-thirty, an S / BA / payment / at...'."""
        lines = "\n".join("The user asked: %s\nThe %s tool returned: %s" % (self.job_title(j), j["kind"], j["res"][:1500]) for j in events)
        prompt = (chat("system", "You are Nina, a friendly voice assistant. Reply in plain spoken English, at most two short sentences, no markdown, no lists, "
                       "no symbols. Say numbers and times the way people say them. Use only facts from the tool result; if it does not contain what was asked, "
                       "say so briefly.") + chat("user", lines + "\nTell the user the answer now.") + THINK_PREFIX)
        body = {"prompt": prompt, "n_predict": 100, "temperature": 0.2, "cache_prompt": True, "stop": ["<|im_end|>", "\n"]}
        t0 = time.perf_counter()
        async with self.http.post(CFG["llama"] + "/completion", json=body) as r:
            d = await r.json()
        return {"text": d.get("content", "").strip(), "first_top": [], "ms": (time.perf_counter() - t0) * 1000}

    # ------------------------------------------------------------ tools (background, never block the loop)
    def pending_tag(self, now):
        return " ".join('{pending: #%d %s "%s" %ds%s}' % (j["id"], j["kind"], self.job_title(j)[:70], (now - j["t0"]) / 1000,
                                                       (" rev%d" % j["rev"]) if j["rev"] else "")
                        for j in self.jobs if not j["done"])

    @staticmethod
    def job_title(j):
        return j["goal"] + ((" | focus: " + "; ".join(j["refine"])) if j["refine"] else "")

    def compose(self, j):
        """What the tool actually receives: conversation context + original request + every refinement. Owned by code,
        independent of the model's token history, so a revision never loses the original ask."""
        if j["kind"] != "hermes":
            return j["goal"]
        ctx = " ".join("%s: %s" % ("User" if w == "user" else "Assistant", t) for w, t in self.sess.context(8))
        out = "Conversation so far: %s\nOriginal request: %s" % (ctx, j["goal"])
        if j["refine"]:
            out += "\nThe user has since refined the request. Apply this focus: " + "; ".join(j["refine"])
        return out

    def take_results(self, now, text, silent_ms, speaking):
        """Deterministic delivery: finished results are queued and handed to the model only when the user is quiet and the
        assistant is not talking, so a result never lands mid-sentence and the model never has to remember it."""
        if text or speaking or self.stt.in_utt or self.stt.pending or silent_ms < 900:
            return []
        ev = [j for j in self.jobs if j["done"] and not j["told"]]
        for j in ev:
            j["told"] = True
            self.sess.task_told(j["id"])
        return ev

    def _launch(self, j):
        rev = j["rev"]

        async def run():
            try:
                k, g = j["kind"], j["goal"]
                res = await (web_search(self.http, g) if k == "search" else ha_say(self.http, g) if k == "ha" else
                             weather(self.http, g) if k == "weather" else calendar(self.http, g) if k == "calendar" else
                             ask_hermes(self.http, self.compose(j)))
            except asyncio.CancelledError:
                return
            except Exception as e:                                          # noqa: BLE001
                res = "(%s failed: %s)" % (j["kind"], str(e)[:80])
            if j["rev"] != rev or j["cancelled"]:
                return                                                       # superseded by a revision / cancelled: drop it
            j["res"], j["done"] = res, True
            self.sess.finish_task(j["id"])
            dt = (self.now() - j["t0"]) / 1000
            log("TOOL #%d %s done in %.1fs (rev %d) req=%s: %s" % (j["id"], j["kind"], dt, rev, (tools_mod._home.get("last_rid") if j["kind"] == "ha" else "-"), res[:160]))
            await self.send(t="tool", id=j["id"], kind=j["kind"], task=self.job_title(j), status="done", secs=round(dt, 1))

        t = asyncio.ensure_future(run())
        j["aio"] = t
        self.job_tasks.add(t)
        t.add_done_callback(self.job_tasks.discard)

    def start_tool(self, kind, task):
        st = self.sess.add_task(kind, task)
        j = {"id": st.id, "kind": kind, "goal": task, "refine": [], "rev": 0, "t0": self.now(), "done": False,
             "told": False, "res": "", "cancelled": False}
        self.jobs.append(j)
        log("TOOL #%d %s started: %r" % (j["id"], kind, task))
        asyncio.ensure_future(self.send(t="tool", id=j["id"], kind=kind, task=task, status="started"))
        self._launch(j)

    def revise_tool(self, jid, text):
        j = next((x for x in self.jobs if x["id"] == jid), None)
        if not j or j["cancelled"]:
            log("REVISE #%s: no such task" % jid)
            self.note_once = "{task #%s does not exist; if the user wants something new, start a new tool}" % jid
            return
        if j["done"]:                                                        # already delivered: follow-up on the same topic
            self.start_tool(j["kind"], j["goal"] + ((" (focus: " + "; ".join(j["refine"]) + ")") if j["refine"] else "") + ". Follow-up: " + text)
            return
        j["rev"] += 1
        if j["kind"] != "hermes":
            j["goal"] = text
        else:
            j["refine"].append(text)
        if j.get("aio"):
            j["aio"].cancel()                                                # abandon the stale run, keep id + context
        log("TOOL #%d REVISED (rev %d): %s" % (jid, j["rev"], self.job_title(j)))
        asyncio.ensure_future(self.send(t="tool", id=jid, kind=j["kind"], task=self.job_title(j), status="revised"))
        self._launch(j)

    def cancel_tool(self, jid):
        j = next((x for x in self.jobs if x["id"] == jid), None)
        if j and not j["done"]:
            j["cancelled"], j["done"], j["told"] = True, True, True
            self.sess.cancel_task(jid)
            if j.get("aio"):
                j["aio"].cancel()
            log("TOOL #%d CANCELLED" % jid)
            asyncio.ensure_future(self.send(t="tool", id=jid, kind=j["kind"], task=self.job_title(j), status="cancelled"))

    def run_tool_call(self, action, arg):
        arg = arg.strip()
        if action in ("search", "hermes", "ha", "weather", "calendar"):
            if arg or action in ("weather", "calendar"):
                self.start_tool(action, arg or "today")
            return
        if action == "remember" and arg:
            facts = load_facts()
            if arg not in facts:
                facts.append(arg[:300])
                save_facts(facts[-60:])
                log("REMEMBERED:", arg[:120])
                self.sess.emit("remembered", fact=arg[:120])
            return
        if action == "forget" and arg:
            keep = [f for f in load_facts() if arg.lower() not in f.lower()]
            save_facts(keep)
            log("FORGOT facts matching:", arg)
            return
        m = re.match(r"#?(\d+)\s*(.*)$", arg, re.S)
        if not m:
            return
        if action == "revise" and m.group(2).strip():
            self.revise_tool(int(m.group(1)), m.group(2).strip())
        elif action == "cancel":
            self.cancel_tool(int(m.group(1)))

    async def call_llm(self, prompt, forced, cont=False):
        self.llm_task = asyncio.ensure_future(self.llm(prompt, forced, cont))
        try:
            return await self.llm_task
        except asyncio.CancelledError:
            return None
        finally:
            self.llm_task = None

    async def tick(self):
        if self.sess.asleep:
            return                                                      # no STT, no LLM while asleep
        S = CFG["sentinel"]
        now = self.now()
        words = self.stt.drain()
        text = " ".join(words)
        if text and time.monotonic() < self.wake_grace_until and not self.wake_clean:        # the wake phrase itself must not reach the model
            ws = text.split()
            while ws and re.sub(r"[^a-z]", "", ws[0].lower()) in WAKE_TOKENS:
                ws.pop(0)
            if ws:
                self.wake_clean = True
            text, words = " ".join(ws), ws
        if text:
            self.dismiss_buf = (self.dismiss_buf + " " + text)[-160:]
            if self.sess.check_dismissal(self.dismiss_buf):
                log("DISMISSED by: %r" % self.dismiss_buf[-60:])
                self.dismiss_buf = ""
                self.interrupt("dismissal")
                await self.dismiss("Okay, I'm here if you need me.")
                return
        if words:
            self.last_word_ms = now
            self.user_buf = (self.user_buf + " " + text).strip()
        silent_ms = now - self.last_word_ms
        speaking = self.tts.speaking(now)
        self.stt.boost = 2.2 if speaking else 1.0                       # echo residue guard
        if self.answer_open and text and not is_backchannel(text):
            self.answer_open = False
            self.cont_n = 0
        events = self.take_results(now, text, silent_ms, speaking)
        note_in = self.note_once
        if events:
            note_in = (note_in + " {tell the user the result now, in one or two short spoken sentences}").strip()
        tag = (self.tts.tag(now) + " " + note_in).strip()
        prompt = self.pol.build(now, text, events, tag, self.pending_tag(now), silent_ms)
        gen = self.gen

        def discard():
            for j in events:
                j["told"] = False                                        # result not delivered: offer it again
            self.pol.commit(text, text, "", "silent")

        if events:
            self.llm_task = asyncio.ensure_future(self.say_result(events))
            try:
                r = await self.llm_task
            except asyncio.CancelledError:
                r = None
            finally:
                self.llm_task = None
        else:
            r = await self.call_llm(prompt, False)
        if r is None:
            return discard()
        raw = r["text"].strip()
        p_s = prob_of(r["first_top"], self.sh.sid)
        tool = TOOL_RE.search(raw)
        note = ""
        if tool:
            out = raw[:tool.start()].strip()
            kind = "speech" if out else "silent"
        else:
            kind, out, _ = parse_out(raw, S)
            if events:                                                       # a spoken tool result: keep ALL of it (parse_out's 16-word cap
                out = " ".join(raw.split())                                  # is for tick chunks, and it chopped results mid-sentence)
                kind = "speech" if out else "silent"
            if kind == "delegate":
                kind = "speech" if out else "silent"
            if not events:
                if (kind == "silent" and self.answer_open and not text and self.cont_n < 2
                        and DANGLING.search(self.last_chunk or "")):
                    note = "continue"                                        # never leave a sentence dangling: force the rest (max 2 ticks)
                elif kind == "silent" and p_s < CFG["gate_lo"]:
                    note = "gate-lo"
                elif (kind == "silent" and not text and self.user_buf and p_s < CFG["gate_hi"] and silent_ms >= CFG["tick_ms"]
                      and not self.stt.in_utt and not self.stt.pending):
                    note = "speak-gate"
                elif kind != "silent" and p_s >= CFG["gate_lo"]:
                    kind, out, note = "silent", "", "forced-silence"
        if note == "continue":
            self.cont_n += 1
        if note in ("gate-lo", "speak-gate", "continue"):
            if gen != self.gen:
                return discard()
            r2 = await self.call_llm(prompt, True, cont=(note == "continue"))
            if r2 is None:
                return discard()
            raw = r2["text"].strip()
            tool = TOOL_RE.search(raw)
            if tool:
                out, kind = raw[:tool.start()].strip(), ("speech" if raw[:tool.start()].strip() else "silent")
            else:
                kind, out, _ = parse_out(raw, S)
                if kind == "delegate":
                    kind = "speech"
            r["ms"] += r2["ms"]
        if kind == "speech" and not re.search(r"\w", out):               # a forced 'speech' of pure punctuation is just silence
            kind, out = "silent", ""
        # don't start talking over a user who is still mid-sentence (the model sees partial words): wait for the sentence, except danger
        if (not events and not speaking and self.stt.in_utt and (kind == "speech" or tool) and not DANGER.search(text + " " + self.user_buf)):
            log("   (held back: user still mid-utterance) %r" % (out or raw))
            kind, out, tool, note = "silent", "", None, "held-mid-utterance"
        if gen != self.gen:                                              # interrupted while thinking: obsolete, never speak it
            log("   (discarded obsolete response: %r)" % raw)
            return discard()
        self.note_once = ""
        log("TICK %5.1fs words=%-30r %s-> %-6s %r%s p_s=%.2f %s %dms" % (
            now / 1000, text[:30], ("+%d result(s) " % len(events)) if events else "", kind.upper(), out,
            (" [@%s %s]" % (tool.group(1), tool.group(2)[:50])) if tool else "", p_s, note, r["ms"]))
        hist = (out + (" @%s %s" % (tool.group(1), tool.group(2).strip()) if tool else "")).strip()
        self.pol.commit(text, text, hist, "speech" if hist else "silent")
        if tool:
            arg = tool.group(2).strip()
            if tool.group(1) in ("ha", "search", "hermes") and len(arg.split()) < 4:
                # the model sometimes writes a truncated tag ('@ha are the'); the user's own words are the better request
                utter = re.sub(r"^(?:hey,? |okay,? )?(?:nina,? |jarvis,? )?(?:do you know |can you tell me |could you |can you |please |tell me )?(?:if |whether )?", "",
                               (self.user_buf + " " + text).strip(), flags=re.I)
                if len(utter.split()) >= 3:
                    log("   (repaired truncated tool request %r -> %r)" % (arg, utter[:80]))
                    arg = utter
            if tool.group(1) == "ha":
                # a write request must carry the user's own words: if the model's rewrite adds a command word the user never said
                # ("turn off the voice and turn on the test lamp"), use the utterance instead
                utter = (self.user_buf + " " + text).strip()
                cmd = {"on", "off", "and", "then", "open", "close", "lock", "unlock"}
                tools_mod._home["transcript"] = utter                      # for the optional mutation audit
                uw = set(re.findall(r"[a-z]+", utter.lower()))
                if len(utter.split()) >= 3 and (set(re.findall(r"[a-z]+", arg.lower())) & cmd) - uw:
                    log("   (ha request %r adds command words not in the user's words; using %r)" % (arg, utter[:80]))
                    arg = utter
            self.run_tool_call(tool.group(1), arg)
        if text:
            self.sess.add_turn("user", text)
        if kind == "speech" and out:
            self.sess.add_turn("assistant", out)
        if kind == "speech" and out:
            if not is_filler(out):
                self.user_buf = ""
            self.answer_open = not re.search(r"[.?!][\"')]*$", out)
            self.last_chunk = out
            if not self.answer_open:
                self.cont_n = 0
            self.speak(out)
            self.set_state("SPEAKING")
            await self.send(t="assistant", text=out)
        else:
            if self.sbuf:
                self.flush_speech()                                   # answer finished: speak what is buffered
            if not self.tts.speaking(self.now()):
                self.resp_started = False
            if self.state in ("SPEAKING", "INTERRUPTED") and not self.tts.speaking(self.now()):
                self.set_state("LISTENING")

    async def tick_loop(self):
        T = CFG["tick_ms"] / 1000
        nxt = self.loop.time() + T
        while not self.closed:
            await asyncio.sleep(max(0, nxt - self.loop.time()))
            if self.closed:
                break
            try:
                await self.tick()
            except Exception:                                           # noqa: BLE001  one bad tick must not kill the conversation
                import traceback
                log("TICK ERROR", traceback.format_exc()[-600:])
            nxt = max(nxt + T, self.loop.time())
            busy = self.tts.speaking(self.now()) or bool(self.sbuf)       # assistant activity only: ambient speech must not count
            for tr in self.sess.tick(busy):
                await self.after_transition(tr)
            # speaking state ends when the estimated playback queue drains
            if self.state == "SPEAKING" and not self.tts.speaking(self.now()):
                self.set_state("LISTENING")

    async def duck_watch(self):
        """Duck playback as soon as the user starts talking over it; the keyword/genuine-interruption logic decides the rest."""
        while True:
            await asyncio.sleep(0.1)
            speaking = self.tts.speaking(self.now())
            if speaking and self.stt.in_utt and self.stt.speech_ms >= 260 and not self.duck:
                self.duck = True
                await self.send(t="duck", on=True)
            elif self.duck and (not self.stt.in_utt or not speaking):
                self.duck = False
                await self.send(t="duck", on=False)

    # ------------------------------------------------------------ session lifecycle: Needle (needle_voice/session.py) decides, we act
    def on_session_event(self, e):
        needle_telemetry.write(SESSION_LOG, {**e, "source": "duplex-live"})
        if e["event"] not in ("configured",):
            log("SESSION", e["event"], {k: v for k, v in e.items() if k not in ("event", "ts", "source", "state", "session")})
        asyncio.ensure_future(self.send(t="session_event", **{k: v for k, v in e.items() if k not in ("ts", "t")}))

    async def send_mgr(self):
        c, s = self.sess.cfg, self.sess
        await self.send(t="mgr", state=s.state, wake=c.wake_word if c.wake_enabled else "off", words=WAKE_WORDS,
                        active_timeout=c.active_timeout_s, idle_timeout=c.idle_timeout_s, suspend_ttl=c.suspend_ttl_s,
                        session=s.session.id if s.session else None, tasks=len(s.pending_tasks()))

    async def setup_wake(self):
        self.sync_listen_mode()
        if not self.sess.cfg.wake_enabled or self.phrase_mode:
            return                                                       # no acoustic detector needed
        try:
            self.wake_det = WakeDetector(self.sess.cfg.wake_word, lambda n: asyncio.ensure_future(self.activate("wake_word")), log)
            await self.wake_det.start()
        except Exception as e:                                           # noqa: BLE001
            log("wake service unavailable, staying always-on:", repr(e)[:100])
            self.wake_det = None
            self.sess.configure(wake_enabled=False)
            await self.send(t="log", text="Wake-word service unreachable: always listening instead.")
            await self.send_mgr()

    def reset_session_state(self):
        """Session closed (DORMANT): drop everything scoped to it."""
        for j in self.jobs:
            if j.get("aio"):
                j["aio"].cancel()
        self.jobs = []
        self.reset_conv()
        self.dismiss_buf = ""

    async def after_transition(self, t):
        if not t:
            return
        to = t["to"]
        if to == DORMANT:
            self.reset_session_state()
        if to in (DORMANT, SUSPENDED):
            self.slept_at = time.monotonic()
            self.stt.reset_utt()
            self.stt.pending = []
            self.user_buf = ""
            self.set_state("LISTENING")
            if self.wake_det:
                try:
                    await self.wake_det.start()                          # fresh detector state: no stale 'hey jarvis' in its buffer
                except Exception as e:                                   # noqa: BLE001
                    log("wake detector restart failed:", repr(e)[:80])
            await self.send(t="earcon", kind="sleep")
        elif to == ACTIVE and t["from"] in (DORMANT, SUSPENDED):
            if not getattr(self, "_keep_stt", False):                     # phrase wake: the utterance is still in flight, keep it
                self.stt.reset_utt()
                self.stt.pending = []
            self.dismiss_buf = ""
            self.wake_grace_until, self.wake_clean = time.monotonic() + 4.0, False
            self.set_state("LISTENING")
            await self.send(t="earcon", kind="wake")
        self.sync_listen_mode()
        await self.send_mgr()

    async def activate(self, source):
        if self.sess.state == ACTIVE:
            self.sess.engage(source)
            return
        if source == "wake_word" and time.monotonic() - self.slept_at < 2.0 and self.sess.asleep:
            log("ignoring wake detection right after sleeping (stale detector state)")
            return
        self._keep_stt = source == "wake_phrase"
        try:
            await self.after_transition(self.sess.wake(source))
        finally:
            self._keep_stt = False

    async def dismiss(self, farewell=None):
        if self.sess.asleep or not self.sess.cfg.wake_enabled:
            return
        if farewell:
            self.resp_started = False
            self.speak(farewell, final=True)
            await self.send(t="assistant", text=farewell)
        await self.after_transition(self.sess.dismiss("spoken" if farewell else "button"))

    async def set_cfg(self, m):
        kw = {}
        for src, dst in (("active_timeout", "active_timeout_s"), ("idle_timeout", "idle_timeout_s"), ("suspend_ttl", "suspend_ttl_s"), ("timeout", "active_timeout_s")):
            if src in m:
                kw[dst] = max(0.0, float(m[src]))
        if "wake" in m:
            w = m["wake"]
            if w == "off":
                kw["wake_enabled"] = False
            elif w in WAKE_WORDS:
                kw.update(wake_enabled=True, wake_word=w)
        old = (self.sess.cfg.wake_enabled, self.sess.cfg.wake_word)
        self.sess.configure(**kw)
        new = (self.sess.cfg.wake_enabled, self.sess.cfg.wake_word)
        if new != old:
            if self.wake_det:
                await self.wake_det.stop()
                self.wake_det = None
            if new[0]:
                await self.setup_wake()
            await self.after_transition({"from": ACTIVE, "to": self.sess.state}) if self.sess.state == DORMANT else None
        log("config:", self.sess.cfg)
        self.sync_listen_mode()
        await self.send_mgr()

    @property
    def phrase_mode(self):
        return self.sess.cfg.wake_enabled and self.sess.cfg.wake_word in PHRASE_WAKE

    def sync_listen_mode(self):
        self.stt.listen_only = self.sess.asleep and self.phrase_mode

    def mic_diag(self, pcm):
        """While asleep, log once per 5 s whether audio is arriving and how loud, so 'it doesn't hear me' is diagnosable."""
        import numpy as np
        a = np.frombuffer(pcm, np.int16)
        d = self.__dict__.setdefault("_diag", {"n": 0, "peak": 0, "t": time.monotonic()})
        d["n"] += 1
        d["peak"] = max(d["peak"], int(np.abs(a).max()) if len(a) else 0)
        if time.monotonic() - d["t"] > 5:
            log("asleep: mic frames=%d peak=%d/32768 (say '%s')" % (d["n"], d["peak"], self.sess.cfg.wake_word.replace("_", " ")))
            d.update(n=0, peak=0, t=time.monotonic())

    async def run(self):
        await self.setup_wake()
        self.tasks = [asyncio.ensure_future(c) for c in (self.stt.run(), self.tick_loop(), self.duck_watch())]
        await self.send(t="hello", sr=self.sh.sample_rate, tick_ms=CFG["tick_ms"], stt=CFG["stt_model"],
                        tts=self.sh.tts_engine.kind, voices=self.sh.tts_engine.voices, voice=self.voice, sens=self.stt.sens,
                        tts_note=self.sh.tts_engine.note)
        await self.send_mgr()
        try:
            async for msg in self.ws:
                if msg.type == web.WSMsgType.BINARY:
                    if self.sess.asleep:
                        if self.phrase_mode:
                            self.stt.feed(msg.data)                  # phrase wake word: recogniser in listen-only mode
                        elif self.wake_det:
                            self.wake_det.feed(msg.data)             # asleep: audio only reaches the wake-word detector
                        self.mic_diag(msg.data)
                    else:
                        self.stt.feed(msg.data)
                elif msg.type == web.WSMsgType.TEXT:
                    m = json.loads(msg.data)
                    if m.get("t") == "stop":
                        self.interrupt("stop button")
                    elif m.get("t") == "sens":
                        self.stt.sens = max(0.0, min(0.9, float(m.get("v", 0))))
                        log("distant-voice filter set to", self.stt.sens)
                    elif m.get("t") == "voice" and m.get("name") in self.sh.tts_engine.voices:
                        self.voice = m["name"]
                    elif m.get("t") == "cfg":
                        await self.set_cfg(m)
                    elif m.get("t") == "wake":
                        await self.activate("tap")
                    elif m.get("t") == "sleep":
                        await self.dismiss()
                    elif m.get("t") == "text" and m.get("text", "").strip():       # typed input: same path as recognised speech
                        await self.activate("typed")
                        self.stt.pending += m["text"].split()
                        await self.send(t="stt", kind="final", text=m["text"])
                    elif m.get("t") == "reset":
                        self.interrupt("reset")
                        self.reset_conv()
                        await self.send(t="log", text="conversation reset")
        finally:
            self.closed = True
            if self.wake_det:
                await self.wake_det.stop()
            for t in self.tasks:
                t.cancel()
            self.interrupt("disconnect")


async def ws_handler(request):
    ws = web.WebSocketResponse(max_msg_size=1 << 20, heartbeat=20)
    await ws.prepare(request)
    log("client connected", request.remote, request.headers.get("User-Agent", "")[:80])
    await Session(ws, request.app["shared"], request.app["http"]).run()
    log("client disconnected", request.remote)
    return ws


async def api_config(request):
    """Everything the page needs to fill its dropdowns BEFORE a conversation is started (the WebSocket hello only exists after Start)."""
    sh = request.app["shared"]
    wake = os.environ.get("WAKE", "hey_jarvis")
    return web.json_response({
        "tts": sh.tts_engine.kind, "tts_note": sh.tts_engine.note, "voices": sh.tts_engine.voices, "voice": sh.tts_engine.default_voice,
        "wake_words": WAKE_WORDS, "wake": wake, "active_timeout": float(os.environ.get("TIMEOUT", "25")),
        "idle_timeout": float(os.environ.get("IDLE_TIMEOUT", "20")), "suspend_ttl": float(os.environ.get("SUSPEND_TTL", "300")),
        "sens": float(os.environ.get("SENS", "0.4")), "stt": CFG["stt_model"]})


async def index(request):
    return web.FileResponse(os.path.join(HERE, "static", "index.html"))


async def on_startup(app):
    app["shared"] = Shared()
    app["http"] = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60))


def main():
    app = web.Application()
    app.on_startup.append(on_startup)
    app.router.add_get("/", index)
    app.router.add_get("/api/config", api_config)
    app.router.add_get("/ws", ws_handler)
    ctx = None
    cert, key = os.path.join(HERE, "cert.pem"), os.path.join(HERE, "key.pem")
    if os.path.exists(cert):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
    log("serving %s on port %d" % ("https" if ctx else "http", CFG["port"]))
    web.run_app(app, host="0.0.0.0", port=CFG["port"], ssl_context=ctx, print=None)


if __name__ == "__main__":
    main()
