"""Consent records: when, and to which wording (LEGALPAGES, 4.58.0).

The privacy policy says PawPoller records when someone said yes and to which wording, and asks again
when the wording changes. Before, check-ins and error reports were bare yes/no settings and the
Instagram relay was on by default with no question.
"""
from __future__ import annotations

import config
import consent_records as cr
import techcentre


def test_an_answer_is_recorded_with_time_and_wording():
    rec = cr.record("tech_usage", True)
    assert rec["value"] is True and rec["at"].endswith("+00:00") and rec["wording"] == cr.WORDING["tech_usage"]
    assert cr.valid("tech_usage") is True


def test_a_wording_change_asks_again():
    cr.record("tech_reports", True)
    s = config.get_settings()
    s["consent_records"]["tech_reports"]["wording"] = cr.WORDING["tech_reports"] - 1
    assert cr.valid("tech_reports", s) is None
    assert "tech_reports" in cr.needs_reconfirm(s)


def test_a_no_is_never_re_asked():
    assert cr.needs_reconfirm({"tech_usage": False, "tech_reports": "false", "ig_relay_enabled": False}) == []


def test_the_tech_centre_switches_record_their_answer():
    techcentre.set_usage_consent(True)
    techcentre.set_consent(False)
    assert techcentre.usage_consent() is True and techcentre.consent() is False
    recs = config.get_settings()["consent_records"]
    assert recs["tech_usage"]["value"] is True and recs["tech_reports"]["value"] is False


def test_the_bell_asks_once_for_a_yes_from_before_records():
    from routes import api as api_routes
    config.save_settings({"tech_usage": True})          # an old yes, no record
    items = [i for i in api_routes.get_notifications(40)["items"] if i.get("kind") == "consent"]
    assert len(items) == 1
    assert "anonymous check-ins" in items[0]["summary"] and "Settings → Diagnostics" in items[0]["summary"]
    assert items[0]["timestamp"] == ""                  # a reminder, never re-toasted as new
    techcentre.set_usage_consent(True)                  # answered: the reminder goes
    assert not [i for i in api_routes.get_notifications(40)["items"] if i.get("kind") == "consent"]
