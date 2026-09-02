"""The floating-logs-button preference (`logs_panel_enabled`) must round-trip
through the settings allowlist in routes/api.py.

The preferences endpoint only persists explicitly-whitelisted keys, so a dropped
or reordered allowlist entry silently stops the Settings toggle from saving.
These guard both the default and the save/read round-trip.
"""

from routes.api import get_preferences, save_preferences


def test_logs_panel_enabled_defaults_true():
    assert get_preferences()["logs_panel_enabled"] is True


def test_logs_panel_enabled_round_trips():
    save_preferences({"logs_panel_enabled": False})
    assert get_preferences()["logs_panel_enabled"] is False
    save_preferences({"logs_panel_enabled": True})
    assert get_preferences()["logs_panel_enabled"] is True


def test_logs_panel_enabled_coerced_to_bool():
    save_preferences({"logs_panel_enabled": 0})
    assert get_preferences()["logs_panel_enabled"] is False
