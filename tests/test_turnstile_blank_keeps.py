"""SEC069 (2): a blank Turnstile key keeps the saved one; only "Turn off" turns login protection off.

The secret box is never filled in on load, so a blank used to mean "cleared": changing only the
site key silently switched the protection off.
"""
import config
from routes.dashboard_auth import save_turnstile_config


def _keys():
    s = config.get_settings()
    return s.get("turnstile_site_key"), s.get("turnstile_secret_key")


def test_blank_keeps_and_off_clears():
    save_turnstile_config({"site_key": "0xSITE", "secret_key": "0xSECRET"})
    assert _keys() == ("0xSITE", "0xSECRET")

    out = save_turnstile_config({"site_key": "0xNEWSITE", "secret_key": ""})   # secret box left blank
    assert _keys() == ("0xNEWSITE", "0xSECRET") and "enabled" in out["message"]

    save_turnstile_config({"site_key": "", "secret_key": "   "})
    assert _keys() == ("0xNEWSITE", "0xSECRET")

    out = save_turnstile_config({"off": True, "site_key": "0xIGNORED"})
    assert not any(_keys()) and "disabled" in out["message"]


def test_off_must_be_true_itself():
    save_turnstile_config({"site_key": "0xSITE", "secret_key": "0xSECRET"})
    save_turnstile_config({"off": "yes"})
    assert _keys() == ("0xSITE", "0xSECRET")
