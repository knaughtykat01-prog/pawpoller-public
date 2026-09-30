#!/usr/bin/env bash
# One-command PawPoller server updater.  Run it from the repo:  ./update.sh
#
# Updates a running Docker Compose server to the latest release. It figures out
# on its own whether you built from source or run the prebuilt image, backs up
# the database, refreshes the checkout, updates the container the matching way,
# then checks the new version really started cleanly. Your data (settings,
# database, logs) lives in named volumes and is untouched.
#
#   ./update.sh                update now
#   ./update.sh --to <commit>  go to one exact commit instead (use it to roll back)
#   ./update.sh --check        say whether a newer version exists; change nothing
#   ./update.sh --quiet        only print on a change or an error (for cron/systemd)
#   ./update.sh --if-requested update only if the dashboard asked (used by the host agent)
#   ./update.sh --help         this help
#
# Every update first copies the database to data/backups/pre-update-*.db inside
# the data volume (the newest 5 are kept). After it, the script waits for the
# app, checks it is running the commit it just built, checks the database, and
# reads the startup log: a crash or a damaged database fails the update and
# prints the exact command to roll back.
#
# Run it as the user who owns the checkout, or with sudo: as root it still does
# the git work as the checkout's owner, so the repo never ends up root-owned.
#
# Env: PAWPOLLER_PORT (default 8420) — the host port the server answers on.
set -euo pipefail

cd "$(cd "$(dirname "$0")" && pwd)"   # repo root (this script lives here)

PORT="${PAWPOLLER_PORT:-8420}"
HEALTH="http://127.0.0.1:${PORT}/api/health"
LOCK="${TMPDIR:-/tmp}/pawpoller-update.lock"
QUIET=0; MODE_CHECK=0; IF_REQUESTED=0; TARGET=""

while [ $# -gt 0 ]; do
  case "$1" in
    --quiet|-q)     QUIET=1 ;;
    --check)        MODE_CHECK=1 ;;
    --if-requested) IF_REQUESTED=1 ;;
    --to)           shift; TARGET="${1:-}"; [ -n "$TARGET" ] || { echo "update.sh: --to needs a commit" >&2; exit 2; } ;;
    -h|--help)      awk 'NR==1{next} /^#/{sub(/^# ?/,"");print;next}{exit}' "$0"; exit 0 ;;
    *) echo "update.sh: unknown option '$1' (try --help)" >&2; exit 2 ;;
  esac
  shift
done

say(){ [ "$QUIET" = 1 ] || printf '%s\n' "$*"; }
err(){ printf '%s\n' "$*" >&2; }

# Git as the checkout's owner. Under sudo, a root `git pull` would leave root-owned
# files in .git and break the next pull by the real owner.
OWNER="$(stat -c %U . 2>/dev/null || echo "")"
g(){
  if [ "$(id -u)" = 0 ] && [ -n "$OWNER" ] && [ "$OWNER" != root ]; then
    sudo -u "$OWNER" git "$@"
  else
    git "$@"
  fi
}

# Running version / commit, read from the app's own health endpoint (empty if it's down).
health_now(){ curl -fsS --max-time 5 "$HEALTH" 2>/dev/null || true; }
field(){ sed -n "s/.*\"$1\" *: *\"\\([^\"]*\\)\".*/\\1/p"; }
version_now(){ health_now | field version; }

# How is PawPoller running? The prebuilt-image install uses a ghcr.io image; the
# build-from-source install uses a locally-built one. Read it off the container.
detect_mode(){
  local cid img
  cid="$(docker ps -q --filter name=pawpoller 2>/dev/null | head -n1 || true)"
  [ -n "$cid" ] && img="$(docker inspect -f '{{.Config.Image}}' "$cid" 2>/dev/null || true)" || img=""
  case "$img" in
    *ghcr.io/*) echo image ;;
    *)          echo source ;;
  esac
}

# --check: is a newer version published? Compare the running version to the
# APP_VERSION on the remote's main branch. Read-only (fetch, not pull).
if [ "$MODE_CHECK" = 1 ]; then
  g fetch --quiet || { err "Could not reach the git remote to check for updates."; exit 1; }
  latest="$(g show origin/HEAD:config.py 2>/dev/null | sed -n 's/^APP_VERSION *= *"\([^"]*\)".*/\1/p' | head -n1)"
  [ -z "$latest" ] && latest="$(g show origin/main:config.py 2>/dev/null | sed -n 's/^APP_VERSION *= *"\([^"]*\)".*/\1/p' | head -n1)"
  current="$(version_now || true)"
  if [ -n "$current" ] && [ "$current" = "$latest" ]; then
    say "Up to date (${current})."
  else
    say "Update available: ${current:-unknown} -> ${latest:-unknown}. Run ./update.sh to apply."
  fi
  exit 0
fi

# --if-requested: only proceed if the dashboard button asked. The claim endpoint
# is loopback-only and hands out a pending request exactly once.
if [ "$IF_REQUESTED" = 1 ]; then
  resp="$(curl -fsS --max-time 5 -X POST "http://127.0.0.1:${PORT}/api/server/update-claim" 2>/dev/null || true)"
  case "$resp" in
    *'"requested"'*'true'*) : ;;              # a request is pending → fall through and update
    *) exit 0 ;;                              # nothing requested (or app down) → quietly do nothing
  esac
fi

# ── Do the update, serialized so two runs can't overlap ────────────────────────
exec 9>"$LOCK"
if ! flock -n 9; then
  err "Another update is already running."
  exit 0
fi

MODE="$(detect_mode)"
COMPOSE=(docker compose)
[ "$MODE" = image ] && COMPOSE=(docker compose -f docker-compose.image.yml)
before="$(version_now || true)"
# The commit that is RUNNING — what a rollback must return to. The checkout's HEAD can
# already be ahead of it (someone pulled without rebuilding), so ask the app first;
# older versions don't report one, then HEAD is the best guess.
before_commit="$(health_now | field commit)"
[ -n "$before_commit" ] || before_commit="$(g rev-parse --short HEAD)"
say "PawPoller updater — install type: ${MODE}"
[ -n "$before" ] && say "  current version: ${before} (${before_commit})"

# Back up the database first (4.43.2). SQLite's own backup API, run inside the
# container, gives a consistent copy even mid-write (a plain file copy of a live
# WAL database can be torn). No running container = nothing to back up yet.
if [ -n "$("${COMPOSE[@]}" ps -q pawpoller 2>/dev/null || true)" ]; then
  say "  backing up the database..."
  if ! backup="$("${COMPOSE[@]}" exec -T pawpoller python - "$before_commit" <<'PY'
import sqlite3, sys, time
import config
d = config.DATA_DIR / "backups"
d.mkdir(parents=True, exist_ok=True)
dst = d / f"pre-update-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}-{sys.argv[1]}.db"
src = sqlite3.connect(config.DB_PATH)
out = sqlite3.connect(dst)
src.backup(out)
out.close(); src.close()
# Keep the newest 5 of OUR pre-update copies; nothing else in the folder is touched.
for old in sorted(d.glob("pre-update-*.db"))[:-5]:
    old.unlink()
print("BACKUP:" + dst.name)
PY
  )"; then
    err "The database backup failed, so nothing was changed. Fix that before updating."
    exit 1
  fi
  backup="${backup##*BACKUP:}"     # importing config may print first; the name is last
  say "  database saved as data/backups/${backup}"
fi

# Refresh the checkout. Fast-forward only: if you have local commits/edits (e.g. a
# customized compose file) this stops rather than discarding them. After a
# rollback (--to) the checkout is on a bare commit; go back to the branch first.
if [ -n "$TARGET" ]; then
  say "  going to ${TARGET}..."
  g fetch --quiet || true
  # Resolve to a commit first: a value starting with "-" must never reach git as an option.
  target_sha="$(g rev-parse --verify --quiet "${TARGET}^{commit}" 2>/dev/null || true)"
  [ -n "$target_sha" ] || { err "No such commit: ${TARGET}"; exit 1; }
  g checkout --quiet --detach "$target_sha" || { err "No such commit: ${TARGET}"; exit 1; }
else
  if ! g symbolic-ref -q HEAD >/dev/null; then
    # origin/HEAD is missing on some clones; then try master, then main. (Not
    # `rev-parse --abbrev-ref`: on a missing ref it still PRINTS "origin/HEAD",
    # and `git checkout HEAD` would leave the checkout detached.)
    branch="$(g symbolic-ref -q --short refs/remotes/origin/HEAD 2>/dev/null | sed 's#^origin/##' || true)"
    g checkout --quiet "${branch:-master}" 2>/dev/null || g checkout --quiet main
  fi
  say "  pulling latest..."
  if ! g pull --ff-only; then
    err "git pull --ff-only failed — you have local changes to the checkout."
    err "Commit or stash them (or 'git stash') and re-run ./update.sh."
    exit 1
  fi
fi
want_commit="$(g rev-parse --short HEAD)"

say "  updating container (${MODE})..."
started="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
if [ "$MODE" = image ]; then
  docker compose -f docker-compose.image.yml pull
  docker compose -f docker-compose.image.yml up -d
else
  # The commit is baked into the image so /api/health can say exactly what runs.
  GIT_SHA="$want_commit" docker compose up -d --build
fi

# Wait for it to answer again — on the NEW commit, not the old container
# still draining — and report the new version.
after=""; after_commit=""
for _ in $(seq 1 90); do
  h="$(health_now)"
  after="$(printf '%s' "$h" | field version)"
  after_commit="$(printf '%s' "$h" | field commit)"
  if [ -n "$after" ] && { [ "$MODE" = image ] || [ "$after_commit" = "$want_commit" ]; }; then
    break
  fi
  sleep 1
done

rollback_hint(){
  err "To roll back:  ./update.sh --to ${before_commit}"
  err "(the database copy from just before this update is in data/backups/)"
}

if [ -z "$after" ]; then
  err "PawPoller did not answer on :${PORT} after the update."
  err "Check:  docker compose logs --tail=50"
  rollback_hint
  exit 1
fi
if [ "$MODE" != image ] && [ "$after_commit" != "$want_commit" ]; then
  err "PawPoller answers, but as ${after_commit:-an unknown commit}, not ${want_commit} — the new build did not start."
  rollback_hint
  exit 1
fi

# Did it start cleanly? Healthy is not the same as working: a migration can
# fail after the web server is up. Block on what is severe (a crash, a damaged
# database); only report plain errors, which can be a site being down.
sleep 10
logs="$("${COMPOSE[@]}" logs --since "$started" pawpoller 2>&1 || true)"
db_ok="$("${COMPOSE[@]}" exec -T pawpoller python -c \
  "import sqlite3, config; print(sqlite3.connect(config.DB_PATH).execute('PRAGMA quick_check').fetchone()[0])" \
  2>/dev/null || echo "could not run the check")"
if [ "$db_ok" != "ok" ]; then
  err "The database check failed after the update: ${db_ok}"
  rollback_hint
  exit 1
fi
# A traceback is severe unless it belongs to a poller reporting a failed poll: that's a
# HANDLED platform error (a site down, a bot check) logged with its trace. A scheduled poll
# landing seconds after the restart once failed a healthy deploy this way (4.45.2).
# Tight on purpose, so a real crash can't hide behind one:
#  - a "record" is a line with the app's own timestamp + [LEVEL] (text inside a message
#    can't pose as one);
#  - a handled poll error excuses a traceback only if it is the very NEXT non-blank line —
#    anything else in between (a thread's "Exception in thread…", uvicorn's own "ERROR:")
#    ends the excuse;
#  - a chained trace is excused only right under Python's "During handling of the above
#    exception" / "The above exception was the direct cause" line, inside a trace;
#  - [CRITICAL] anywhere always counts.
# Prints each severe line plus the 8 after it, for the message below. POSIX awk only (no
# {n} intervals), so gawk, mawk and busybox all run it.
severe="$(printf '%s\n' "$logs" | awk '
  { rec = ($0 ~ /^([^|]*[|] +)?[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9:,.]+ \[(DEBUG|INFO|WARNING|ERROR|CRITICAL)\]/)
    tb = ($0 ~ /Traceback \(most recent call last\)/)
    blank = ($0 ~ /^([^|]*[|])? *$/) }
  /\[CRITICAL\]/ { n = 9 }
  rec { expect = ($0 ~ /\[ERROR\] polling\.[A-Za-z0-9_]+: .*poll failed/); chain = 0; intrace = 0 }
  !rec && tb { if (!(expect || chain)) n = 9; expect = 0; chain = 0; intrace = 1 }
  !rec && !tb && !blank {
    if (intrace && $0 ~ /During handling of the above exception|The above exception was the direct cause/) chain = 1
    else { expect = 0; chain = 0 }
  }
  n > 0 { print; n-- }
')"
if [ -n "$severe" ]; then
  err "PawPoller started, but crashed or failed while starting. From the log:"
  printf '%s\n' "$severe" | tail -n 30 >&2
  rollback_hint
  exit 1
fi
errors="$(printf '%s\n' "$logs" | grep -F '[ERROR]' || true)"

if [ "$QUIET" = 1 ] && [ "$before" = "$after" ] && [ "$before_commit" = "$want_commit" ] && [ -z "$errors" ]; then
  exit 0   # cron/systemd: silent when nothing changed
fi
if [ -n "$errors" ]; then
  say "  note — errors in the startup log (not blocking; often a site being down):"
  printf '%s\n' "$errors" | tail -n 5 | sed 's/^/    /'
fi
say "PawPoller is now running ${after} (${want_commit}), was ${before:-unknown} (${before_commit})  [healthy, database ok]"
