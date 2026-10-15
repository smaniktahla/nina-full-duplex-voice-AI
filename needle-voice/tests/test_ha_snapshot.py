"""Regression tests for ha_snapshot.build_entity_snapshot against STUBBED HA.
The ComputedNameType case reproduces a real failure seen on HA 2026.9.3: every
live request fell through to Hermes because sorted() on registry aliases raised."""
import sys, types
import pytest
from needle_voice import ha_snapshot as hs


class ComputedNameType:          # stands in for HA's sentinel; deliberately not orderable against str
    pass


def install(monkeypatch, entity_regs, states, areas, exposed, device_areas=None):
    def mod(name):
        m = types.ModuleType(name); monkeypatch.setitem(sys.modules, name, m); return m
    mod("homeassistant"); comps = mod("homeassistant.components"); conv = mod("homeassistant.components.conversation"); conv.DOMAIN = "conversation"
    comps.conversation = conv
    hacomp = mod("homeassistant.components.homeassistant"); exp = mod("homeassistant.components.homeassistant.exposed_entities")
    exp.async_should_expose = lambda hass, assistant, eid: eid in exposed
    hacomp.exposed_entities = exp; comps.homeassistant = hacomp
    helpers = mod("homeassistant.helpers")
    er_m, dr_m, ar_m = mod("homeassistant.helpers.entity_registry"), mod("homeassistant.helpers.device_registry"), mod("homeassistant.helpers.area_registry")
    helpers.entity_registry, helpers.device_registry, helpers.area_registry = er_m, dr_m, ar_m
    ns = types.SimpleNamespace
    er_m.async_get = lambda hass: ns(async_get=lambda eid: entity_regs.get(eid))
    dr_m.async_get = lambda hass: ns(async_get=lambda did: ns(area_id=(device_areas or {}).get(did)))
    ar_m.async_get = lambda hass: ns(async_list_areas=lambda: areas)
    return ns(states=ns(async_all=lambda: states))


def st(eid, name): return types.SimpleNamespace(entity_id=eid, name=name)


def test_aliases_containing_the_computed_name_marker_do_not_break_the_snapshot(monkeypatch):
    R = types.SimpleNamespace
    hass = install(monkeypatch,
        entity_regs={"switch.fan": R(aliases={"exhaust", ComputedNameType()}, area_id="pool", device_id=None)},
        states=[st("switch.fan", "Pool Fan")], exposed={"switch.fan"},
        areas=[R(id="pool", name="Pool", aliases={"swim", ComputedNameType()})])
    snap = hs.build_entity_snapshot(hass)
    assert snap is not None
    assert snap["entities"] == [{"entity_id": "switch.fan", "name": "Pool Fan", "aliases": ["exhaust"], "area_id": "pool"}]
    assert snap["areas"] == [{"area_id": "pool", "name": "Pool", "aliases": ["swim"]}]


def test_one_bad_entity_is_skipped_not_fatal(monkeypatch):
    R = types.SimpleNamespace

    class Boom:
        @property
        def aliases(self): raise RuntimeError("weird registry entry")

    hass = install(monkeypatch,
        entity_regs={"light.ok": R(aliases=set(), area_id=None, device_id=None), "light.bad": Boom()},
        states=[st("light.ok", "OK"), st("light.bad", "Bad")], exposed={"light.ok", "light.bad"}, areas=[])
    snap = hs.build_entity_snapshot(hass)
    assert [e["entity_id"] for e in snap["entities"]] == ["light.ok"]


def test_returns_none_when_ha_is_unusable(monkeypatch):
    for n in [n for n in sys.modules if n == "homeassistant" or n.startswith("homeassistant.")]:
        monkeypatch.delitem(sys.modules, n, raising=False)
    monkeypatch.setitem(sys.modules, "homeassistant", None)
    assert hs.build_entity_snapshot(object()) is None


def test_runtime_files_are_forgiving(tmp_path):
    lex = tmp_path / "lex.json"
    assert hs.read_runtime_files(str(lex), str(tmp_path / "flag")) == ({}, False)          # nothing exists
    lex.write_text('{"light.a": ["den lamp"]}')
    assert hs.read_runtime_files(str(lex), str(tmp_path / "flag"))[0] == {"light.a": ["den lamp"]}
    (tmp_path / "flag").touch()
    assert hs.read_runtime_files(str(lex), str(tmp_path / "flag"))[1] is True
    lex.write_text("not json{{")
    assert hs.read_runtime_files(str(lex), str(tmp_path / "flag"))[0] == {}                # unreadable -> ignored
    lex.write_text("[1,2]")
    assert hs.read_runtime_files(str(lex), str(tmp_path / "flag"))[0] == {}                # wrong shape -> ignored
