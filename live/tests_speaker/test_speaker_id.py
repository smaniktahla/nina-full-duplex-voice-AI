import os, sys, tempfile
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ["SPEAKER_DIR"] = tempfile.mkdtemp()
from speaker_id import identify, store
from speaker_id.identify import Status, decide


def test_short_is_insufficient():
    assert decide({"a": .9}, 300).status == Status.INSUFFICIENT_AUDIO

def test_identified_unknown_ambiguous():
    assert decide({"a": .8, "b": .2}, 2000).status == Status.IDENTIFIED
    assert decide({"a": .3, "b": .2}, 2000).status == Status.UNKNOWN
    r = decide({"child_a": .7, "child_b": .68}, 2000)
    assert r.status == Status.AMBIGUOUS and r.speaker_id is None

def test_no_profiles_unknown():
    assert decide({}, 2000).status == Status.UNKNOWN

def test_failopen_off(monkeypatch=None):
    os.environ["SPEAKER_ID"] = "off"
    try: assert identify.Identifier().identify(np.zeros(32000, np.int16)).status == Status.UNKNOWN
    finally: del os.environ["SPEAKER_ID"]

def test_store_perms_and_remove():
    store.save_profile("x", "X", "adult", [np.ones(192, np.float32)] * 3)
    assert oct(os.stat(store._p("embeddings", "x.npy")).st_mode & 0o777) == "0o600"
    assert store.remove("x") == 2 and not store.list_profiles()
