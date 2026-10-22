"""Current local time, independent of Home Assistant and the language model."""
import datetime as dt
import re
from zoneinfo import ZoneInfo


def owns(text):
    # The wake greeting can echo into the microphone, and STT can clip "what".
    # Remove only known greeting prefixes; do not match arbitrary trailing text.
    text = (text or "").lower().replace("\u2019", "'")
    text = " ".join(re.sub(r"[^a-z' ]", " ", text).split())
    text = re.sub(r"^(?:hey nina |nina )", "", text)
    text = re.sub(r"^(?:(?:hey|yeah) what(?:'s|s| is) up|hey what can i do for you)\s+", "", text)
    return bool(re.fullmatch(
        r"(?:please )?(?:(?:can|could) you (?:please )?)?(?:"
        r"(?:what )?time is it(?: right now| now)?|what(?:'s| is) the (?:current )?time"
        r"|(?:tell me|give me) (?:the )?(?:current )?time|do you have the time)(?: please)?", text))



def answer(timezone, now=None):
    zone = ZoneInfo(timezone)
    local = dt.datetime.now(zone) if now is None else now.astimezone(zone)
    return "It's %d:%02d %s." % (local.hour % 12 or 12, local.minute, local.strftime("%p"))
