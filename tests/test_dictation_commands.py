"""Spoken dictation commands: start / finish / cancel / not-recording, and what must NOT be taken for a command."""
import os, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "live"))
import notes_tool                                                              # noqa: E402
from notes_tool import voice_command as vc, Recording                          # noqa: E402


def test_start_phrases_when_not_recording():
    for t in ("Take notes.", "Can you write down the following for me?", "start taking notes", "Okay take a note", "write this down",
              "I want to dictate a note", "let's dictate", "start dictation", "make a transcript"):
        r = vc(t, False)
        assert r and r[0] == "start", t
    assert vc("make a transcript of this", False)[1] == "transcript" and vc("take notes", False)[1] == "notes"


def test_finish_phrases_while_recording():
    for t in ("And then go ahead and write that down.", "okay write that into the note", "write this to Doc Most", "Save the notes.",
              "Save that to doc most", "put that in the note", "create the note in DocMost", "finish the notes", "wrap up the notes",
              "That's all.", "okay, that's it", "I'm done.", "Done.", "okay write that down", "write it all down"):
        r = vc(t, True)
        assert r and r[0] == "finish", t


def test_title_is_taken_from_the_command():
    assert vc("save the notes and call it Weekend Plans", True) == ("finish", "Weekend Plans")
    assert vc("take notes and call it grocery run", False)[2] == "grocery run"


def test_cancel_while_recording():
    for t in ("cancel the notes", "discard those notes", "throw away the dictation", "scrap the transcript"):
        assert vc(t, True) == ("cancel", ""), t


def test_dictated_content_is_not_a_command():
    for t in ("Later today I'm going to be riding my motorcycle.", "Then I'm going to floss my teeth.", "Perhaps I'll choose to write a poem.",
              "I need to save the notes for the meeting tomorrow at ten and then send them to everyone on the team",     # long: content
              "Bananas, grapes and some oranges.", "He wants to take notes at school", "what's the weather tomorrow"):
        assert vc(t, True) is None, t


def test_not_recording_save_phrases_get_a_hint_not_a_crash():
    for t in ("And then write this to Doc Most.", "Are you able to create the note in DocMost?", "save the notes"):
        assert vc(t, False) == ("not_recording", ""), t
    for t in ("write that down", "what time is it", "That's all.", "okay thanks"):
        assert vc(t, False) is None, t                                          # no note words: not ours


def test_frozen_recording_ignores_the_command_itself():
    r = Recording("notes", "", "u", "s", path=os.path.join(tempfile.mkdtemp(), "x.jsonl"))
    r.add("user", "first thing"); r.add_point("first thing")
    r.frozen = True
    r.add("user", "go ahead and write that down"); r.add_point("go ahead and write that down")
    assert [t[2] for t in r.turns] == ["first thing"] and r.points == ["first thing"]
