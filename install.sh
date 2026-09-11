#!/usr/bin/env bash
#
# Install Codeville for the current user. No root, no virtualenv, no pip:
# everything Codeville needs is either the Python standard library or the system
# GTK stack, which cannot be pip-installed anyway.
#
#   ./install.sh              install into ~/.local
#   ./install.sh --uninstall  remove everything it created
#
set -euo pipefail

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="${PREFIX:-$HOME/.local}"
BIN_DIR="$PREFIX/bin"
APP_DIR="$PREFIX/share/applications"
ICON_DIR="$PREFIX/share/icons/hicolor/scalable/apps"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
warn() { printf '\033[33m!\033[0m %s\n' "$*"; }
ok()   { printf '\033[32m✓\033[0m %s\n' "$*"; }

# --------------------------------------------------------------- uninstall

if [[ "${1:-}" == "--uninstall" ]]; then
  bold "Removing Codeville"
  systemctl --user disable --now codeville.service 2>/dev/null || true
  rm -f "$BIN_DIR/codeville" "$BIN_DIR/codeville-tray"
  rm -f "$APP_DIR/codeville.desktop" "$ICON_DIR/codeville.svg"
  rm -f "$UNIT_DIR/codeville.service"
  rm -rf "${XDG_RUNTIME_DIR:-$HOME/.cache}/codeville"
  command -v update-desktop-database >/dev/null && update-desktop-database "$APP_DIR" 2>/dev/null || true
  ok "Codeville removed. Your Claude Code data was never touched."
  exit 0
fi

# ------------------------------------------------------------ dependencies

bold "Checking dependencies"

python3 - <<'PY' || { warn "Python 3.9+ is required"; exit 1; }
import sys
raise SystemExit(0 if sys.version_info >= (3, 9) else 1)
PY
ok "python3 $(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')"

missing=()
check_gi() {
  python3 - "$1" "$2" <<'PY' 2>/dev/null
import sys, gi
gi.require_version(sys.argv[1], sys.argv[2])
__import__("gi.repository." + sys.argv[1])
PY
}

check_gi Gtk 3.0            || missing+=("Gtk 3.0")
check_gi WebKit2 4.1        || missing+=("WebKit2 4.1")
check_gi AyatanaAppIndicator3 0.1 || missing+=("AyatanaAppIndicator3 0.1")

if (( ${#missing[@]} )); then
  warn "Missing: ${missing[*]}"
  echo
  echo "  Debian / Ubuntu:"
  echo "    sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-webkit2-4.1 \\"
  echo "                     gir1.2-ayatanaappindicator3-0.1"
  echo "  Fedora:"
  echo "    sudo dnf install python3-gobject gtk3 webkit2gtk4.1 libayatana-appindicator-gtk3"
  echo "  Arch:"
  echo "    sudo pacman -S python-gobject gtk3 webkit2gtk-4.1 libayatana-appindicator"
  echo
  echo "Codeville still runs without them — use 'codeville' and open the printed"
  echo "URL in a browser. The tray applet needs the packages above."
  echo
else
  ok "GTK, WebKit and AppIndicator present"
fi

# GNOME hides tray icons unless an AppIndicator extension is enabled.
if [[ "${XDG_CURRENT_DESKTOP:-}" == *GNOME* ]]; then
  if command -v gnome-extensions >/dev/null; then
    if ! gnome-extensions list --enabled 2>/dev/null | grep -qiE 'appindicator|tray'; then
      warn "GNOME is running without an AppIndicator extension."
      echo "     The panel icon will not appear until you enable one, e.g."
      echo "       https://extensions.gnome.org/extension/615/appindicator-support/"
      echo "     On Ubuntu it is usually already installed as ubuntu-appindicators."
    else
      ok "AppIndicator extension enabled"
    fi
  fi
fi

# ---------------------------------------------------------------- install

bold "Installing into $PREFIX"
mkdir -p "$BIN_DIR" "$APP_DIR" "$ICON_DIR"

# Symlink rather than copy so `git pull` updates the installed copy.
ln -sf "$SOURCE_DIR/bin/codeville"      "$BIN_DIR/codeville"
ln -sf "$SOURCE_DIR/bin/codeville-tray" "$BIN_DIR/codeville-tray"
ok "commands: codeville, codeville-tray"

install -m 0644 "$SOURCE_DIR/packaging/codeville.svg" "$ICON_DIR/codeville.svg"
sed "s|Exec=codeville-tray|Exec=$BIN_DIR/codeville-tray|" \
  "$SOURCE_DIR/packaging/codeville.desktop" > "$APP_DIR/codeville.desktop"
chmod 0644 "$APP_DIR/codeville.desktop"
ok "desktop entry and icon"

command -v update-desktop-database >/dev/null && update-desktop-database "$APP_DIR" 2>/dev/null || true
command -v gtk-update-icon-cache >/dev/null && \
  gtk-update-icon-cache -f -t "$PREFIX/share/icons/hicolor" 2>/dev/null || true

if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
  warn "$BIN_DIR is not on your PATH. Add this to your shell profile:"
  echo "     export PATH=\"\$HOME/.local/bin:\$PATH\""
fi

# -------------------------------------------------------- start on login?

if command -v systemctl >/dev/null; then
  read -r -p "Start Codeville automatically when you log in? [y/N] " reply || reply=n
  if [[ "$reply" =~ ^[Yy]$ ]]; then
    mkdir -p "$UNIT_DIR"
    sed "s|%h/.local/bin/codeville-tray|$BIN_DIR/codeville-tray|" \
      "$SOURCE_DIR/packaging/codeville.service" > "$UNIT_DIR/codeville.service"
    systemctl --user daemon-reload
    systemctl --user enable codeville.service
    ok "enabled at login (systemctl --user disable codeville.service to undo)"
  fi
fi

echo
bold "Done."
echo "  Run it now:   codeville-tray        (panel applet)"
echo "  Or headless:  codeville             (prints a URL to open)"
echo
echo "Codeville only ever reads ~/.claude/projects, and serves it to 127.0.0.1"
echo "behind a per-run token. Nothing leaves your machine."
