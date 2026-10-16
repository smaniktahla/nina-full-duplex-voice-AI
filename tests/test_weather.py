"""Weather forecast composition and fallback (no network: HA is faked)."""
import asyncio, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "live"))
import tools                                                                   # noqa: E402

TWICE = [
    {"datetime": "2026-10-15T20:00:00-04:00", "condition": "rainy", "temperature": 59, "is_daytime": False, "precipitation_probability": 43},
    {"datetime": "2026-10-16T06:00:00-04:00", "condition": "rainy", "temperature": 63, "is_daytime": True, "precipitation_probability": 60},
    {"datetime": "2026-10-16T18:00:00-04:00", "condition": "partlycloudy", "temperature": 48, "is_daytime": False, "precipitation_probability": 5},
]


def test_compose_forecast():
    t = tools.compose_forecast(TWICE, "2026-10-16")
    assert t == "rain then partly cloudy, high 63, low 48, 60 percent chance of precipitation"
    assert tools.compose_forecast(TWICE, "2026-10-17") is None
    assert tools.compose_forecast([{"datetime": "2026-10-16T06:00:00-04:00", "condition": "sunny", "temperature": 70, "is_daytime": True,
                                    "precipitation_probability": 0}], "2026-10-16") == "sunny, high 70"


class FakeHA:
    """Stands in for the aiohttp session + the module's HA helpers."""
    def __init__(self, states, service=None):
        self.states, self.service = states, service


def _patch(states, service):
    async def st(http, e): return states.get(e, {})
    async def ids(http): return list(states)
    async def svc(http, w, date): return tools.compose_forecast(service, date) if service else None
    tools._state, tools._states_list, tools._forecast_service = st, ids, svc


def run(coro): return asyncio.run(coro)


def test_day_forecast_prefers_configured_then_siblings_then_service():
    states = {"sensor.nws_tomorrow_forecast_2": {"state": "unknown"}, "sensor.nws_tomorrow_forecast": {"state": "Chance Rain Showers"}}
    _patch(states, TWICE)
    prim = states["sensor.nws_tomorrow_forecast_2"]
    assert run(tools._day_forecast(None, "tomorrow", {"state": "Cloudy all day"}, "weather.x", "sensor.nws_tomorrow_forecast_2")) == "Cloudy all day"
    assert run(tools._day_forecast(None, "tomorrow", prim, "weather.x", "sensor.nws_tomorrow_forecast_2")) == "Chance Rain Showers"   # sibling sensor
    _patch({"sensor.nws_tomorrow_forecast_2": {"state": "unavailable"}}, [dict(e, datetime=e["datetime"].replace("2026-10-16", __import__("datetime").date.today().isoformat())) for e in TWICE[1:]] or None)
    out = run(tools._day_forecast(None, "tomorrow", {"state": "unknown"}, "weather.x", "sensor.nws_tomorrow_forecast_2"))
    assert out.startswith(("no forecast", "rain"))                                                      # service path or the honest fallback
    _patch({}, None)
    assert run(tools._day_forecast(None, "tomorrow", {}, "weather.x", "sensor.t")) == "no forecast available right now"
