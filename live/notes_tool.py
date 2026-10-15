"""Conversation notes / transcripts saved to DocMost, on request.

The user asks ("Nina, take notes", "write down this conversation", "make a transcript of this call"); the model answers with a tag and the CODE does
the rest, so the recording never depends on the model remembering anything:

    @notes start [notes|transcript] [title]   begin recording (what is said from now on)
    @notes add <text>                         put one explicit point into the notes ("note that ...")
    @notes stop [title]  /  @notes save [title]   finish and write the DocMost note (stop = recording ends, save = same, kept for natural phrasing)
    @notes cancel                             throw the recording away

Modes: `notes` (default) = concise bullet notes + decisions + action items written from the conversation by one clean LLM request;
`transcript` = the verbatim turns with times. Every note carries date, time (in the household time zone), title and who was speaking.
A recording is flushed to a private local file as it grows (survives a crash) and saved automatically if the session ends first.
"""
import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

import docmost
import household

BUFFER_DIR = os.environ.get("NOTES_BUFFER_DIR", os.path.expanduser("~/.local/share/nina/notes_buffer"))
FAILED_DIR = os.environ.get("NOTES_FAILED_DIR", os.path.expanduser("~/.local/share/nina/notes_failed"))

PROMPT = ("\nNOTES (only when the user asks you to take notes, write down / record this conversation, or make a transcript): say a short acknowledgement and "
          "write '@notes start notes <title if they gave one>' (or '@notes start transcript <title>' if they said transcript). While recording, "
          "if the user states something to put in the notes, '@notes add <it>'. When they say stop / that's enough notes / save it, say a short "
          "acknowledgement and write '@notes stop <title if given>'; the result is spoken back to you. '@notes cancel' discards. Never start "
          "recording on your own, and tell the user when you do.")


def enabled() -> bool:
    return docmost.configured()


def tz():
    try:
        return ZoneInfo(household.get("timezone"))
    except Exception:                                                # noqa: BLE001
        return ZoneInfo("America/New_York")


def parse_arg(arg: str):
    """'start transcript Weekly planning' -> ('start', 'transcript', 'Weekly planning'); 'stop' -> ('stop', None, '')."""
    arg = (arg or "").strip()
    m = re.match(r"(?i)(start|begin|stop|end|save|finish|add|cancel|discard)\b[:\s]*(.*)$", arg, re.S)
    if not m:
        return "", None, ""
    action = {"begin": "start", "end": "stop", "finish": "stop", "discard": "cancel"}.get(m.group(1).lower(), m.group(1).lower())
    rest = m.group(2).strip()
    mode = None
    if action == "start":
        mm = re.match(r"(?i)(notes|note|transcript|transcription)\b[:\s,-]*(.*)$", rest, re.S)
        if mm:
            mode = "transcript" if mm.group(1).lower().startswith("transcript") else "notes"
            rest = mm.group(2).strip()
    return action, mode, rest.strip(" \"'")


def clean_title(t: str, limit: int = 90) -> str:
    t = re.sub(r"[\r\n\t]+", " ", t or "").strip(" \"'`*#-:")
    t = re.sub(r"\s+", " ", t)
    return t[:limit].rstrip()


@dataclass
class Recording:
    mode: str
    title: str
    user: str
    session_id: str
    started: float = field(default_factory=time.time)
    turns: list = field(default_factory=list)            # (epoch seconds, who, text)
    points: list = field(default_factory=list)           # explicit "@notes add" points
    path: str = ""

    def add(self, who: str, text: str):
        text = (text or "").strip()
        if not text:
            return
        if self.turns and self.turns[-1][1] == who and self.turns[-1][2] == text:
            return
        self.turns.append((time.time(), who, text))
        self._flush({"t": self.turns[-1][0], "who": who, "text": text})

    def add_point(self, text: str):
        self.points.append(text.strip())
        self._flush({"t": time.time(), "who": "note", "text": text.strip()})

    def _flush(self, rec: dict):
        if not self.path:
            return
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            new = not os.path.exists(self.path)
            with open(self.path, "a") as f:
                f.write(json.dumps(rec) + "\n")
            if new:
                os.chmod(self.path, 0o600)
        except OSError:
            pass

    def drop_buffer(self):
        try:
            if self.path:
                os.remove(self.path)
        except OSError:
            pass

    def transcript_text(self, limit: int = 14000) -> str:
        zone = tz()
        lines = ["[%s] %s: %s" % (datetime.fromtimestamp(t, zone).strftime("%H:%M:%S"), "Nina" if w == "assistant" else self.user or "User", x)
                 for t, w, x in self.turns]
        s = "\n".join(lines)
        return s if len(s) <= limit else "(earlier part omitted)\n" + s[-limit:]


def header(rec: Recording, ended: float, saved_by: str = "") -> str:
    zone = tz()
    a, b = datetime.fromtimestamp(rec.started, zone), datetime.fromtimestamp(ended, zone)
    when = a.strftime("%-I:%M %p") + (" to " + b.strftime("%-I:%M %p") if b.strftime("%H:%M") != a.strftime("%H:%M") else "")
    return ("**Date:** %s  \n**Time:** %s %s  \n**Speaker:** %s  \n**Source:** Nina voice assistant (%s, session %s)%s\n\n---\n\n"
            % (a.strftime("%m/%d/%Y"), when, a.strftime("%Z"), rec.user or "unknown", rec.mode, rec.session_id or "-",
               ("  \n**Saved:** " + saved_by) if saved_by else ""))


def render_transcript(rec: Recording) -> str:
    zone = tz()
    out = ["## Transcript\n"]
    for t, who, text in rec.turns:
        out.append("- **%s** (%s): %s" % ("Nina" if who == "assistant" else (rec.user or "User"), datetime.fromtimestamp(t, zone).strftime("%-I:%M:%S %p"), text))
    return "\n".join(out) + "\n"


def note_title(rec: Recording, title: str) -> str:
    d = datetime.fromtimestamp(rec.started, tz()).strftime("%m/%d/%Y")
    return "%s - %s" % (d, clean_title(title) or "Nina conversation")


class NotesTool:
    def __init__(self, session, llama_url: str, http):
        self.s, self.llama, self.http = session, llama_url, http
        self.rec = None

    # ------------------------------------------------------------ who / state
    def speaker(self) -> str:
        return (getattr(self.s.sess, "speaker", None) or household.get("notes_default_user") or "").strip() or "unknown"

    def active(self) -> bool:
        return self.rec is not None

    def _ui(self):
        r = self.rec
        asyncio.ensure_future(self.s.send(t="notes", recording=bool(r), mode=r.mode if r else "", title=r.title if r else ""))

    # ------------------------------------------------------------ tag handling (sync part; the save runs as a background job)
    def handle(self, arg: str):
        action, mode, rest = parse_arg(arg)
        if action == "start":
            if self.rec:
                return
            sid = self.s.sess.session.id if self.s.sess.session else "-"
            self.rec = Recording(mode or "notes", clean_title(rest), self.speaker(), sid,
                                 path=os.path.join(BUFFER_DIR, "%s-%d.jsonl" % (sid, int(time.time()))))
            self._ui()
        elif action == "add" and self.rec and rest:
            self.rec.add_point(rest)
        elif action in ("stop", "save"):
            if self.rec:
                self.s.start_tool("notes", "save|" + clean_title(rest))
        elif action == "cancel" and self.rec:
            self.rec.drop_buffer()
            self.rec = None
            self._ui()

    def capture(self, who: str, text: str):
        if self.rec:
            self.rec.add(who, text)

    # ------------------------------------------------------------ the save (background job result is spoken)
    async def finish(self, goal: str, quiet: bool = False) -> str:
        rec, self.rec = self.rec, None
        self._ui()
        if not rec or not (rec.turns or rec.points):
            if rec:
                rec.drop_buffer()
            return "There was nothing to save yet."
        ended = time.time()
        title = goal.split("|", 1)[1] if "|" in goal else ""
        title = clean_title(title) or rec.title
        try:
            body, gen_title = await self._compose(rec, title)
            title = title or gen_title
            content = header(rec, ended, "automatically when the conversation ended" if quiet else "") + body
            page = await docmost.create_note(self.http, note_title(rec, title), content)
        except Exception as e:                                       # noqa: BLE001 - never lose a recording
            self._park(rec, title, ended, "%s: %s" % (type(e).__name__, str(e)[:120]))
            return "I couldn't save the note to DocMost (%s). I kept a local copy so nothing is lost." % str(e)[:80]
        rec.drop_buffer()
        return "Saved the %s to DocMost as '%s'." % ("transcript" if rec.mode == "transcript" else "notes", note_title(rec, title))

    async def autosave(self, reason: str):
        """Session ended while recording: save what we have (the user was told recording is on)."""
        if self.rec:
            msg = await self.finish("save|", quiet=True)
            return msg

    def _park(self, rec, title, ended, why):
        try:
            os.makedirs(FAILED_DIR, exist_ok=True)
            p = os.path.join(FAILED_DIR, "%s-%d.md" % (rec.session_id, int(rec.started)))
            with open(p, "w") as f:
                f.write("# %s\n\n_save failed: %s_\n\n%s%s" % (note_title(rec, title), why, header(rec, ended), render_transcript(rec)))
            os.chmod(p, 0o600)
            rec.drop_buffer()
        except OSError:
            pass

    async def _compose(self, rec: Recording, title: str):
        if rec.mode == "transcript":
            gen = "" if title else await self._title(rec)
            return render_transcript(rec), gen
        notes, gen = await self._summarise(rec, want_title=not title)
        pts = ("## Points you asked me to note\n\n" + "\n".join("- " + p for p in rec.points) + "\n\n") if rec.points else ""
        return "## Notes\n\n" + notes.strip() + "\n\n" + pts, gen

    # ------------------------------------------------------------ one clean LLM request each (no tick history)
    async def _oneshot(self, system: str, user: str, n: int, stop=None) -> str:
        from duplex.loop import THINK_PREFIX, chat
        body = {"prompt": chat("system", system) + chat("user", user) + THINK_PREFIX, "n_predict": n, "temperature": 0.2, "cache_prompt": True,
                "stop": stop or ["<|im_end|>"]}
        async with self.http.post(self.llama + "/completion", json=body) as r:
            d = await r.json()
        return d.get("content", "").strip()

    async def _title(self, rec: Recording) -> str:
        t = await self._oneshot("You title conversations. Reply with only a title of 3 to 7 words, no quotes, no punctuation at the end.",
                                rec.transcript_text(4000), 24, ["<|im_end|>", "\n"])
        return clean_title(t)

    async def _summarise(self, rec: Recording, want_title: bool):
        who = rec.user if rec.user != "unknown" else "the user"
        sysmsg = ("You write concise notes from a conversation between %s and Nina, a voice assistant. Use only what was said; never invent. "
                  "Output markdown bullet points: key facts, decisions, then 'Action items:' (only if there are any). %s" %
                  (who, "Put 'TITLE: <3 to 7 word title>' on the first line." if want_title else "Do not write a title."))
        text = await self._oneshot(sysmsg, rec.transcript_text() + ("\n\nExplicit points: " + "; ".join(rec.points) if rec.points else ""), 450)
        title = ""
        m = re.match(r"(?is)\s*TITLE:\s*(.+?)\n(.*)$", text)
        if m:
            title, text = clean_title(m.group(1)), m.group(2)
        return text or "- (nothing noteworthy was said)", title
