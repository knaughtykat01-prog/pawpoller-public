"""The desktop window must keep its cookie jar across restarts (4.31.1).

pywebview defaults to private_mode=True, which discards all cookies and HTML5
storage when the process exits. The dashboard issues a 30-day "remember me"
session cookie, but in private mode the embedded browser threw it away on every
restart — and because the desktop app lives in the tray, the only time it
restarts is an update. So each update looked like it logged the operator out.

The fix starts pywebview with private_mode=False and a storage_path under the
persistent per-user app-data dir (%APPDATA%\\PawPoller on Windows), which survives
app updates.
"""
import config
import main


def test_webview_start_persists_cookies_under_appdata():
    kw = main._webview_start_kwargs()
    # Not private → the embedded browser keeps cookies + HTML5 storage between runs.
    assert kw["private_mode"] is False
    # Stored under the persistent per-user app-data dir (survives updates), never a
    # temp/unpack dir that a new install would replace.
    assert kw["storage_path"] == str(config.APPDATA_DIR / "webview")
