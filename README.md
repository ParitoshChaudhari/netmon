# netmon

Created by Paritosh Chaudhari.

netmon is a network usage monitor for Ubuntu (Linux). It shows live download and upload speed, usage totals for today, yesterday, the last 7 days and the last 30 days, and charts of traffic per hour, per day and per network interface.

It has two parts:

- `netmon_gui.py` is a native desktop window built with GTK 3. It runs in the background after you log in, collects data, and can be opened from the application menu.
- `netmon.py` is the collector and reporting module. It can also be run from the terminal to print a usage report.

netmon uses only Python's standard library and Ubuntu's built-in GTK 3 bindings. It needs no internet access and sends no data anywhere.

## Features

- Live download and upload speed, and the session total since the window was opened.
- Usage summary table for Today, Yesterday, Last 7 days and Last 30 days.
- Charts:
  - Live download and upload speed for the last 2 minutes (updated every second).
  - Traffic per hour for the last 24 hours.
  - Traffic per day for the last 30 days.
  - Traffic per network interface for today (up to 8 interfaces, busiest first).
- Autostart at login, on by default. It can be turned on or off in the Settings dialog. When netmon starts at login it collects data in the background and does not show the window; open it from the menu whenever you want to see the numbers.
- Closing the window keeps monitoring in the background. Use the main menu (Quit) or Ctrl+Q to exit completely.
- Starting the app again brings the existing window to the front.

## Requirements

- Ubuntu (or another Linux distribution with GNOME and GTK 3).
- Python 3 at `/usr/bin/python3`.
- GTK 3 Python bindings: `python3-gi`, `python3-gi-cairo` and `gir1.2-gtk-3.0`. The cairo bridge (`python3-gi-cairo`) is what lets the charts draw; without it the charts stay blank. The installer can install these for you with apt, which needs sudo.

To install the GTK packages manually:

```
sudo apt-get install python3-gi python3-gi-cairo gir1.2-gtk-3.0
```

## Installation

There are two ways to install. Use one of them, not both.

### Option 1: Debian package (all users)

From the folder that contains `netmon_1.0.0_all.deb`, install it with apt. apt also installs the GTK 3 packages netmon needs:

```
sudo apt install ./netmon_1.0.0_all.deb
```

This installs netmon for every user on the computer:

- The program files in `/usr/lib/netmon/`.
- The command `/usr/bin/netmon`.
- The application menu entry and icon.
- An autostart entry in `/etc/xdg/autostart/netmon.desktop`, so netmon starts at login in the background.

To remove it, run:

```
sudo apt remove netmon
```

Your usage history in `~/.local/share/netmon/` is kept.

If you installed netmon earlier with `./install.sh` (Option 2), uninstall that first with `./install.sh --uninstall`. Otherwise you will have two menu entries, and `~/.local/bin/netmon` will take precedence over `/usr/bin/netmon`.

### Option 2: Installer script (current user only)

Run the installer from the netmon folder:

```
./install.sh
```

If the file is not executable, run `chmod +x install.sh` first.

The installer does the following, without needing root for these steps:

1. Checks that the GTK 3 bindings are available. If they are missing, it asks whether to install them with apt.
2. Copies `netmon.py` and `netmon_gui.py` to `~/.local/opt/netmon/`.
3. Installs the app icon to `~/.local/share/icons/hicolor/scalable/apps/netmon.svg`.
4. Adds an application menu entry at `~/.local/share/applications/local.netmon.gui.desktop`.
5. Creates the command `~/.local/bin/netmon`, a link to `netmon_gui.py`.

After installation, open netmon from the application menu, or run:

```
netmon
```

If `~/.local/bin` is not in your PATH, use the menu entry or the full path `~/.local/bin/netmon`.

Running `./install.sh` again updates the installed files. If autostart was already enabled, its entry is updated to point to the installed copy.

## Uninstallation

Run:

```
./install.sh --uninstall
```

This removes:

- The program files in `~/.local/opt/netmon/`.
- The command `~/.local/bin/netmon`.
- The application menu entry and the older menu entry name, if present.
- The app icon.
- The autostart entry `~/.config/autostart/netmon.desktop`.
- The settings file `~/.config/netmon/settings.json`, and the folder `~/.config/netmon/` if it is empty.

The usage history is not removed. To delete it as well, remove the data folder:

```
rm -r ~/.local/share/netmon
```

The GTK packages installed with apt are not removed by the uninstaller.

## Where data is stored

| Path | Contents |
| --- | --- |
| `~/.local/share/netmon/usage.db` | SQLite database with one row per minute per interface (`minute`, `iface`, `rx_bytes`, `tx_bytes`). |
| `~/.config/netmon/settings.json` | Settings, including whether the first-run defaults have been applied. |
| `~/.config/autostart/netmon.desktop` | Autostart entry, present only when autostart is on. With a Debian package install, switching autostart off writes a `Hidden=true` entry here instead, because the package's system-wide entry would otherwise still start netmon. |

The database is created automatically the first time netmon runs.

## Command-line use

The collector module can be used directly from the folder it is in. The default database is `~/.local/share/netmon/usage.db`.

```
python3 netmon.py live                 # collect data and show the live dashboard (Ctrl+C to quit)
python3 netmon.py collect              # collect data silently
python3 netmon.py report               # print the usage summary and exit
python3 netmon.py report --iface eth0  # summary for one interface only
```

Options:

- `--db PATH` selects a different SQLite file.
- `--interval SECONDS` sets the sampling interval. The default is 1 second.
- `--iface NAME` limits the `report` command to one interface.
- Only one netmon collects at a time. If the GUI is running, `netmon.py live` and `collect` refuse to start. `report` always works.

## How it works

netmon reads the byte counters for each network interface from `/proc/net/dev` once per sampling interval. It takes the difference from the previous sample and adds it to the current minute in memory. Pending values are written to the SQLite database in batches. If a counter goes backwards, for example after a driver reload or reboot, that sample is skipped.

Virtual and tunnel interfaces are not counted: `lo`, `docker*`, `br-*`, `veth*`, `virbr*`, `tun*`, `tap*`, `wg*` and `tailscale*`. A VPN's traffic is counted on the physical network card it runs over, so VPN totals include the encryption overhead.

The Charts page reads the database and redraws every 5 seconds while it is visible. Totals are computed from the local time zone, so day and hour boundaries follow your system settings.

## Troubleshooting

- **"GTK 3 Python bindings are missing"**: install them with `sudo apt-get install python3-gi python3-gi-cairo gir1.2-gtk-3.0`, then run the installer again.
- **The charts are blank**: install the cairo bridge with `sudo apt-get install python3-gi-cairo`, then restart netmon (the app prints this hint to the terminal when the bridge is missing).
- **The `netmon` command is not found**: add `~/.local/bin` to your PATH, or open netmon from the application menu.
- **No data in the charts**: data is only collected while netmon is running. Check that the app is running, or that autostart is on.
- **No window after login**: this is expected. At login netmon starts in the background and collects data without showing the window. Open netmon from the application menu to see the numbers.
- **"not started at login: another netmon ... is already recording usage" in the log**: another netmon or `netmon.py collect` is already collecting, so the login start stops without a dialog. Open netmon from the menu to see the existing collector's numbers.
- **"another netmon is already recording usage"**: the GUI or another `live`/`collect` process is collecting. Close it, then start again. Two collectors would count the same traffic twice.
- **Errors reading the database**: the file may be locked by another process or damaged. Stop netmon, then back up or remove `~/.local/share/netmon/usage.db`.

## Files in this project

| File | Purpose |
| --- | --- |
| `netmon.py` | Collector, database and report code. |
| `netmon_gui.py` | GTK 3 desktop window, charts and settings. |
| `install.sh` | Installer and uninstaller. |
| `assets/netmon.svg` | Application icon. |
| `netmon_1.0.0_all.deb` | Debian package for system-wide installation (Option 1). |
