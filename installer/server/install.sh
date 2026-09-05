#!/usr/bin/env bash
# PawPoller Server — one-shot installer for Linux (systemd) and macOS (launchd).
#
#   curl -fsSL https://raw.githubusercontent.com/knaughtykat01-prog/pawpoller-public/main/installer/server/install.sh | bash
#
# What it does: downloads the matching release, verifies its checksum, unpacks it under
# <root>/releases/<version>, points <root>/current at it, writes a service unit that starts
# it on boot and restarts it if it stops, starts it, and waits for /api/health. The server
# keeps itself up to date from then on (server_updater.py) and keeps the previous release
# on disk for a rollback.
#
# Knobs (environment variables, all optional):
#   VERSION=4.12.0            install a specific version (default: latest release)
#   ARCHIVE=/path/to.tar.gz   install from a downloaded archive instead (offline)
#   PAWPOLLER_SERVER_ROOT     where releases live   (Linux: /opt/pawpoller-server;
#                                                    macOS: ~/Library/Application Support/PawPoller-Server)
#   PAWPOLLER_APPDATA_DIR     where data lives      (Linux: /var/lib/pawpoller; macOS: <root>/data)
#   PAWPOLLER_PORT=8420       dashboard port         PAWPOLLER_BIND=127.0.0.1  bind address
#
# Binds to 127.0.0.1 on purpose: reach it through Tailscale (`tailscale serve --bg 8420`) or a
# reverse proxy. Set PAWPOLLER_BIND=0.0.0.0 only if you know why.
set -euo pipefail

REPO="knaughtykat01-prog/pawpoller-public"
PORT="${PAWPOLLER_PORT:-8420}"
BIND="${PAWPOLLER_BIND:-127.0.0.1}"
SERVICE="pawpoller-server"

say()  { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "'$1' is required but not installed"; }

need curl; need tar
OS="$(uname -s)"; ARCH="$(uname -m)"
case "$OS" in
  Linux)  TAG_OS="linux" ;;
  Darwin) TAG_OS="darwin" ;;
  *)      die "unsupported OS: $OS (Linux and macOS only; Windows uses install.ps1)" ;;
esac
case "$ARCH" in
  x86_64|amd64)   TAG_ARCH="x86_64" ;;
  aarch64|arm64)  TAG_ARCH="arm64" ;;
  *)              die "unsupported architecture: $ARCH" ;;
esac
TAG="${TAG_OS}-${TAG_ARCH}"

if [ "$TAG_OS" = "linux" ]; then
  ROOT="${PAWPOLLER_SERVER_ROOT:-/opt/pawpoller-server}"
  DATA="${PAWPOLLER_APPDATA_DIR:-/var/lib/pawpoller}"
  SUDO=""; [ "$(id -u)" -eq 0 ] || { need sudo; SUDO="sudo"; }
else
  ROOT="${PAWPOLLER_SERVER_ROOT:-$HOME/Library/Application Support/PawPoller-Server}"
  DATA="${PAWPOLLER_APPDATA_DIR:-$ROOT/data}"
  SUDO=""
fi

# ── 1. which version ─────────────────────────────────────────────────────────
if [ -z "${ARCHIVE:-}" ]; then
  if [ -z "${VERSION:-}" ]; then
    say "Looking up the latest release"
    VERSION="$(curl -fsSL "https://api.github.com/repos/$REPO/releases/latest" \
      | grep -m1 '"tag_name"' | sed -E 's/.*"v?([^"]+)".*/\1/')"
    [ -n "$VERSION" ] || die "could not read the latest version from GitHub"
  fi
  ASSET="PawPoller-Server-${VERSION}-${TAG}.tar.gz"
  URL="https://github.com/$REPO/releases/download/v${VERSION}/${ASSET}"
  TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
  say "Downloading $ASSET"
  curl -fL --progress-bar -o "$TMP/$ASSET" "$URL" || die "download failed — is there a $TAG build for $VERSION?"
  curl -fsSL -o "$TMP/$ASSET.sha256" "$URL.sha256" || die "checksum file missing for $ASSET"
  say "Verifying checksum"
  EXPECTED="$(grep -oE '[0-9a-fA-F]{64}' "$TMP/$ASSET.sha256" | head -1 | tr 'A-F' 'a-f')"
  if command -v sha256sum >/dev/null 2>&1; then ACTUAL="$(sha256sum "$TMP/$ASSET" | cut -d' ' -f1)";
  else ACTUAL="$(shasum -a 256 "$TMP/$ASSET" | cut -d' ' -f1)"; fi
  [ "$EXPECTED" = "$ACTUAL" ] || die "checksum mismatch: expected $EXPECTED got $ACTUAL"
  ARCHIVE="$TMP/$ASSET"
else
  [ -f "$ARCHIVE" ] || die "ARCHIVE not found: $ARCHIVE"
  VERSION="${VERSION:-$(basename "$ARCHIVE" | sed -E 's/^PawPoller-Server-([^-]+)-.*/\1/')}"
fi

# ── 2. unpack into releases/<version>, point current at it ───────────────────
say "Installing $VERSION under $ROOT"
$SUDO mkdir -p "$ROOT/releases" "$DATA"
STAGE="$ROOT/releases/$VERSION.staging"
$SUDO rm -rf "$STAGE"; $SUDO mkdir -p "$STAGE"
$SUDO tar -xzf "$ARCHIVE" -C "$STAGE"
# a single top-level folder (dist/PawPoller-Server) is flattened. It is named like the binary inside
# it, so it is renamed out of the way first — `mv PawPoller-Server/PawPoller-Server .` onto a folder of
# that name would fail.
if [ "$(ls -A "$STAGE" | wc -l)" -eq 1 ] && [ -d "$STAGE/$(ls -A "$STAGE")" ]; then
  INNER="$STAGE/$(ls -A "$STAGE")"
  $SUDO sh -c "mv '$INNER' '$STAGE/.flatten' && mv '$STAGE/.flatten'/* '$STAGE'/ && rmdir '$STAGE/.flatten'"
fi
[ -x "$STAGE/PawPoller-Server" ] || $SUDO chmod +x "$STAGE/PawPoller-Server" 2>/dev/null || die "PawPoller-Server binary not found in the archive"
$SUDO rm -rf "$ROOT/releases/$VERSION"; $SUDO mv "$STAGE" "$ROOT/releases/$VERSION"
$SUDO ln -sfn "$ROOT/releases/$VERSION" "$ROOT/current.new"; $SUDO mv -Tf "$ROOT/current.new" "$ROOT/current" 2>/dev/null || $SUDO mv -f "$ROOT/current.new" "$ROOT/current"

# ── 3. service ───────────────────────────────────────────────────────────────
if [ "$TAG_OS" = "linux" ]; then
  if ! id -u pawpoller >/dev/null 2>&1; then
    say "Creating the 'pawpoller' system user"
    $SUDO useradd --system --home-dir "$DATA" --shell /usr/sbin/nologin pawpoller 2>/dev/null \
      || $SUDO useradd -r -d "$DATA" -s /sbin/nologin pawpoller
  fi
  $SUDO chown -R pawpoller:pawpoller "$DATA" "$ROOT/releases"
  ENVF="/etc/pawpoller-server.env"
  if [ ! -f "$ENVF" ]; then
    say "Writing $ENVF"
    $SUDO tee "$ENVF" >/dev/null <<EOF
# PawPoller Server — environment for the systemd unit. Edit, then: sudo systemctl restart $SERVICE
PAWPOLLER_APPDATA_DIR=$DATA
PAWPOLLER_SERVER_ROOT=$ROOT
PAWPOLLER_SERVER_MANAGED=1
PAWPOLLER_BIND=$BIND
PAWPOLLER_PORT=$PORT
PAWPOLLER_AUTO_BACKUP=1
PAWPOLLER_AUTO_BACKUP_DIR=$DATA/backups
EOF
    $SUDO chmod 640 "$ENVF"; $SUDO chown root:pawpoller "$ENVF"
  fi
  say "Writing /etc/systemd/system/$SERVICE.service"
  $SUDO tee "/etc/systemd/system/$SERVICE.service" >/dev/null <<EOF
[Unit]
Description=PawPoller Server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pawpoller
Group=pawpoller
EnvironmentFile=$ENVF
WorkingDirectory=$ROOT/current
ExecStart=$ROOT/current/PawPoller-Server --host \${PAWPOLLER_BIND} --port \${PAWPOLLER_PORT}
# The server exits with code 75 after staging a self-update; 'always' brings the new build up.
Restart=always
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ReadWritePaths=$DATA $ROOT

[Install]
WantedBy=multi-user.target
EOF
  $SUDO systemctl daemon-reload
  $SUDO systemctl enable --now "$SERVICE" >/dev/null
  $SUDO systemctl restart "$SERVICE"
else
  PLIST="$HOME/Library/LaunchAgents/app.syncopates.pawpoller-server.plist"
  mkdir -p "$HOME/Library/LaunchAgents" "$DATA"
  say "Writing $PLIST"
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>app.syncopates.pawpoller-server</string>
  <key>ProgramArguments</key><array>
    <string>$ROOT/current/PawPoller-Server</string><string>--host</string><string>$BIND</string><string>--port</string><string>$PORT</string>
  </array>
  <key>EnvironmentVariables</key><dict>
    <key>PAWPOLLER_APPDATA_DIR</key><string>$DATA</string>
    <key>PAWPOLLER_SERVER_ROOT</key><string>$ROOT</string>
    <key>PAWPOLLER_SERVER_MANAGED</key><string>1</string>
    <key>PAWPOLLER_AUTO_BACKUP</key><string>1</string>
    <key>PAWPOLLER_AUTO_BACKUP_DIR</key><string>$DATA/backups</string>
  </dict>
  <key>WorkingDirectory</key><string>$ROOT/current</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$DATA/server.log</string>
  <key>StandardErrorPath</key><string>$DATA/server.log</string>
</dict></plist>
EOF
  launchctl bootout "gui/$(id -u)" "$PLIST" >/dev/null 2>&1 || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
fi

# ── 4. health ────────────────────────────────────────────────────────────────
say "Waiting for the server"
for i in $(seq 1 40); do
  if curl -fsS "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then
    echo; say "PawPoller Server $VERSION is running."
    echo "    Dashboard (this machine):  http://127.0.0.1:$PORT"
    if command -v tailscale >/dev/null 2>&1; then
      echo "    From your other devices:   sudo tailscale serve --bg $PORT   then open https://$(tailscale status --json 2>/dev/null | grep -m1 '"DNSName"' | sed -E 's/.*"([^"]+)\.".*/\1/')"
    else
      echo "    From your other devices:   install Tailscale on both machines, then: sudo tailscale serve --bg $PORT"
    fi
    if [ "$TAG_OS" = "linux" ]; then
      echo "    Logs:                      journalctl -u $SERVICE -f"
      echo "    Data:                      $DATA        Releases: $ROOT/releases"
    else
      echo "    Logs:                      $DATA/server.log"
    fi
    echo "    It updates itself; the previous release is kept for a rollback."
    exit 0
  fi
  sleep 1
done
if [ "$TAG_OS" = "linux" ]; then $SUDO journalctl -u "$SERVICE" -n 30 --no-pager || true; else tail -n 30 "$DATA/server.log" 2>/dev/null || true; fi
die "the server did not answer on port $PORT within 40 s — see the log above"
