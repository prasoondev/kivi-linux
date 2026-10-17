#!/bin/sh
# Installs kivi for the current user. Safe to run again; never uses sudo itself.
set -e
cd "$(dirname "$0")"
REPO=$PWD
PY=/usr/bin/python3

missing_py=""
$PY -c "import evdev" 2>/dev/null || missing_py="$missing_py python-evdev"
$PY -c "import gi; gi.require_version('Gtk', '4.0'); from gi.repository import Gtk" 2>/dev/null || missing_py="$missing_py gtk4 python-gobject"
$PY -c "import gi; gi.require_version('Adw', '1'); from gi.repository import Adw" 2>/dev/null || missing_py="$missing_py libadwaita"
if [ -n "$missing_py" ]; then
  echo "kivi needs:$missing_py"
  echo "  sudo pacman -S --needed$missing_py"
  exit 1
fi

mkdir -p ~/.local/bin ~/.config/systemd/user \
  ~/.local/share/icons/hicolor/scalable/apps ~/.local/share/applications ~/.local/share/kivi
mkdir -p -m 700 ~/.config/kivi

chmod +x bin/kivi
ln -sfn "$REPO/bin/kivi" ~/.local/bin/kivi

install -m 644 data/kivi.svg ~/.local/share/icons/hicolor/scalable/apps/kivi.svg
gtk-update-icon-cache -q -t ~/.local/share/icons/hicolor 2>/dev/null || true
# absolute Exec: ~/.local/bin isn't always on the session PATH
sed "s|^Exec=kivi |Exec=$HOME/.local/bin/kivi |" data/kivi.desktop > ~/.local/share/applications/ai.kivi.Linux.desktop
update-desktop-database -q ~/.local/share/applications 2>/dev/null || true

if [ ! -f ~/.config/kivi/config.toml ]; then
  cp config.example.toml ~/.config/kivi/config.toml
elif grep -q '^hotkey *=' ~/.config/kivi/config.toml; then
  echo "note: your config has v1 keys (hotkey/command_key); shortcuts now live under [shortcuts]."
  echo "      see config.example.toml, or change them in kivi settings → shortcuts."
fi
[ -f ~/.config/kivi/api_key ] && chmod 600 ~/.config/kivi/api_key

install -m 644 kivi.service ~/.config/systemd/user/kivi.service
systemctl --user daemon-reload
systemctl --user enable kivi.service >/dev/null 2>&1
systemctl --user restart kivi.service || echo "kivi did not start: journalctl --user -u kivi -n 30"

# --- what's still missing (all optional except keyboard access)
pkgs=""
[ -e /usr/lib/libgtk4-layer-shell.so ] || pkgs="$pkgs gtk4-layer-shell"
command -v wl-copy >/dev/null 2>&1 || pkgs="$pkgs wl-clipboard"

input_ok=""
for dev in /dev/input/event*; do
  [ -r "$dev" ] && { input_ok=1; break; }
done
in_group=""
id -nG | grep -qw input && in_group=1
uinput_ok=""
[ -w /dev/uinput ] && uinput_ok=1

fix=""
[ -n "$pkgs" ] && fix="sudo pacman -S --needed$pkgs"
if [ -z "$in_group" ]; then
  [ -n "$fix" ] && fix="$fix && "
  fix="${fix}sudo usermod -aG input \$USER"
fi

echo
echo "kivi is installed. open it from your app menu, or run: kivi settings"
[ -s ~/.config/kivi/api_key ] || [ -n "$SARVAM_API_KEY" ] || \
  echo "  add your sarvam api key in kivi settings → api key (get one at dashboard.sarvam.ai)"
if [ -n "$fix" ]; then
  echo
  echo "one more step:"
  echo "  $fix"
fi
if [ -z "$input_ok" ] || [ -z "$uinput_ok" ]; then
  echo
  echo "to use hotkeys right now without logging out (lasts until reboot):"
  [ -z "$input_ok" ] && echo "  sudo setfacl -m u:\$USER:rw /dev/input/event*"
  [ -z "$uinput_ok" ] && echo "  sudo setfacl -m u:\$USER:rw /dev/uinput"
  echo "  systemctl --user restart kivi"
fi
if [ -n "$input_ok" ]; then
  echo
  echo "hold right ctrl and talk. logs: journalctl --user -u kivi -f"
fi
