#!/usr/bin/env python3
"""netmon GUI - native desktop app (GTK 3) for network usage monitoring.
Created by Paritosh Chaudhari.

- Summary page: live download/upload speed and usage for today, yesterday,
  the last 7 days and the last 30 days. Data is stored in SQLite.
- Charts page: live download/upload over the last 2 minutes, traffic per hour
  (last 24 h), per day (last 30 days) and per network interface (today).
- Starts automatically when you log in (on by default).
  Turn this off or on in the Settings dialog.
- Closing the window keeps monitoring in the background.
  Use Quit in the menu or Ctrl+Q to exit completely.
- Launching the app again just brings the existing window forward.

Requires only Ubuntu's built-in packages: python3-gi and gir1.2-gtk-3.0.
"""

import json
import math
import os
import re
import signal
import sqlite3
import sys
import tempfile
import traceback
from collections import defaultdict, deque
from datetime import datetime, timedelta

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gio, GLib, Gtk  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import netmon  # noqa: E402  (collector, database and formatting helpers)

APP_ID = "local.netmon.gui"
ICON_NAME = "netmon"  # installed by install.sh into the hicolor icon theme
SCRIPT = os.path.abspath(__file__)
INSTALLED_SCRIPT = os.path.expanduser("~/.local/opt/netmon/netmon_gui.py")
SYSTEM_PYTHON = "/usr/bin/python3"
CONFIG_DIR = os.path.expanduser("~/.config/netmon")
SETTINGS_FILE = os.path.join(CONFIG_DIR, "settings.json")
AUTOSTART_FILE = os.path.expanduser("~/.config/autostart/netmon.desktop")
SYSTEM_AUTOSTART_FILE = "/etc/xdg/autostart/netmon.desktop"  # shipped by the .deb package
BACKGROUND_ARG = "--background"  # autostart: collect data without showing the window
CHART_REFRESH_SECONDS = 5
REALTIME_SECONDS = 120  # width of the live download/upload chart (one sample per second)

# Chart colours: one calm blue and one warm amber. Both read well on light and dark
# themes, and they stay distinct for people with red-green colour blindness.
DOWN_COLOR = (0.21, 0.52, 0.89)  # blue   (#3584e4)
UP_COLOR = (0.90, 0.65, 0.04)    # amber  (#e5a50a)

# Neutral, theme-aware styling. Surfaces are translucent greys, so they follow
# the light or dark theme without hard-coded backgrounds.
STYLE_CSS = b"""
.netmon-page {
    padding: 18px 20px 14px 20px;
}
.netmon-card {
    background-color: rgba(128, 128, 128, 0.08);
    border: 1px solid rgba(128, 128, 128, 0.16);
    border-radius: 14px;
    padding: 14px 16px;
}
.netmon-caption {
    font-size: 85%;
    opacity: 0.62;
}
.netmon-section {
    font-weight: 600;
}
.netmon-value {
    font-size: 19pt;
    font-weight: 600;
}
.netmon-session {
    font-size: 11pt;
}
.netmon-table-head {
    font-size: 85%;
    font-weight: 600;
    opacity: 0.62;
}
.netmon-today {
    font-weight: 600;
}
.netmon-rule {
    background-color: rgba(128, 128, 128, 0.16);
    min-height: 1px;
}
.netmon-footer {
    font-size: 85%;
    opacity: 0.55;
}
.netmon-hint {
    opacity: 0.7;
}
"""

# Maps _text_label() keyword flags to the CSS classes above.
LABEL_STYLES = {
    "caption": "netmon-caption",
    "section": "netmon-section",
    "table_head": "netmon-table-head",
    "today": "netmon-today",
    "footer": "netmon-footer",
    "hint": "netmon-hint",
}


# ---------- file helpers ----------

def _atomic_write(path, text):
    """Write text to path so that readers never see a partially written file."""
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".netmon-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _desktop_exec(path):
    """Quote a path for a .desktop Exec= line (Exec quoting, then string escaping, then %)."""
    quoted = '"' + re.sub(r'(["`$\\])', r'\\\1', path) + '"'
    return quoted.replace("\\", "\\\\").replace("%", "%%")


# ---------- settings & autostart ----------

def _launch_command(background=False):
    """Command for the autostart entry: the installed copy if present, else this file.

    background=True adds --background so the app collects data without showing the window.
    """
    python = SYSTEM_PYTHON if os.path.exists(SYSTEM_PYTHON) else sys.executable
    script = INSTALLED_SCRIPT if os.path.exists(INSTALLED_SCRIPT) else SCRIPT
    command = f"{_desktop_exec(python)} {_desktop_exec(script)}"
    return f"{command} {BACKGROUND_ARG}" if background else command


def desktop_entry(autostart):
    lines = [
        "[Desktop Entry]",
        "Type=Application",
        "Name=netmon",
        "Comment=Network usage monitor",
        f"Exec={_launch_command(background=True)}",
        f"Icon={ICON_NAME}",
        "Terminal=false",
        "Categories=Network;System;",
        f"StartupWMClass={APP_ID}",
    ]
    if autostart:
        lines.append("X-GNOME-Autostart-enabled=true")
    else:
        lines.append("Hidden=true")  # overrides a system-wide entry with the same name
    return "\n".join(lines) + "\n"


def autostart_enabled():
    """True if netmon starts at login. A user entry with Hidden=true switches it off."""
    if not os.path.exists(AUTOSTART_FILE):
        return False
    try:
        with open(AUTOSTART_FILE, encoding="utf-8") as f:
            return "Hidden=true" not in f.read().splitlines()
    except OSError:
        return True  # the file exists but cannot be read; treat it as on


def set_autostart(enabled):
    if enabled:
        _atomic_write(AUTOSTART_FILE, desktop_entry(autostart=True))
    elif os.path.exists(SYSTEM_AUTOSTART_FILE):
        # Deleting the user entry would let the system entry start netmon again,
        # so write a hidden entry that overrides it.
        _atomic_write(AUTOSTART_FILE, desktop_entry(autostart=False))
    else:
        try:
            os.remove(AUTOSTART_FILE)
        except FileNotFoundError:
            pass


def load_settings():
    """Return the saved settings dict.

    {} if there is no settings file yet, None if the file exists but is unreadable
    or is not a JSON object (so the caller can leave the user's choices alone).
    """
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):  # ValueError covers bad JSON and bad UTF-8
        return None
    return data if isinstance(data, dict) else None


def save_settings(settings):
    _atomic_write(SETTINGS_FILE, json.dumps(settings, indent=2) + "\n")


def apply_first_run_defaults():
    """Enable autostart the first time the app runs. Later choices are kept."""
    settings = load_settings()
    if settings is None:
        print(f"netmon: ignoring unreadable settings file {SETTINGS_FILE}", file=sys.stderr)
        return
    if settings.get("defaults_applied"):
        return
    try:
        set_autostart(True)
        settings["defaults_applied"] = True
        save_settings(settings)
    except OSError as exc:
        print(f"netmon: could not apply first-run defaults: {exc}", file=sys.stderr)


# ---------- chart data (read from SQLite) ----------

def _hour_totals(conn, since_ts):
    """{local_hour_start_ts: (rx, tx)} for all interfaces, from since_ts onwards.

    SQLite groups by local hour itself, so half-hour timezones and DST are handled.
    """
    rows = conn.execute(
        "SELECT strftime('%Y-%m-%d %H', minute, 'unixepoch', 'localtime') AS hour, "
        "SUM(rx_bytes), SUM(tx_bytes) FROM usage WHERE minute >= ? GROUP BY hour",
        (int(since_ts),)).fetchall()
    return {int(datetime.strptime(hour, "%Y-%m-%d %H").timestamp()): (int(rx), int(tx))
            for hour, rx, tx in rows}


def hourly_series(conn, now=None):
    """Last 24 hourly buckets: (hour_start_timestamps, rx_list, tx_list)."""
    now = now or datetime.now()
    last_hour = now.replace(minute=0, second=0, microsecond=0)
    # Step in real seconds, not wall-clock hours, so a DST change can't skip or repeat a bucket.
    newest = int(last_hour.timestamp())
    stamps = [newest - 3600 * (23 - i) for i in range(24)]
    totals = _hour_totals(conn, stamps[0])
    rx = [totals.get(t, (0, 0))[0] for t in stamps]
    tx = [totals.get(t, (0, 0))[1] for t in stamps]
    return stamps, rx, tx


def daily_series(conn, days=30, now=None):
    """Last `days` local calendar days: (dates, rx_list, tx_list)."""
    now = now or datetime.now()
    midnight = datetime.combine(now.date(), datetime.min.time())
    first = midnight - timedelta(days=days - 1)
    totals = _hour_totals(conn, first.timestamp())
    per_day = defaultdict(lambda: [0, 0])
    for hour, (rx, tx) in totals.items():
        day = datetime.fromtimestamp(hour).date()
        per_day[day][0] += rx
        per_day[day][1] += tx
    dates = [(first + timedelta(days=i)).date() for i in range(days)]
    rx = [per_day[d][0] for d in dates]
    tx = [per_day[d][1] for d in dates]
    return dates, rx, tx


def realtime_series(rx_rates, tx_rates, length=REALTIME_SECONDS):
    """Last `length` one-second samples as (rx_list, tx_list) in bytes/s, oldest first.

    Shorter histories (right after start-up) are padded with zeros at the start.
    """
    pad = [0] * (length - len(rx_rates))
    return pad + list(rx_rates), pad + list(tx_rates)


def interfaces_today(conn, now=None, limit=8):
    """[(iface, rx, tx)] for today, busiest first."""
    now = now or datetime.now()
    midnight = datetime.combine(now.date(), datetime.min.time())
    rows = conn.execute(
        "SELECT iface, SUM(rx_bytes), SUM(tx_bytes) FROM usage "
        "WHERE minute >= ? GROUP BY iface",
        (int(midnight.timestamp()),)).fetchall()
    rows = [(iface, int(rx), int(tx)) for iface, rx, tx in rows]
    rows.sort(key=lambda r: r[1] + r[2], reverse=True)
    return rows[:limit]


# ---------- chart drawing (cairo) ----------

def _cairo_bridge_available():
    """PyGObject needs its cairo bridge (package python3-gi-cairo) to pass a cairo
    context to the draw handlers. Without it every chart is silently blank."""
    try:
        import gi._gi_cairo  # noqa: F401
    except ImportError:
        return False
    return True


def _nice_max(value):
    """Round a y-axis maximum up to a readable value (1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10 x 10^n)."""
    if value <= 0:
        return 1
    exp = 10 ** math.floor(math.log10(value))
    for mult in (1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10):
        if mult * exp >= value:
            return mult * exp
    return 10 * exp


def _text(cr, x, y, s, fg, size=10, align="left", alpha=0.85, bold=False):
    cr.select_font_face("Sans", 0, 1 if bold else 0)  # cairo: normal slant, bold/normal weight
    cr.set_font_size(size)
    width = cr.text_extents(s).x_advance
    if align == "right":
        x -= width
    elif align == "center":
        x -= width / 2
    cr.set_source_rgba(fg[0], fg[1], fg[2], alpha)
    cr.move_to(x, y)
    cr.show_text(s)


def _empty_note(cr, w, h, fg):
    _text(cr, w / 2, h / 2, "No traffic recorded yet", fg, size=12, align="center", alpha=0.6)


def _legend(cr, x, y, items, fg):
    for name, color in items:
        cr.set_source_rgb(*color)
        cr.rectangle(x, y - 9, 10, 10)
        cr.fill()
        _text(cr, x + 15, y, name, fg, size=10)
        x += 15 + cr.text_extents(name).x_advance + 18


def _frame(cr, w, h, fg, ymax, fmt=netmon.human_bytes):
    """Draw grid lines and y-axis labels. Returns the plot box (left, top, right, bottom)."""
    left, top, right, bottom = 70, 34, w - 12, h - 26
    for i in range(5):
        y = bottom - (bottom - top) * i / 4
        cr.set_source_rgba(fg[0], fg[1], fg[2], 0.12)
        cr.set_line_width(1)
        cr.move_to(left, round(y) + 0.5)
        cr.line_to(right, round(y) + 0.5)
        cr.stroke()
        _text(cr, left - 6, y + 4, fmt(ymax * i / 4), fg,
              size=9, align="right", alpha=0.7)
    return left, top, right, bottom


def _area(cr, xs, ys, baseline, color):
    cr.move_to(xs[0], baseline)
    for x, y in zip(xs, ys):
        cr.line_to(x, y)
    cr.line_to(xs[-1], baseline)
    cr.close_path()
    cr.set_source_rgba(*color, 0.22)
    cr.fill()
    cr.move_to(xs[0], ys[0])
    for x, y in zip(xs[1:], ys[1:]):
        cr.line_to(x, y)
    cr.set_source_rgb(*color)
    cr.set_line_width(2)
    cr.stroke()


def paint_hourly(cr, w, h, data, fg):
    stamps, rx, tx = data
    totals = [a + b for a, b in zip(rx, tx)]
    if not any(totals):
        _empty_note(cr, w, h, fg)
        return
    ymax = _nice_max(max(totals))
    left, top, right, bottom = _frame(cr, w, h, fg, ymax)
    n = len(stamps)
    xs = [left + (right - left) * i / (n - 1) for i in range(n)]

    def y_of(value):
        return bottom - (bottom - top) * value / ymax

    _area(cr, xs, [y_of(v) for v in tx], bottom, UP_COLOR)
    _area(cr, xs, [y_of(v) for v in rx], bottom, DOWN_COLOR)

    for x, stamp in zip(xs, stamps):
        when = datetime.fromtimestamp(stamp)
        if when.hour % 6 == 0:
            _text(cr, x, h - 8, when.strftime("%H:%M"), fg, size=9, align="center", alpha=0.7)
    _legend(cr, left, 14, [("Download", DOWN_COLOR), ("Upload", UP_COLOR)], fg)


def paint_daily(cr, w, h, data, fg):
    dates, rx, tx = data
    totals = [a + b for a, b in zip(rx, tx)]
    if not any(totals):
        _empty_note(cr, w, h, fg)
        return
    ymax = _nice_max(max(totals))
    left, top, right, bottom = _frame(cr, w, h, fg, ymax)
    n = len(dates)
    slot = (right - left) / n
    bar = slot * 0.7
    scale = (bottom - top) / ymax
    for i, (down, up) in enumerate(zip(rx, tx)):
        x = left + i * slot + (slot - bar) / 2
        down_h = down * scale
        up_h = up * scale
        cr.set_source_rgb(*DOWN_COLOR)
        cr.rectangle(x, bottom - down_h, bar, down_h)
        cr.fill()
        cr.set_source_rgb(*UP_COLOR)
        cr.rectangle(x, bottom - down_h - up_h, bar, up_h)
        cr.fill()
        if i % 5 == 4 or i == n - 1:  # label every 5th day, always including today
            _text(cr, x + bar / 2, h - 8, dates[i].strftime("%d %b"), fg,
                  size=9, align="center", alpha=0.7)
    _legend(cr, left, 14, [("Download", DOWN_COLOR), ("Upload", UP_COLOR)], fg)


def paint_realtime(cr, w, h, data, fg):
    rx, tx = data
    totals = [a + b for a, b in zip(rx, tx)]
    if not any(totals):
        _empty_note(cr, w, h, fg)
        return
    ymax = _nice_max(max(totals))
    left, top, right, bottom = _frame(cr, w, h, fg, ymax, fmt=netmon.human_rate)
    n = len(rx)
    xs = [left + (right - left) * i / (n - 1) for i in range(n)]

    def y_of(value):
        return bottom - (bottom - top) * value / ymax

    _area(cr, xs, [y_of(v) for v in tx], bottom, UP_COLOR)
    _area(cr, xs, [y_of(v) for v in rx], bottom, DOWN_COLOR)

    for i, x in enumerate(xs):
        ago = n - 1 - i  # seconds before the newest sample
        if ago % 30 == 0:
            label = "now" if ago == 0 else f"-{ago // 60}:{ago % 60:02d}"
            _text(cr, x, h - 8, label, fg, size=9, align="center", alpha=0.7)
    _legend(cr, left, 14, [("Download", DOWN_COLOR), ("Upload", UP_COLOR)], fg)


def paint_interfaces(cr, w, h, data, fg):
    if not any(rx + tx for _, rx, tx in data):
        _empty_note(cr, w, h, fg)
        return
    label_w, value_w = 96, 90
    row_h = (h - 16) / len(data)
    bar_max = max(40, w - label_w - value_w - 12)
    max_total = max(rx + tx for _, rx, tx in data)
    for i, (name, rx, tx) in enumerate(data):
        y = 8 + i * row_h + row_h * 0.2
        bar_h = row_h * 0.6
        _text(cr, 8, y + bar_h / 2 + 4, name, fg, size=11)
        x = label_w
        down_w = bar_max * rx / max_total
        up_w = bar_max * tx / max_total
        cr.set_source_rgb(*DOWN_COLOR)
        cr.rectangle(x, y, down_w, bar_h)
        cr.fill()
        x += down_w
        cr.set_source_rgb(*UP_COLOR)
        cr.rectangle(x, y, up_w, bar_h)
        cr.fill()
        x += up_w
        _text(cr, x + 8, y + bar_h / 2 + 4, netmon.human_bytes(rx + tx), fg, size=10)


class ChartArea(Gtk.DrawingArea):
    """A DrawingArea that paints one chart from data set with set_data()."""

    def __init__(self, painter, height):
        super().__init__()
        self.set_size_request(-1, height)
        self._painter = painter
        self._data = None
        self.connect("draw", self._on_draw)

    def set_data(self, data):
        self._data = data
        self.queue_draw()

    def _on_draw(self, widget, cr):
        if self._data is not None:
            color = self.get_style_context().get_color(Gtk.StateFlags.NORMAL)
            fg = (color.red, color.green, color.blue)  # follows light/dark theme
            self._painter(cr, self.get_allocated_width(), self.get_allocated_height(),
                          self._data, fg)
        return False


# ---------- main window ----------

class NetmonWindow(Gtk.ApplicationWindow):
    PERIOD_LABELS = [label for label, _, _ in netmon.periods(datetime.now())]

    def __init__(self, app):
        super().__init__(application=app, title="netmon")
        self.set_icon_name(ICON_NAME)
        self.set_default_size(560, 640)
        self.set_size_request(380, 420)

        self.conn = netmon.open_db(netmon.DEFAULT_DB)
        self.collector = netmon.Collector(self.conn)
        self.last_rates = (0, 0)  # (rx, tx) bytes/s from the latest sample
        self._rx_history = deque(maxlen=REALTIME_SECONDS)  # per-second rates for the live chart
        self._tx_history = deque(maxlen=REALTIME_SECONDS)
        self._ticks = 0

        self._build_header()
        self._build_body()
        self.connect("delete-event", self._on_close_requested)

        GLib.timeout_add(1000, self._tick)
        self._tick()

    def _build_header(self):
        self.header = Gtk.HeaderBar(show_close_button=True)  # native close button

        settings_btn = Gtk.Button.new_from_icon_name(
            "preferences-system-symbolic", Gtk.IconSize.BUTTON)
        settings_btn.set_tooltip_text("Settings")
        settings_btn.connect("clicked", self._on_settings)
        self.header.pack_end(settings_btn)

        # Primary menu (hamburger), the standard Ubuntu place for Quit.
        menu = Gio.Menu()
        menu.append("Quit", "app.quit")
        menu_btn = Gtk.MenuButton(menu_model=menu)
        menu_btn.set_image(Gtk.Image.new_from_icon_name("open-menu-symbolic", Gtk.IconSize.BUTTON))
        menu_btn.set_tooltip_text("Main menu")
        self.header.pack_end(menu_btn)

        self.set_titlebar(self.header)

    def _build_body(self):
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE,
                               transition_duration=200)
        self.stack.add_titled(self._scrolled(self._build_summary()), "summary", "Summary")
        self.stack.add_titled(self._scrolled(self._build_charts()), "charts", "Charts")
        self.stack.connect("notify::visible-child-name", self._on_page_changed)
        self.header.set_custom_title(Gtk.StackSwitcher(stack=self.stack))
        self.add(self.stack)

    @staticmethod
    def _scrolled(child):
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                      vscrollbar_policy=Gtk.PolicyType.AUTOMATIC)
        scroller.add(child)
        return scroller

    @staticmethod
    def _card(*children, spacing=8):
        """A rounded surface that stacks its children vertically."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=spacing)
        box.get_style_context().add_class("netmon-card")
        for child in children:
            box.pack_start(child, False, False, 0)
        return box

    def _build_summary(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        page.get_style_context().add_class("netmon-page")

        # Live speed: download and upload side by side.
        self.down_now = self._value_label(big=True)
        self.up_now = self._value_label(big=True)
        live = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        for caption, value in [("Download", self.down_now), ("Upload", self.up_now)]:
            card = self._card(self._text_label(caption, caption=True), value)
            card.set_hexpand(True)
            live.pack_start(card, True, True, 0)
        page.pack_start(live, False, False, 0)

        # Session totals.
        self.session = self._value_label()
        self.session.get_style_context().add_class("netmon-session")
        page.pack_start(self._card(self._text_label("This session", caption=True), self.session),
                        False, False, 0)

        # Usage table.
        table = Gtk.Grid(column_spacing=24, row_spacing=10)
        table.set_column_homogeneous(False)
        headers = ["Period", "Download", "Upload", "Total"]
        for col, title in enumerate(headers):
            label = self._text_label(title, table_head=True, align=col > 0)
            table.attach(label, col, 0, 1, 1)

        self.cells = {}
        for index, label in enumerate(self.PERIOD_LABELS):
            row = 2 * index + 1
            rule = Gtk.Separator()
            rule.get_style_context().add_class("netmon-rule")
            table.attach(rule, 0, row, len(headers), 1)
            name = self._text_label(label, today=(index == 0))
            name.set_hexpand(True)
            table.attach(name, 0, row + 1, 1, 1)
            down, up, total = (self._value_label(align=True) for _ in range(3))
            for col, widget in enumerate([down, up, total], start=1):
                table.attach(widget, col, row + 1, 1, 1)
            self.cells[label] = (down, up, total)
        page.pack_start(self._card(table), False, False, 0)

        # Footer: centred, pinned to the bottom of the page.
        footer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        footer.set_margin_top(4)
        self.status = self._text_label("", footer=True, center=True)
        footer.pack_start(self.status, False, False, 0)
        footer.pack_start(self._text_label("Created by Paritosh Chaudhari", footer=True,
                                           center=True), False, False, 0)
        page.set_vexpand(True)  # take the full height so the footer can sit at the bottom
        page.pack_end(footer, False, False, 0)
        return page

    def _build_charts(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        page.get_style_context().add_class("netmon-page")
        self.realtime = ChartArea(paint_realtime, 180)
        self.hourly = ChartArea(paint_hourly, 180)
        self.daily = ChartArea(paint_daily, 180)
        self.ifaces = ChartArea(paint_interfaces, 210)
        for title, hint, chart in [
            ("Live", "Last 2 minutes", self.realtime),
            ("Last 24 hours", "Traffic per hour", self.hourly),
            ("Last 30 days", "Traffic per day", self.daily),
            ("Network interfaces", "Today, busiest first", self.ifaces),
        ]:
            heading = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            heading.pack_start(self._text_label(title, section=True), False, False, 0)
            heading.pack_start(self._text_label(hint, caption=True), False, False, 0)
            page.pack_start(self._card(heading, chart, spacing=6), False, False, 0)
        return page

    @staticmethod
    def _text_label(text, align=False, center=False, **styles):
        """A plain label. Pass keyword flags such as caption=True or section=True
        to give it one of the netmon text styles defined in STYLE_CSS."""
        label = Gtk.Label(xalign=0.5 if center else 1 if align else 0)
        label.set_text(text)
        if center:
            label.set_justify(Gtk.Justification.CENTER)
            label.set_hexpand(True)
        context = label.get_style_context()
        for flag, css_class in LABEL_STYLES.items():
            if styles.get(flag):
                context.add_class(css_class)
        return label

    @staticmethod
    def _value_label(big=False, align=False):
        label = Gtk.Label(xalign=1 if align else 0)
        label.set_text("0 B/s" if big else "0 B" if align else "")
        if big:
            label.get_style_context().add_class("netmon-value")
        label.set_selectable(not big)  # speed readings are not text to copy; no caret
        return label

    # --- periodic update ---

    def _tick(self):
        # Always sample so session totals and the database stay current, even when
        # hidden. The collector writes to SQLite on its own schedule (FLUSH_SECONDS).
        self._ticks += 1
        try:
            self.last_rates = self.collector.tick()
            self._rx_history.append(self.last_rates[0])
            self._tx_history.append(self.last_rates[1])
            if self.get_visible():
                self._refresh_view()
                if self._charts_showing():
                    self._refresh_realtime()  # live chart moves every second
                    if self._ticks % CHART_REFRESH_SECONDS == 0:
                        self._refresh_charts()
        except Exception:
            # Any error must not stop the timer: GLib silently removes a timer whose
            # callback raises, which would freeze the display. Log it and retry next second.
            traceback.print_exc()
        return True  # keep the timer running

    def _charts_showing(self):
        return self.stack.get_visible_child_name() == "charts"

    def _on_page_changed(self, *_):
        if self._charts_showing():
            self._refresh_realtime()
            self._refresh_charts()  # show current data as soon as the page opens

    def _refresh_view(self):
        rx_rate, tx_rate = self.last_rates

        self.down_now.set_text(netmon.human_rate(rx_rate))
        self.up_now.set_text(netmon.human_rate(tx_rate))
        self.session.set_text(
            f"↓ {netmon.human_bytes(self.collector.session_rx)}   "
            f"↑ {netmon.human_bytes(self.collector.session_tx)}")

        for label, rx, tx in netmon.summary_rows(self.conn):
            down, up, total = self.cells[label]
            down.set_text(netmon.human_bytes(rx))
            up.set_text(netmon.human_bytes(tx))
            total.set_text(netmon.human_bytes(rx + tx))

        self.status.set_text(
            f"Updated {datetime.now():%H:%M:%S} · "
            f"Autostart {'on' if autostart_enabled() else 'off'}")

    def _refresh_realtime(self):
        self.realtime.set_data(realtime_series(self._rx_history, self._tx_history))

    def _refresh_charts(self):
        try:
            self.hourly.set_data(hourly_series(self.conn))
            self.daily.set_data(daily_series(self.conn))
            self.ifaces.set_data(interfaces_today(self.conn))
        except Exception:
            traceback.print_exc()

    def show_window(self):
        self.show_all()
        self.present()
        # Show current numbers right away instead of waiting for the next tick.
        try:
            self._refresh_view()
            if self._charts_showing():
                self._refresh_realtime()
                self._refresh_charts()
        except Exception:
            traceback.print_exc()

    # --- window events ---

    def _on_close_requested(self, *_):
        # Hide instead of destroying so monitoring continues in the background.
        self.hide()
        return True

    def shutdown(self):
        try:
            self.collector.flush()
        finally:
            self.conn.close()

    # --- settings dialog ---

    def _on_settings(self, _button):
        dialog = Gtk.Dialog(title="Settings", transient_for=self, modal=True)
        dialog.add_button("Close", Gtk.ResponseType.CLOSE)
        dialog.set_default_size(400, 0)

        content = dialog.get_content_area()
        content.set_border_width(20)
        content.set_spacing(12)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        title = self._text_label("Start when I log in", section=True)
        hint = self._text_label(
            "Open netmon automatically at login and record usage "
            "from the start of every session.", hint=True)
        hint.set_line_wrap(True)
        text.pack_start(title, False, False, 0)
        text.pack_start(hint, False, False, 0)

        switch = Gtk.Switch(active=autostart_enabled(), valign=Gtk.Align.CENTER)
        busy = {"resetting": False}  # True while we move the switch back ourselves

        def on_toggled(sw, _param):
            if busy["resetting"]:
                return
            try:
                set_autostart(sw.get_active())
            except OSError as exc:
                busy["resetting"] = True
                try:
                    error = Gtk.MessageDialog(
                        transient_for=dialog, modal=True,
                        message_type=Gtk.MessageType.ERROR,
                        buttons=Gtk.ButtonsType.CLOSE,
                        text="Could not change the autostart setting.")
                    error.format_secondary_text(str(exc))
                    error.run()
                    error.destroy()
                    sw.set_active(autostart_enabled())
                finally:
                    busy["resetting"] = False

        switch.connect("notify::active", on_toggled)

        row.pack_start(text, True, True, 0)
        row.pack_end(switch, False, False, 0)
        content.pack_start(row, False, False, 0)
        dialog.show_all()
        dialog.run()
        dialog.destroy()


# ---------- application ----------

def _apply_style():
    """Load STYLE_CSS for every window on the screen."""
    screen = Gdk.Screen.get_default()
    if screen is None:  # no display; GTK will report that when the app starts
        return
    provider = Gtk.CssProvider()
    try:
        provider.load_from_data(STYLE_CSS)
    except GLib.Error as exc:
        print(f"netmon: could not load the window style: {exc}", file=sys.stderr)
        return
    Gtk.StyleContext.add_provider_for_screen(
        screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


class NetmonApp(Gtk.Application):
    def __init__(self, start_hidden=False):
        super().__init__(application_id=APP_ID,
                         flags=Gio.ApplicationFlags.FLAGS_NONE)
        self.window = None
        # True when started by autostart (--background): collect without showing the window.
        self._start_hidden = start_hidden

        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_: self.quit())
        self.add_action(quit_action)
        self.set_accels_for_action("app.quit", ["<Primary>q"])

    def do_startup(self):
        Gtk.Application.do_startup(self)
        # Keep running (monitoring) even when the window is hidden.
        self.hold()
        # Ctrl+C in a terminal or `kill` should quit through do_shutdown, which saves
        # pending samples. Without this, the process dies and loses up to 10 s of data.
        for sig in (signal.SIGTERM, signal.SIGINT):
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, self._on_quit_signal)

    def _on_quit_signal(self):
        self.quit()
        return GLib.SOURCE_REMOVE

    def do_activate(self):
        # Only the first activation can be the login start; later ones come from the menu.
        start_hidden = self._start_hidden
        self._start_hidden = False
        if self.window is None:
            try:
                self.collector_lock = netmon.acquire_collector_lock(netmon.DEFAULT_DB)
            except netmon.CollectorBusy as exc:
                if start_hidden:
                    # Nobody asked for a window at login, so do not pop up a dialog.
                    print(f"netmon: not started at login: another netmon (pid {exc}) "
                          "is already recording usage.", file=sys.stderr)
                else:
                    self._explain_busy(str(exc))
                self.quit()
                return
            self.window = NetmonWindow(self)
            if start_hidden:
                # Login start: the window is built (its timer samples traffic) but not shown.
                return
        self.window.show_window()

    @staticmethod
    def _explain_busy(pid):
        dialog = Gtk.MessageDialog(
            message_type=Gtk.MessageType.WARNING, buttons=Gtk.ButtonsType.CLOSE,
            text="netmon is already running",
            secondary_text=f"Another netmon (pid {pid}) is already recording network usage. "
                           "Close it, then open netmon again.")
        dialog.run()
        dialog.destroy()

    def do_shutdown(self):
        if self.window is not None:
            self.window.shutdown()
        Gtk.Application.do_shutdown(self)


def main():
    apply_first_run_defaults()
    if not _cairo_bridge_available():
        print("netmon: the charts stay blank without the cairo bridge. Install it with: "
              "sudo apt-get install python3-gi-cairo", file=sys.stderr)
    Gtk.Window.set_default_icon_name(ICON_NAME)
    _apply_style()
    start_hidden = BACKGROUND_ARG in sys.argv[1:]
    argv = [arg for arg in sys.argv if arg != BACKGROUND_ARG]
    return NetmonApp(start_hidden=start_hidden).run(argv)


if __name__ == "__main__":
    sys.exit(main())
