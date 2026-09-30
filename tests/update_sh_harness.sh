#!/usr/bin/env bash
# Exercise update.sh with fake docker/curl/flock/sleep in a throwaway repo pair (4.43.2).
# Run by tests/test_update_tooling.py; never touches a real container or checkout.
set -u
SRC="$1"                                   # path to the real update.sh
T="$(mktemp -d)"; cd "$T"
mkdir bin state
export PATH="$T/bin:$PATH" STATE="$T/state"

cat > bin/flock <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
cat > bin/sleep <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
cat > bin/curl <<'EOF'
#!/usr/bin/env bash
for a in "$@"; do case "$a" in *update-claim*) echo '{}'; exit 0 ;; esac; done
[ -f "$STATE/running" ] || exit 7
echo "{\"status\":\"ok\",\"version\":\"$(cat "$STATE/version")\",\"commit\":\"$(cat "$STATE/running")\"}"
EOF
cat > bin/docker <<'EOF'
#!/usr/bin/env bash
echo "docker $*" >> "$STATE/calls"
case "$*" in
  "ps -q --filter name=pawpoller") echo cid1 ;;
  inspect*) echo pawpoller-pawpoller ;;
  *" ps -q pawpoller") echo cid1 ;;
  *"exec -T pawpoller python - "*) cat > "$STATE/backup_script"; echo "some config noise"; echo "BACKUP:pre-update-TEST-${7}.db" ;;
  *"exec -T pawpoller python -c"*) echo "${FAKE_DB:-ok}" ;;
  *"up -d --build") echo "${GIT_SHA:-}" > "$STATE/running"; echo "built $GIT_SHA" >> "$STATE/calls" ;;
  *logs*) printf '%b\n' "${FAKE_LOGS:-2026 [INFO] started}" ;;
esac
exit 0
EOF
chmod +x bin/*

git init -q --bare origin.git
git clone -q origin.git work 2>/dev/null
cd work
git config user.email t@example.com; git config user.name t
cp "$SRC" update.sh; echo one > f; git add -A; git commit -qm A; git push -q origin HEAD:master 2>/dev/null
git branch -q -M master; git branch -q -u origin/master 2>/dev/null
A="$(git rev-parse --short HEAD)"
( cd .. && git clone -q origin.git other && cd other && git config user.email t@example.com && git config user.name t \
  && echo two > f && git commit -qam B && git push -q origin HEAD:master )
echo "4.43.1" > "$STATE/version"; echo "$A" > "$STATE/running"

run(){ echo "--- $*"; "$@" 2>&1; echo "exit=$?"; }

echo "=== 1. normal update A -> B"
run ./update.sh
B="$(git rev-parse --short HEAD)"
echo "running=$(cat "$STATE/running") want=$B head_is_B=$([ "$(git rev-parse --short HEAD)" != "$A" ] && echo yes)"
grep -c "sqlite3" "$STATE/backup_script" >/dev/null && echo "backup script received"

echo "=== 2. a traceback in the startup log fails the update"
FAKE_LOGS='x [INFO] up\nTraceback (most recent call last):\n  boom' run ./update.sh

echo "=== 3. a damaged database fails the update"
FAKE_DB='*** in database main ***' run ./update.sh

echo "=== 4. plain [ERROR] lines only warn"
FAKE_LOGS='x [ERROR] FA poll failed' run ./update.sh

echo "=== 5. roll back with --to A, then a normal update returns to the branch"
run ./update.sh --to "$A"
echo "running=$(cat "$STATE/running") (want $A); detached=$(git symbolic-ref -q HEAD || echo yes)"
run ./update.sh
echo "running=$(cat "$STATE/running") (want $B); branch=$(git symbolic-ref --short HEAD)"

echo "=== 6. the new build never starts (health keeps the old commit)"
sed -i 's|echo "${GIT_SHA:-}" > "$STATE/running";|:;|' "$T/bin/docker"
run ./update.sh --to "$A"

echo "=== 7. bad --to"
run ./update.sh --to deadbeef

echo "=== 8. the checkout was pulled ahead of what runs: the rollback names what RUNS"
sed -i 's|:;|echo "${GIT_SHA:-}" > "$STATE/running";|' "$T/bin/docker"
echo "$A" > "$STATE/running"
git checkout -q master 2>/dev/null; git pull -q --ff-only 2>/dev/null
echo "expect-rollback-to=$A head=$(git rev-parse --short HEAD)"
FAKE_LOGS='Traceback (most recent call last):' run ./update.sh

echo "=== 9. a --to that looks like an option never reaches git as one"
run ./update.sh --to --orphan

echo "=== 10. a HANDLED poll error's traceback doesn't fail a healthy deploy"
FAKE_LOGS='pawpoller-1  | 2026-09-30 02:45:00,100 [INFO] app: up\npawpoller-1  | 2026-09-30 02:45:00,275 [ERROR] polling.fn_poller: fn poll failed: behind a bot check\npawpoller-1  | Traceback (most recent call last):\npawpoller-1  |   File "fn_poller.py", line 137\npawpoller-1  | ValueError: blocked\npawpoller-1  | \npawpoller-1  | During handling of the above exception, another exception occurred:\npawpoller-1  | \npawpoller-1  | Traceback (most recent call last):\npawpoller-1  |   boom\npawpoller-1  | 2026-09-30 02:45:01,000 [INFO] app: next' run ./update.sh

echo "=== 11. a crash traceback AFTER a handled one still fails"
FAKE_LOGS='2026-09-30 02:45:00,275 [ERROR] polling.fn_poller: fn poll failed: blocked\nTraceback (most recent call last):\n  File "fn_poller.py"\nException in thread worker:\nTraceback (most recent call last):\n  crash' run ./update.sh

echo "=== 12. a handled poll error WITHOUT a trace does not excuse the next crash"
FAKE_LOGS='2026-09-30 02:45:00,275 [ERROR] polling.multi_account: ib account 3 (x) poll failed: timeout\nException in thread worker:\nTraceback (most recent call last):\nRuntimeError: real crash' run ./update.sh

echo "=== 13. uvicorn's own error format after a handled poll error still fails"
FAKE_LOGS='2026-09-30 02:45:00,275 [ERROR] polling.multi_account: ib account 3 (x) poll failed: timeout\nERROR:    Exception in ASGI application\nTraceback (most recent call last):\n  boom' run ./update.sh

echo "=== 14. remote text posing as a chain marker does not excuse a later crash"
FAKE_LOGS='2026-09-30 02:45:00,275 [ERROR] polling.fn_poller: fn poll failed: x\nTraceback (most recent call last):\n  File "c.py"\nValueError: site said:\nDuring handling of the above exception, another exception occurred:\nException in thread worker:\nTraceback (most recent call last):\n  crash' run ./update.sh
