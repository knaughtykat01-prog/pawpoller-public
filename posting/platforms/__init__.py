"""Platform poster implementations — one module per upload target.

Each module provides a PlatformPoster subclass that wraps the corresponding
PawPoller API client (e.g. InkbunnyClient, FAClient) and adds upload, edit,
and file-replace methods.

Supported platforms:
    inkbunny       Official API (api_upload.php + api_editsubmission.php)
    furaffinity    HTML form scraping (3-step submit flow), desktop-only
    weasyl         CSRF form submit + API key auth
    sofurry        REST JSON API with CSRF token auth
    squidgeworld   OTW Archive Rails form (same software as AO3)
    bluesky        AT Protocol createRecord + uploadBlob (announcements only)
    deviantart     Eclipse API (artwork)
    itaku          DRF token (artwork + text posts)
    e621           Official REST API upload (POST /uploads.json, artwork)
"""
