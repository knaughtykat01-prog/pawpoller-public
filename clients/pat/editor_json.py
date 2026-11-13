"""PawPoller's own small HTML → Patreon's editor JSON (spec 084, research R1 step 6). Fixed rules, stdlib only.

Patreon keeps a post's body only as ``content_json_string`` (its editor's ProseMirror-style JSON); HTML sent alone
reads back empty. Whatever sits ABOVE the ``paywallBreakpoint`` node is the free teaser, so for a locked post the
paywall node goes first and there is no teaser (FR-SEC-010). The node is written here in the shape Patreon's own
editor sends (captured 13 Nov); Patreon stores the body split at it and keeps no node.

Rules (FR-075, the node shapes b7 saw Patreon's editor store on 13 Nov): ``<p>`` / ``<div>`` and other blocks end a
paragraph; ``<br>`` starts a new one; headings become bold paragraphs (Patreon's heading node isn't known); ``<ul>`` /
``<ol>`` / ``<li>`` become ``bulletList`` / ``orderedList`` / ``listItem`` (a list inside a list joins its parent);
``<blockquote>`` a ``blockquote``; ``<hr>`` a ``horizontalRule``; ``<center>`` or ``text-align: center`` a centred
paragraph. Marks: bold, italic, underline (``u``/``ins``), strike (``s``/``strike``/``del``) and ``link`` for an
``http``/``https`` address only (FR-SEC-017); any other link keeps just its text. Any other tag is dropped to its text.
Text with no tags at all is split into paragraphs on its line breaks. The same input always gives the same JSON.
"""
from __future__ import annotations

import json
import re
from html.parser import HTMLParser

_PARAS = {"p", "div", "section", "article", "pre", "table", "tr", "dd", "dt", "figure", "figcaption", "address"}
_HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_MARK_TAGS = {"strong": "bold", "b": "bold", "em": "italic", "i": "italic", "u": "underline", "ins": "underline",
              "s": "strike", "strike": "strike", "del": "strike"}
_MARK_ORDER = ("bold", "italic", "underline", "strike")
_SKIP = {"script", "style", "head", "title", "template", "noscript"}
_WS = re.compile(r"\s+")
_CENTRE = re.compile(r"text-align\s*:\s*center", re.I)
_TAG = re.compile(r"</?[A-Za-z][^>]*>")
_PARA_ATTRS = {"nodeIndent": None, "nodeTextAlignment": None, "nodeLineHeight": None, "style": ""}


def safe_href(href: str | None) -> str:
    """The address when it is an absolute http(s) one, else "" (FR-SEC-017)."""
    h = (href or "").strip()
    return h if re.match(r"(?i)https?://[^\s<>\"']+$", h) else ""


class _Builder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[dict] = []
        self.containers: list[list] = [self.out]   # where finished blocks go (top level, a quote, a list item)
        self.lists: list[dict] = []                 # open list nodes; a nested one joins the outermost
        self.stack: list[tuple[str, str, object]] = []   # (tag, what it opened, its node) to undo on the end tag
        self.cur: list[tuple[str, tuple]] = []
        self.marks = {m: 0 for m in _MARK_ORDER}
        self.links: list[str] = []
        self.centre = 0
        self.skip = 0

    def _marks(self) -> tuple:
        out = tuple(m for m in _MARK_ORDER if self.marks[m])
        live = [h for h in self.links if h]
        return out + ((("link", live[-1]),) if live else ())

    def _end(self):
        if any(t.strip() for t, _ in self.cur):
            self.containers[-1].append(_paragraph(self.cur, centred=self.centre > 0))
        self.cur = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in _SKIP:
            self.skip += 1
            self.stack.append((tag, "skip", None))
            return
        if tag == "br":
            self._end()
            return
        if tag == "hr":
            self._end()
            self.containers[-1].append({"type": "horizontalRule"})
            return
        if tag in _MARK_TAGS:
            self.marks[_MARK_TAGS[tag]] += 1
            self.stack.append((tag, "mark", _MARK_TAGS[tag]))
            return
        if tag == "a":
            self.links.append(safe_href(a.get("href")))
            self.stack.append((tag, "link", None))
            return
        if tag in ("ul", "ol"):
            self._end()
            if self.lists:                                   # nested: its items join the outer list
                self.stack.append((tag, "flat", None))
                return
            node = ({"type": "orderedList", "attrs": {"order": 1}, "content": []} if tag == "ol"
                    else {"type": "bulletList", "content": []})
            self.containers[-1].append(node)
            self.lists.append(node)
            self.stack.append((tag, "list", (node, self.containers[-1])))
            return
        if tag == "li":
            self._end()
            if not self.lists:
                self.stack.append((tag, "para", None))
                return
            item = {"type": "listItem", "attrs": {"closed": False, "nested": False}, "content": []}
            self.lists[-1]["content"].append(item)
            self.containers.append(item["content"])
            self.stack.append((tag, "item", item))
            return
        if tag == "blockquote":
            self._end()
            node = {"type": "blockquote", "content": []}
            self.containers[-1].append(node)
            self.containers.append(node["content"])
            self.stack.append((tag, "quote", node))
            return
        if tag in _HEADINGS:
            self._end()
            self.marks["bold"] += 1
            centred = int(bool(_CENTRE.search(a.get("style") or "") or (a.get("align") or "").lower() == "center"))
            self.centre += centred
            self.stack.append((tag, "heading", centred))
            return
        if tag == "center" or (tag in _PARAS and (_CENTRE.search(a.get("style") or "")
                                                   or (a.get("align") or "").lower() == "center")):
            self._end()
            self.centre += 1
            self.stack.append((tag, "centre", None))
            return
        if tag in _PARAS:
            self._end()
            self.stack.append((tag, "para", None))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in ("br", "hr"):
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if not any(t == tag for t, _, _ in self.stack):
            return                                           # a stray end tag changes nothing
        while self.stack:
            t, kind, node = self.stack.pop()
            self._undo(kind, node)
            if t == tag:
                break

    def _undo(self, kind, node):
        if kind == "skip":
            self.skip = max(self.skip - 1, 0)
        elif kind == "mark":
            self.marks[node] = max(self.marks[node] - 1, 0)
        elif kind == "link":
            if self.links:
                self.links.pop()
        elif kind == "heading":
            self._end()
            self.marks["bold"] = max(self.marks["bold"] - 1, 0)
            self.centre = max(self.centre - node, 0)
        elif kind == "centre":
            self._end()
            self.centre = max(self.centre - 1, 0)
        elif kind in ("para", "flat"):
            self._end()
        elif kind in ("item", "quote"):
            self._end()
            self.containers.pop()
            if not node["content"]:
                node["content"].append({"type": "paragraph"})   # Patreon's editor never stores an empty one
        elif kind == "list":
            self._end()
            lst, parent = node
            self.lists.pop()
            if not lst["content"] and lst in parent:
                parent.remove(lst)

    def handle_data(self, data):
        if not self.skip and data:
            self.cur.append((data, self._marks()))

    def finish(self):
        self.close()
        while self.stack:
            _, kind, node = self.stack.pop()
            self._undo(kind, node)
        self._end()
        return self.out


def _mark(m) -> dict:
    if isinstance(m, tuple):
        return {"type": "link", "attrs": {"href": m[1], "target": "_blank", "auto": False}}
    return {"type": m}


def _paragraph(runs, centred: bool = False) -> dict:
    """Join runs, collapse whitespace, merge neighbours with the same marks; strip the paragraph's ends."""
    merged: list[list] = []
    for text, marks in runs:
        text = _WS.sub(" ", text)
        if merged and merged[-1][1] == marks:
            merged[-1][0] += text
        else:
            merged.append([text, marks])
    if merged:
        merged[0][0] = merged[0][0].lstrip()
        merged[-1][0] = merged[-1][0].rstrip()
    content = []
    for text, marks in merged:
        if not text:
            continue
        node = {"type": "text", "text": text}
        if marks:
            node["marks"] = [_mark(m) for m in marks]
        content.append(node)
    para: dict[str, object] = {"type": "paragraph", "content": content} if content else {"type": "paragraph"}
    if centred:
        para["attrs"] = {**_PARA_ATTRS, "nodeTextAlignment": "center"}
    return para


def paragraphs(html: str) -> list[dict]:
    """The body's block nodes (paragraphs, lists, quotes, dividers)."""
    html = html or ""
    if not _TAG.search(html):          # "<3" in a plain description isn't a tag: keep its line breaks
        return [_paragraph([(line, ())]) for line in html.splitlines() if line.strip()]
    b = _Builder()
    b.feed(html)
    return b.finish()


def paywall_node(currency: str, post_id: str) -> dict:
    """The node Patreon's own editor sends first in a locked post (captured 13 Nov). Patreon doesn't store it: it
    splits the body there and keeps what sits above as the free preview, so first means no preview."""
    return {"type": "paywallBreakpoint", "attrs": {"paywallBreakpointCtaProps": {
        "currencyCode": (currency or "USD").upper()[:3], "isMonetized": False, "isPaidAccessSelected": True,
        "isPaidMembersSelected": True, "showCtas": False, "postId": str(post_id)}}}


def is_empty_doc(json_string: str | None) -> bool:
    """True only for a real editor document holding nothing but empty paragraphs (the stored free preview of a
    locked post). Missing, unreadable, any text, any picture or other node → False."""
    try:
        doc = json.loads(json_string or "")
    except (TypeError, ValueError):
        return False
    if not isinstance(doc, dict) or doc.get("type") != "doc":
        return False

    def empty(node) -> bool:
        if not isinstance(node, dict) or node.get("type") not in ("doc", "paragraph"):
            return False
        return all(empty(n) for n in node.get("content") or [])
    return empty(doc)


def picture_node(src: str, media_id: str, post_id: str, caption: str = "", alt: str = "") -> dict:
    """An in-text picture as Patreon's editor sends it (captured 13 Nov); Patreon stores it as an ``image`` node."""
    return {"type": "customImage", "attrs": {"src": src, "caption": caption, "postId": str(post_id), "alt": alt,
                                             "customLink": None, "data-media-id": str(media_id),
                                             "data-alignment": "center"}}


def to_editor_json(html: str, *, paywall: dict | None, lead: list[dict] | None = None) -> str:
    """The document for ``content_json_string``. ``paywall`` (``paywall_node``) goes first for a locked post, so
    nothing is a free preview; None for a public post. ``lead`` (in-text pictures) comes next, then the text."""
    content = ([paywall] if paywall else []) + list(lead or []) + paragraphs(html)
    if len(content) == (1 if paywall else 0):
        content.append({"type": "paragraph"})
    return json.dumps({"type": "doc", "content": content}, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
