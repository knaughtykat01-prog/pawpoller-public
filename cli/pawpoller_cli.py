"""PawPoller TUI CLI.

A menu-driven terminal interface that talks to a PawPoller server over
HTTP. Same script runs locally (against the GCP VM) and on the VM
itself (against 127.0.0.1). All actions go through the same API the
web dashboard uses, so anything that's persisted via the dashboard is
identically reachable here.

Run:
    python pawpoller_cli.py              # interactive menu
    python pawpoller_cli.py setup        # (re)write config

Config resolution order:
    1. Env vars PAWPOLLER_URL + PAWPOLLER_KEY
    2. ~/.pawpoller-cli.json
    3. Local fallback: http://127.0.0.1:8420 + first API key read from
       the SQLite DB (PAWPOLLER_DB_PATH, default /app/data/pawpoller.db).

Dependencies: rich, httpx. Install with:
    pip install rich httpx
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

try:
    import httpx
    from rich import box
    from rich.console import Console
    from rich.live import Live
    from rich.panel import Panel
    from rich.prompt import Confirm, Prompt
    from rich.table import Table
    from rich.text import Text
except ImportError:
    print("Missing dependencies. Install with: pip install rich httpx")
    sys.exit(1)


console = Console()


# ── Constants ──────────────────────────────────────────────────────

PLATFORMS = ["ib", "fa", "ws", "sf", "sqw", "ao3", "da", "wp", "ik", "bsky", "tw", "mast", "tum", "pix", "thr", "ig", "e621"]
PLATFORM_LABELS = {
    "ib": "Inkbunny", "fa": "FurAffinity", "ws": "Weasyl", "sf": "SoFurry",
    "sqw": "SquidgeWorld", "ao3": "AO3", "da": "DeviantArt", "wp": "Wattpad",
    "ik": "Itaku", "bsky": "Bluesky", "tw": "X/Twitter", "mast": "Mastodon",
    "tum": "Tumblr", "pix": "Pixiv", "thr": "Threads", "ig": "Instagram",
    "e621": "e621",
}

POSTING_PLATFORMS = ["ib", "fa", "ws", "sf", "sqw", "ao3", "ik", "da", "bsky"]

CATEGORIES = [
    "infrastructure", "platforms-auth", "platforms-polling", "editor",
    "story-reader", "posting", "dashboard-auth", "external", "scheduling",
    "notifications", "archive", "pytest-suite",
]

CONFIG_PATH = Path.home() / ".pawpoller-cli.json"
# Local/self-host fallback: read the first API key straight from the DB when
# no env vars or config file are set. Override with PAWPOLLER_DB_PATH.
VM_DB_PATH = Path(os.environ.get("PAWPOLLER_DB_PATH", "/app/data/pawpoller.db"))
DEFAULT_LOCAL_URL = "http://127.0.0.1:8420"


# ── Config loading ─────────────────────────────────────────────────

@dataclass
class CLIConfig:
    base_url: str
    api_key: str
    source: str  # "env" | "file" | "vm-fallback"


def load_config() -> CLIConfig | None:
    env_url = os.environ.get("PAWPOLLER_URL", "").strip()
    env_key = os.environ.get("PAWPOLLER_KEY", "").strip()
    if env_url and env_key:
        return CLIConfig(env_url.rstrip("/"), env_key, "env")

    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if data.get("base_url") and data.get("api_key"):
                return CLIConfig(
                    data["base_url"].rstrip("/"), data["api_key"], "file"
                )
        except Exception:
            pass

    if VM_DB_PATH.exists():
        try:
            conn = sqlite3.connect(str(VM_DB_PATH))
            row = conn.execute(
                "SELECT key_hash FROM api_keys ORDER BY created_at LIMIT 1"
            ).fetchone()
            conn.close()
            if row:
                console.print(
                    "[yellow]VM fallback: the local DB only stores key "
                    "HASHES, not the plaintext key. You need to either "
                    "set PAWPOLLER_KEY env var or run [cyan]setup[/cyan] "
                    "to enter your key.[/yellow]"
                )
        except Exception:
            pass

    return None


def setup_config() -> CLIConfig:
    console.print(Panel.fit(
        "[bold]PawPoller CLI setup[/bold]\n\n"
        "Stores connection info to [cyan]" + str(CONFIG_PATH) + "[/cyan]",
        border_style="cyan",
    ))
    default_url = os.environ.get("PAWPOLLER_URL", "http://35.243.213.49:8420")
    url = Prompt.ask("Base URL", default=default_url).rstrip("/")
    key = Prompt.ask("API key (starts with [cyan]pp_[/cyan])", password=False)
    if not key.startswith("pp_"):
        console.print("[red]Warning:[/red] API keys typically start with 'pp_'")
    CONFIG_PATH.write_text(
        json.dumps({"base_url": url, "api_key": key}, indent=2),
        encoding="utf-8",
    )
    console.print(f"[green]Saved to {CONFIG_PATH}[/green]")
    return CLIConfig(url, key, "file")


# ── API client ─────────────────────────────────────────────────────

class API:
    def __init__(self, cfg: CLIConfig):
        self.cfg = cfg
        self.client = httpx.Client(
            base_url=cfg.base_url,
            headers={"Authorization": f"Bearer {cfg.api_key}"},
            timeout=30.0,
        )

    def get(self, path: str, **params) -> Any:
        r = self.client.get(path, params=params or None)
        r.raise_for_status()
        return r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text

    def post(self, path: str, **json_body) -> Any:
        r = self.client.post(path, json=json_body or None)
        r.raise_for_status()
        return r.json() if r.content else {}

    def put(self, path: str, **json_body) -> Any:
        r = self.client.put(path, json=json_body or None)
        r.raise_for_status()
        return r.json() if r.content else {}

    def delete(self, path: str, **params) -> Any:
        r = self.client.delete(path, params=params or None)
        r.raise_for_status()
        return r.json() if r.content else {}

    def stream(self, path: str):
        """Yield SSE events from path. Each event is a dict parsed from JSON."""
        with self.client.stream("GET", path, timeout=None) as r:
            r.raise_for_status()
            for raw in r.iter_lines():
                if not raw or not raw.startswith("data:"):
                    continue
                payload = raw[len("data:"):].strip()
                if not payload:
                    continue
                try:
                    yield json.loads(payload)
                except json.JSONDecodeError:
                    continue


# ── UI helpers ─────────────────────────────────────────────────────

def clear() -> None:
    console.clear()


def banner(api: API) -> None:
    """Top-of-screen status banner. Pulls live state from the API."""
    # Best-effort: any failures fall back to "unknown".
    parts: list[str] = []
    try:
        p = api.get("/api/poll/paused")
        parts.append(
            "[red]Polling: PAUSED[/red]" if p.get("paused")
            else "[green]Polling: live[/green]"
        )
    except Exception:
        parts.append("[dim]Polling: ?[/dim]")
    try:
        q = api.get("/api/posting/queue")
        items = q if isinstance(q, list) else q.get("items", [])
        pending = sum(1 for it in items if it.get("status") == "pending")
        parts.append(f"Queue: {pending} pending / {len(items)} total")
    except Exception:
        parts.append("[dim]Queue: ?[/dim]")
    try:
        a = api.get("/api/testing/active")
        if a.get("active"):
            parts.append("[yellow]Diagnostics: running[/yellow]")
        else:
            parts.append("Diagnostics: idle")
    except Exception:
        parts.append("[dim]Diagnostics: ?[/dim]")
    parts.append(f"[dim]{api.cfg.base_url}[/dim]")
    console.print(Panel(" · ".join(parts), border_style="cyan", padding=(0, 1)))


def numbered_menu(title: str, items: list[tuple[str, str, Callable]]) -> Callable | None:
    """Render a numbered action menu. Returns the chosen callable, or
    None for the back/quit action. `items` is a list of
    (label, description, callable) tuples.
    """
    table = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
    table.add_column("#", style="cyan", no_wrap=True)
    table.add_column("Action", style="bold")
    table.add_column("Description", style="dim")
    for i, (label, desc, _) in enumerate(items, 1):
        table.add_row(str(i), label, desc)
    table.add_row("q", "Back / Quit", "")
    console.print(Panel(table, title=title, title_align="left",
                        border_style="white"))
    while True:
        choice = Prompt.ask("Pick", default="q").strip().lower()
        if choice in ("q", ""):
            return None
        if choice.isdigit() and 1 <= int(choice) <= len(items):
            return items[int(choice) - 1][2]
        console.print(f"[red]Invalid choice: {choice}[/red]")


def pick_from(label: str, options: list[tuple[str, str]],
              allow_back: bool = True) -> str | None:
    """Render a numbered picker. `options` is [(value, display), ...].
    Returns the chosen value or None on back.
    """
    if not options:
        console.print(f"[yellow]No {label.lower()} available.[/yellow]")
        Prompt.ask("Press enter to continue")
        return None
    table = Table(box=box.SIMPLE, show_header=False)
    table.add_column("#", style="cyan", no_wrap=True)
    table.add_column(label)
    for i, (_, disp) in enumerate(options, 1):
        table.add_row(str(i), disp)
    if allow_back:
        table.add_row("q", "[dim]Back[/dim]")
    console.print(table)
    while True:
        choice = Prompt.ask("Pick", default="q" if allow_back else "1").strip().lower()
        if allow_back and choice in ("q", ""):
            return None
        if choice.isdigit() and 1 <= int(choice) <= len(options):
            return options[int(choice) - 1][0]
        console.print(f"[red]Invalid choice: {choice}[/red]")


def show_error(e: Exception) -> None:
    if isinstance(e, httpx.HTTPStatusError):
        try:
            detail = e.response.json().get("detail", e.response.text)
        except Exception:
            detail = e.response.text
        console.print(f"[red]HTTP {e.response.status_code}:[/red] {detail}")
    else:
        console.print(f"[red]Error:[/red] {e}")


def press_enter() -> None:
    Prompt.ask("\n[dim]Press enter to continue[/dim]", default="")


# ── Polling submenu ────────────────────────────────────────────────

def poll_status(api: API) -> None:
    try:
        progress = api.get("/api/poll/all-progress")
        paused = api.get("/api/poll/paused")
    except Exception as e:
        show_error(e); press_enter(); return
    table = Table(title="Per-platform poll status",
                  box=box.SIMPLE, header_style="bold")
    table.add_column("Platform")
    table.add_column("Phase")
    table.add_column("Progress")
    table.add_column("Last cycle")
    for plat, data in (progress.get("platforms", progress) or {}).items():
        if not isinstance(data, dict):
            continue
        phase = data.get("phase") or data.get("status") or "—"
        done = data.get("completed", 0)
        total = data.get("total", 0)
        prog = f"{done}/{total}" if total else "—"
        last = data.get("last_cycle_at") or data.get("last_completed") or "—"
        table.add_row(PLATFORM_LABELS.get(plat, plat), str(phase), prog, str(last))
    console.print(table)
    state = "[red]PAUSED[/red]" if paused.get("paused") else "[green]LIVE[/green]"
    console.print(f"\nOrchestrator: {state}")
    press_enter()


def poll_pause_toggle(api: API) -> None:
    try:
        state = api.get("/api/poll/paused")
    except Exception as e:
        show_error(e); press_enter(); return
    if state.get("paused"):
        if Confirm.ask("Polling is currently PAUSED. Resume?"):
            api.post("/api/poll/resume")
            console.print("[green]Polling resumed.[/green]")
    else:
        if Confirm.ask("Polling is currently LIVE. Pause?"):
            api.post("/api/poll/pause")
            console.print("[yellow]Polling paused.[/yellow]")
    press_enter()


def poll_trigger(api: API) -> None:
    plat = pick_from(
        "Trigger poll for",
        [(p, f"{PLATFORM_LABELS[p]} ({p})") for p in PLATFORMS],
    )
    if not plat:
        return
    try:
        api.post(f"/api/{plat}/poll/trigger")
        console.print(f"[green]Triggered {PLATFORM_LABELS[plat]} poll.[/green]")
    except Exception as e:
        show_error(e)
    press_enter()


def poll_full_resync(api: API) -> None:
    plat = pick_from(
        "Full resync (re-snapshot every submission) on",
        [(p, f"{PLATFORM_LABELS[p]} ({p})") for p in PLATFORMS],
    )
    if not plat:
        return
    if not Confirm.ask(
        f"Full resync hits {PLATFORM_LABELS[plat]} hard. Continue?",
        default=False,
    ):
        return
    try:
        api.post(f"/api/{plat}/poll/full-resync")
        console.print(f"[green]Full resync started on {PLATFORM_LABELS[plat]}.[/green]")
    except Exception as e:
        show_error(e)
    press_enter()


def polling_menu(api: API) -> None:
    while True:
        clear(); banner(api)
        fn = numbered_menu("Polling", [
            ("Status", "Per-platform progress + orchestrator state",
             lambda: poll_status(api)),
            ("Pause / Resume", "Toggle the orchestrator",
             lambda: poll_pause_toggle(api)),
            ("Trigger poll", "Run a single platform now",
             lambda: poll_trigger(api)),
            ("Full resync", "Re-snapshot every submission on a platform",
             lambda: poll_full_resync(api)),
        ])
        if fn is None:
            return
        fn()


# ── Publishing & queue submenu ─────────────────────────────────────

def queue_list(api: API) -> None:
    try:
        data = api.get("/api/posting/queue")
    except Exception as e:
        show_error(e); press_enter(); return
    items = data if isinstance(data, list) else data.get("items", [])
    if not items:
        console.print("[dim]Queue is empty.[/dim]"); press_enter(); return
    table = Table(title=f"Posting queue ({len(items)} items)",
                  box=box.SIMPLE, header_style="bold")
    table.add_column("#"); table.add_column("Story"); table.add_column("Ch")
    table.add_column("Plat"); table.add_column("Action"); table.add_column("Status")
    table.add_column("Scheduled")
    for it in items:
        status = it.get("status", "")
        cls = ("yellow" if status == "processing"
               else "red" if status == "failed"
               else "green" if status == "pending"
               else "white")
        table.add_row(
            str(it.get("queue_id", "")),
            (it.get("story_name") or "")[:30],
            str(it.get("chapter_index", 0)),
            it.get("platform", ""),
            it.get("action", ""),
            f"[{cls}]{status}[/{cls}]",
            (it.get("scheduled_at") or "—")[:19],
        )
    console.print(table)
    press_enter()


def queue_cancel(api: API) -> None:
    try:
        data = api.get("/api/posting/queue")
    except Exception as e:
        show_error(e); press_enter(); return
    items = data if isinstance(data, list) else data.get("items", [])
    if not items:
        console.print("[dim]Queue is empty.[/dim]"); press_enter(); return
    opts = [
        (str(it["queue_id"]),
         f"#{it['queue_id']} · {it.get('platform','')} · "
         f"{(it.get('story_name') or '')[:30]} ch{it.get('chapter_index',0)} · "
         f"{it.get('status','')}")
        for it in items
    ]
    qid = pick_from("Cancel which queue item?", opts)
    if not qid:
        return
    try:
        api.delete(f"/api/posting/queue/{qid}")
        console.print(f"[green]Cancelled queue item #{qid}.[/green]")
    except Exception as e:
        show_error(e)
    press_enter()


def _pick_story(api: API) -> str | None:
    try:
        data = api.get("/api/editor/stories")
    except Exception as e:
        show_error(e); press_enter(); return None
    stories = data.get("stories", data) if isinstance(data, dict) else data
    if not stories:
        console.print("[dim]No stories.[/dim]"); press_enter(); return None
    names = sorted(
        s.get("name", s) if isinstance(s, dict) else s for s in stories
    )
    return pick_from("Story", [(n, n) for n in names])


def _pick_chapter(api: API, story: str) -> int | None:
    try:
        content = api.get(f"/api/editor/stories/{story}/content")
    except Exception as e:
        show_error(e); press_enter(); return None
    chapters = content.get("chapters", []) or []
    opts: list[tuple[str, str]] = [("0", "0 — Full story")]
    for c in chapters:
        idx = c.get("number") or c.get("index") or 0
        title = c.get("title") or f"Chapter {idx}"
        opts.append((str(idx), f"{idx} — {title}"))
    pick = pick_from("Chapter", opts)
    return int(pick) if pick is not None else None


def _pick_platform(label: str = "Platform") -> str | None:
    return pick_from(label, [(p, f"{PLATFORM_LABELS[p]} ({p})")
                             for p in POSTING_PLATFORMS])


def publish_check(api: API) -> None:
    story = _pick_story(api)
    if not story:
        return
    try:
        m = api.get(f"/api/editor/stories/{story}/publish-check")
    except Exception as e:
        show_error(e); press_enter(); return
    table = Table(title=f"Publish matrix · {story}",
                  box=box.SIMPLE, header_style="bold")
    table.add_column("Chapter")
    for p in POSTING_PLATFORMS:
        table.add_column(p)
    for row in m.get("rows", []):
        ch_label = row.get("chapter_label") or f"ch{row.get('chapter_index',0)}"
        cells_row = [ch_label]
        for p in POSTING_PLATFORMS:
            cell = (row.get("cells") or {}).get(p, {})
            status = cell.get("status", "—")
            cls = ({
                "posted": "green", "posted_drifted": "yellow",
                "ready": "cyan", "ready_retry": "cyan",
                "blocked": "red", "no_credentials": "magenta",
                "deleted_upstream": "red",
            }.get(status, "white"))
            cells_row.append(f"[{cls}]{status}[/{cls}]")
        table.add_row(*cells_row)
    console.print(table)
    press_enter()


def publish_action(api: API) -> None:
    story = _pick_story(api)
    if not story:
        return
    plat = _pick_platform()
    if not plat:
        return
    chapter = _pick_chapter(api, story)
    if chapter is None:
        return
    action = pick_from("Action", [
        ("dry_run", "Dry run — preview package, no upload"),
        ("post", "Post — create new submission"),
        ("update", "Update — push fresh content to existing post"),
        ("update_metadata", "Update metadata only"),
    ])
    if not action:
        return
    draft = True
    if action != "dry_run":
        draft = Confirm.ask("Save as draft? (No = LIVE publish)", default=True)
        if not draft and not Confirm.ask(
            f"[red bold]LIVE PUBLISH[/red bold] {story} ch{chapter} to {plat}. Continue?",
            default=False,
        ):
            return
    try:
        resp = api.post(
            f"/api/editor/stories/{story}/publish",
            platform=plat, chapter=chapter, action=action,
            draft=draft, confirm_live=True,
        )
        if resp.get("ok"):
            console.print(f"[green]OK[/green]")
        else:
            console.print(f"[red]Failed:[/red] {resp.get('error') or resp}")
        url = (resp.get("publication") or {}).get("external_url")
        if url:
            console.print(f"URL: [cyan]{url}[/cyan]")
    except Exception as e:
        show_error(e)
    press_enter()


def schedule_action(api: API) -> None:
    story = _pick_story(api)
    if not story:
        return
    plat = _pick_platform()
    if not plat:
        return
    chapter = _pick_chapter(api, story)
    if chapter is None:
        return
    action = pick_from("Schedule what?", [
        ("post", "Post"), ("update", "Update"), ("update_metadata", "Metadata only"),
    ])
    if not action:
        return
    when = Prompt.ask(
        "When? ISO datetime UTC (e.g. 2026-05-14T03:00:00Z) — leave blank for +1h",
        default="",
    ).strip()
    if not when:
        when = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600)
        )
    try:
        resp = api.post(
            f"/api/editor/stories/{story}/schedule",
            platform=plat, chapter=chapter, action=action, scheduled_at=when,
        )
        console.print(
            f"[green]Scheduled[/green] queue #{resp.get('queue_id')} for {when}"
        )
    except Exception as e:
        show_error(e)
    press_enter()


def forget_publication(api: API) -> None:
    story = _pick_story(api)
    if not story:
        return
    plat = _pick_platform()
    if not plat:
        return
    chapter = _pick_chapter(api, story)
    if chapter is None:
        return
    typed = Prompt.ask(
        f"This DROPS PawPoller's memory of the {plat} publication for "
        f"{story} ch{chapter}. It does NOT touch the upstream.\n"
        f"Type [cyan]{plat}[/cyan] to confirm",
        default="",
    ).strip()
    if typed != plat:
        console.print("[yellow]Cancelled.[/yellow]"); press_enter(); return
    try:
        api.delete(
            f"/api/editor/stories/{story}/publication",
            platform=plat, chapter=chapter, confirm_platform=plat,
        )
        console.print("[green]Forgotten.[/green]")
    except Exception as e:
        show_error(e)
    press_enter()


def set_publication_url(api: API) -> None:
    story = _pick_story(api)
    if not story:
        return
    plat = _pick_platform()
    if not plat:
        return
    chapter = _pick_chapter(api, story)
    if chapter is None:
        return
    url = Prompt.ask("Paste the live submission URL").strip()
    if not url:
        return
    try:
        resp = api.put(
            f"/api/editor/stories/{story}/publication",
            platform=plat, chapter=chapter, url=url,
        )
        console.print(
            f"[green]Saved.[/green] external_id={resp.get('external_id')}"
        )
    except Exception as e:
        show_error(e)
    press_enter()


def publishing_menu(api: API) -> None:
    while True:
        clear(); banner(api)
        fn = numbered_menu("Publishing & Queue", [
            ("View queue", "All non-terminal queue rows",
             lambda: queue_list(api)),
            ("Cancel queue item", "Pick one and cancel",
             lambda: queue_cancel(api)),
            ("Publish matrix", "Per-platform status grid for a story",
             lambda: publish_check(api)),
            ("Post / update / dry-run", "Run a publish action on a cell",
             lambda: publish_action(api)),
            ("Schedule", "Queue a future publish",
             lambda: schedule_action(api)),
            ("Forget publication", "Clear PawPoller's row (no upstream call)",
             lambda: forget_publication(api)),
            ("Set URL manually", "Anchor PawPoller to a live submission URL",
             lambda: set_publication_url(api)),
        ])
        if fn is None:
            return
        fn()


# ── Diagnostics submenu ────────────────────────────────────────────

def diag_last_results(api: API) -> None:
    try:
        data = api.get("/api/testing/last-results")
    except Exception as e:
        show_error(e); press_enter(); return
    summary = data.get("summary", {}) or {}
    console.print(Panel(
        f"Passed: [green]{summary.get('passed',0)}[/green]  "
        f"Failed: [red]{summary.get('failed',0)}[/red]  "
        f"Skipped: [yellow]{summary.get('skipped',0)}[/yellow]  "
        f"Errored: [red]{summary.get('errored',0)}[/red]\n"
        f"Run at: {summary.get('finished_at') or summary.get('started_at') or '—'}",
        title="Last diagnostics run",
        border_style="cyan",
    ))
    results = data.get("results", []) or []
    fails = [r for r in results if r.get("status") in ("failed", "errored")]
    if fails:
        t = Table(title=f"{len(fails)} failures",
                  box=box.SIMPLE, header_style="bold red")
        t.add_column("Test"); t.add_column("Status"); t.add_column("Message")
        for r in fails[:30]:
            t.add_row(r.get("test_id", ""), r.get("status", ""),
                      (r.get("message") or "")[:80])
        console.print(t)
    press_enter()


def _stream_run(api: API, run_id: str, label: str) -> None:
    console.print(f"[dim]Streaming {label} (Ctrl-C to detach)…[/dim]\n")
    try:
        for ev in api.stream(f"/api/testing/stream/{run_id}"):
            kind = ev.get("event")
            if kind == "suite_start":
                console.print(
                    f"[cyan]Run started:[/cyan] {ev.get('total',0)} tests"
                )
            elif kind == "test_start":
                console.print(
                    f"  [dim]▶ {ev.get('idx','?')}/{ev.get('total','?')} "
                    f"{ev.get('test_id','')}[/dim]"
                )
            elif kind == "test_end":
                status = ev.get("status", "?")
                cls = ({"passed": "green", "failed": "red",
                        "errored": "red", "skipped": "yellow"}).get(status, "white")
                console.print(
                    f"    [{cls}]{status.upper()}[/{cls}] "
                    f"{ev.get('test_id','')} "
                    f"({ev.get('duration_ms',0):.0f}ms) "
                    f"{ev.get('message','')[:80]}"
                )
            elif kind == "log":
                lvl = ev.get("level", "info")
                if lvl in ("error", "warning"):
                    cls = "red" if lvl == "error" else "yellow"
                    console.print(
                        f"      [{cls}]{lvl}:[/{cls}] {ev.get('message','')[:120]}"
                    )
            elif kind == "suite_complete":
                s = ev.get("summary", {})
                console.print(
                    f"\n[bold]Run complete:[/bold] "
                    f"[green]{s.get('passed',0)} passed[/green] · "
                    f"[red]{s.get('failed',0)} failed[/red] · "
                    f"[yellow]{s.get('skipped',0)} skipped[/yellow] "
                    f"in {s.get('duration_ms',0)/1000:.1f}s"
                )
                return
    except KeyboardInterrupt:
        console.print("\n[yellow]Detached (run continues on server)[/yellow]")
    except Exception as e:
        show_error(e)


def diag_run_one(api: API) -> None:
    try:
        tests = api.get("/api/testing/tests")
    except Exception as e:
        show_error(e); press_enter(); return
    items = tests.get("tests", tests) if isinstance(tests, dict) else tests
    opts = [(t["test_id"], f"{t['test_id']:55s} {t.get('name','')}")
            for t in sorted(items, key=lambda x: x["test_id"])]
    tid = pick_from("Test", opts)
    if not tid:
        return
    try:
        resp = api.post(f"/api/testing/run/{tid}")
        run_id = resp.get("run_id")
        if run_id:
            _stream_run(api, run_id, tid)
    except Exception as e:
        show_error(e)
    press_enter()


def diag_run_category(api: API) -> None:
    cat = pick_from("Category", [(c, c) for c in CATEGORIES])
    if not cat:
        return
    try:
        resp = api.post(f"/api/testing/run-category/{cat}")
        run_id = resp.get("run_id")
        if run_id:
            _stream_run(api, run_id, cat)
    except Exception as e:
        show_error(e)
    press_enter()


def diag_run_suite(api: API) -> None:
    if not Confirm.ask("Run the full diagnostics suite?", default=True):
        return
    try:
        resp = api.post("/api/testing/run-suite")
        run_id = resp.get("run_id")
        if run_id:
            _stream_run(api, run_id, "suite")
    except Exception as e:
        show_error(e)
    press_enter()


def diag_attach_active(api: API) -> None:
    try:
        a = api.get("/api/testing/active")
    except Exception as e:
        show_error(e); press_enter(); return
    if not a.get("active"):
        console.print("[dim]No active run.[/dim]"); press_enter(); return
    _stream_run(api, a["run_id"], "active run")
    press_enter()


def diagnostics_menu(api: API) -> None:
    while True:
        clear(); banner(api)
        fn = numbered_menu("Diagnostics", [
            ("Last results", "Summary + failures from the last run",
             lambda: diag_last_results(api)),
            ("Run one test", "Pick a single test and stream",
             lambda: diag_run_one(api)),
            ("Run a category", "All tests in one category",
             lambda: diag_run_category(api)),
            ("Run full suite", "Every non-destructive test",
             lambda: diag_run_suite(api)),
            ("Attach to active run", "Watch an in-progress suite",
             lambda: diag_attach_active(api)),
        ])
        if fn is None:
            return
        fn()


# ── Stories submenu ────────────────────────────────────────────────

def stories_list(api: API) -> None:
    try:
        data = api.get("/api/editor/stories")
    except Exception as e:
        show_error(e); press_enter(); return
    stories = data.get("stories", data) if isinstance(data, dict) else data
    table = Table(title=f"{len(stories)} stories",
                  box=box.SIMPLE, header_style="bold")
    table.add_column("Name"); table.add_column("Chapters"); table.add_column("Words")
    for s in sorted(stories, key=lambda x: (x.get("name") if isinstance(x, dict) else x).lower()):
        if isinstance(s, dict):
            table.add_row(
                s.get("name", "?"),
                str(s.get("total_chapters", "—")),
                str(s.get("total_words", "—")),
            )
        else:
            table.add_row(str(s), "—", "—")
    console.print(table)
    press_enter()


def stories_regen_one(api: API) -> None:
    story = _pick_story(api)
    if not story:
        return
    include_pdf = Confirm.ask("Include PDF? (slow)", default=False)
    if not Confirm.ask(f"Regenerate {story}?", default=True):
        return
    try:
        resp = api.post(
            f"/api/editor/stories/{story}/regenerate",
            include_pdf=include_pdf,
        )
        if resp.get("ok"):
            console.print(
                f"[green]Regen complete.[/green] "
                f"Files: {resp.get('files_written', '?')}"
            )
        else:
            console.print(
                f"[red]Failed:[/red] {resp.get('error') or resp}"
            )
    except Exception as e:
        show_error(e)
    press_enter()


def _stream_regen_all(api: API, run_id: str) -> None:
    console.print(f"[dim]Streaming bulk regen {run_id} (Ctrl-C to detach)…[/dim]\n")
    try:
        for ev in api.stream(f"/api/editor/regenerate-all/stream/{run_id}"):
            kind = ev.get("event")
            if kind == "story_start":
                console.print(
                    f"  [cyan]▶[/cyan] {ev.get('idx','?')}/{ev.get('total','?')} "
                    f"{ev.get('story_name','')}"
                )
            elif kind == "story_end":
                status = ev.get("status", "?")
                cls = ({"ok": "green", "partial": "yellow",
                        "failed": "red"}).get(status, "white")
                console.print(
                    f"    [{cls}]{status.upper()}[/{cls}] "
                    f"{ev.get('story_name','')} "
                    f"({ev.get('duration_ms',0)/1000:.1f}s)"
                )
            elif kind == "log":
                msg = ev.get("message", "")
                if msg:
                    console.print(f"      [dim]{msg[:120]}[/dim]")
            elif kind == "complete":
                s = ev.get("summary", {})
                console.print(
                    f"\n[bold]Bulk regen complete:[/bold] "
                    f"[green]{s.get('ok',0)} OK[/green] · "
                    f"[yellow]{s.get('partial',0)} partial[/yellow] · "
                    f"[red]{s.get('failed',0)} failed[/red] "
                    f"in {s.get('duration_ms',0)/1000:.1f}s"
                )
                return
    except KeyboardInterrupt:
        console.print("\n[yellow]Detached (regen continues on server)[/yellow]")
    except Exception as e:
        show_error(e)


def stories_regen_all(api: API) -> None:
    skip_pdf = Confirm.ask("Skip PDF? (much faster)", default=True)
    if not Confirm.ask(
        "[bold]Regenerate ALL stories from MASTER.md?[/bold]",
        default=False,
    ):
        return
    try:
        resp = api.post("/api/editor/regenerate-all", skip_pdf=skip_pdf)
        run_id = resp.get("run_id")
        if run_id:
            _stream_regen_all(api, run_id)
    except Exception as e:
        show_error(e)
    press_enter()


def stories_attach_regen(api: API) -> None:
    try:
        a = api.get("/api/editor/regenerate-all/active")
    except Exception as e:
        show_error(e); press_enter(); return
    if not a.get("active"):
        console.print("[dim]No active regen.[/dim]"); press_enter(); return
    _stream_regen_all(api, a["run_id"])
    press_enter()


def stories_probe_drafts(api: API) -> None:
    story = _pick_story(api)
    if not story:
        return
    if not Confirm.ask(
        "Probe drafts/submissions for this story on every platform? "
        "(makes 9+ HTTP calls)",
        default=True,
    ):
        return
    try:
        resp = api.post(f"/api/editor/stories/{story}/probe-drafts")
        results = resp.get("results", {}) or {}
        t = Table(title=f"Draft probe · {story}",
                  box=box.SIMPLE, header_style="bold")
        t.add_column("Platform"); t.add_column("Found"); t.add_column("Detail")
        for plat, info in results.items():
            t.add_row(
                plat,
                "✓" if info.get("found") else "✗",
                str(info.get("message") or info.get("count") or "")[:80],
            )
        console.print(t)
    except Exception as e:
        show_error(e)
    press_enter()


def stories_menu(api: API) -> None:
    while True:
        clear(); banner(api)
        fn = numbered_menu("Stories", [
            ("List", "Every story with chapter/word counts",
             lambda: stories_list(api)),
            ("Regenerate one", "Rebuild all formats from MASTER.md",
             lambda: stories_regen_one(api)),
            ("Regenerate all", "Bulk regen with live progress",
             lambda: stories_regen_all(api)),
            ("Attach to active regen", "Watch an in-progress bulk regen",
             lambda: stories_attach_regen(api)),
            ("Publish matrix", "Per-cell status grid",
             lambda: publish_check(api)),
            ("Probe drafts", "Ask every platform what they have for this story",
             lambda: stories_probe_drafts(api)),
        ])
        if fn is None:
            return
        fn()


# ── Settings & status submenu ──────────────────────────────────────

def settings_ping(api: API) -> None:
    t0 = time.time()
    try:
        api.get("/api/health")
        console.print(
            f"[green]Pong[/green] ({(time.time()-t0)*1000:.0f}ms) "
            f"@ {api.cfg.base_url}"
        )
    except Exception as e:
        show_error(e)
    press_enter()


def settings_view(api: API) -> None:
    try:
        s = api.get("/api/posting/settings")
    except Exception as e:
        show_error(e); press_enter(); return
    table = Table(title="Posting settings", box=box.SIMPLE, header_style="bold")
    table.add_column("Key"); table.add_column("Value")
    for k, v in sorted(s.items()):
        sv = str(v)
        if len(sv) > 60:
            sv = sv[:57] + "..."
        table.add_row(k, sv)
    console.print(table)
    press_enter()


def settings_apikeys(api: API) -> None:
    try:
        data = api.get("/api/auth/api-keys")
    except Exception as e:
        show_error(e); press_enter(); return
    keys = data.get("keys", data) if isinstance(data, dict) else data
    table = Table(title=f"API keys ({len(keys)})",
                  box=box.SIMPLE, header_style="bold")
    table.add_column("Prefix"); table.add_column("Label"); table.add_column("Created")
    table.add_column("Last used")
    for k in keys:
        table.add_row(
            k.get("prefix", ""), k.get("label", ""),
            k.get("created_at", "")[:19], (k.get("last_used_at") or "—")[:19],
        )
    console.print(table)
    press_enter()


def settings_show_config(api: API) -> None:
    console.print(Panel(
        f"Base URL: [cyan]{api.cfg.base_url}[/cyan]\n"
        f"API key:  pp_…{api.cfg.api_key[-6:]}\n"
        f"Source:   {api.cfg.source}\n"
        f"File:     {CONFIG_PATH}",
        title="Current CLI config",
        border_style="cyan",
    ))
    press_enter()


def settings_reconfigure(api: API) -> None:
    setup_config()
    console.print("[yellow]Restart the CLI to pick up the new config.[/yellow]")
    press_enter()


def settings_menu(api: API) -> None:
    while True:
        clear(); banner(api)
        fn = numbered_menu("Settings & Status", [
            ("Ping", "Verify connection + measure latency",
             lambda: settings_ping(api)),
            ("View settings", "Read /api/posting/settings",
             lambda: settings_view(api)),
            ("API keys", "List keys (prefixes only)",
             lambda: settings_apikeys(api)),
            ("Show current config", "Where this CLI is connecting",
             lambda: settings_show_config(api)),
            ("Re-run setup", "Rewrite ~/.pawpoller-cli.json",
             lambda: settings_reconfigure(api)),
        ])
        if fn is None:
            return
        fn()


# ── Main loop ──────────────────────────────────────────────────────

def main_menu(api: API) -> None:
    while True:
        clear()
        console.print(Panel.fit(
            "[bold magenta]PawPoller CLI[/bold magenta]\n"
            "[dim]Menu-driven control of the dashboard API[/dim]",
            border_style="magenta",
        ))
        banner(api)
        fn = numbered_menu("Main menu", [
            ("Polling",             "Pause/resume, trigger, status",
             lambda: polling_menu(api)),
            ("Publishing & Queue",  "Queue ops, publish/schedule, forget, set URL",
             lambda: publishing_menu(api)),
            ("Diagnostics",         "Run tests, stream, last results",
             lambda: diagnostics_menu(api)),
            ("Stories",             "List, regen, matrix, probe drafts",
             lambda: stories_menu(api)),
            ("Settings & Status",   "Ping, view settings, API keys, reconfigure",
             lambda: settings_menu(api)),
        ])
        if fn is None:
            if Confirm.ask("[yellow]Quit?[/yellow]", default=True):
                return
            continue
        fn()


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "setup":
        setup_config()
        return 0

    cfg = load_config()
    if cfg is None:
        console.print(
            "[yellow]No config found. Running first-time setup.[/yellow]\n"
        )
        cfg = setup_config()

    api = API(cfg)
    # Smoke-test the connection so we fail fast on bad URL/key.
    try:
        api.get("/api/health")
    except Exception as e:
        console.print(f"[red]Cannot reach API:[/red]")
        show_error(e)
        if Confirm.ask("Re-run setup?", default=True):
            cfg = setup_config()
            api = API(cfg)
        else:
            return 1

    try:
        main_menu(api)
    except KeyboardInterrupt:
        console.print("\n[dim]Bye.[/dim]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
