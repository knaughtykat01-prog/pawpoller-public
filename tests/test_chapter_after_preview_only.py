"""Test: does just the preview POST add the chapter to the draft?

Or do we need the save_button follow-up step? Critical question.

  - Creates draft (1 chapter)
  - POSTs chapter 2 with preview_button (NO follow-up)
  - Checks chapter count via get_chapter_ids
  - Checks if work is still in drafts
  - Deletes test work
"""
import asyncio, sys, re
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from clients.sqw.client import SquidgeWorldClient


async def main() -> int:
    settings = config.get_settings()
    client = SquidgeWorldClient(
        settings.get("sqw_author_username") or settings.get("sqw_username"),
        settings.get("sqw_author_password") or settings.get("sqw_password"),
        settings.get("sqw_target_user", ""),
    )
    await client.ensure_logged_in()

    work_id = None
    try:
        # 1. Create draft
        print("Creating test draft...")
        result = await client.create_work(
            title="DELETE ME — Preview Only Test",
            content="<p>Test chapter 1.</p>",
            fandom="Original Work",
            rating="General Audiences",
            warnings=["No Archive Warnings Apply"],
            categories=["Gen"],
            additional_tags="test",
            summary="Test.",
            chapter_title="Chapter 1",
        )
        work_id = result["work_id"]
        print(f"  Created {work_id}")

        # Verify draft
        print(f"  Initially: in_drafts={await client.is_work_in_drafts(work_id)}, in_published={await client.is_work_published(work_id)}")
        ids = await client.get_chapter_ids(work_id)
        print(f"  Initial chapter count: {len(ids)}")

        # 2. POST chapter 2 with ONLY preview_button (no follow-up)
        print()
        print("POSTing chapter 2 with preview_button only (no follow-up)...")
        form_resp = await client._http.get(f"https://www.squidgeworld.org/works/{work_id}/chapters/new")
        form_html = form_resp.text
        token = re.search(r'name="authenticity_token"[^>]*value="([^"]+)"', form_html).group(1)
        pseud = re.search(
            r'<input[^>]*value="(\d+)"[^>]*name="chapter\[author_attributes\]\[ids\]\[\]"',
            form_html,
        ).group(1)

        post_data = [
            ("authenticity_token", token),
            ("chapter[author_attributes][ids][]", pseud),
            ("chapter[title]", "Chapter 2"),
            ("chapter[summary]", ""),
            ("chapter[notes]", ""),
            ("chapter[endnotes]", ""),
            ("chapter[content]", "<p>Test chapter 2 content.</p>"),
            ("chapter[position]", "2"),
            ("preview_button", "Preview"),
        ]
        resp = await client._http.post(
            f"https://www.squidgeworld.org/works/{work_id}/chapters",
            content=urlencode(post_data, doseq=True),
            headers={
                "Referer": f"https://www.squidgeworld.org/works/{work_id}/chapters/new",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=60.0,
        )
        print(f"  POST returned status={resp.status_code} url={resp.url}")

        # Wait a beat
        await asyncio.sleep(2)

        # 3. Check state
        print()
        print("After preview POST:")
        in_drafts = await client.is_work_in_drafts(work_id)
        in_pub = await client.is_work_published(work_id)
        print(f"  in_drafts:    {in_drafts}")
        print(f"  in_published: {in_pub}")

        ids = await client.get_chapter_ids(work_id)
        print(f"  chapter count: {len(ids)}")
        for ch in ids:
            print(f"    index={ch.get('index', '?')} id={ch.get('chapter_id', '?')}")

        if in_pub:
            print()
            print("[CRITICAL] Work was PUBLISHED by the preview POST!")
            return 1
        if not in_drafts:
            print()
            print("[CRITICAL] Work is no longer a draft!")
            return 1
        if len(ids) >= 2:
            print()
            print("[OK] Preview POST alone added the chapter and kept the work as draft.")
            print("      No save_button follow-up needed.")
            return 0
        else:
            print()
            print(f"[FAIL] Preview POST did NOT add the chapter (count={len(ids)}).")
            return 1

    finally:
        if work_id:
            print()
            print(f"[CLEANUP] Deleting {work_id}...")
            try:
                await client.delete_work(work_id)
                print("[CLEANUP] Deleted")
            except Exception as e:
                print(f"[CLEANUP] FAILED: {e}")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
