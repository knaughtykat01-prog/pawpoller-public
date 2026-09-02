"""Extract one version's CHANGELOG.md section into RELEASE_NOTES.md, used as the
GitHub Release body by .github/workflows/build.yml.

Why: the repo commits directly to master (no PRs), so GitHub's
`generate_release_notes` yields only a bare "Full Changelog" compare link. The
CHANGELOG entry is the real, human-written summary — use it instead.

Usage:  python installer/changelog_extract.py <version>
Writes: RELEASE_NOTES.md  (in the CWD, i.e. the repo root on CI)
"""
import re
import sys
from pathlib import Path

ver = sys.argv[1].lstrip("v") if len(sys.argv) > 1 else ""

# The public distribution ships without CHANGELOG.md on purpose. Degrade to a
# minimal note so a tagged build still produces a release rather than failing
# the whole job on a missing input.
_src = Path("CHANGELOG.md")
if not _src.is_file():
    Path("RELEASE_NOTES.md").write_text(
        f"PawPoller {ver}\n\nSee the project README for what this release contains.\n",
        encoding="utf-8",
    )
    print(f"CHANGELOG.md not present - wrote a minimal RELEASE_NOTES.md for {ver!r}")
    raise SystemExit(0)

text = _src.read_text(encoding="utf-8")

# The section from "## [ver] …" up to the next "## [" (or EOF), minus the
# trailing "---" separator.
m = re.search(r"(?m)^##\s*\[" + re.escape(ver) + r"\][^\n]*\n(.*?)(?=^##\s*\[|\Z)",
              text, re.S) if ver else None
if m:
    body = re.sub(r"\n*-{3,}\s*$", "", m.group(1)).strip() + "\n"
else:
    body = f"PawPoller {ver or '(unknown version)'}. See CHANGELOG.md for details.\n"

Path("RELEASE_NOTES.md").write_text(body, encoding="utf-8")
print(f"Wrote RELEASE_NOTES.md ({len(body)} chars) for version '{ver}'")
