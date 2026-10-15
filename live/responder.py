"""Reply generation decoupled from the input tick loop (flag RESPONDER=1 in server.py).

The tick loop only decides WHETHER to speak (turn-taking, barge-in, dismissal). When it decides to speak and no tool is involved, ONE request
generates the whole reply from a clean prompt (not the tick-format history that taught the model to emit a few words and end its turn), and the
reply is spoken one sentence per TTS unit, sentence 1 as soon as it completes. Pure helpers + an injected token stream, so it is testable offline.
"""

from __future__ import annotations

import re
from typing import AsyncIterator, Awaitable, Callable

_END = re.compile(r"""(?<=[.!?])["')\]]*\s+""")
MIN_WORDS = 3                                  # a 1-2 word sentence ("Sure.") is joined to the next one rather than spoken alone


def split_ready(buf: str, final: bool = False) -> tuple[list[str], str]:
    """Complete sentences in `buf` (a sentence is complete once end punctuation is followed by whitespace, or the stream ended)."""
    out, pos = [], 0
    for m in _END.finditer(buf):
        out.append(buf[pos:m.end()].strip())
        pos = m.end()
    rest = buf[pos:]
    if final and rest.strip():
        out.append(rest.strip())
        rest = ""
    return [s for s in out if s], rest


def clean(text: str) -> str:
    """Spoken text only: drop tool tags, markdown and symbols the voice shouldn't read."""
    text = text.split("@")[0]
    text = re.sub(r"[*_`#>]+", "", text)
    return re.sub(r"\s+", " ", text).strip()


def persona(facts: list[str]) -> str:
    base = ("You are Nina, a friendly voice assistant in a live spoken conversation with one person. Reply in plain spoken English: one to three short "
            "sentences, no markdown, no lists, no symbols, no emoji. Say numbers and times the way people say them. Answer the user's latest message "
            "directly and use the conversation so far; do not repeat what you already said. If you do not know something, say so briefly. Never claim you cannot access the user's calendar, home or devices: those are handled by separate tools; if asked about them and the answer is not in the conversation, just say you'll check.")
    if facts:
        base += "\nWhat you know about the user (use it, don't recite it unprompted):\n- " + "\n- ".join(facts)
    return base


def build_prompt(chat: Callable[[str, str], str], think_prefix: str, system: str, convo: list[tuple[str, str]], user: str, note: str = "") -> str:
    parts = [chat("system", system)]
    for role, text in convo[-10:]:
        parts.append(chat(role, text))
    parts.append(chat("user", user + (("\n" + note) if note else "")))
    return "".join(parts) + think_prefix


async def generate(stream: AsyncIterator[str], on_sentence: Callable[[str, bool], Awaitable[bool]], max_sentences: int = 4) -> list[str]:
    """Consume a token stream, call on_sentence(text, is_first) per spoken unit; stop if it returns False (interrupted). Returns what was spoken."""
    spoken: list[str] = []
    buf, carry = "", ""

    async def emit(units: list[str]) -> bool:
        nonlocal carry
        for u in units:
            u = clean(carry + " " + u if carry else u)
            carry = ""
            if not u:
                continue
            if len(u.split()) < MIN_WORDS:
                carry = u
                continue
            if len(spoken) >= max_sentences:
                return False
            if not await on_sentence(u, not spoken):
                return False
            spoken.append(u)
        return True

    async for piece in stream:
        buf += piece
        ready, buf = split_ready(buf)
        if not await emit(ready):
            return spoken
    ready, buf = split_ready(buf, final=True)
    if await emit(ready) and carry and len(spoken) < max_sentences:
        if await on_sentence(carry, not spoken):
            spoken.append(carry)
    return spoken
