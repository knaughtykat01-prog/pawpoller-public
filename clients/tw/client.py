"""X/Twitter (TW) HTTP client using cookie-based GraphQL scraping.

X/Twitter's internal GraphQL API is accessed via browser cookies, the same
approach used by the DeviantArt integration. Authentication requires two
cookies from the user's browser session: auth_token and ct0 (CSRF token).

Key details:
  - Tweet IDs are numeric strings (TEXT in SQLite — 64-bit ints exceed safe range)
  - Stats: views, likes, retweets, replies, quotes, bookmarks (6 metrics)
  - Auth: cookie-based (auth_token + ct0 from browser)
  - GraphQL endpoints: https://x.com/i/api/graphql/{queryId}/{operationName}
  - Rate limiting: aggressive — 2.0s delay between requests, 60s on 429
  - Content type detection: tweet, reply, retweet, quote

Note: GraphQL query IDs may rotate over time. The IDs below are hardcoded
from the current X web client JS bundle. If requests start failing with 404,
these IDs may need updating.
"""

from __future__ import annotations
import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

import httpx

import config

logger = logging.getLogger(__name__)

_BASE = "https://x.com"

# Public bearer token — NOT a secret.  This is embedded in X's web client JS
# bundle and shared by all users.  It identifies the "X Web App" client, not
# any individual user.
_BEARER = "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer": "https://x.com/",
    "Authorization": f"Bearer {_BEARER}",
    "x-twitter-active-user": "yes",
    "x-twitter-client-language": "en",
}

# GraphQL query IDs — these may rotate when X updates their web client.
# Last verified: 2025-03.  If requests return 404, update these IDs by
# inspecting X's main.*.js bundle for the corresponding operation names.
_GRAPHQL_USER_BY_SCREEN_NAME = "xmU6X_CKVnQ5lSrCbAmJsg"
_GRAPHQL_USER_TWEETS = "E3opETHurmVJflFsUBVuUQ"
_GRAPHQL_TWEET_RESULT_BY_REST_ID = "zXaXQgfyR4GxE3UFlgapRQ"
# Posting mutation. Query IDs rotate; if CreateTweet 404s, refresh this from
# x.com's main.*.js bundle (search for "CreateTweet").
_GRAPHQL_CREATE_TWEET = "a1p9RWpkYKBjWv_I3WzS-A"

# Standard GraphQL features dict required by X's API
_GRAPHQL_FEATURES = {
    "rweb_tipjar_consumption_enabled": True,
    "responsive_web_graphql_exclude_directive_enabled": True,
    "verified_phone_label_enabled": False,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "communities_web_enable_tweet_community_results_fetch": True,
    "c9s_tweet_anatomy_moderator_badge_enabled": True,
    "articles_preview_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "view_counts_everywhere_api_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "responsive_web_twitter_article_tweet_consumption_enabled": True,
    "tweet_awards_web_tipping_enabled": False,
    "creator_subscriptions_quote_tweet_preview_enabled": False,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "standardized_nudges_misinfo": True,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "rweb_video_timestamps_enabled": True,
    "longform_notetweets_rich_text_read_enabled": True,
    "longform_notetweets_inline_media_enabled": True,
    "responsive_web_enhance_cards_enabled": False,
}

# CreateTweet needs its own feature set. This too rotates — if the mutation
# errors with "features cannot be null" / a missing-feature list, sync this with
# x.com's current CreateTweet payload.
_CREATE_TWEET_FEATURES = {
    "communities_web_enable_tweet_community_results_fetch": True,
    "c9s_tweet_anatomy_moderator_badge_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "view_counts_everywhere_api_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "responsive_web_twitter_article_tweet_consumption_enabled": True,
    "tweet_awards_web_tipping_enabled": False,
    "longform_notetweets_rich_text_read_enabled": True,
    "longform_notetweets_inline_media_enabled": True,
    "rweb_video_timestamps_enabled": True,
    "responsive_web_graphql_exclude_directive_enabled": True,
    "verified_phone_label_enabled": False,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "standardized_nudges_misinfo": True,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_enhance_cards_enabled": False,
}


def _safe_int(val: Any) -> int:
    """Safely convert a value to int."""
    if val is None:
        return 0
    try:
        if isinstance(val, str):
            val = val.replace(",", "").strip()
        return int(val)
    except (ValueError, TypeError):
        return 0


# Snowflake epoch (2010-11-04T01:42:54.657Z). X tweet ids encode their creation
# time in the high bits — the reliable source for a tweet's date now that X has
# stopped consistently populating legacy.created_at in the timeline response.
_TWITTER_EPOCH_MS = 1288834974657


def _snowflake_to_utc(tweet_id: Any) -> str:
    """Creation time from a Snowflake tweet id as 'YYYY-MM-DD HH:MM:SS' (UTC),
    or '' if the id isn't a usable snowflake."""
    try:
        ms = (int(tweet_id) >> 22) + _TWITTER_EPOCH_MS
        dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    except (ValueError, TypeError, OverflowError, OSError):
        return ""
    if dt.year < 2006:   # sanity: pre-Twitter → not a snowflake
        return ""
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _is_repost(result: dict) -> bool:
    """True if a UserTweets timeline result is a repost (retweet of another
    account). The UserTweets timeline interleaves the account's own posts and
    replies with its retweets; a retweet's engagement (likes/views/etc.) belongs
    to the ORIGINAL author, not this account, so we skip them when discovering
    what to track. Quote tweets are the account's own posts (they use
    `quoted_status_id_str`, not `retweeted_status_result`) and are kept.
    """
    legacy = (result or {}).get("legacy", {}) or {}
    return bool(legacy.get("retweeted_status_result"))


def _repost_original(result: dict) -> dict | None:
    """The original tweet object inside a repost timeline entry, or None."""
    orig = (((result or {}).get("legacy", {}) or {})
            .get("retweeted_status_result", {}) or {}).get("result", {})
    if orig.get("__typename") == "TweetWithVisibilityResults":
        orig = orig.get("tweet", orig)
    return orig or None


def _user_tagged_in(result: dict, target_user: str) -> bool:
    """True if *target_user* is @-mentioned in the tweet — or, for a repost, in
    the original post. Used to keep the reposts that actually tag the account
    while still dropping the rest.
    """
    tu = (target_user or "").lower().lstrip("@")
    if not tu:
        return False
    for r in (result, _repost_original(result)):
        if not r:
            continue
        mentions = (((r.get("legacy", {}) or {}).get("entities", {}) or {})
                    .get("user_mentions", []) or [])
        for m in mentions:
            if (m.get("screen_name", "") or "").lower() == tu:
                return True
    return False


class TWClient:
    """Async HTTP client for X/Twitter using cookie-based GraphQL endpoints."""

    def __init__(self, auth_token: str, ct0: str, target_user: str,
                 proxy_url: str = "", proxy_key: str = ""):
        self.auth_token = auth_token    # auth_token cookie from browser
        self.ct0 = ct0                  # ct0 CSRF cookie from browser
        self.target_user = target_user  # Username to track (without @)
        self._user_rest_id: str = ""    # Cached user ID
        self.throttled = False          # set True on a 429; read + reset by the poller each cycle

        if proxy_url and proxy_key:
            from polling.cf_proxy import CloudflareProxyTransport
            transport = CloudflareProxyTransport(proxy_url, proxy_key)
            logger.info("TW client using CF proxy: %s", proxy_url)
        else:
            transport = httpx.AsyncHTTPTransport(retries=2)
        self._http = httpx.AsyncClient(
            timeout=30.0,
            follow_redirects=True,
            headers=_HEADERS,
            transport=transport,
        )
        self._update_cookies()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def close(self) -> None:
        await self._http.aclose()

    def _update_cookies(self) -> None:
        """Set auth_token + ct0 cookies and CSRF header on the HTTP client."""
        self._http.cookies.set("auth_token", self.auth_token, domain=".x.com")
        self._http.cookies.set("ct0", self.ct0, domain=".x.com")
        self._http.headers["x-csrf-token"] = self.ct0

    def update_credentials(self, auth_token: str, ct0: str, target_user: str) -> None:
        """Update stored credentials and refresh cookies."""
        self.auth_token = auth_token
        self.ct0 = ct0
        self.target_user = target_user
        self._user_rest_id = ""
        self._update_cookies()

    # -- Authentication -------------------------------------------------------

    async def validate_cookies(self) -> bool:
        """Verify the active poll backend can authenticate.

        Backend-aware, mirroring get_all_tweets() priority:
          1. **Official X API v2** — validates the Bearer token; needs NO cookies.
          2. **gallery-dl** — a single-item fetch (avoids brittle GraphQL query IDs).
          3. **GraphQL** UserByScreenName with the auth_token + ct0 cookies.
        A definitive result from a higher-priority backend wins; a valid Bearer
        token lets a cookie-less user poll. (Posting still needs cookies, but
        that's validated separately by the Posts flow.)
        """
        settings = config.get_settings()
        # 1. Official API (opt-in). A valid Bearer token authenticates polling
        #    with no cookies. A bad/None verdict falls through, so a user who
        #    ALSO has cookies can still poll via a scraper.
        try:
            from clients.tw import official_api
            if official_api.is_enabled(settings):
                if await official_api.validate(None, self.target_user, settings) is True:
                    return True
        except Exception as e:
            logger.debug("TW: official-API validation errored: %s", e)

        # 2/3 need cookies.
        if not self.auth_token or not self.ct0:
            return False
        try:
            from clients.tw import gallerydl
            verdict = await gallerydl.validate(self.auth_token, self.ct0, self.target_user)
        except Exception as e:
            logger.debug("TW: gallery-dl validation errored, using GraphQL: %s", e)
            verdict = None
        if verdict is not None:
            return verdict
        try:
            # Fallback: UserByScreenName endpoint
            user_id = await self._get_user_id()
            return bool(user_id)
        except Exception as e:
            logger.warning("TW: Cookie validation failed: %s", e)
            return False

    # -- User Resolution ------------------------------------------------------

    async def _get_user_id(self) -> str:
        """Resolve screen name to user rest_id via UserByScreenName GraphQL."""
        if self._user_rest_id:
            return self._user_rest_id

        variables = json.dumps({"screen_name": self.target_user, "withSafetyModeUserFields": True})
        features = json.dumps(_GRAPHQL_FEATURES)

        data = await self._get_json(
            f"{_BASE}/i/api/graphql/{_GRAPHQL_USER_BY_SCREEN_NAME}/UserByScreenName",
            params={"variables": variables, "features": features},
        )

        if not data or not isinstance(data, dict):
            return ""

        user_result = data.get("data", {}).get("user", {}).get("result", {})
        rest_id = user_result.get("rest_id", "")
        if rest_id:
            self._user_rest_id = rest_id
            logger.info("TW: Resolved %s → rest_id=%s", self.target_user, rest_id)
        return rest_id

    async def get_follower_count(self) -> int | None:
        """Follower count for the tracked account.

        Order mirrors get_all_tweets() so the follower total rides along for free
        with whatever backend already polled the timeline this cycle:
          1. **gallery-dl** — the count captured during this cycle's tweet fetch
             (author.followers_count); zero extra requests, so a gallery-dl poll
             stays $0 end to end.
          2. **Official API** — the value its own fetch_tweets() cached, else one
             billed user-lookup (only reached when the official API is the active
             backend; a cache hit still costs nothing).
          3. **GraphQL UserByScreenName** — the cookie-based scrape fallback.
        """
        # 1. gallery-dl already fetched this during get_all_tweets() — free.
        try:
            from clients.tw import gallerydl
            fc = gallerydl.get_follower_count(self.target_user)
            if fc is not None:
                return fc
        except Exception as e:
            logger.debug("TW: gallery-dl follower lookup errored: %s", e)

        # 2. Official API (paid) — cached during its fetch, else one billed call.
        try:
            from clients.tw import official_api
            fc = await official_api.get_follower_count(None, self.target_user)
            if fc is not None:
                return fc
        except Exception as e:
            logger.debug("TW: official-API follower lookup errored: %s", e)

        variables = json.dumps({"screen_name": self.target_user, "withSafetyModeUserFields": True})
        features = json.dumps(_GRAPHQL_FEATURES)
        data = await self._get_json(
            f"{_BASE}/i/api/graphql/{_GRAPHQL_USER_BY_SCREEN_NAME}/UserByScreenName",
            params={"variables": variables, "features": features},
        )
        if not data or not isinstance(data, dict):
            return None
        result = data.get("data", {}).get("user", {}).get("result", {}) or {}
        legacy = result.get("legacy", {}) or {}
        fc = legacy.get("followers_count")
        if fc is None:
            fc = result.get("followers_count")
        return _safe_int(fc) if fc is not None else None

    # -- HTTP Helpers ---------------------------------------------------------

    async def _get_json(self, url: str, params: dict | None = None) -> dict | None:
        """Fetch a JSON endpoint with error handling for X's rate limiting.

        Each request first acquires a slot in the shared cross-account X rate
        limiter (polling/rate_limit.py) so polling several accounts back-to-back
        can't blow through X's per-IP window.
        """
        from polling.rate_limit import tw_acquire
        try:
            await tw_acquire()
            resp = await self._http.get(url, params=params)

            if resp.status_code == 403:
                logger.error("TW: Access denied (403) for %s", url.split("?")[0])
                return None

            if resp.status_code == 429:
                logger.warning("TW: Rate limited (429), waiting 60s...")
                self.throttled = True   # surfaced by the poller as a 'partial' poll
                await asyncio.sleep(60)
                await tw_acquire()
                resp = await self._http.get(url, params=params)

            if resp.status_code == 404:
                logger.warning("TW: Not found (404) for %s", url.split("?")[0])
                return None

            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as e:
            logger.error("TW: Failed to fetch %s: %s", url.split("?")[0], e)
            return None
        except Exception as e:
            logger.error("TW: JSON parse error: %s", e)
            return None

    async def _post_graphql(self, query_id: str, op_name: str,
                            variables: dict, features: dict) -> dict | None:
        url = f"{_BASE}/i/api/graphql/{query_id}/{op_name}"
        body = {"variables": variables, "features": features, "queryId": query_id}
        try:
            resp = await self._http.post(url, json=body)
            if resp.status_code != 200:
                logger.error("TW: %s failed (%s): %s", op_name, resp.status_code, resp.text[:300])
                return None
            return resp.json()
        except Exception as e:
            logger.error("TW: %s error: %s", op_name, e)
            return None

    # -- Posting --------------------------------------------------------------

    async def upload_media(self, image_path: str) -> str | None:
        """Upload one image to X's media endpoint; return its media_id (or None).

        Uses the simple (non-chunked) v1.1 upload, which handles the small images
        a tweet carries (X caps tweet images at ~5 MB). Reuses the exact same
        cookie / x-csrf-token / bearer auth as the GraphQL polling client —
        ``self._http`` already carries all three. The endpoint is on upload.x.com
        so the ``.x.com`` cookies are sent; if X moves it back to
        upload.twitter.com this will 401 and the domain here needs updating.
        Returns the media_id_string, or None on any failure (logged).
        """
        import mimetypes
        import os
        if not (self.auth_token and self.ct0):
            return None
        mime = mimetypes.guess_type(image_path)[0] or "image/jpeg"
        try:
            with open(image_path, "rb") as f:
                files = {"media": (os.path.basename(image_path), f, mime)}
                resp = await self._http.post(
                    "https://upload.x.com/1.1/media/upload.json",
                    files=files, timeout=120.0,
                )
            if resp.status_code != 200:
                logger.error("TW: media upload failed (%s): %s",
                             resp.status_code, resp.text[:200])
                return None
            return str((resp.json() or {}).get("media_id_string") or "") or None
        except Exception as e:
            logger.error("TW: media upload error: %s", e)
            return None

    async def create_tweet(self, text: str, media_ids: list[str] | None = None) -> dict | None:
        """Post a tweet via the internal CreateTweet GraphQL mutation.

        Same cookie auth as polling; attaches up to 4 uploaded ``media_ids``
        (from :meth:`upload_media`) when given. X's query IDs + feature flags
        rotate and X actively fights automation — if this 404s/errors, refresh
        ``_GRAPHQL_CREATE_TWEET`` / ``_CREATE_TWEET_FEATURES`` from x.com's bundle.
        Returns {id, url} or None.
        """
        if not (self.auth_token and self.ct0):
            return None
        media_entities = [{"media_id": str(m), "tagged_users": []}
                          for m in (media_ids or [])]
        variables = {
            "tweet_text": text,
            "dark_request": False,
            "media": {"media_entities": media_entities, "possibly_sensitive": False},
            "semantic_annotation_ids": [],
        }
        data = await self._post_graphql(
            _GRAPHQL_CREATE_TWEET, "CreateTweet", variables, _CREATE_TWEET_FEATURES)
        if not data:
            return None
        result = ((data.get("data", {}) or {}).get("create_tweet", {})
                  .get("tweet_results", {}).get("result", {}))
        rest_id = result.get("rest_id", "")
        if not rest_id:
            logger.error("TW: CreateTweet returned no rest_id: %s", str(data)[:300])
            return None
        handle = self.target_user or "i"
        return {"id": str(rest_id), "url": f"https://x.com/{handle}/status/{rest_id}"}

    # -- Tweet Discovery ------------------------------------------------------

    async def get_all_tweets(self) -> list[dict]:
        """Fetch the target user's tweets with full stats.

        Backend priority (each returns None when it's not its turn / fails, so we
        simply fall through):
          1. **gallery-dl** (clients/tw/gallerydl.py) — free, tracks X's internal
             API; the primary path so a normal poll costs nothing.
          2. **Official X API v2** (clients/tw/official_api.py) — the paid
             fallback: ToS-compliant and IP-agnostic, so it rescues a poll only
             when gallery-dl fails. You're billed a request only on that fallback,
             not every cycle.
          3. **GraphQL scrape** (:meth:`_get_all_tweets_graphql`) — always-available
             last-ditch fallback (fragile: hardcoded query IDs).

        The ``tw_polling_backend`` setting overrides this: ``"gallerydl"`` drops
        the paid fallback, ``"official"`` forces the paid API first (gallery-dl
        disabled), ``"graphql"`` forces the scrape only. Under the default
        ``"auto"`` all three chain in the order above.

        A backend result (even an empty list) is authoritative; only ``None``
        triggers the next backend. Posting is unaffected by all of this.
        """
        # 1. gallery-dl subprocess (free primary — X polls cost nothing here)
        try:
            from clients.tw import gallerydl
            via_gdl = await gallerydl.fetch_tweets(self.auth_token, self.ct0, self.target_user)
        except Exception as e:
            logger.warning("TW: gallery-dl backend error, trying next: %s", e)
            via_gdl = None
        if via_gdl is not None:
            return via_gdl

        # 2. Official X API v2 (paid fallback — only reached when gallery-dl fails)
        try:
            from clients.tw import official_api
            via_api = await official_api.fetch_tweets(None, self.target_user)
        except Exception as e:
            logger.warning("TW: official-API backend error, using GraphQL: %s", e)
            via_api = None
        if via_api is not None:
            return via_api

        # 3. GraphQL timeline scrape (always-available last-ditch fallback)
        return await self._get_all_tweets_graphql()

    async def _get_all_tweets_graphql(self) -> list[dict]:
        """Fetch the target user's tweets — with full stats — via UserTweets.

        The timeline response already carries each tweet's text and engagement
        (legacy.favorite_count, retweet_count, views, …), so we parse stats
        straight from it instead of a second per-tweet TweetResultByRestId fetch
        (that endpoint's GraphQL id rotates and was returning 404 for every
        tweet, leaving everything "(untitled)" with zero stats).

        Reposts are dropped unless the account is tagged in them; a kept repost
        surfaces the ORIGINAL post's stats. Returns a list of full detail dicts
        (same shape as :meth:`_extract_tweet_stats`). Cursor-paginated.
        """
        user_id = await self._get_user_id()
        if not user_id:
            logger.error("TW: Could not resolve user ID for %s", self.target_user)
            return []

        all_tweets: list[dict] = []
        seen_ids: set[str] = set()
        cursor: str | None = None

        for _page_safety in range(1000):
            variables: dict[str, Any] = {
                "userId": user_id,
                "count": 40,
                "includePromotedContent": False,
                "withQuickPromoteEligibilityTweetFields": True,
                "withVoice": True,
                "withV2Timeline": True,
            }
            if cursor:
                variables["cursor"] = cursor

            params = {
                "variables": json.dumps(variables),
                "features": json.dumps(_GRAPHQL_FEATURES),
            }

            data = await self._get_json(
                f"{_BASE}/i/api/graphql/{_GRAPHQL_USER_TWEETS}/UserTweets",
                params=params,
            )

            if not data or not isinstance(data, dict):
                break

            # Navigate the nested timeline structure
            timeline = (data.get("data", {})
                        .get("user", {})
                        .get("result", {})
                        .get("timeline_v2", {})
                        .get("timeline", {}))
            instructions = timeline.get("instructions", [])

            new_this_page = 0
            next_cursor = None

            for instruction in instructions:
                inst_type = instruction.get("type", "")

                entries = []
                if inst_type == "TimelineAddEntries":
                    entries = instruction.get("entries", [])
                elif inst_type == "TimelineAddToModule":
                    entries = instruction.get("moduleItems", [])

                for entry in entries:
                    entry_id = entry.get("entryId", "")

                    # Cursor entries for pagination
                    if "cursor-bottom" in entry_id:
                        content = entry.get("content", {})
                        next_cursor = content.get("value", "")
                        continue
                    if "cursor-top" in entry_id:
                        continue

                    # Tweet entries
                    content = entry.get("content", {})
                    item_content = content.get("itemContent", {})
                    tweet_results = item_content.get("tweet_results", {})
                    result = tweet_results.get("result", {})

                    # Handle tweet with visibility results
                    if result.get("__typename") == "TweetWithVisibilityResults":
                        result = result.get("tweet", result)

                    # Reposts (retweets of other accounts) are dropped UNLESS the
                    # account is tagged in them — those are posts about you worth
                    # keeping. A kept repost reports the original post's stats.
                    repost = _is_repost(result)
                    if repost and not _user_tagged_in(result, self.target_user):
                        continue
                    source = (_repost_original(result) or result) if repost else result

                    detail = self._extract_tweet_stats(source)
                    if repost:
                        detail["content_type"] = "retweet"
                    tweet_id = detail.get("tweet_id", "")
                    if tweet_id and tweet_id not in seen_ids:
                        seen_ids.add(tweet_id)
                        all_tweets.append(detail)
                        new_this_page += 1

            if new_this_page == 0 and not next_cursor:
                break

            cursor = next_cursor
            if not cursor:
                break

            await asyncio.sleep(config.TW_REQUEST_DELAY_SECONDS)

        logger.info("TW: Found %d tweets for %s", len(all_tweets), self.target_user)
        return all_tweets

    # -- Tweet Details --------------------------------------------------------

    async def get_tweet_detail(self, tweet_id: str) -> dict:
        """Fetch full stats for a single tweet via TweetResultByRestId GraphQL."""
        variables = json.dumps({
            "tweetId": tweet_id,
            "withCommunity": False,
            "includePromotedContent": False,
            "withVoice": False,
        })
        features = json.dumps(_GRAPHQL_FEATURES)

        data = await self._get_json(
            f"{_BASE}/i/api/graphql/{_GRAPHQL_TWEET_RESULT_BY_REST_ID}/TweetResultByRestId",
            params={"variables": variables, "features": features},
        )

        if not data or not isinstance(data, dict):
            return self._empty_detail(tweet_id)

        result = (data.get("data", {})
                  .get("tweetResult", {})
                  .get("result", {}))

        if result.get("__typename") == "TweetWithVisibilityResults":
            result = result.get("tweet", result)

        if not result.get("rest_id"):
            return self._empty_detail(tweet_id)

        return self._extract_tweet_stats(result)

    async def get_tweet_details_batch(self, items: list[dict]) -> list[dict]:
        """Fetch details for multiple tweets sequentially with rate limiting."""
        details = []
        for i, item in enumerate(items):
            if i > 0:
                await asyncio.sleep(config.TW_REQUEST_DELAY_SECONDS)
            try:
                detail = await self.get_tweet_detail(item["tweet_id"])
                details.append(detail)
            except Exception as e:
                logger.warning("TW: Failed to fetch tweet %s: %s", item.get("tweet_id"), e)
                details.append(self._empty_detail(item["tweet_id"]))
        return details

    # -- Parsing Helpers ------------------------------------------------------

    def _extract_tweet_stats(self, result: dict) -> dict:
        """Parse a GraphQL tweet result into a normalised stats dict."""
        tweet_id = result.get("rest_id", "")
        legacy = result.get("legacy", {})
        core = result.get("core", {})
        user_results = core.get("user_results", {}).get("result", {})
        user_legacy = user_results.get("legacy", {})

        text = legacy.get("full_text", "")
        username = user_legacy.get("screen_name", self.target_user)

        # Content type detection
        content_type = "tweet"
        if legacy.get("in_reply_to_status_id_str"):
            content_type = "reply"
        elif legacy.get("retweeted_status_result"):
            content_type = "retweet"
        elif legacy.get("quoted_status_id_str"):
            content_type = "quote"

        # Stats from legacy object
        views_data = result.get("views", {})
        views = _safe_int(views_data.get("count", 0)) if isinstance(views_data, dict) else 0

        likes = _safe_int(legacy.get("favorite_count", 0))
        retweets = _safe_int(legacy.get("retweet_count", 0))
        replies = _safe_int(legacy.get("reply_count", 0))
        quotes = _safe_int(legacy.get("quote_count", 0))
        bookmarks = _safe_int(legacy.get("bookmark_count", 0))

        # Thumbnail (first item) + the full set of PHOTO urls. extended_entities
        # covers multi-image tweets (up to 4); entities.media is the older single
        # fallback. `media_urls` keeps every photo so the artwork importer can
        # bring them all in — videos/GIFs are skipped (their media_url_https is a
        # still preview, not an importable image).
        thumbnail_url = ""
        media = ((legacy.get("extended_entities") or {}).get("media")
                 or (legacy.get("entities") or {}).get("media") or [])
        media_urls = [m.get("media_url_https", "") for m in media
                      if m.get("type") == "photo" and m.get("media_url_https")]
        if media:
            thumbnail_url = media[0].get("media_url_https", "")
        # Quote tweets usually carry no media of their own — the image lives in
        # the QUOTED post (e.g. quoting someone's art). Fall back to it so the
        # quoted image still shows (and imports).
        if not thumbnail_url:
            quoted = ((result.get("quoted_status_result") or {}).get("result") or {})
            if quoted.get("__typename") == "TweetWithVisibilityResults":
                quoted = quoted.get("tweet", quoted)
            q_legacy = quoted.get("legacy", {}) or {}
            q_media = ((q_legacy.get("extended_entities") or {}).get("media")
                       or (q_legacy.get("entities") or {}).get("media") or [])
            if q_media:
                thumbnail_url = q_media[0].get("media_url_https", "")
            if not media_urls:
                media_urls = [m.get("media_url_https", "") for m in q_media
                              if m.get("type") == "photo" and m.get("media_url_https")]

        # Posted at — derive from the Snowflake id (X no longer reliably fills
        # legacy.created_at in the timeline); fall back to created_at if needed.
        posted_at = _snowflake_to_utc(tweet_id) or legacy.get("created_at", "")

        # Keywords from hashtags
        hashtags = legacy.get("entities", {}).get("hashtags", [])
        keywords = [h.get("text", "") for h in hashtags if h.get("text")]

        # Link
        link = f"https://x.com/{username}/status/{tweet_id}"

        return {
            "tweet_id": tweet_id,
            "title": text[:80] + ("..." if len(text) > 80 else "") if text else "",
            "username": username,
            "posted_at": posted_at,
            "content_type": content_type,
            "rating": "General",
            "description": text,
            "keywords": keywords,
            "link": link,
            "thumbnail_url": thumbnail_url,
            "media_urls": media_urls,
            "views": views,
            "likes": likes,
            "retweets": retweets,
            "replies": replies,
            "quotes": quotes,
            "bookmarks": bookmarks,
        }

    def _empty_detail(self, tweet_id: str) -> dict:
        """Return an empty detail dict for a tweet that couldn't be fetched."""
        return {
            "tweet_id": tweet_id,
            "title": "",
            "username": self.target_user,
            "posted_at": "",
            "content_type": "tweet",
            "rating": "General",
            "description": "",
            "keywords": [],
            "link": f"https://x.com/{self.target_user}/status/{tweet_id}",
            "thumbnail_url": "",
            "media_urls": [],
            "views": 0,
            "likes": 0,
            "retweets": 0,
            "replies": 0,
            "quotes": 0,
            "bookmarks": 0,
        }
