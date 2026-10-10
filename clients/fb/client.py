"""Facebook Pages client (spec 022 posting; spec 029 stats + comments, 4.59.0).

Facebook lets apps post to **Pages**, never to personal profiles. The credential is a
**Page access token**; one made from a long-lived *user* token has no expiry date, so
the connect flow does that exchange once (server-side — it needs the app secret, which
is used for the one call and never stored) and keeps only the Page token.

Unlike the Instagram / Threads siblings, the Pages API takes the FILE itself
(multipart ``source``) for photos and videos, so no public image host is involved and
posting works the same from the desktop app as from a server.

Endpoints (Graph API v25.0, checked 2026-10-07):
  - ``GET  /oauth/access_token?grant_type=fb_exchange_token`` → long-lived user token
  - ``GET  /me/accounts?fields=id,name,access_token,tasks`` → the Pages it manages
  - ``POST /{page}/photos``  (``source``, ``caption``, ``published``) → {id, post_id}
  - ``POST /{page}/feed``    (``message``, ``link``, ``attached_media[n]``) → {id}
  - ``POST graph-video …/{page}/videos`` (``source``, ``title``, ``description``) → {id}
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_GRAPH = "https://graph.facebook.com/v25.0"
_GRAPH_VIDEO = "https://graph-video.facebook.com/v25.0"
_HEADERS = {"User-Agent": "PawPoller/1.0", "Accept": "application/json"}

# What the user token needs, named in the guide and in error messages.
REQUIRED_PERMISSIONS = ("pages_show_list", "pages_manage_posts", "pages_read_engagement")


class FbError(RuntimeError):
    """A Graph refusal, already turned into a sentence a non-expert can act on."""


def plain_error(status: int, body: Any) -> str:
    """Meta's error object → one plain sentence (FR-007). Pure, so tests can feed it."""
    err = (body or {}).get("error") if isinstance(body, dict) else None
    if not isinstance(err, dict):
        return f"Facebook refused the request (HTTP {status})"
    code, sub = err.get("code"), err.get("error_subcode")
    msg = str(err.get("error_user_msg") or err.get("message") or "").strip()
    low = msg.lower()
    if code == 190 or sub in (463, 467):
        return ("Facebook says the token has expired or was revoked. Make a new one in Graph API Explorer "
                "and connect again (Settings → Platforms → Facebook).")
    if code in (10, 200) or "permission" in low:
        return ("Facebook says the token is missing a permission. When you make it in Graph API Explorer, tick "
                + ", ".join(REQUIRED_PERMISSIONS) + f". ({msg})")
    if code == 100 and "client_secret" in low:
        return "Facebook didn't accept the App secret. Copy it again from your app's Basic settings page."
    if code == 101 or "app id" in low:
        return "Facebook didn't recognise the App ID. Copy it again from your app's Basic settings page."
    if code in (4, 17, 32, 613):
        return "Facebook is limiting how fast this app can post. Wait a few minutes and try again."
    if code == 368:
        return f"Facebook blocked this post as against its rules: {msg}"
    return f"Facebook refused the request: {msg or f'HTTP {status}'}"


class FbClient:
    """Async client for one Facebook Page."""

    def __init__(self, page_token: str = "", page_id: str = "", transport: httpx.AsyncBaseTransport | None = None):
        self.page_token = (page_token or "").strip()
        self.page_id = (page_id or "").strip()
        self.page_name = ""
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(60.0, read=600.0), headers=_HEADERS,
                                       transport=transport or httpx.AsyncHTTPTransport(retries=2))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def close(self) -> None:
        await self._http.aclose()

    # -- plumbing -------------------------------------------------------------

    async def _call(self, method: str, url: str, **kw) -> dict:
        try:
            resp = await self._http.request(method, url, **kw)
        except httpx.HTTPError as e:
            raise FbError(f"Couldn't reach Facebook: {e}") from e
        try:
            body = resp.json() if resp.content else {}
        except ValueError:
            body = {}
        if resp.status_code >= 400 or (isinstance(body, dict) and "error" in body):
            text = plain_error(resp.status_code, body)
            logger.warning("FB: %s %s -> %s: %s", method, url.split("?")[0], resp.status_code, text)
            raise FbError(text)
        return body if isinstance(body, (dict, list)) else {}   # a batch call answers with a list

    def _tok(self, data: dict | None = None) -> dict:
        return {**(data or {}), "access_token": self.page_token}

    # -- connecting -----------------------------------------------------------

    async def exchange_token(self, user_token: str, app_id: str, app_secret: str) -> str:
        """Short-lived user token → long-lived user token (~60 days). The secret is
        sent to Facebook once and never kept or logged."""
        body = await self._call("GET", f"{_GRAPH}/oauth/access_token", params={
            "grant_type": "fb_exchange_token", "client_id": app_id.strip(),
            "client_secret": app_secret.strip(), "fb_exchange_token": user_token.strip()})
        tok = str(body.get("access_token") or "")
        if not tok:
            raise FbError("Facebook didn't hand back a long-lived token. Check the App ID and secret.")
        return tok

    async def list_pages(self, user_token: str) -> list[dict]:
        """Pages this user token can post to: [{id, name, access_token, can_post}]. A
        PAGE token pasted here is answered with that one Page."""
        try:
            body = await self._call("GET", f"{_GRAPH}/me/accounts", params={
                "fields": "id,name,access_token,tasks", "limit": 100, "access_token": user_token})
        except FbError as e:
            # A Page token has no /accounts edge: ask who it is instead.
            if "accounts" not in str(e).lower() and "nonexisting field" not in str(e).lower():
                raise
            me = await self._call("GET", f"{_GRAPH}/me", params={"fields": "id,name", "access_token": user_token})
            return [{"id": str(me.get("id", "")), "name": me.get("name", ""), "access_token": user_token,
                     "can_post": True}]
        pages = []
        for p in body.get("data") or []:
            tasks = p.get("tasks") or []
            pages.append({"id": str(p.get("id", "")), "name": p.get("name", ""),
                          "access_token": p.get("access_token", ""),
                          # CREATE_CONTENT is what posting needs; an empty list means Meta didn't say.
                          "can_post": (not tasks) or "CREATE_CONTENT" in tasks})
        return pages

    async def validate_session(self) -> str | None:
        """The Page's name when the token reads it, else None (expired / wrong token)."""
        if not self.page_token:
            return None
        try:
            me = await self._call("GET", f"{_GRAPH}/{self.page_id or 'me'}",
                                  params=self._tok({"fields": "id,name"}))
        except FbError as e:
            logger.info("FB: validate failed: %s", e)
            return None
        self.page_id = self.page_id or str(me.get("id", ""))
        self.page_name = str(me.get("name", ""))
        return self.page_name or None

    # -- posting --------------------------------------------------------------

    async def permalink(self, post_id: str) -> str:
        try:
            body = await self._call("GET", f"{_GRAPH}/{post_id}", params=self._tok({"fields": "permalink_url"}))
            return str(body.get("permalink_url") or "")
        except FbError:
            return f"https://www.facebook.com/{post_id}"

    async def post_text(self, message: str, link: str = "") -> dict:
        data = {"message": message}
        if link:
            data["link"] = link
        body = await self._call("POST", f"{_GRAPH}/{self.page_id}/feed", data=self._tok(data))
        pid = str(body.get("id") or "")
        return {"id": pid, "url": await self.permalink(pid)}

    async def _upload_photo(self, path: str, caption: str, published: bool) -> dict:
        mime = mimetypes.guess_type(path)[0] or "image/jpeg"
        with open(path, "rb") as fh:
            files = {"source": (os.path.basename(path), fh.read(), mime)}
        data = self._tok({"published": "true" if published else "false"})
        if caption and published:
            data["caption"] = caption
        return await self._call("POST", f"{_GRAPH}/{self.page_id}/photos", data=data, files=files)

    async def post_photos(self, paths: list[str], caption: str) -> dict:
        """One photo → a photo post. Several → each uploaded unpublished, then one feed
        post carrying them all."""
        paths = [p for p in paths if p]
        if not paths:
            raise FbError("No image to post")
        if len(paths) == 1:
            body = await self._upload_photo(paths[0], caption, published=True)
            pid = str(body.get("post_id") or body.get("id") or "")
            return {"id": pid, "url": await self.permalink(pid)}
        media = []
        for p in paths:
            body = await self._upload_photo(p, "", published=False)
            media.append(str(body.get("id") or ""))
        data = {"message": caption}
        for i, mid in enumerate(media):
            data[f"attached_media[{i}]"] = json.dumps({"media_fbid": mid})
        body = await self._call("POST", f"{_GRAPH}/{self.page_id}/feed", data=self._tok(data))
        pid = str(body.get("id") or "")
        return {"id": pid, "url": await self.permalink(pid)}

    async def post_video(self, path: str, title: str, description: str) -> dict:
        mime = mimetypes.guess_type(path)[0] or "video/mp4"
        with open(path, "rb") as fh:
            files = {"source": (os.path.basename(path), fh.read(), mime)}
        data = self._tok({"description": description})
        if title:
            data["title"] = title
        body = await self._call("POST", f"{_GRAPH_VIDEO}/{self.page_id}/videos", data=data, files=files)
        vid = str(body.get("id") or "")
        return {"id": vid, "url": f"https://www.facebook.com/{self.page_id}/videos/{vid}" if vid else ""}

    # -- stats + comments (spec 029, 4.59.0) -----------------------------------

    async def list_posts(self, limit: int = 50, after: str = "") -> tuple[list[dict], str]:
        """One page of the Page's posts, newest first → ([{id, created_time, permalink_url, message,
        media_type, target_id}], next_cursor or "")."""
        params = self._tok({"fields": "id,created_time,permalink_url,message,attachments{media_type,target{id}}",
                            "limit": max(1, min(int(limit), 100))})
        if after:
            params["after"] = after
        body = await self._call("GET", f"{_GRAPH}/{self.page_id}/posts", params=params)
        posts = []
        for p in body.get("data") or []:
            att = ((p.get("attachments") or {}).get("data") or [{}])[0]
            posts.append({"id": str(p.get("id") or ""), "created_time": p.get("created_time") or "",
                          "permalink_url": p.get("permalink_url") or "", "message": p.get("message") or "",
                          "media_type": str(att.get("media_type") or ""),
                          "target_id": str((att.get("target") or {}).get("id") or "")})
        nxt = ((body.get("paging") or {}).get("cursors") or {}).get("after", "") if (body.get("paging") or {}).get("next") else ""
        return posts, nxt

    async def post_stats(self, posts: list[dict]) -> tuple[dict[str, dict], str]:
        """Numbers for many posts in batched calls (≤ 50 requests each; two per post).

        ``posts`` = [{id, video: bool}] → ({post_id: {views, reactions, reactions_by_type, comments,
        shares, plays}}, missing_permission). A figure Facebook didn't give is None ("not available").
        Insights (views / reactions / plays) need ``read_insights``; when Facebook refuses them for a
        permission the counts still come back and the permission is named, so posting is never affected.
        A metric Facebook calls invalid is dropped for the rest of the call and retried without it.
        """
        out: dict[str, dict] = {}
        missing = ""
        bad: set[str] = set()
        for i in range(0, len(posts), 25):
            chunk = posts[i:i + 25]
            for attempt in range(3):
                reqs = []
                for p in chunk:
                    pid = p["id"]
                    reqs.append({"method": "GET", "relative_url":
                                 f"{pid}?fields=comments.summary(total_count).limit(0),shares,"
                                 f"reactions.summary(total_count).limit(0)"})
                    metrics = [m for m in ("post_media_view", "post_reactions_by_type_total")
                               + (("post_video_views",) if p.get("video") else ()) if m not in bad]
                    reqs.append({"method": "GET", "relative_url": f"{pid}/insights?metric={','.join(metrics)}"})
                body = await self._call("POST", "https://graph.facebook.com/v25.0/",
                                        data=self._tok({"batch": json.dumps(reqs), "include_headers": "false"}))
                answers = body if isinstance(body, list) else (body.get("data") if isinstance(body, dict) else None) or []
                retry = False
                for n, p in enumerate(chunk):
                    counts = _batch_json(answers, 2 * n)
                    ins = _batch_json(answers, 2 * n + 1)
                    row = {"views": None, "reactions": None, "reactions_by_type": {}, "comments": None,
                           "shares": None, "plays": None}
                    if "error" not in counts:
                        row["comments"] = _summary_count(counts.get("comments"))
                        row["shares"] = int((counts.get("shares") or {}).get("count") or 0)
                        row["reactions"] = _summary_count(counts.get("reactions"))
                    err = ins.get("error") if isinstance(ins, dict) else None
                    if err:
                        text = str(err.get("message") or "")
                        if err.get("code") in (10, 200) or "permission" in text.lower():
                            missing = missing or "read_insights"
                        elif err.get("code") == 100 and "metric" in text.lower():
                            for m in ("post_media_view", "post_reactions_by_type_total", "post_video_views"):
                                if m in text and m not in bad:
                                    bad.add(m)
                                    retry = True
                    else:
                        for d in ins.get("data") or []:
                            val = ((d.get("values") or [{}])[0] or {}).get("value")
                            name = d.get("name")
                            if name == "post_media_view" and isinstance(val, (int, float)):
                                row["views"] = int(val)
                            elif name == "post_video_views" and isinstance(val, (int, float)):
                                row["plays"] = int(val)
                            elif name == "post_reactions_by_type_total" and isinstance(val, dict):
                                row["reactions_by_type"] = {str(k): int(v) for k, v in val.items()
                                                            if isinstance(v, (int, float))}
                                row["reactions"] = sum(row["reactions_by_type"].values())
                    out[p["id"]] = row
                if not retry:
                    break
        return out, missing

    async def get_follower_count(self) -> int | None:
        """The Page's follower count (``capture_followers`` calls this)."""
        try:
            body = await self._call("GET", f"{_GRAPH}/{self.page_id}", params=self._tok({"fields": "followers_count"}))
        except FbError:
            return None
        val = body.get("followers_count")
        return int(val) if isinstance(val, (int, float)) else None

    async def get_comments(self, post_id: str, limit: int = 50) -> list[dict]:
        """Newest comments on one post → [{comment_id, author, author_id, body, commented_at, permalink,
        parent_id}]. Reading other people's comments needs ``pages_read_user_content``; a refusal raises
        FbPermissionError naming it."""
        try:
            body = await self._call("GET", f"{_GRAPH}/{post_id}/comments", params=self._tok({
                "fields": "id,from{id,name},message,created_time,parent{id},permalink_url",
                "order": "reverse_chronological", "filter": "stream", "limit": max(1, min(int(limit), 100))}))
        except FbError as e:
            if "permission" in str(e).lower():
                raise FbPermissionError("pages_read_user_content", str(e)) from e
            raise
        rows = []
        for c in body.get("data") or []:
            frm = c.get("from") or {}
            rows.append({"comment_id": str(c.get("id") or ""), "author": frm.get("name") or "Facebook user",
                         "author_id": str(frm.get("id") or ""), "body": c.get("message") or "",
                         "commented_at": c.get("created_time") or "", "permalink": c.get("permalink_url") or "",
                         "parent_id": str((c.get("parent") or {}).get("id") or "")})
        return rows

    async def reply_comment(self, comment_id: str, text: str) -> str:
        """Reply under a comment as the Page → the new comment's id. Needs ``pages_manage_engagement``."""
        try:
            body = await self._call("POST", f"{_GRAPH}/{comment_id}/comments", data=self._tok({"message": text}))
        except FbError as e:
            if "permission" in str(e).lower():
                raise FbPermissionError("pages_manage_engagement", str(e)) from e
            raise
        return str(body.get("id") or "")


class FbPermissionError(FbError):
    """Facebook refused for a missing permission; ``permission`` names the one to add."""

    def __init__(self, permission: str, detail: str = ""):
        self.permission = permission
        super().__init__(f"Add {permission} to your Meta app, then connect Facebook again "
                         f"(Settings → Platforms → Facebook, guide step 3). {detail}".strip())


def _batch_json(answers: list, idx: int) -> dict:
    """One answer of a Graph batch call → its JSON body ({} when missing or unreadable)."""
    try:
        a = answers[idx] or {}
        body = json.loads(a.get("body") or "{}")
        return body if isinstance(body, dict) else {}
    except (IndexError, ValueError, TypeError, AttributeError):
        return {}


def _summary_count(edge) -> int | None:
    try:
        return int(((edge or {}).get("summary") or {}).get("total_count"))
    except (TypeError, ValueError):
        return None
