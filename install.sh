#!/usr/bin/env bash
# netmon installer for Ubuntu (per-user, no root needed except for the optional
# GTK package install).
#
#   ./install.sh               install netmon and add it to the application menu
#   ./install.sh --uninstall   remove the program, icon, menu entry and settings
#
# Installs to:
#   ~/.local/opt/netmon/                 program files
#   ~/.local/bin/netmon                  command-line launcher
#   ~/.local/share/applications/local.netmon.gui.desktop   menu entry (with icon)
#   ~/.local/share/icons/hicolor/scalable/apps/netmon.svg  app icon
# Usage data is kept in ~/.local/share/netmon/usage.db and is NOT removed on uninstall.

set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$HOME/.local/opt/netmon"
BIN_LINK="$HOME/.local/bin/netmon"
# Named after the application ID so GNOME/Ubuntu match running windows to this launcher
DESKTOP_FILE="$HOME/.local/share/applications/local.netmon.gui.desktop"
LEGACY_DESKTOP_FILE="$HOME/.local/share/applications/netmon.desktop"  # name used by older installs
ICON_FILE="$HOME/.local/share/icons/hicolor/scalable/apps/netmon.svg"
AUTOSTART_FILE="$HOME/.config/autostart/netmon.desktop"
CONFIG_DIR="$HOME/.config/netmon"
SETTINGS_FILE="$CONFIG_DIR/settings.json"
ICON_THEME_DIR="$HOME/.local/share/icons/hicolor"
APP_ID="local.netmon.gui"
SYSTEM_PYTHON="/usr/bin/python3"  # every launcher and the GTK check use this interpreter

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mError:\033[0m %s\n' "$*" >&2; exit 1; }

refresh_caches() {
    command -v gtk-update-icon-cache >/dev/null 2>&1 &&
        gtk-update-icon-cache -q -f -t "$ICON_THEME_DIR" 2>/dev/null || true
    command -v update-desktop-database >/dev/null 2>&1 &&
        update-desktop-database -q "$HOME/.local/share/applications" 2>/dev/null || true
}

# Print a double-quoted Exec= argument, escaped for a .desktop file:
# Exec quoting (\" \` \$ \\), then desktop-string escaping (\ -> \\), then % -> %%.
desktop_quote() {
    local s=$1 out='' c i
    for (( i = 0; i < ${#s}; i++ )); do
        c=${s:i:1}
        case $c in
            \\)            out+='\\\\' ;;
            '"'|'`'|'$')   out+='\\'"$c" ;;
            %)             out+='%%' ;;
            *)             out+=$c ;;
        esac
    done
    printf '"%s"' "$out"
}

# The Exec= value used by both the menu entry and the autostart entry.
exec_value() {
    printf '%s %s' "$(desktop_quote "$SYSTEM_PYTHON")" "$(desktop_quote "$APP_DIR/netmon_gui.py")"
}

# Replace the Exec= line of an existing .desktop file, keeping every other line as is.
rewrite_exec() {
    local file=$1 value=$2 tmp line
    tmp=$(mktemp "$file.XXXXXX")
    if ! {
        while IFS= read -r line || [[ -n "$line" ]]; do
            if [[ "$line" == Exec=* ]]; then
                printf 'Exec=%s\n' "$value"
            else
                printf '%s\n' "$line"
            fi
        done < "$file"
    } > "$tmp"; then
        rm -f "$tmp"
        die "Could not update $file"
    fi
    chmod 0644 "$tmp"
    mv -f "$tmp" "$file"
}

check_gtk() {
    [[ -x "$SYSTEM_PYTHON" ]] || die "$SYSTEM_PYTHON not found. Install python3 first."
    if "$SYSTEM_PYTHON" -c "import gi; gi.require_version('Gtk', '3.0'); from gi.repository import Gtk; import gi._gi_cairo" \
            >/dev/null 2>&1; then
        return
    fi
    warn "GTK 3 Python bindings are missing (python3-gi, python3-gi-cairo, gir1.2-gtk-3.0)."
    command -v apt-get >/dev/null 2>&1 || die "Install python3-gi, python3-gi-cairo and gir1.2-gtk-3.0 manually."

    # Ask on the terminal even when the script itself is piped in (curl ... | bash).
    if ! { true </dev/tty; } 2>/dev/null; then
        die "Install them first: sudo apt-get install python3-gi python3-gi-cairo gir1.2-gtk-3.0"
    fi
    local answer=""
    read -r -p "Install them now with apt (needs sudo)? [y/N] " answer </dev/tty || answer=""
    [[ "$answer" =~ ^[Yy]$ ]] || die "Cannot continue without GTK 3 bindings."
    sudo apt-get install -y python3-gi python3-gi-cairo gir1.2-gtk-3.0
}

install_app() {
    [[ -f "$SRC_DIR/netmon.py" && -f "$SRC_DIR/netmon_gui.py" && -f "$SRC_DIR/assets/netmon.svg" ]] \
        || die "Run this script from the netmon folder (netmon.py, netmon_gui.py, assets/ required)."

    check_gtk

    info "Copying program files to $APP_DIR"
    mkdir -p "$APP_DIR"
    install -m 0644 "$SRC_DIR/netmon.py" "$APP_DIR/netmon.py"
    install -m 0755 "$SRC_DIR/netmon_gui.py" "$APP_DIR/netmon_gui.py"

    info "Installing icon"
    mkdir -p "$(dirname "$ICON_FILE")"
    install -m 0644 "$SRC_DIR/assets/netmon.svg" "$ICON_FILE"

    info "Adding application menu entry"
    mkdir -p "$(dirname "$DESKTOP_FILE")"
    # Remove the launcher written by older versions (only if it is ours).
    if grep -qs "^StartupWMClass=$APP_ID\$" "$LEGACY_DESKTOP_FILE"; then
        rm -f "$LEGACY_DESKTOP_FILE"
    fi
    cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=netmon
GenericName=Network Usage Monitor
Comment=Live network speed and usage history
Exec=$(exec_value)
Icon=netmon
Terminal=false
Categories=Network;Monitor;
Keywords=network;bandwidth;usage;data;
StartupNotify=true
StartupWMClass=$APP_ID
EOF
    chmod 0644 "$DESKTOP_FILE"

    info "Creating command: netmon"
    mkdir -p "$(dirname "$BIN_LINK")"
    if [[ -e "$BIN_LINK" && ! -L "$BIN_LINK" ]]; then
        warn "$BIN_LINK exists and is not a netmon link, so it was left alone. Start netmon from the menu."
    else
        ln -sfn "$APP_DIR/netmon_gui.py" "$BIN_LINK"
    fi

    # If autostart was already enabled (from a previous run), point it at the installed copy.
    # --background makes the login start collect data without showing the window.
    if [[ -f "$AUTOSTART_FILE" ]]; then
        info "Updating autostart entry to the installed copy"
        rewrite_exec "$AUTOSTART_FILE" "$(exec_value) --background"
    fi

    refresh_caches

    info "netmon installed. Open it from the application menu, or run: netmon"
    case ":$PATH:" in
        *":$HOME/.local/bin:"*) ;;
        *) warn "$HOME/.local/bin is not in your PATH; use the menu entry or the full path." ;;
    esac
}

uninstall_app() {
    info "Removing netmon program files, icon, menu entry, command, autostart entry and settings"
    rm -rf "$APP_DIR"
    rm -f "$ICON_FILE" "$DESKTOP_FILE" "$LEGACY_DESKTOP_FILE" "$BIN_LINK" "$AUTOSTART_FILE" "$SETTINGS_FILE"
    rmdir "$CONFIG_DIR" 2>/dev/null || true
    refresh_caches
    info "Uninstalled. Usage history was kept at ~/.local/share/netmon/usage.db"
    info "Delete that folder too if you want to remove your data."
}

case "${1:-}" in
    "")          install_app ;;
    --uninstall) uninstall_app ;;
    -h|--help)   sed -n '2,15p' "${BASH_SOURCE[0]}" ;;
    *)           die "Unknown option: $1 (use --uninstall or --help)" ;;
esac
