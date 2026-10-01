"""Data classification: what PawPoller keeps, how sensitive it is, and how each kind is handled.

Spec 011 (the ISO 27001 practices plan, §2). ONE list that every protection reads, so that
protecting a new secret is one edit here and nowhere else:

* ``config.CREDENTIAL_FIELDS`` (what goes in the encrypted vault) is ``vault_fields()``;
* the log redactor's "keep this legible" set is ``identity_keys()``;
* the Tech Centre scrubber masks ``identity_keys()`` values and other people's names from
  ``name_columns()``;
* ``deploy/make_public.py`` refuses any file ``public_copy_refused()`` names;
* Settings → Privacy is ``holdings()``.

The tests (``tests/test_data_classification.py``) fail when a table, a setting the code
reads, or a file in the data area is not in this list — so the practice can't quietly lapse.

Four classes, highest first:

  restricted    would let someone act as you, lose you an account, or link your personas
  confidential  other people's information, or your unpublished work
  internal      yours and low-harm, but not meant to be published
  public        already published, or meant to be

Anything that CONTAINS several classes (the database file, a backup, a log) takes the highest.

This module is stdlib only and imports nothing from the app, so config, the log redactor,
make_public and the tests can all import it. It ships in the public copy: it names kinds of
data, never an account, a handle or a person.
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import NamedTuple

# ── Classes and how each is handled ──────────────────────────────────────────

CLASSES = (
    # (cls, rank, label, meaning — plain words, shown on the Privacy page)
    ("restricted", 3, "Restricted", "Would let someone act as you, lose you an account, or link your personas together."),
    ("confidential", 2, "Confidential", "Other people's information, or your unpublished work."),
    ("internal", 1, "Internal", "Yours and low-harm, but not meant to be published."),
    ("public", 0, "Public", "Already published, or meant to be."),
)
_RANK = {c[0]: c[1] for c in CLASSES}

SITUATIONS = (
    ("at_rest", "Where it's kept"),
    ("in_logs", "In the log"),
    ("public_copy", "In the public source code"),
    ("leaves_machine", "Leaving this computer"),
    ("backups", "In backups"),
    ("retention", "How long it's kept"),
    ("who_reads", "Who can read it"),
)

HANDLING = {
    "restricted": {
        "at_rest": "Passwords, tokens and keys: the encrypted vault. Your handles: the settings file, readable only by you. "
                   "Links between accounts and personas: the database file, readable only by you.",
        "in_logs": "Never — masked before a line is written. Your own handles and addresses stay readable in your own log.",
        "public_copy": "Never. Building the public copy fails if one is found.",
        "leaves_machine": "Only to the site it belongs to, or to your own paired install.",
        "backups": "Yes, and a backup that holds any of it is itself Restricted.",
        "retention": "Until you remove the account or setting.",
        "who_reads": "You, signed in. Locked when the dashboard has no password.",
    },
    "confidential": {
        "at_rest": "The database or data folder, readable only by you.",
        "in_logs": "Not written on purpose.",
        "public_copy": "Never. Building the public copy fails if one is found.",
        "leaves_machine": "Only to the site it came from or is posted to, or to your own paired install. "
                          "Never in error reports: names are replaced before a report is made.",
        "backups": "Yes.",
        "retention": "Until you remove it.",
        "who_reads": "You, signed in.",
    },
    "internal": {
        "at_rest": "The database, data folder or log, readable only by you.",
        "in_logs": "Allowed.",
        "public_copy": "Code only, never data.",
        "leaves_machine": "Only as counts, and only in the check-in you switched on.",
        "backups": "Yes, unless it can be rebuilt.",
        "retention": "Old history is thinned by the regular pruning; the rest until you remove it.",
        "who_reads": "You, signed in.",
    },
    "public": {
        "at_rest": "The database or data folder.",
        "in_logs": "Allowed.",
        "public_copy": "Code only, never data.",
        "leaves_machine": "Allowed — it's already public.",
        "backups": "Yes.",
        "retention": "Until you remove it.",
        "who_reads": "Anyone, once published.",
    },
}

# ── Groups: what the Privacy page shows, and each group's class ──────────────
# (group, cls, plain description, where it may go, how to remove it)

GROUPS = {g[0]: g for g in (
    ("Sign-ins and keys", "restricted",
     "Passwords, cookies, tokens and API keys for the sites you connect.",
     "Only the site each one belongs to, or your own paired install.",
     "Settings → Platforms → disconnect the site."),
    ("This install's own locks", "restricted",
     "The dashboard password, two-factor secret, session secret and the API keys you made.",
     "Nowhere.",
     "Settings → Security."),
    ("Delivery addresses", "restricted",
     "Where announcements and digests are sent: webhook addresses, bot tokens, chat ids, the mail server login.",
     "Only the service each one belongs to.",
     "Settings → Notifications or Telegram → clear the field."),
    ("Your handles", "restricted",
     "Your usernames, ids and addresses on each site. One at a time they're public; listed together they show "
     "which accounts are the same person.",
     "Only the site each one belongs to, or your own paired install. Replaced before any error report.",
     "Settings → Platforms → disconnect the site."),
    ("Which account is which persona", "restricted",
     "Your accounts and personas, and which account belongs to which persona.",
     "Only your own paired install.",
     "Settings → Platforms (accounts) and the Personas page."),
    ("Links you share", "restricted",
     "Private reading links for stories. Anyone holding one can read that story.",
     "Only to whoever you give the link to.",
     "The story's Share panel → switch the link off."),
    ("The database and its copies", "restricted",
     "The database file and the copies made before each update. It holds everything above and below.",
     "Only your own paired install, or a backup you download.",
     "Removing the data inside it; old update copies are thinned to the newest 5."),
    ("People you work with", "confidential",
     "Artists you credit, their handles, characters and their owners, commission clients, and people you mention.",
     "Only the site you post to, when you credit or mention them.",
     "The Artists, Characters and Commissions pages."),
    ("Your audience", "confidential",
     "Who watches, favourites, gives kudos to or comments on your work.",
     "Nowhere — read from each site, kept here.",
     "It follows the site: disconnecting a site stops new copies."),
    ("Your boards", "confidential",
     "Your Trello boards, cards and comments, mirrored here.",
     "Only back to Trello.",
     "Settings → Trello → disconnect."),
    ("Unpublished work", "confidential",
     "Drafts, stories, artwork, media and promo images not yet posted.",
     "Only the site you post to, when you post.",
     "Delete the draft or file where it lives. Artwork is only ever hidden, never deleted."),
    ("Error reports", "confidential",
     "Error reports waiting to go to the Tech Centre, already cleaned of logins, handles and names.",
     "The Tech Centre, only if you switched error reports on.",
     "Settings → Logs & diagnostics → switch error reports off."),
    ("Numbers over time", "internal",
     "Views, favourites, followers and goals, recorded each poll.",
     "Counts only, in the check-in you switched on.",
     "Old numbers are thinned by the regular pruning."),
    ("Polling and posting history", "internal",
     "When each site was polled, what was queued and posted, and what went wrong.",
     "Nowhere.",
     "Kept as your history."),
    ("How your library is organised", "internal",
     "Collections, groups, links between copies of the same work, tags and hidden items.",
     "Only your own paired install.",
     "The Library and Collections pages."),
    ("Preferences", "internal",
     "How the app looks and behaves: intervals, defaults, switches and reminders.",
     "Only your own paired install.",
     "Settings."),
    ("App housekeeping", "internal",
     "Small files the app keeps for itself: update requests, test results, the install's id.",
     "The install id goes in the check-in, only if you switched it on.",
     "Remade automatically."),
    ("Logs", "internal",
     "What the app did, with passwords and tokens masked.",
     "The last few lines go with an error report, cleaned first, only if you switched reports on.",
     "Rotated automatically."),
    ("Your published works", "public",
     "Titles, descriptions, tags and links of work that's already live.",
     "It's already public.",
     "Remove it on the site; hide it in the Library."),
    ("Your podcast", "public",
     "Your podcast feed and episodes, published as a public feed.",
     "The public feed.",
     "The Podcasts page."),
)}

# db = in pawpoller.db, so in every backup; files = in the full/nightly backup zip (routes/backup_api.py:
# settings.json, the vault, artwork/, posts_media/, story-archive/); derivable = rebuilt on its own.
BACKUP_ROUTES = ("db", "files", "derivable")   # or "gap:<BACKLOG-ID>"


class Entry(NamedTuple):
    kind: str             # "table" | "setting" | "path"
    name: str             # exact, or a template: <P> platform prefix; acct_<id>_<field>; trailing "/" = folder
    group: str            # a key of GROUPS — which sets the class
    why: str = ""         # one line; empty = the group's description stands for it
    identity: bool = False  # settings: your own handle/address — legible in YOUR log, scrubbed elsewhere
    vault: bool = False   # settings: kept in the encrypted vault (secrets always are; see vault_fields)
    names: tuple = ()     # tables: columns holding other people's names or handles
    backup: str = ""      # tables default "db"; settings default "files" (settings.json + vault are in the zip)
    obsolete: bool = False  # a leftover of a removed feature, classified so it's never "unknown"

    @property
    def cls(self) -> str:
        return GROUPS[self.group][1]

    @property
    def reason(self) -> str:
        return self.why or GROUPS[self.group][2]

    @property
    def backup_route(self) -> str:
        if self.backup:
            return self.backup
        return {"table": "db", "setting": "files"}.get(self.kind, "")


def T(name, group, why="", names=(), backup="", obsolete=False):
    return Entry("table", name, group, why, names=tuple(names), backup=backup, obsolete=obsolete)


def S(name, group, why="", identity=False, vault=None, backup="", obsolete=False):
    # Secrets live in the vault by definition; identity fields only where they already did.
    if vault is None:
        vault = GROUPS[group][1] == "restricted" and not identity
    return Entry("setting", name, group, why, identity=identity, vault=vault, backup=backup, obsolete=obsolete)


def P(name, group, why="", backup="", obsolete=False):
    return Entry("path", name, group, why, backup=backup, obsolete=obsolete)


# ── Platform prefixes: what <P> means ─────────────────────────────────────────
# Each owns <P>_submissions / <P>_snapshots / <P>_poll_log. Inkbunny, the first site,
# uses the bare names. A test checks this matches what init_db actually creates.
PLATFORM_PREFIXES = (
    "ao3", "bsky", "da", "e621", "fa", "fbr", "fn", "ig", "ik", "mast", "ng", "pix",
    "pic", "sc", "sf", "sqw", "tg", "thr", "tum", "tw", "wp", "ws", "yt",
)

# ── Tables ────────────────────────────────────────────────────────────────────

_TABLES = (
    # restricted
    T("accounts", "Which account is which persona", "Your account on each site, its handle, and the persona it belongs to."),
    T("personas", "Which account is which persona", "Your personas listed together — the list itself links them."),
    T("session_cache", "Sign-ins and keys", "A live Inkbunny session id.", backup="derivable"),
    T("share_tokens", "Links you share", "Private reading links; anyone holding one can read that story."),
    # confidential — people
    T("artists", "People you work with", "Artists you credit, and their aliases.", names=("name", "aliases")),
    T("artist_handles", "People you work with", "Artists' handles on each site.", names=("handle",)),
    T("characters", "People you work with", "Characters and who owns them."),
    T("commissions", "People you work with", "Commission clients and what they asked for.", names=("client_name",)),
    T("post_contacts", "People you work with", "People you mention in posts, and their handles.",
      names=("name", "handle_bsky", "handle_tw", "handle_mast", "handle_thr", "handle_tum")),
    T("post_mentions", "People you work with", "Who is mentioned in which post.", names=("token",)),
    # confidential — audience
    T("watchers", "Your audience", "Who watches you on Inkbunny.", names=("username",)),
    T("faving_users", "Your audience", "Who favourited your work.", names=("username",)),
    T("fa_watchers", "Your audience", "Who watches you on FurAffinity.", names=("username",)),
    T("sf_watchers", "Your audience", "Who watches you on SoFurry.", names=("username",)),
    T("ao3_kudos_users", "Your audience", "Who gave kudos on AO3.", names=("username",)),
    T("sqw_kudos_users", "Your audience", "Who gave kudos on SquidgeWorld.", names=("username",)),
    T("comments", "Your audience", "Comments on your Inkbunny work, and who wrote them.", names=("username",)),
    T("fa_comments", "Your audience", "Comments on your FurAffinity work, and who wrote them.", names=("username",)),
    T("platform_comments", "Your audience", "Comments from the other sites, and who wrote them.", names=("author",)),
    # confidential — boards
    T("trello_boards", "Your boards", "Your Trello boards."),
    T("trello_lists", "Your boards", "The lists on your boards."),
    T("trello_cards", "Your boards", "Your cards — often a client's request."),
    T("trello_labels", "Your boards", "Card labels."),
    T("trello_checklists", "Your boards", "Card checklists."),
    T("trello_check_items", "Your boards", "Checklist items."),
    T("trello_comments", "Your boards", "Comments on cards, and who wrote them.", names=("author_name",)),
    T("trello_covers", "Your boards", "Card cover images kept here.", backup="derivable"),
    T("trello_outbox", "Your boards", "Changes waiting to go back to Trello."),
    T("trello_mirror_conflicts", "Your boards", "Where Trello and PawPoller disagreed."),
    T("trello_links", "People you work with", "Which card belongs to which commission client.", names=("client_name",)),
    T("trello_conflicts", "People you work with", "Where a client's card and commission disagreed.", names=("client_name",)),
    # confidential — unpublished work
    T("posts", "Unpublished work", "Your posts, drafts included."),
    T("post_media", "Unpublished work", "Pictures attached to posts."),
    T("promos", "Unpublished work", "Promo images you've made."),
    # internal — numbers
    T("snapshots", "Numbers over time", "Inkbunny numbers per poll."),
    T("<P>_snapshots", "Numbers over time", "That site's numbers per poll."),
    T("fa_profile_stats", "Numbers over time", "FurAffinity profile views per poll."),
    T("pic_channel_snapshots", "Numbers over time", "Picarto channel views, followers and subscribers per poll."),
    T("account_follower_snapshots", "Numbers over time", "Follower counts per poll."),
    T("goals", "Numbers over time", "Milestones you set."),
    # internal — history
    T("poll_log", "Polling and posting history", "When Inkbunny was polled and what came back."),
    T("<P>_poll_log", "Polling and posting history", "When that site was polled and what came back."),
    T("posting_queue", "Polling and posting history", "What is waiting to be posted, and to which account."),
    T("posting_log", "Polling and posting history", "What was posted where, and what went wrong."),
    T("publications", "Polling and posting history", "Where each work is published."),
    T("post_publications", "Polling and posting history", "Where each post is published."),
    # internal — library
    T("collections", "How your library is organised", "Your collections."),
    T("collection_members", "How your library is organised", "What is in each collection."),
    T("masterpieces", "How your library is organised", "Works grouped across sites."),
    T("masterpiece_members", "How your library is organised", "Which copies belong to each work."),
    T("masterpiece_not_duplicate", "How your library is organised", "Pairs you said are different works."),
    T("masterpiece_not_variant", "How your library is organised", "Pairs you said are not versions of one piece."),
    T("submission_groups", "How your library is organised", "Your groups of submissions."),
    T("submission_group_members", "How your library is organised", "What is in each group."),
    T("submission_links", "How your library is organised", "Copies of the same work on different sites."),
    T("submission_link_members", "How your library is organised", "The members of each link."),
    T("submission_tags", "How your library is organised", "Your own tags on submissions."),
    T("tags", "How your library is organised", "Your own tags."),
    T("ignored_submissions", "How your library is organised", "Submissions you hid."),
    T("image_hashes", "How your library is organised", "Picture fingerprints used to spot copies.", backup="derivable"),
    T("inbox_state", "How your library is organised", "Which comments you've dealt with."),
    T("mirror_tombstones", "How your library is organised", "What was deleted, so a paired install deletes it too."),
    T("pp_meta", "App housekeeping", "One-time upgrade markers."),
    # public
    T("submissions", "Your published works", "Your Inkbunny submissions."),
    T("<P>_submissions", "Your published works", "Your submissions on that site."),
    T("podcast_feeds", "Your podcast", "Your podcast feeds."),
    T("podcast_episodes", "Your podcast", "Your podcast episodes."),
)

# ── Settings ──────────────────────────────────────────────────────────────────

_SECRET = "Sign-ins and keys"
_LOCKS = "This install's own locks"
_DELIVERY = "Delivery addresses"
_HANDLE = "Your handles"
_PREF = "Preferences"
_HOUSE = "App housekeeping"

_SETTINGS = (
    # Platform secrets — the vault. (These were config.CREDENTIAL_FIELDS until 4.44.0; the notes came along.)
    #  * SoFurry moved to an official-API token in 3.4.0; the old login fields stay listed so a value
    #    left in an existing vault is still treated as sensitive until the migration clears it.
    #  * Client ids, token expiries and handles are NOT secrets and stay in plain settings (e621, FN,
    #    SC, NG, YT, Furbooru usernames/ids); only the secret half of each pair is vaulted.
    #  * Trello: the key identifies the app but the token is bearer-equivalent, and Trello echoes query
    #    parameters in some error bodies — so both are secrets. The Secret (spec 006) signs webhook
    #    deliveries and is treated as a signing key.
    #  * tg_bot_token is the Posts module's OWN bot since 4.8.0, never the notification bot (that is
    #    how a digest once landed in a public channel).
    *(S(k, _SECRET) for k in (
        "password", "fa_cookie_a", "fa_cookie_b", "ws_api_key", "sf_api_token", "sf_password",
        "sf_session_cookies", "sqw_password", "sqw_author_password", "ao3_password", "ao3_session_cookie",
        "da_client_secret", "da_cookie", "da_refresh_token", "e621_api_key",
        "fbr_api_key", "fn_access_token", "fn_password", "fn_refresh_token", "ik_auth_token",
        "bsky_app_password", "tw_api_bearer_token", "tw_auth_token", "tw_ct0", "mast_access_token",
        "tum_api_key", "tum_consumer_secret", "tum_oauth_token", "tum_oauth_token_secret",
        "pix_refresh_token", "thr_access_token", "ig_access_token", "ng_cookie", "sc_access_token",
        "sc_client_secret", "sc_refresh_token", "yt_access_token", "yt_client_secret", "yt_refresh_token",
        "trello_api_key", "trello_secret", "trello_token", "github_pat", "cf_worker_key",
        "posting_server_api_key",
    )),
    S("sf_totp_code", _SECRET, "An old SoFurry sign-in code, only ever deleted now.", obsolete=True),
    # This install's own locks — the vault.
    *(S(k, _LOCKS) for k in (
        "dashboard_password", "auth_password_hash", "auth_session_secret", "auth_totp_secret",
        "auth_totp_pending_secret", "auth_totp_backup_codes", "auth_totp_enabled", "auth_api_keys",
        "turnstile_secret_key", "turnstile_site_key",
    )),
    # Delivery addresses — the vault. discord_webhook_url joined in 4.44.0: the URL carries its token.
    *(S(k, _DELIVERY) for k in ("telegram_bot_token", "telegram_chat_id", "tg_bot_token", "smtp_password",
                                "discord_webhook_url")),
    # Your handles, already in the vault (kept there; legible in your own log as before).
    *(S(k, _HANDLE, identity=True, vault=True) for k in (
        "username", "sf_username", "sqw_username", "sqw_author_username", "ao3_username", "bsky_identifier",
        "dashboard_user", "cf_worker_url", "posting_server_url",
    )),
    # Your handles, in the settings file.
    *(S(k, _HANDLE, identity=True) for k in (
        "fa_username", "ws_username", "e621_username", "fbr_username", "fn_username", "ng_username",
        "sc_username", "yt_username", "pic_channel", "ig_username", "thr_username", "sf_display_name", "mast_handle",
        "mast_instance_url", "tum_blog", "ig_user_id", "pix_user_id", "thr_user_id", "tg_channel",
        "ao3_target_user", "da_target_user", "ik_target_user", "sqw_target_user", "tw_target_user",
        "wp_target_user", "auth_username", "default_author", "smtp_username", "smtp_from",
        "email_digest_recipients", "ig_public_base_url", "ig_relay_url", "<P>_own_handle",
    )),
    # Preferences.
    *(S(k, _PREF) for k in (
        "announce_defaults", "auto_backup_dir", "auto_backup_enabled", "auto_backup_interval_hours",
        "auto_backup_keep", "auto_sync_enabled", "auto_update", "credits", "credential_mode",
        "da_client_id", "sc_client_id", "yt_client_id", "dashboard_layout", "discord_announce_on_publish",
        "display_timezone", "email_digest_enabled", "email_digest_interval_days", "hidden_platforms",
        "logs_panel_enabled", "minimize_to_tray", "mirror_auto_check", "mirror_check_interval_minutes",
        "mobile_mode", "muted_session_codes", "never_post_account_ids", "notification_comments_only", "notification_min_faves_delta",
        "notification_min_views_delta", "notifications_enabled", "pawpoller_attribution",
        "pinned_submissions", "poll_interval_minutes", "polling_paused", "polling_paused_platforms",
        "setup_mode", "smtp_host", "smtp_port", "smtp_use_tls", "theme", "tours_seen", "trello",
        "update_skip_version", "watcher_notifications_enabled", "tech_reports", "tech_usage",
        "milestone_comments", "milestone_faves", "milestone_score", "milestone_views",
        "artwork_archive_path", "artwork_da_catpath", "artwork_default_platforms", "artwork_default_rating",
        "artwork_enabled", "artwork_fa_category", "artwork_fa_gender", "artwork_fa_species",
        "artwork_fa_theme", "artwork_sf_sub_type", "artwork_watermark_enabled", "artwork_watermark_opacity",
        "artwork_watermark_position", "artwork_watermark_text", "artwork_ws_subtype",
        "posting_default_platforms", "posting_default_rating", "posting_enabled", "posting_fa_category",
        "posting_fa_gender", "posting_fa_species", "posting_fa_theme", "posting_story_archive_path",
        "telegram_digest", "telegram_digest_interval_hours", "telegram_enabled", "telegram_error_alerts",
        "telegram_milestones", "telegram_poll_summaries", "telegram_weekly_digest",
        "tg_channel_digest_enabled", "tg_document", "tg_no_tags", "tg_protect", "tg_silent",
        "fa_direct_polling", "fa_notification_comments_only", "fa_watcher_notification_mode",
        "fa_watcher_notifications_enabled", "sf_notification_comments_only", "ws_notification_comments_only",
        "mast_instance_flavour", "ig_relay_open", "tw_account_stagger_seconds", "tw_gallerydl_path",
        "tw_polling_backend", "tw_roundrobin_batch", "tw_roundrobin_save_tokens", "yt_long_uploads",
        "ib_use_cf_proxy", "<P>_use_cf_proxy", "<P>_notifications_enabled", "<P>_poll_interval_minutes",
    )),
    S("pod_feed_slug", "Your podcast", "The address of your public podcast feed."),
    S("trello_instance_tag", _HOUSE, "Which device this is, so only one install talks to Trello."),
    S("_diagnostics_test_marker", _HOUSE, "Left behind by an older settings self-test; removed on start.",
      backup="derivable", obsolete=True),
    # Housekeeping — rebuilt or re-stamped on their own.
    *(S(k, _HOUSE, backup="derivable") for k in (
        "credential_set_at", "last_auto_backup_at", "last_digest_sent_at", "last_email_digest_sent_at",
        "last_poll_completed_at", "last_session_check_at", "last_snapshot_prune_at",
        "last_tg_channel_digest_sent_at", "last_weekly_digest_sent_at", "mirror_last_sync",
        "mirror_seeded_at", "notifications_cleared_at", "notifications_last_read_at", "setup_complete",
        "sc_token_expires_at", "yt_token_expires_at",
    )),
)

# ── The data area (paths relative to the app's data home; "/" = a folder and all inside) ──

_BACKUPGAPS = "gap:BACKUPGAPS"
_PATHS = (
    P("data/pawpoller.db", "The database and its copies", "The database: every table above.", backup="db"),
    P("data/pawpoller.db-wal", "The database and its copies", "The database's recent writes.", backup="db"),
    P("data/pawpoller.db-shm", "The database and its copies", "The database's shared index.", backup="derivable"),
    P("data/backups/", "The database and its copies", "Copies of the database made before each update.", backup="db"),
    P("data/inkbunny_analytics.db", "The database and its copies", "The database's old name, from early versions.",
      backup="derivable", obsolete=True),
    P("data/settings.json", _HANDLE, "Your settings, including your handles. Never passwords.", backup="files"),
    P("data/settings.vault.json", _SECRET, "The encrypted vault: every password, token and key.", backup="files"),
    # Kept out of backups ON PURPOSE: a backup holding the key beside the vault would undo the encryption.
    # 4.45.2: a vault the app couldn't read is set aside under this name, never overwritten.
    P("data/settings.vault.json.unreadable-<stamp>", _SECRET,
      "A vault that couldn't be unlocked, kept so no login is lost. Recoverable with the right key.",
      backup=_BACKUPGAPS),
    P("data/.vault_key", _SECRET, "The key that opens the vault, when the system keyring isn't used. "
      "Kept out of backups on purpose — keep your own copy.", backup=_BACKUPGAPS),
    P("data/auto-backups/", "The database and its copies",
      "Your nightly backups: the database, settings, the encrypted vault and a copy of your media.",
      backup="derivable"),
    P("data/analytics.db", "The database and its copies", "An empty file from early versions.",
      backup="derivable", obsolete=True),
    P("data/inkbunny.db", "The database and its copies", "An empty file from early versions.",
      backup="derivable", obsolete=True),
    P("data/data/", _HOUSE, "Leftover from an old self-test run that wrote into a nested folder.",
      backup="derivable", obsolete=True),
    P("data/logs/", "Logs", "Leftover from an old self-test run that wrote into a nested folder.",
      backup="derivable", obsolete=True),
    P("settings.json", _HANDLE, "Where settings lived in early versions; moved on start-up.", backup="derivable",
      obsolete=True),
    P("webview/", _SECRET, "The built-in browser's cookies, for sites you sign into inside the app.",
      backup="derivable"),
    P("data/tech_pending.json", "Error reports", "Error reports waiting to be sent.", backup="derivable"),
    P("data/tech_state.json", _HOUSE, "The install's id and the Tech Centre's last answers.", backup="derivable"),
    P("data/agent_queue.json", _HOUSE, "Jobs waiting for the desktop helper.", backup="derivable"),
    P("data/diagnostics_results.json", _HOUSE, "The last self-test results.", backup="derivable"),
    P("data/.update-request", _HOUSE, "An update asked for from the dashboard.", backup="derivable"),
    P("data/.update-agent-seen", _HOUSE, "When the update helper last checked in.", backup="derivable"),
    P("data/artwork/", "Unpublished work", "Your artwork files.", backup="files"),
    P("data/stories/", "Unpublished work", "Your story files.", backup="files"),
    P("story-archive/", "Unpublished work", "Your story files (desktop default location).", backup="files"),
    P("data/posts_media/", "Unpublished work", "Pictures attached to posts.", backup="files"),
    P("data/inbox/", "Unpublished work", "Media you uploaded but haven't used yet.", backup="files"),
    P("data/promos/", "Unpublished work", "Promo images and their backgrounds.", backup="files"),
    P("data/ig_pending/", "Unpublished work", "Pictures waiting to go to Instagram.", backup="derivable"),
    P("data/thumbs/", "Unpublished work", "Small copies of covers, remade when needed.", backup="derivable"),
    P("data/trello_covers/", "Your boards", "Card cover images, fetched again from Trello when needed.",
      backup="derivable"),
    P("data/commission_files/", "People you work with", "Files a commission client sent you.", backup="files"),
    P("helpers/", _HOUSE, "A small program downloaded for the Instagram picture link.", backup="derivable"),
    P("data/podcasts/", "Your podcast", "Your podcast audio and artwork.", backup="files"),
    P("logs/", "Logs", "The app's logs, with secrets masked.", backup="derivable"),
)

ENTRIES = _TABLES + _SETTINGS + _PATHS

# One documented rule that classes rows, not tables (research R12). Works belonging to accounts
# PawPoller only watches — a friend's gallery kept for reference — are other people's data. The
# accounts are read at run time from a setting; no account is ever named in this file.
ROW_RULES = (
    ("Works from accounts you only watch and never post from", "confidential",
     "They're someone else's work. Bulk actions already leave them alone."),
)

# ── Matching ─────────────────────────────────────────────────────────────────

_ACCT = re.compile(r"^acct_\d+_(.+)$")


_STAMP = r"\d{8}-\d{6}(?:-\d+)?"   # <stamp>: a UTC time stamp in a file name, 20260930-024500[-2]


def _compile(entry: Entry) -> re.Pattern | None:
    if "<P>" not in entry.name and "<stamp>" not in entry.name:
        return None
    alts = "|".join(re.escape(p) for p in PLATFORM_PREFIXES)
    pat = re.escape(entry.name).replace(re.escape("<P>"), f"(?:{alts})").replace(re.escape("<stamp>"), _STAMP)
    return re.compile("^" + pat + "$")


_EXACT: dict[tuple[str, str], Entry] = {}
_TEMPLATES: list[tuple[str, re.Pattern, Entry]] = []
_FOLDERS: list[Entry] = []
for _e in ENTRIES:
    _pat = _compile(_e)
    if _pat is not None:
        _TEMPLATES.append((_e.kind, _pat, _e))
    elif _e.kind == "path" and _e.name.endswith("/"):
        _FOLDERS.append(_e)
    else:
        _EXACT.setdefault((_e.kind, _e.name), _e)


def entry_for(kind: str, name: str) -> Entry | None:
    """The entry that classifies *name*, or None. Never raises."""
    try:
        if kind == "setting":
            m = _ACCT.match(name)
            if m:                       # acct_<id>_<field>: an extra account's copy of <field>
                name = m.group(1)
        if kind == "path":
            name = name.replace("\\", "/").lstrip("/")
        hit = _EXACT.get((kind, name))
        if hit:
            return hit
        for k, pat, e in _TEMPLATES:
            if k == kind and pat.match(name):
                return e
        if kind == "path":
            for e in _FOLDERS:
                if name == e.name.rstrip("/") or name.startswith(e.name):
                    return e
    except Exception:  # noqa: BLE001 — a lookup must never break its caller
        return None
    return None


def _cls(kind, name):
    e = entry_for(kind, name)
    return e.cls if e else None


def table_class(name: str) -> str | None:
    return _cls("table", name)


def setting_class(key: str) -> str | None:
    return _cls("setting", key)


def path_class(relpath: str) -> str | None:
    return _cls("path", relpath)


def rank(cls: str | None) -> int:
    return _RANK.get(cls or "", -1)


def vault_fields() -> frozenset:
    """Settings kept in the encrypted vault — ``config.CREDENTIAL_FIELDS``."""
    return frozenset(e.name for e in ENTRIES if e.kind == "setting" and e.vault and "<P>" not in e.name)


def is_identity_key(key: str) -> bool:
    """Your own handle or address: legible in your log, scrubbed from everything that leaves."""
    e = entry_for("setting", key)
    return bool(e and e.identity)


def identity_keys() -> frozenset:
    return frozenset(e.name for e in ENTRIES if e.kind == "setting" and e.identity)


def name_columns() -> tuple:
    """(table, column) pairs holding other people's names or handles."""
    return tuple((e.name, c) for e in ENTRIES if e.kind == "table" for c in e.names)


def public_copy_refused(relpath: str) -> bool:
    """True for a file that must never ship: any data-area file, wherever it sits.

    Every class's handling says the same for the public copy — code only, never data — so
    this doesn't look at class. The public copy is source code, so it matches by FILE NAME
    (a stray settings.json or a .db anywhere in the tree) and by the data folders' names at
    the top of the tree.
    """
    p = relpath.replace("\\", "/").lstrip("/")
    base = p.rsplit("/", 1)[-1]
    for e in ENTRIES:
        if e.kind != "path":
            continue
        if e.name.endswith("/"):
            top = e.name.split("/")[-2]
            if p.startswith(e.name) or p.startswith(top + "/"):
                return True
        elif base == e.name.rsplit("/", 1)[-1]:
            return True
    # Name patterns too (a set-aside vault's time-stamped name), by the file's base name.
    if entry_for("path", "data/" + base):
        return True
    return base.endswith((".db", ".db-wal", ".db-shm", ".sqlite", ".sqlite3"))


# ── What this instance holds (Settings → Privacy, `python -m datamap check`) ──

def _tables(conn) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]


def _data_items(home: Path) -> list[str]:
    """Top-level items of the data folder and the logs folder, plus the few known app-level ones.

    The app's home folder itself is NOT walked: in a source checkout or the Docker image it is
    the source tree.
    """
    out = []
    for sub in ("data", "logs"):
        d = home / sub
        if not d.is_dir():
            continue
        if sub == "logs":
            out.append("logs/")
            continue
        for child in sorted(d.iterdir()):
            out.append(f"data/{child.name}" + ("/" if child.is_dir() else ""))
    for name in ("webview", "story-archive"):
        if (home / name).is_dir():
            out.append(name + "/")
    if (home / "settings.json").is_file():
        out.append("settings.json")
    return out


def unclassified(conn, settings: dict, home: Path) -> list[dict]:
    """Names (never values) of what this instance holds that the list doesn't know."""
    out = []
    if conn is not None:
        out += [{"kind": "table", "name": t} for t in _tables(conn) if not table_class(t)]
    out += [{"kind": "setting", "name": k} for k in sorted(settings or {}) if not setting_class(k)]
    if home is not None:
        out += [{"kind": "path", "name": p} for p in _data_items(Path(home)) if not path_class(p)]
    return out


def _count_table(conn, name) -> int | None:
    try:
        return conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
    except Exception:  # noqa: BLE001
        return None


def _count_path(home: Path, rel: str) -> int | None:
    try:
        p = home / rel.rstrip("/")
        if not p.exists():
            return 0
        if p.is_file():
            return 1
        return sum(1 for f in p.rglob("*") if f.is_file())
    except Exception:  # noqa: BLE001
        return None


def holdings(conn, settings: dict, home: Path) -> dict:
    """What this install holds, by class and group — counts, never values."""
    home = Path(home) if home is not None else None
    items: dict[str, list] = {}
    if conn is not None:
        for t in _tables(conn):
            e = entry_for("table", t)
            if e:
                items.setdefault(e.group, []).append({"kind": "table", "name": t, "count": _count_table(conn, t)})
    for k, v in sorted((settings or {}).items()):
        e = entry_for("setting", k)
        if e and v not in (None, "", [], {}, False):
            items.setdefault(e.group, []).append({"kind": "setting", "name": k, "count": 1})
    if home is not None:
        for p in _data_items(home):
            e = entry_for("path", p)
            if e:
                items.setdefault(e.group, []).append({"kind": "path", "name": p, "count": _count_path(home, p)})
    classes = []
    for cls, _rank, label, meaning in CLASSES:
        groups = []
        for g in GROUPS.values():
            if g[1] != cls or g[0] not in items:
                continue
            its = items[g[0]]
            total = sum(i["count"] or 0 for i in its)
            groups.append({"group": g[0], "about": g[2], "goes_to": g[3], "remove": g[4],
                           "items": its, "count": total})
        classes.append({"cls": cls, "label": label, "meaning": meaning,
                        "handling": [{"situation": lbl, "rule": HANDLING[cls][key]} for key, lbl in SITUATIONS],
                        "groups": groups})
    return {"classes": classes, "row_rules": [{"rule": r[0], "cls": r[1], "why": r[2]} for r in ROW_RULES],
            "unclassified": unclassified(conn, settings, home)}


# ── python -m datamap check ──────────────────────────────────────────────────

def _main(argv=None) -> int:
    import argparse
    import json
    ap = argparse.ArgumentParser(prog="python -m datamap",
                                 description="List what an install holds that the classification list doesn't know.")
    ap.add_argument("command", choices=["check"])
    ap.add_argument("--db", help="path to pawpoller.db (opened read-only)")
    ap.add_argument("--settings", help="path to settings.json (key names only are read)")
    ap.add_argument("--home", help="the app's data home: the folder holding data/ and logs/")
    a = ap.parse_args(argv)
    conn = sqlite3.connect(f"file:{Path(a.db).as_posix()}?mode=ro", uri=True) if a.db else None
    keys = {}
    if a.settings:
        keys = {k: None for k in json.loads(Path(a.settings).read_text(encoding="utf-8"))}
    try:
        missing = unclassified(conn, keys, Path(a.home) if a.home else None)
    finally:
        if conn is not None:
            conn.close()
    for m in missing:
        print(f"UNCLASSIFIED {m['kind']} {m['name']}".encode("ascii", "replace").decode("ascii"))
    print("[OK] everything classified" if not missing else f"[FAIL] {len(missing)} unclassified")
    return 0 if not missing else 1


if __name__ == "__main__":
    raise SystemExit(_main())
