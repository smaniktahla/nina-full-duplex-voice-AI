from needle_voice import guards as g


def test_single_simple_commands_pass_the_guard():
    for t in ("turn on the kitchen lights", "pause plex", "is the pool fan on", "what's the weather today"):
        assert g.pre_guard(t) is None, t


def test_multi_target_utterances_escalate():
    for t in ("turn off the kitchen and basement lights", "turn off everything downstairs", "turn on all the lights", "lights on then fan off"):
        assert g.pre_guard(t) == "multi_target_markers", t


def test_negation_escalates():
    assert g.pre_guard("don't turn on the kitchen light") == "negation"
    assert g.pre_guard("do not turn on the light") == "negation"


def test_reasoning_escalates():
    assert g.pre_guard("Why do you think the pool temperature is dropping faster tonight?") == "reasoning_markers"
    assert g.pre_guard("is the basement colder than upstairs") == "reasoning_markers"
    assert g.pre_guard("how come the fan is on") == "reasoning_markers"


def test_empty_escalates():
    assert g.pre_guard("") == "empty_utterance" and g.pre_guard("   ") == "empty_utterance" and g.pre_guard(None) == "empty_utterance"


def test_intent_signals():
    assert g.intent_signals("pause plex") == {"media_pause"}
    assert "turn_on" in g.intent_signals("turn the light on")
    assert "turn_off" in g.intent_signals("turn off the bedroom light")
    assert "turn_on" not in g.intent_signals("turn off the bedroom light")
    assert "turn_off" in g.intent_signals("shut off the fan") and "close" not in g.intent_signals("shut off the fan")
    assert "close" in g.intent_signals("shut the garage door")
    assert g.intent_signals("unlock the front door") == {"unlock"}
    assert g.intent_signals("lock the front door") == {"lock"}
    assert "set_brightness" in g.intent_signals("dim the kitchen light to 30 percent")
    assert "set_temperature" in g.intent_signals("set the thermostat to 70 degrees")
    assert "get_weather" in g.intent_signals("what's the weather today")


def test_a_question_about_state_does_not_imply_a_write():
    s = g.intent_signals("is the pool exhaust fan on")
    assert "get_state" in s and "turn_on" not in s


def test_corroboration_catches_the_observed_pause_vs_play_error():
    assert g.corroborates("media_play", "pause plex")[0] is False   # what Qwen3.5-0.8B actually returned
    assert g.corroborates("media_pause", "pause plex")[0] is True


def test_corroboration_catches_needles_observed_off_vs_on_error():
    assert g.corroborates("turn_on", "turn off the bedroom light")[0] is False


def test_conflicting_write_words_fail():
    ok, why = g.corroborates("open", "open close the door")
    assert not ok and why.startswith("conflicting")


def test_unrelated_intent_fails():
    assert g.corroborates("lock", "what is the temperature")[0] is False


def test_switch_the_noun_is_not_switch_the_verb():
    """Found live: "is the needle test switch on" was read as turn_on because of the noun."""
    assert "turn_on" not in g.intent_signals("is the needle test switch on")
    assert "get_state" in g.intent_signals("is the needle test switch on")
    assert "turn_off" not in g.intent_signals("is the light switch off")
    assert "turn_on" in g.intent_signals("turn on the needle test switch")


def test_switch_the_verb_still_works():
    for text, want in (("switch off the lamp", "turn_off"), ("switch on the pool fan", "turn_on"),
                       ("please switch the lamp on", "turn_on"), ("can you switch off the fan", "turn_off")):
        assert want in g.intent_signals(text), text
