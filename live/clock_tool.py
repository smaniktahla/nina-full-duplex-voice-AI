"""Current local time, independent of Home Assistant and the language model."""
import datetime as dt
import re
from zoneinfo import ZoneInfo


def owns(text):
    text = re.sub(r"[^a-z' ]", "", (text or "").lower()).strip()
    text = re.sub(r"^(?:hey nina |nina )", "", text)
    return bool(re.fullmatch(
        r"(?:please )?(?:(?:can|could) you (?:please )?)?(?:"
        r"what time is it(?: right now| now)?|what(?:'s| is) the (?:current )?time"
        r"|(?:tell me|give me) (?:the )?(?:current )?time|do you have the time)(?: please)?", text))


def answer(timezone, now=None):
    zone = ZoneInfo(timezone)
    local = dt.datetime.now(zone) if now is None else now.astimezone(zone)
    return "It's %d:%02d %s." % (local.hour % 12 or 12, local.minute, local.strftime("%p"))
