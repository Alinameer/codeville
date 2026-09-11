#!/usr/bin/env python3
"""Codeville's panel applet: a live indicator plus the village window.

Runs the daemon in-process (as background threads) rather than talking to a
separate service. There is only ever one thing to launch and one thing to quit,
the indicator can read world state directly instead of polling its own HTTP API,
and there is no IPC to go wrong. The HTTP server still runs, so the same village
is reachable from any browser at the printed URL.

Requires the system GTK stack, which ships with most desktops and cannot be pip
installed:

    Gtk 3.0, WebKit2 4.1, AyatanaAppIndicator3 0.1

Note for anyone launching this from a terminal inside a snap (the VS Code snap,
for instance): the snap's LD_LIBRARY_PATH and GTK_PATH break the system GTK and
python3 dies with an undefined-symbol error from libpthread. `bin/codeville-tray`
strips those variables before exec'ing this file.
"""

from __future__ import annotations

import os
import sys
import tempfile
import webbrowser

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("AyatanaAppIndicator3", "0.1")
from gi.repository import GLib, Gtk  # noqa: E402
from gi.repository import AyatanaAppIndicator3 as AppIndicator  # noqa: E402

try:
    gi.require_version("WebKit2", "4.1")
    from gi.repository import WebKit2
    HAVE_WEBKIT = True
except (ValueError, ImportError):  # pragma: no cover - depends on the host
    WebKit2 = None
    HAVE_WEBKIT = False

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "daemon"))

from codeville.app import Codeville  # noqa: E402

APP_ID = "codeville"
REFRESH_SECONDS = 2


def icon_svg(working: int, connected: bool) -> str:
    """The panel icon, drawn at runtime so it can reflect state.

    Shipping themed PNGs would mean a file per state per theme; one small SVG
    rewritten on change is simpler and stays sharp on HiDPI panels.
    """
    if not connected:
        body, eye = "#8A8FA8", "#D7DAE5"
    elif working:
        body, eye = "#7C5CFF", "#FFFFFF"
    else:
        body, eye = "#5FB58F", "#FFFFFF"

    badge = ""
    if working:
        badge = (
            '<circle cx="17" cy="5.5" r="4.6" fill="#FF7FA8" stroke="#fff" stroke-width="1.2"/>'
            f'<text x="17" y="7.6" font-size="6" font-family="sans-serif" font-weight="bold"'
            f' text-anchor="middle" fill="#fff">{min(working, 9)}</text>'
        )
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 22 22">
  <rect x="3" y="6" width="16" height="12" rx="5.5" fill="{body}"/>
  <circle cx="8" cy="11.5" r="2" fill="{eye}"/>
  <circle cx="14" cy="11.5" r="2" fill="{eye}"/>
  <path d="M11 6 V3" stroke="{body}" stroke-width="1.6" stroke-linecap="round"/>
  <circle cx="11" cy="2.4" r="1.5" fill="{body}"/>
  {badge}
</svg>"""


class VillageWindow:
    """The WebKit window showing the village."""

    def __init__(self, url: str):
        self.url = url
        self.window = None
        self.view = None

    def present(self) -> None:
        if not HAVE_WEBKIT:
            webbrowser.open(self.url)
            return
        if self.window is not None:
            self.window.present()
            return

        self.window = Gtk.Window(title="Codeville")
        self.window.set_default_size(1180, 780)
        self.window.set_icon_name("applications-development")
        self.view = WebKit2.WebView()
        settings = self.view.get_settings()
        settings.set_enable_developer_extras(True)
        self.view.load_uri(self.url)

        scroller = Gtk.ScrolledWindow()
        scroller.add(self.view)
        self.window.add(scroller)
        # Hide instead of destroy so reopening is instant and keeps scroll state.
        self.window.connect("delete-event", self._on_close)
        self.window.show_all()

    def _on_close(self, *_args) -> bool:
        self.window.hide()
        return True

    def reload(self) -> None:
        if self.view is not None:
            self.view.reload()


class Tray:
    def __init__(self) -> None:
        self.app = Codeville().start()
        self.url = self.app.server.url
        self.window = VillageWindow(self.url)

        self.icon_dir = tempfile.mkdtemp(prefix="codeville-icons-")
        self._icon_serial = 0
        self._last_icon_key = None

        self.indicator = AppIndicator.Indicator.new(
            APP_ID, self._write_icon(0, True),
            AppIndicator.IndicatorCategory.APPLICATION_STATUS)
        self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        self.indicator.set_icon_theme_path(self.icon_dir)
        self.indicator.set_title("Codeville")

        self.items = {}
        self.indicator.set_menu(self._build_menu())
        self.refresh()
        GLib.timeout_add_seconds(REFRESH_SECONDS, self.refresh)

    # -- icon ------------------------------------------------------------

    def _write_icon(self, working: int, connected: bool) -> str:
        """Write the icon and return its path.

        The filename changes every time: AppIndicator caches by name, so
        rewriting the same path leaves the old pixels on the panel.
        """
        self._icon_serial += 1
        path = os.path.join(self.icon_dir, f"codeville-{self._icon_serial}.svg")
        with open(path, "w") as handle:
            handle.write(icon_svg(working, connected))
        # Keep the directory small — the panel only ever needs the current icon.
        for stale in os.listdir(self.icon_dir):
            if stale != os.path.basename(path):
                try:
                    os.remove(os.path.join(self.icon_dir, stale))
                except OSError:
                    pass
        return path

    # -- menu ------------------------------------------------------------

    def _build_menu(self) -> Gtk.Menu:
        menu = Gtk.Menu()

        self.items["summary"] = Gtk.MenuItem(label="Starting…")
        self.items["summary"].set_sensitive(False)
        menu.append(self.items["summary"])

        menu.append(Gtk.SeparatorMenuItem())

        open_item = Gtk.MenuItem(label="Open Codeville")
        open_item.connect("activate", lambda *_: self.window.present())
        menu.append(open_item)

        browser_item = Gtk.MenuItem(label="Open in browser")
        browser_item.connect("activate", lambda *_: webbrowser.open(self.url))
        menu.append(browser_item)

        menu.append(Gtk.SeparatorMenuItem())

        self.items["villages"] = Gtk.MenuItem(label="Busy villages")
        self.items["villages"].set_sensitive(False)
        menu.append(self.items["villages"])

        self.items["village_menu"] = Gtk.Menu()
        holder = Gtk.MenuItem(label="Villages")
        holder.set_submenu(self.items["village_menu"])
        menu.append(holder)
        self.items["village_holder"] = holder

        menu.append(Gtk.SeparatorMenuItem())

        copy_item = Gtk.MenuItem(label="Copy village URL")
        copy_item.connect("activate", self._copy_url)
        menu.append(copy_item)

        quit_item = Gtk.MenuItem(label="Quit Codeville")
        quit_item.connect("activate", self._quit)
        menu.append(quit_item)

        menu.show_all()
        return menu

    def _copy_url(self, *_args) -> None:
        from gi.repository import Gdk
        clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        clipboard.set_text(self.url, -1)
        clipboard.store()

    def _quit(self, *_args) -> None:
        self.app.stop()
        Gtk.main_quit()

    # -- live refresh ------------------------------------------------------

    def refresh(self) -> bool:
        stats = self.app.world.stats()
        working = stats.get("agents_working", 0)
        busy_villages = stats.get("villages_live", 0)

        key = (working, busy_villages)
        if key != self._last_icon_key:
            self._last_icon_key = key
            self.indicator.set_icon_full(
                os.path.basename(self._write_icon(working, True))[:-4], "Codeville")

        label = f"{working} working" if working else ""
        self.indicator.set_label(label, "Codeville")

        self.items["summary"].set_label(
            f"{working} agent{'' if working == 1 else 's'} working "
            f"in {busy_villages} village{'' if busy_villages == 1 else 's'}"
            if working else f"{stats.get('villages', 0)} villages, all quiet")

        self._refresh_villages()
        return True  # keep the timeout alive

    def _refresh_villages(self) -> None:
        submenu = self.items["village_menu"]
        for child in submenu.get_children():
            submenu.remove(child)

        villages = sorted(self.app.world.villages.values(),
                          key=lambda v: (v.busy_count, v.last_active), reverse=True)
        shown = [v for v in villages if v.busy_count] or villages[:8]
        if not shown:
            empty = Gtk.MenuItem(label="No villages yet")
            empty.set_sensitive(False)
            submenu.append(empty)
        else:
            for village in shown[:12]:
                suffix = f"  ({village.busy_count} working)" if village.busy_count else ""
                item = Gtk.MenuItem(label=f"{village.name}{suffix}")
                item.connect("activate", lambda *_: self.window.present())
                submenu.append(item)
        submenu.show_all()


def main() -> int:
    missing = []
    if not HAVE_WEBKIT:
        missing.append("WebKit2 4.1 (the village will open in your browser instead)")
    for note in missing:
        print(f"codeville: {note}", file=sys.stderr)

    tray = Tray()
    print(f"Codeville is watching. Open: {tray.url}", flush=True)
    try:
        Gtk.main()
    except KeyboardInterrupt:
        tray.app.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
