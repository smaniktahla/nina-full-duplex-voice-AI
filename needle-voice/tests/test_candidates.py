import json, os
from needle_voice import candidates as c

REAL = json.load(open(os.path.join(os.path.dirname(__file__), "real_snapshot.json")))

SMALL = {
    "entities": [
        {"entity_id": "switch.swimming_pool_exhaust_fan", "name": "Swimming Pool Exhaust Fan", "aliases": [], "area_id": "pool"},
        {"entity_id": "fan.pool_fan", "name": "Pool Fan", "aliases": [], "area_id": "pool"},
        {"entity_id": "light.kitchen_main", "name": "Kitchen Main", "aliases": [], "area_id": "kitchen"},
        {"entity_id": "light.den_lamp", "name": "Den Lamp", "aliases": [], "area_id": "den"},
        {"entity_id": "light.hall_1", "name": "Hall One", "aliases": [], "area_id": "hall"},
        {"entity_id": "light.hall_2", "name": "Hall Two", "aliases": [], "area_id": "hall"},
    ],
    "areas": [{"area_id": "kitchen", "name": "Kitchen", "aliases": []}, {"area_id": "den", "name": "Den", "aliases": []},
              {"area_id": "pool", "name": "Swimming Pool", "aliases": ["pool"]}, {"area_id": "hall", "name": "Hall", "aliases": []}],
}


def top(text, snap=SMALL):
    return c.unique_top(c.rank(text, snap))


def test_exact_named_device_beats_lookalike():
    assert top("turn on the pool fan").id == "fan.pool_fan"
    assert top("is the pool exhaust fan on").id == "switch.swimming_pool_exhaust_fan"


def test_kitchen_lights_resolves_to_the_area_group():
    assert top("turn on the kitchen lights").id == "area:kitchen"


def test_generic_words_alone_never_identify_a_target():
    assert c.rank("turn on the light", SMALL) == []
    assert c.rank("turn on the fan", SMALL) == []


def test_unrelated_request_has_no_candidates():
    assert c.rank("play some jazz", SMALL) == []


def test_ambiguity_yields_no_unique_top():
    snap = {"entities": [
        {"entity_id": "light.a", "name": "Garage Lamp", "aliases": [], "area_id": None},
        {"entity_id": "light.b", "name": "Garage Lamp", "aliases": [], "area_id": None}], "areas": []}
    assert len(c.rank("turn on the garage lamp", snap)) == 2
    assert c.unique_top(c.rank("turn on the garage lamp", snap)) is None


def test_plural_stripping():
    assert top("turn off the den lamps").id in ("light.den_lamp", "area:den")


def test_lexicon_adds_household_phrases_and_ignores_unknown_ids():
    lex = {"switch.swimming_pool_exhaust_fan": ["the vent"], "light.does_not_exist": ["ghost light"]}
    merged = c.merge_lexicon(SMALL, lex)
    assert top("turn on the vent", merged).id == "switch.swimming_pool_exhaust_fan"
    assert c.rank("turn on the ghost light", merged) == []      # stale entries can't create a target
    assert c.rank("turn on the vent", SMALL) == []              # original snapshot untouched


def test_lexicon_can_alias_an_area():
    merged = c.merge_lexicon(SMALL, {"area:den": ["man cave"]})
    assert top("turn on the man cave lights", merged).id == "area:den"


def test_merge_lexicon_tolerates_garbage():
    assert c.merge_lexicon(SMALL, None) is SMALL
    assert c.merge_lexicon(None, {"a": ["b"]}) is None
    assert c.merge_lexicon(SMALL, {"light.den_lamp": "notalist"}) is not None


def test_candidate_list_is_capped_for_the_prompt():
    assert len(c.rank("kitchen", REAL)) <= c.MAX_CANDIDATES


def test_real_data_kitchen_lights_and_tv():
    assert c.unique_top(c.rank("turn on the kitchen lights", REAL)).id == "area:kitchen"
    assert c.unique_top(c.rank("pause the living room tv", REAL)).id == "media_player.living_room_tv"


def test_garbage_snapshot_is_safe():
    for bad in (None, {}, {"entities": "x"}, {"entities": [None, 3, {"entity_id": None}], "areas": [None]}):
        assert c.rank("turn on the kitchen lights", bad) == []


def test_spoken_numbers_and_units_are_arguments_not_identity():
    snap = {"entities": [{"entity_id": "climate.living_room_thermostat", "name": "Living Room Thermostat", "aliases": [], "area_id": "living_room"}],
            "areas": [{"area_id": "living_room", "name": "Living Room", "aliases": []}]}
    assert c.unique_top(c.rank("set the living room thermostat to 70 degrees", snap)).id == "climate.living_room_thermostat"


def test_look_alike_clutter_is_trimmed():
    ids = [x.id for x in c.rank("turn on the pool fan", REAL)]
    assert ids == ["fan.pool_fan"]


def test_equivalent_candidates_only_for_an_area_with_exactly_one_light():
    one = {"entities": [{"entity_id": "light.k1", "name": "K1", "aliases": [], "area_id": "kitchen"}], "areas": [{"area_id": "kitchen", "name": "Kitchen", "aliases": []}]}
    two = {"entities": one["entities"] + [{"entity_id": "light.k2", "name": "K2", "aliases": [], "area_id": "kitchen"}], "areas": one["areas"]}
    top1 = c.unique_top(c.rank("turn on the kitchen lights", one))
    assert [x.id for x in c.equivalent_candidates(top1, one)] == ["light.k1"]
    assert c.equivalent_candidates(c.unique_top(c.rank("turn on the kitchen lights", two)), two) == []
    ent_top = c.Candidate("light.k1", "entity", "K1", "light", None, 1.0)
    assert c.equivalent_candidates(ent_top, one) == [] and c.equivalent_candidates(None, one) == []
