import ast, os
from needle_voice import intents

PKG = os.path.join(os.path.dirname(os.path.dirname(__file__)), "needle_voice")
ADAPTER_DIR = os.path.expanduser("~/needle-adapter")


def src(name):
    return open(os.path.join(PKG, name)).read()


def test_only_the_executor_can_change_the_house():
    for name in os.listdir(PKG):
        if not name.endswith(".py") or name == "executor.py":
            continue
        s = src(name)
        for token in ("async_call", "hass.services", "call_service"):
            assert token not in s, f"{name} contains {token!r}; only executor.py may reach HA services"


def test_pure_logic_modules_import_only_stdlib_and_siblings():
    allowed = {"__future__", "json", "os", "re", "time", "asyncio", "dataclasses", "typing", "urllib", "uuid", "datetime"}
    for name in ("guards.py", "candidates.py", "qwen.py", "intents.py", "executor.py", "router.py", "telemetry.py"):
        for node in ast.walk(ast.parse(src(name))):
            if isinstance(node, ast.Import):
                for a in node.names:
                    assert a.name.split(".")[0] in allowed, (name, a.name)
            elif isinstance(node, ast.ImportFrom):
                assert node.level >= 1 or (node.module or "").split(".")[0] in allowed, (name, node.module)


def test_no_homeassistant_import_outside_the_glue():
    glue = {"conversation.py", "config_flow.py", "ha_snapshot.py", "__init__.py"}
    for name in os.listdir(PKG):
        if name.endswith(".py") and name not in glue:
            assert "homeassistant" not in src(name), name


def test_qwen_never_names_a_service_or_free_form_data():
    s = src("qwen.py")
    assert "services" not in s.replace("spec.get(\"services\"", "")  # only reads the fixed intent table
    assert '"service"' not in s


def test_intent_vocabulary_matches_the_spec_plus_weather():
    spec = {"turn_on", "turn_off", "toggle", "set_brightness", "set_temperature", "lock", "unlock", "open", "close",
            "get_state", "get_temperature", "run_script", "media_play", "media_pause", "media_stop"}
    assert set(intents.INTENTS) == spec | {"get_weather"}


def test_dangerous_direction_is_flagged_sensitive():
    assert {k for k, v in intents.INTENTS.items() if v.get("sensitive")} == {"unlock", "open"}


def test_security_and_camera_domains_are_never_targetable():
    for d in ("alarm_control_panel", "camera", "vacuum", "siren", "button", "automation"):
        assert d not in intents.ALL_TARGET_DOMAINS


def test_vendored_modules_match_the_reviewed_sources():
    for name in ("needle_adapter.py", "policy_engine.py", "entity_resolver.py"):
        assert src(name) == open(os.path.join(ADAPTER_DIR, name)).read(), f"{name} drifted from ~/needle-adapter/"
