#!/usr/bin/env python3
"""netmon - real-time network usage monitor for Ubuntu/Linux.
Created by Paritosh Chaudhari.

Reads byte counters from /proc/net/dev, stores per-minute usage in SQLite,
and shows live upload/download speed plus usage totals for:
  today, yesterday, last 7 days, last 30 days.

Commands:
  netmon.py live      collect data and show the live dashboard (Ctrl+C to quit)
  netmon.py collect   collect data silently (for background / autostart use)
  netmon.py report    print the usage summary and exit

Options:
  --db PATH           SQLite file (default: ~/.local/share/netmon/usage.db)
  --interval SECONDS  sampling interval (default: 1)
  --iface NAME        report only one interface (report command)

Only one process may collect into a database at a time (see acquire_collector_lock).
Only the Python standard library is used.
"""

import argparse
import fcntl
import os
import signal
import sqlite3
import sys
import time
from datetime import datetime, timedelta

PROC_NET_DEV = "/proc/net/dev"
DEFAULT_DB = os.path.expanduser("~/.local/share/netmon/usage.db")
# Virtual interfaces that would double-count traffic already seen on real NICs.
# Tunnels (VPNs) carry traffic that is also counted on the physical NIC they run over.
IGNORED_PREFIXES = ("lo", "docker", "br-", "veth", "virbr", "tun", "tap", "wg", "tailscale")
FLUSH_SECONDS = 10  # how often collected data is written to SQLite
DB_TIMEOUT_SECONDS = 10  # how long to wait for another writer before a write fails


# ---------- formatting ----------

def human_bytes(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{int(n)} B" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} TB"


def human_rate(bps):
    return human_bytes(bps) + "/s"


# ---------- system counters ----------

def read_counters():
    """Return {iface: (rx_bytes, tx_bytes)} from /proc/net/dev."""
    counters = {}
    with open(PROC_NET_DEV) as f:
        lines = f.readlines()[2:]  # first two lines are headers
    for line in lines:
        if ":" not in line:
            continue
        name, data = line.split(":", 1)
        name = name.strip()
        if name.startswith(IGNORED_PREFIXES):
            continue
        fields = data.split()
        if len(fields) < 9:
            continue
        counters[name] = (int(fields[0]), int(fields[8]))
    return counters


# ---------- database ----------

def _restrict_permissions(path):
    """Usage history shows when and how much you use the network: keep it private."""
    for p in (path, path + "-wal", path + "-shm"):
        try:
            os.chmod(p, 0o600)
        except FileNotFoundError:
            pass


def open_db(path):
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    conn = sqlite3.connect(path, timeout=DB_TIMEOUT_SECONDS)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS usage (
            minute   INTEGER NOT NULL,          -- unix time, start of the minute
            iface    TEXT    NOT NULL,
            rx_bytes INTEGER NOT NULL DEFAULT 0,
            tx_bytes INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (minute, iface)
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )""")
    conn.commit()
    _restrict_permissions(path)
    return conn


class CollectorBusy(Exception):
    """Another process is already recording traffic into this database.

    str(exc) is the PID of that process, when known.
    """


def acquire_collector_lock(db_path):
    """Take the single-collector lock for db_path and return it.

    Two collectors reading the same counters would both add the same bytes to the
    database, doubling the totals. Keep the returned file descriptor alive while
    collecting. The kernel releases the lock when the process exits, even after a
    crash, so a stale lock file never blocks the next start.
    Raises CollectorBusy if another process holds the lock.
    """
    lock_path = os.path.abspath(db_path) + ".lock"
    os.makedirs(os.path.dirname(lock_path), mode=0o700, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        owner = os.pread(fd, 32, 0).decode(errors="replace").strip()
        os.close(fd)
        raise CollectorBusy(owner or "unknown") from None
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode())
    return fd


# ---------- daily rollup ----------
# Once a day, the per-minute rows of finished days are replaced by one row per local
# hour and interface. That keeps the database small and keeps per-interface history.
# Older versions stored one summed row per hour under ROLLUP_IFACE ("*"). Those rows
# cannot be split by interface, so they are kept as they are.

ROLLUP_IFACE = "*"  # legacy: sum over all interfaces (a real NIC name never contains "*")
ROLLUP_META_KEY = "last_rollup_date"


def rollup_past_days(conn, now=None):
    """Replace per-minute rows before today with one summed row per local hour and interface.

    Runs in one transaction. Re-running it is harmless: hourly rows are summed again
    into the same keys. Returns the number of hourly rows written.
    """
    now = now or datetime.now()
    today_start = int(datetime.combine(now.date(), datetime.min.time()).timestamp())
    conn.commit()  # make sure no earlier work is still pending
    conn.execute("BEGIN IMMEDIATE")  # lock out other writers while we read and rewrite
    try:
        rows = conn.execute(
            "SELECT strftime('%Y-%m-%d %H', minute, 'unixepoch', 'localtime') AS hour, "
            "iface, SUM(rx_bytes), SUM(tx_bytes) FROM usage "
            "WHERE minute < ? AND iface != ? GROUP BY hour, iface",
            (today_start, ROLLUP_IFACE)).fetchall()
        hourly = [(int(datetime.strptime(hour, "%Y-%m-%d %H").timestamp()),
                   iface, int(rx), int(tx))
                  for hour, iface, rx, tx in rows]
        if hourly:
            conn.execute("DELETE FROM usage WHERE minute < ? AND iface != ?",
                         (today_start, ROLLUP_IFACE))
            # An hour may already have a rollup row if late samples arrived after an
            # earlier rollup; add to it instead of failing on the primary key.
            conn.executemany("""
                INSERT INTO usage (minute, iface, rx_bytes, tx_bytes)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(minute, iface) DO UPDATE SET
                    rx_bytes = rx_bytes + excluded.rx_bytes,
                    tx_bytes = tx_bytes + excluded.tx_bytes
            """, hourly)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return len(hourly)


def maybe_rollup(conn, now=None):
    """Run the rollup at most once per local calendar day. Returns rows written (0 if skipped)."""
    now = now or datetime.now()
    today = now.date().isoformat()
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (ROLLUP_META_KEY,)).fetchone()
    if row and row[0] == today:
        return 0
    written = rollup_past_days(conn, now)
    conn.execute("""
        INSERT INTO meta (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
    """, (ROLLUP_META_KEY, today))
    conn.commit()
    return written


def legacy_summary_start(conn):
    """Oldest time covered only by pre-upgrade summed rows (no per-interface detail), or None."""
    row = conn.execute("SELECT MIN(minute) FROM usage WHERE iface = ?", (ROLLUP_IFACE,)).fetchone()
    return row[0] if row and row[0] is not None else None


def period_totals(conn, start, end, iface=None):
    sql = ("SELECT COALESCE(SUM(rx_bytes), 0), COALESCE(SUM(tx_bytes), 0) "
           "FROM usage WHERE minute >= ? AND minute < ?")
    args = [int(start), int(end)]
    if iface:
        sql += " AND iface = ?"
        args.append(iface)
    return conn.execute(sql, args).fetchone()


def periods(now):
    """Return [(label, start_ts, end_ts)] for the summary periods."""
    now_ts = now.timestamp()
    end = now_ts + 60  # include the current minute bucket
    midnight = datetime.combine(now.date(), datetime.min.time())
    yesterday = midnight - timedelta(days=1)
    return [
        ("Today",        midnight.timestamp(),                        end),
        ("Yesterday",    yesterday.timestamp(),                       midnight.timestamp()),
        ("Last 7 days",  (now - timedelta(days=7)).timestamp(),       end),
        ("Last 30 days", (now - timedelta(days=30)).timestamp(),      end),
    ]


def summary_rows(conn, iface=None):
    now = datetime.now()
    return [(label, *period_totals(conn, start, end, iface))
            for label, start, end in periods(now)]


# ---------- collector ----------

class Collector:
    def __init__(self, conn):
        self.conn = conn
        self.prev = read_counters()
        # Elapsed time uses the monotonic clock, so a clock change can't skew rates or
        # stall flushing. The wall clock is only used to pick the minute for each sample.
        self.prev_mono = time.monotonic()
        self.pending = {}  # (minute, iface) -> [rx, tx], not yet written
        self.last_flush = time.monotonic()
        self.session_rx = 0
        self.session_tx = 0

    def tick(self):
        """Take one sample. Returns (rx_rate, tx_rate) in bytes/s, all interfaces."""
        now = time.time()
        mono = time.monotonic()
        cur = read_counters()
        elapsed = max(mono - self.prev_mono, 1e-6)
        minute = int(now) // 60 * 60
        rx_total = tx_total = 0

        for iface, (rx, tx) in cur.items():
            drx = dtx = 0
            if iface in self.prev:
                prx, ptx = self.prev[iface]
                # A counter going backwards means a reset (driver reload, reboot): skip it.
                drx = rx - prx if rx >= prx else 0
                dtx = tx - ptx if tx >= ptx else 0
            rx_total += drx
            tx_total += dtx
            slot = self.pending.setdefault((minute, iface), [0, 0])
            slot[0] += drx
            slot[1] += dtx

        self.session_rx += rx_total
        self.session_tx += tx_total
        self.prev, self.prev_mono = cur, mono

        if mono - self.last_flush >= FLUSH_SECONDS:
            self.flush()
        return rx_total / elapsed, tx_total / elapsed

    def flush(self):
        """Save pending samples. Database errors are reported, not raised: the samples stay
        pending and the save is retried at the next flush. Returns True if all work succeeded."""
        ok = True
        rows = [(m, i, rx, tx) for (m, i), (rx, tx) in self.pending.items() if rx or tx]
        if rows:
            try:
                self.conn.executemany("""
                    INSERT INTO usage (minute, iface, rx_bytes, tx_bytes)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(minute, iface) DO UPDATE SET
                        rx_bytes = rx_bytes + excluded.rx_bytes,
                        tx_bytes = tx_bytes + excluded.tx_bytes
                """, rows)
                self.conn.commit()
            except sqlite3.Error as exc:
                self.conn.rollback()
                print(f"netmon: could not save usage data, will retry: {exc}", file=sys.stderr)
                ok = False
        if ok:
            self.pending.clear()
            try:
                maybe_rollup(self.conn)  # no-op unless the day has changed since the last run
            except sqlite3.Error as exc:
                print(f"netmon: daily rollup failed, will retry: {exc}", file=sys.stderr)
                ok = False
        self.last_flush = time.monotonic()
        return ok


# ---------- commands ----------

def table_lines(rows):
    header = f"{'Period':<14}{'Download':>14}{'Upload':>14}{'Total':>14}"
    lines = [header, "-" * len(header)]
    for label, rx, tx in rows:
        lines.append(f"{label:<14}{human_bytes(rx):>14}{human_bytes(tx):>14}"
                     f"{human_bytes(rx + tx):>14}")
    return "\n".join(lines)


def cmd_report(conn, iface):
    scope = f" [{iface}]" if iface else ""
    print(f"Network usage summary{scope} - {datetime.now():%Y-%m-%d %H:%M}")
    print(table_lines(summary_rows(conn, iface)))
    if iface:
        legacy = legacy_summary_start(conn)
        window_start = (datetime.now() - timedelta(days=30)).timestamp()
        if legacy is not None and legacy >= window_start:
            print(f"Note: per-interface detail before {datetime.fromtimestamp(legacy):%Y-%m-%d} "
                  "was summarized by an older netmon version, so --iface totals leave it out.")


def cmd_collect(conn, interval):
    col = Collector(conn)
    try:
        while True:
            time.sleep(interval)
            col.tick()
    finally:
        col.flush()


def cmd_live(conn, interval):
    col = Collector(conn)
    try:
        while True:
            time.sleep(interval)
            rx_rate, tx_rate = col.tick()
            out = [
                "\033[H\033[2J",  # clear screen
                f"netmon  -  {datetime.now():%Y-%m-%d %H:%M:%S}   (Ctrl+C to quit)",
                "",
                f"  Download  {human_rate(rx_rate):>14}",
                f"  Upload    {human_rate(tx_rate):>14}",
                f"  Session   down {human_bytes(col.session_rx)}   up {human_bytes(col.session_tx)}",
                "",
                table_lines(summary_rows(conn)),
            ]
            print("\n".join(out), flush=True)
    finally:
        col.flush()


def main():
    p = argparse.ArgumentParser(description="Real-time network usage monitor with SQLite history.")
    p.add_argument("command", choices=["live", "collect", "report"])
    p.add_argument("--db", default=DEFAULT_DB, help="SQLite database path")
    p.add_argument("--interval", type=float, default=1.0, help="sampling interval in seconds")
    p.add_argument("--iface", help="only report this interface (report command)")
    args = p.parse_args()

    # Make `systemctl stop` behave like Ctrl+C so pending data is saved.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    conn = open_db(args.db)
    try:
        if args.command == "report":
            cmd_report(conn, args.iface)
        else:
            try:
                lock = acquire_collector_lock(args.db)  # noqa: F841  (held until exit)
            except CollectorBusy as exc:
                print(f"netmon: another netmon (pid {exc}) is already recording usage. "
                      "Close it first, or use 'netmon.py report' to view the totals.",
                      file=sys.stderr)
                return 1
            if args.command == "collect":
                cmd_collect(conn, args.interval)
            else:
                cmd_live(conn, args.interval)
    except KeyboardInterrupt:
        pass
    except BrokenPipeError:
        # stdout was closed (for example piped into head): stop quietly.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
