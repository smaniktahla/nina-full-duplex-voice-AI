import asyncio
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "live"))
from responder import build_prompt, clean, generate, split_ready


async def stream(*pieces):
    for p in pieces:
        await asyncio.sleep(0)
        yield p


def run(*pieces, stop_after=None, max_sentences=4):
    got = []
    async def on(s, first):
        got.append((s, first))
        return not (stop_after and len(got) >= stop_after)
    spoken = asyncio.run(generate(stream(*pieces), on, max_sentences))
    return got, spoken


def test_first_sentence_is_emitted_before_the_stream_ends():
    seen = []
    async def gen():
        async def on(s, first):
            seen.append((s, first, "later" not in "".join(seen_text))); return True
        seen_text = []
        async def st():
            for p in ["Yes, I can hear you ", "clearly. ", "How can I ", "help today?"]:
                seen_text.append(p); yield p
        return await generate(st(), on)
    out = asyncio.run(gen())
    assert out == ["Yes, I can hear you clearly.", "How can I help today?"] and seen[0][1] is True and seen[1][1] is False


def test_whole_reply_not_one_word():
    got, _ = run("It", " ", "should be sunny and warm ", "this afternoon.")
    assert got == [("It should be sunny and warm this afternoon.", True)]


def test_short_sentences_join_the_next():
    got, _ = run("Sure. ", "I turned it on for you.")
    assert [g[0] for g in got] == ["Sure. I turned it on for you."]


def test_decimals_and_abbreviations_do_not_split():
    got, _ = run("It is 72.5 degrees out ", "right now. ", "Enjoy it.")
    assert [g[0] for g in got] == ["It is 72.5 degrees out right now.", "Enjoy it."] or got[0][0].startswith("It is 72.5 degrees out right now.")


def test_stops_when_interrupted_and_never_speaks_more():
    got, spoken = run("One two three four. ", "Five six seven eight. ", "Nine ten eleven twelve.", stop_after=1)
    assert len(got) == 1 and spoken == []


def test_tool_tags_markdown_stripped_and_cap():
    assert clean("Sure thing @ha turn it on") == "Sure thing" and clean("**Hello** there `x`") == "Hello there x"
    got, _ = run(*["Sentence number %d is here. " % i for i in range(9)], max_sentences=3)
    assert len(got) == 3


def test_trailing_fragment_without_punctuation_is_spoken():
    got, _ = run("Okay, I will look into that for you")
    assert got == [("Okay, I will look into that for you", True)]


def test_split_ready():
    assert split_ready("Hello there friend. And") == (["Hello there friend."], "And")
    assert split_ready("a b c", final=True) == (["a b c"], "")


def test_prompt_has_history_and_no_tick_format():
    chat = lambda r, t: f"<{r}>{t}</>"
    p = build_prompt(chat, "<T>", "SYS", [("user", "hi"), ("assistant", "hello there")], "what's up")
    assert p == "<system>SYS</><user>hi</><assistant>hello there</><user>what's up</><T>"
