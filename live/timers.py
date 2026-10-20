"""One process-local timer, using monotonic time for expiry and remaining time."""
import math
import re
import time
import uuid

NUMBERS = dict(zip("one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty thirty forty fifty sixty seventy eighty ninety".split(), list(range(1, 21)) + [30, 40, 50, 60, 70, 80, 90]))


def duration(text):
    t = re.sub(r"(?<=[a-z])-(?=[a-z])", " ", text.lower())
    t = re.sub(r"(?<=\d)-(?=(?:hour|minute|second))", " ", t)
    if re.search(r"\b(?:hundred|thousand|quarter)\b|\band (?:a )?half\b", t):
        return None
    t = re.sub(r"\b(" + "|".join(NUMBERS) + r")\b", lambda m: str(NUMBERS[m[0]]), t)
    t = re.sub(r"\b([2-9])0 (\d)\b", lambda m: str(int(m[1]) * 10 + int(m[2])), t)
    t = re.sub(r"\b(?:an?|half an?)\s+(hour|minute|second)\b", lambda m: ("0.5" if m[0].startswith("half") else "1") + " " + m[1], t)
    parts = re.findall(r"(?<![\w.-])(\d+(?:\.\d+)?)\s*(hours?|minutes?|seconds?)\b", t)
    if not parts or re.search(r"-\d|\b(?:minus|negative)\b", t):
        return None
    seconds = sum(float(n) * (3600 if u.startswith("hour") else 60 if u.startswith("minute") else 1) for n, u in parts)
    return seconds if 1 <= seconds <= 86400 else None


def spoken(seconds):
    n = max(0, math.ceil(seconds))
    parts = []
    for unit, size in (("hour", 3600), ("minute", 60), ("second", 1)):
        count, n = divmod(n, size)
        if count:
            parts.append(f"{count} {unit}{'s' if count != 1 else ''}")
    return " and ".join(parts) or "0 seconds"


class Timer:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.current = None
        self.naming_until = 0

    def owns(self, text):
        return bool(re.search(r"\btimers?\b", text, re.I) or self.label_reply(text))

    def label_reply(self, text):
        if not self.current or self.clock() >= self.naming_until:
            return None
        m = re.match(r"(?:sure|yes|yeah|okay|ok)[,.!]?\s*(?:this is |it is |it's )?for\s+(.+?)[.!?]*$", text, re.I)
        if not m:
            m = re.match(r"(?:call it|label it|name it|this is for|it's for)\s+(.+?)[.!?]*$", text, re.I)
        return m[1][:120] if m else None

    def remaining(self):
        return max(0, self.current["deadline"] - self.clock()) if self.current else 0

    def handle(self, text):
        explicit = re.match(r"(?:name|label|call) (?:the |this )?timer (.+?)[.!?]*$", text, re.I)
        label = explicit[1][:120] if explicit and self.current else self.label_reply(text)
        if label:
            self.current["label"] = label
            self.naming_until = 0
            return f"I've labeled the timer {label}.", False
        if re.search(r"\b(cancel|stop|delete|dismiss)\b", text, re.I):
            self.current = None
            self.naming_until = 0
            return "The timer is cancelled.", False
        if re.search(r"\b(set|start|create)\b", text, re.I):
            if self.current and self.remaining() > 0:
                return "There is already a timer running. Please cancel it before setting another one.", False
            seconds = duration(text)
            if seconds is None:
                return "How long should I set the timer for? Please say set a timer followed by a duration between 1 second and 24 hours.", False
            self.current = {"id": uuid.uuid4().hex, "deadline": self.clock() + seconds, "label": "Timer"}
            self.naming_until = self.clock() + 60
            return f"I've set a timer for {spoken(seconds)}. Want me to label this one for something?", True
        if not self.current:
            return "There is no timer set.", False
        return (f"There are {spoken(self.remaining())} left on {self.current['label']}." if self.remaining() else f"{self.current['label']} is finished."), False

    def document(self):
        if not self.current:
            return None
        return {"kind": "timer", "id": self.current["id"], "updated": uuid.uuid4().hex,
                "title": self.current["label"], "remaining_s": self.remaining(), "hold_s": 60}
