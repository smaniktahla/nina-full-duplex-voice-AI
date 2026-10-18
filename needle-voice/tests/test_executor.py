import asyncio, types
import pytest
from needle_voice import executor as ex
from needle_voice.executor import ExecutionError, PlanError, ValidatedIntent as VI

SNAP = {
    "entities": [
        {"entity_id": "light.kitchen_a", "name": "Kitchen A", "aliases": [], "area_id": "kitchen"},
        {"entity_id": "light.kitchen_b", "name": "Kitchen B", "aliases": [], "area_id": "kitchen"},
        {"entity_id": "switch.kitchen_fan", "name": "Kitchen Fan", "aliases": [], "area_id": "kitchen"},
        {"entity_id": "light.den_lamp", "name": "Den Lamp", "aliases": [], "area_id": "den"},
        {"entity_id": "media_player.plex", "name": "Plex", "aliases": [], "area_id": None},
        {"entity_id": "climate.up", "name": "Upstairs Thermostat", "aliases": [], "area_id": None},
        {"entity_id": "lock.front", "name": "Front Door", "aliases": [], "area_id": None},
        {"entity_id": "binary_sensor.garage_door", "name": "Garage Door", "aliases": [], "area_id": None},
        {"entity_id": "sensor.pool_temp", "name": "Pool Temp", "aliases": [], "area_id": None},
        {"entity_id": "weather.home", "name": "Forecast Home", "aliases": [], "area_id": None},
    ],
    "areas": [{"area_id": "kitchen", "name": "Kitchen", "aliases": []}, {"area_id": "den", "name": "Den", "aliases": []},
              {"area_id": "empty", "name": "Empty", "aliases": []}],
}


class St:
    def __init__(self, eid, state, **attrs):
        self.entity_id, self.state, self.attributes, self.name = eid, state, attrs, attrs.get("friendly_name", eid)


class FakeHass:
    """Targets of service calls are 'available' unless listed in `unavailable`; reads use `states`."""
    def __init__(self, states=None, forecast=None, fail_on=None, unavailable=()):
        self.calls, self._states, self._forecast, self._fail = [], states or {}, forecast, fail_on
        self._unavail = set(unavailable)
        self.states = types.SimpleNamespace(get=self._get)
        self.services = types.SimpleNamespace(async_call=self._call)

    def _get(self, eid):
        if eid in self._unavail:
            return St(eid, "unavailable")
        return self._states.get(eid) or St(eid, "off")

    async def _call(self, domain, service, data, blocking=False, return_response=False, context=None):
        if self._fail == (domain, service):
            raise RuntimeError("boom")
        self.calls.append((domain, service, data))
        if return_response:
            if self._forecast is None:
                raise RuntimeError("no forecast")
            return self._forecast


def P(intent, target, args=None, allow=False):
    return ex.plan(VI(intent, target, args or {}, "qwen_fast"), SNAP, allow_sensitive=allow)


def run(hass, plan):
    return asyncio.run(ex.execute(hass, plan))


# ---------------- planning ----------------
def test_area_expands_to_that_areas_exposed_lights_only():
    p = P("turn_off", "area:kitchen")
    assert p.calls[0].domain == "light" and p.calls[0].service == "turn_off"
    assert p.calls[0].data == {"entity_id": ["light.kitchen_a", "light.kitchen_b"]}   # the kitchen FAN switch is not touched


def test_entity_command_maps_to_fixed_service():
    p = P("turn_on", "switch.kitchen_fan")
    assert (p.calls[0].domain, p.calls[0].service, p.calls[0].data) == ("switch", "turn_on", {"entity_id": "switch.kitchen_fan"})


def test_brightness_and_temperature_data():
    assert P("set_brightness", "light.den_lamp", {"brightness": 30}).calls[0].data == {"entity_id": "light.den_lamp", "brightness_pct": 30}
    assert P("set_temperature", "climate.up", {"temperature": 70.0}).calls[0].data == {"entity_id": "climate.up", "temperature": 70.0}
    assert P("set_brightness", "area:den", {"brightness": 10}).calls[0].data["brightness_pct"] == 10


def test_refusals_happen_before_any_action():
    for intent, target in [("turn_off", "area:empty"), ("turn_off", "area:nope"), ("turn_on", "light.not_exposed"),
                           ("turn_on", "sensor.pool_temp"), ("media_pause", "light.den_lamp"), ("lock", "area:kitchen"),
                           ("get_state", "media_player.nope")]:
        with pytest.raises(PlanError):
            P(intent, target, {"brightness": 1} if intent == "set_brightness" else {})


def test_sensitive_intents_blocked_unless_allowed():
    with pytest.raises(PlanError):
        P("unlock", "lock.front")
    assert P("unlock", "lock.front", allow=True).calls[0].service == "unlock"
    assert P("lock", "lock.front").calls[0].service == "lock"


def test_no_snapshot_refuses():
    with pytest.raises(PlanError):
        ex.plan(VI("turn_on", "light.x", {}, "qwen_fast"), None, allow_sensitive=False)


# ---------------- execution + speech ----------------
def test_execute_calls_service_blocking_and_speaks():
    h = FakeHass()
    speech = run(h, P("turn_off", "area:kitchen"))
    assert h.calls == [("light", "turn_off", {"entity_id": ["light.kitchen_a", "light.kitchen_b"]})]
    assert speech == "Turned off the Kitchen lights."
    assert run(FakeHass(), P("set_brightness", "light.den_lamp", {"brightness": 30})) == "Set the Den Lamp to 30 percent."
    assert run(FakeHass(), P("media_pause", "media_player.plex")) == "Paused the Plex."


def test_service_failure_becomes_execution_error():
    with pytest.raises(ExecutionError):
        run(FakeHass(fail_on=("light", "turn_off")), P("turn_off", "area:kitchen"))


def test_read_state_speech():
    h = FakeHass({"sensor.pool_temp": St("sensor.pool_temp", "65.12", unit_of_measurement="°F"),
                  "binary_sensor.garage_door": St("binary_sensor.garage_door", "on", device_class="garage_door"),
                  "lock.front": St("lock.front", "locked")})
    assert run(h, P("get_state", "sensor.pool_temp")) == "The Pool Temp is 65.1 °F."
    assert run(h, P("get_state", "binary_sensor.garage_door")) == "The Garage Door is open."
    assert run(h, P("get_state", "lock.front")) == "The Front Door is locked."
    assert h.calls == []                                            # reads never call a service


def test_read_of_unavailable_state_escalates_via_execution_error():
    h = FakeHass()
    h.states = types.SimpleNamespace(get=lambda eid: None)                      # entity has no state at all
    with pytest.raises(ExecutionError):
        run(h, P("get_state", "sensor.pool_temp"))
    with pytest.raises(ExecutionError):
        run(FakeHass(unavailable={"sensor.pool_temp"}), P("get_state", "sensor.pool_temp"))


def test_climate_and_weather_temperature():
    h = FakeHass({"climate.up": St("climate.up", "heat", current_temperature=68.5),
                  "weather.home": St("weather.home", "cloudy", temperature=60)})
    assert run(h, P("get_temperature", "climate.up")) == "The Upstairs Thermostat is at 68.5 degrees."
    assert run(h, P("get_temperature", "weather.home")) == "It's 60 degrees outside."


def test_weather_with_forecast():
    fc = {"weather.home": {"forecast": [{"condition": "partlycloudy", "temperature": 72, "templow": 55.0, "precipitation_probability": 20}]}}
    h = FakeHass({"weather.home": St("weather.home", "cloudy", temperature=60, temperature_unit="°F")}, forecast=fc)
    out = run(h, P("get_weather", "weather.home"))
    assert out == "Right now it's 60 degrees and cloudy. Today: partly cloudy, with a high of 72 and a low of 55, 20 percent chance of precipitation."
    assert h.calls == [("weather", "get_forecasts", {"entity_id": "weather.home", "type": "daily"})]


def test_weather_falls_back_to_current_conditions_if_forecast_fails():
    h = FakeHass({"weather.home": St("weather.home", "cloudy", temperature=60)}, forecast=None)
    assert run(h, P("get_weather", "weather.home")) == "Right now it's 60 degrees and cloudy."


# ---------------- never claim success for a device that can't respond ----------------
def test_unavailable_target_is_refused_before_any_service_call():
    h = FakeHass(unavailable={"light.den_lamp"})
    with pytest.raises(ExecutionError) as e:
        run(h, P("turn_on", "light.den_lamp"))
    assert "target_unavailable" in str(e.value) and h.calls == []


def test_area_with_some_unavailable_lights_acts_on_the_rest_and_says_so():
    h = FakeHass(unavailable={"light.kitchen_b"})
    speech = run(h, P("turn_off", "area:kitchen"))
    assert h.calls == [("light", "turn_off", {"entity_id": ["light.kitchen_a"]})]
    assert speech == "Turned off the Kitchen lights. 1 of them was unavailable."


def test_area_with_every_light_unavailable_is_refused():
    h = FakeHass(unavailable={"light.kitchen_a", "light.kitchen_b"})
    with pytest.raises(ExecutionError):
        run(h, P("turn_off", "area:kitchen"))
    assert h.calls == []


def test_missing_state_counts_as_unavailable():
    h = FakeHass()
    h._get = lambda eid: None
    h.states = types.SimpleNamespace(get=h._get)
    with pytest.raises(ExecutionError):
        run(h, P("turn_on", "light.den_lamp"))


# ---------------- display actions (the kiosk) ----------------
def test_display_actions_use_a_fixed_table_and_matching_entity_domains():
    h = FakeHass()
    assert asyncio.run(ex.execute_display(h, "show", "script.kiosk_route_show")) is True
    assert asyncio.run(ex.execute_display(h, "close", "script.kiosk_route_close")) is True
    assert asyncio.run(ex.execute_display(h, "pause", "input_boolean.rot_paused")) and asyncio.run(ex.execute_display(h, "resume", "input_boolean.rot_paused"))
    assert h.calls == [("script", "turn_on", {"entity_id": "script.kiosk_route_show"}), ("script", "turn_on", {"entity_id": "script.kiosk_route_close"}),
                       ("input_boolean", "turn_on", {"entity_id": "input_boolean.rot_paused"}), ("input_boolean", "turn_off", {"entity_id": "input_boolean.rot_paused"})]


@pytest.mark.parametrize("action,entity", [("unlock", "lock.front"), ("show", "lock.front"), ("show", "light.kitchen"), ("pause", "script.x"), ("show", "script.a.b"),
                                           ("show", ""), ("show", None), ("turn_on", "script.x")])
def test_display_actions_refuse_anything_else_before_calling(action, entity):
    h = FakeHass()
    with pytest.raises(ExecutionError):
        asyncio.run(ex.execute_display(h, action, entity))
    assert h.calls == []


def test_display_call_failure_is_an_execution_error():
    with pytest.raises(ExecutionError):
        asyncio.run(ex.execute_display(FakeHass(fail_on=("script", "turn_on")), "show", "script.kiosk_route_show"))


# ---------------- navigation push to the owner's phone ----------------
WEB = "https://www.google.com/maps/dir/?api=1&travelmode=driving&destination=X"
APP = "google.navigation:q=X"


def test_notify_has_two_ways_to_open_and_only_goes_to_a_mobile_app_service():
    h = FakeHass()
    assert asyncio.run(ex.execute_notify(h, "mobile_app_test_phone", "Navigate", "Tap to start", APP, WEB)) is True
    d, svc, data = h.calls[0]
    assert (d, svc) == ("notify", "mobile_app_test_phone") and data["title"] == "Navigate"
    assert data["data"]["clickAction"] == APP                                                           # tap: Google Maps app on Android
    assert data["data"]["actions"] == [{"action": "URI", "title": "Open in browser", "uri": WEB}]       # button: the web link


@pytest.mark.parametrize("service,app,web", [
    ("mobile_app_phone", APP, "https://evil.example/x"), ("mobile_app_phone", "https://evil.example/x", WEB), ("mobile_app_phone", WEB, WEB),
    ("mobile_app_phone", APP, APP), ("mobile_app_phone", APP, None), ("mobile_app_phone", None, WEB), ("mobile_app_phone", "intent://x#Intent;end", WEB),
    ("persistent_notification", APP, WEB), ("notify.mobile_app_phone", APP, WEB), ("mobile_app_", APP, WEB), ("", APP, WEB), (None, APP, WEB), ("mobile_app_Phone", APP, WEB)])
def test_notify_refuses_anything_else_before_calling(service, app, web):
    h = FakeHass()
    with pytest.raises(ExecutionError):
        asyncio.run(ex.execute_notify(h, service, "t", "m", app, web))
    assert h.calls == []


def test_notify_failure_is_an_execution_error():
    with pytest.raises(ExecutionError):
        asyncio.run(ex.execute_notify(FakeHass(fail_on=("notify", "mobile_app_p")), "mobile_app_p", "t", "m", APP, WEB))
