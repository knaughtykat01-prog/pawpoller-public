#!/usr/bin/env bash
# One-command PawPoller server updater.  Run it from the repo:  ./update.sh
#
# Updates a running Docker Compose server to the latest release. It figures out
# on its own whether you built from source or run the prebuilt image, refreshes
# the checkout, updates the container the matching way, and reports the version
# before and after with a health check. Your data (settings, database, logs)
# lives in named volumes and is untouched.
#
#   ./update.sh                update now
#   ./update.sh --check        say whether a newer version exists; change nothing
#   ./update.sh --quiet        only print on a change or an error (for cron/systemd)
#   ./update.sh --if-requested update only if the dashboard asked (used by the host agent)
#   ./update.sh --help         this help
#
# Env: PAWPOLLER_PORT (default 8420) — the host port the server answers on.
set -euo pipefail

cd "$(cd "$(dirname "$0")" && pwd)"   # repo root (this script lives here)

PORT="${PAWPOLLER_PORT:-8420}"
HEALTH="http://127.0.0.1:${PORT}/api/health"
LOCK="${TMPDIR:-/tmp}/pawpoller-update.lock"
QUIET=0; MODE_CHECK=0; IF_REQUESTED=0

for a in "$@"; do
  case "$a" in
    --quiet|-q)     QUIET=1 ;;
    --check)        MODE_CHECK=1 ;;
    --if-requested) IF_REQUESTED=1 ;;
    -h|--help)      awk 'NR==1{next} /^#/{sub(/^# ?/,"");print;next}{exit}' "$0"; exit 0 ;;
    *) echo "update.sh: unknown option '$a' (try --help)" >&2; exit 2 ;;
  esac
done

say(){ [ "$QUIET" = 1 ] || printf '%s\n' "$*"; }
err(){ printf '%s\n' "$*" >&2; }

# Running version, read from the app's own health endpoint (empty if it's down).
version_now(){ curl -fsS --max-time 5 "$HEALTH" 2>/dev/null | sed -n 's/.*"version" *: *"\([^"]*\)".*/\1/p'; }

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
  git fetch --quiet || { err "Could not reach the git remote to check for updates."; exit 1; }
  latest="$(git show origin/HEAD:config.py 2>/dev/null | sed -n 's/^APP_VERSION *= *"\([^"]*\)".*/\1/p' | head -n1)"
  [ -z "$latest" ] && latest="$(git show origin/main:config.py 2>/dev/null | sed -n 's/^APP_VERSION *= *"\([^"]*\)".*/\1/p' | head -n1)"
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
before="$(version_now || true)"
say "PawPoller updater — install type: ${MODE}"
[ -n "$before" ] && say "  current version: ${before}"

# Refresh the checkout. Fast-forward only: if you have local commits/edits (e.g. a
# customized compose file) this stops rather than discarding them.
say "  pulling latest..."
if ! git pull --ff-only; then
  err "git pull --ff-only failed — you have local changes to the checkout."
  err "Commit or stash them (or 'git stash') and re-run ./update.sh."
  exit 1
fi

say "  updating container (${MODE})..."
if [ "$MODE" = image ]; then
  docker compose -f docker-compose.image.yml pull
  docker compose -f docker-compose.image.yml up -d
else
  docker compose up -d --build
fi

# Wait for it to answer again and report the new version.
after=""
for _ in $(seq 1 30); do
  after="$(version_now || true)"
  [ -n "$after" ] && break
  sleep 1
done

if [ -z "$after" ]; then
  err "PawPoller did not answer on :${PORT} after the update."
  err "Check:  docker compose logs --tail=50"
  exit 1
fi

if [ "$QUIET" = 1 ] && [ "$before" = "$after" ]; then
  exit 0   # cron/systemd: silent when nothing changed
fi
say "PawPoller is now running ${after} (was ${before:-unknown})  [healthy]"
