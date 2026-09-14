#!/usr/bin/env bash
# One-time installer for the PawPoller server-update host agent.
#
# A container can't rebuild and replace itself, so the dashboard "Update now"
# button and the optional scheduled auto-update are carried out on the host by a
# tiny systemd helper. This installs it. Run once, as root:
#
#   sudo server-update/install.sh          # button support (poll timer)
#   sudo server-update/install.sh --auto    # + enable daily auto-update
#
# Re-running is safe. To remove: see --uninstall.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
UNIT_DIR="/etc/systemd/system"
AUTO=0; UNINSTALL=0
for a in "$@"; do
  case "$a" in
    --auto)      AUTO=1 ;;
    --uninstall) UNINSTALL=1 ;;
    -h|--help)   awk 'NR==1{next} /^#/{sub(/^# ?/,"");print;next}{exit}' "$0"; exit 0 ;;
    *) echo "install.sh: unknown option '$a' (try --help)" >&2; exit 2 ;;
  esac
done

if [ "$(id -u)" != 0 ]; then
  echo "This installer writes systemd units — run it with sudo:" >&2
  echo "  sudo $0 $*" >&2
  exit 1
fi

UNITS="pawpoller-update-poll.service pawpoller-update-poll.timer pawpoller-auto-update.service pawpoller-auto-update.timer"

if [ "$UNINSTALL" = 1 ]; then
  systemctl disable --now pawpoller-update-poll.timer pawpoller-auto-update.timer 2>/dev/null || true
  for u in $UNITS; do rm -f "$UNIT_DIR/$u"; done
  systemctl daemon-reload
  echo "Removed the PawPoller update agent."
  exit 0
fi

# The service runs as whoever owns the checkout (the same user who runs docker
# here) and from the repo directory.
RUNUSER="$(stat -c '%U' "$REPO")"
echo "Installing PawPoller update agent:"
echo "  repo:  $REPO"
echo "  user:  $RUNUSER"

chmod +x "$REPO/update.sh" 2>/dev/null || true

# Fill the placeholders and install each unit.
for u in $UNITS; do
  sed -e "s#__REPO__#${REPO}#g" -e "s#__USER__#${RUNUSER}#g" "$HERE/$u" > "$UNIT_DIR/$u"
done
systemctl daemon-reload

# The poll timer supports the dashboard button (it only acts on a request).
systemctl enable --now pawpoller-update-poll.timer
echo "  enabled: pawpoller-update-poll.timer (dashboard 'Update now')"

if [ "$AUTO" = 1 ]; then
  systemctl enable --now pawpoller-auto-update.timer
  echo "  enabled: pawpoller-auto-update.timer (daily auto-update)"
else
  echo "  installed (disabled): pawpoller-auto-update.timer"
  echo "     enable daily auto-update later with:"
  echo "     sudo systemctl enable --now pawpoller-auto-update.timer"
fi

echo "Done. The dashboard's 'Update now' button will work within ~2 minutes."
if ! id -nG "$RUNUSER" 2>/dev/null | tr ' ' '\n' | grep -qx docker && [ "$RUNUSER" != root ]; then
  echo "NOTE: user '$RUNUSER' is not in the 'docker' group — updates may fail to run docker." >&2
  echo "      Add it with:  sudo usermod -aG docker $RUNUSER   (then re-login)" >&2
fi
