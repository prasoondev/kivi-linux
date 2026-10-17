#!/bin/sh
# Removes everything install.sh put in your home directory. No sudo inside.
#   ./uninstall.sh          remove the app, keep settings, api key, learned words and history
#   ./uninstall.sh --purge  also delete ~/.config/kivi and ~/.local/share/kivi
set -e

purge=""
[ "$1" = "--purge" ] && purge=1

systemctl --user disable --now kivi.service >/dev/null 2>&1 || true
rm -f ~/.config/systemd/user/kivi.service
systemctl --user daemon-reload 2>/dev/null || true
systemctl --user reset-failed kivi.service 2>/dev/null || true
pkill -f "kivi settings" 2>/dev/null || true

rm -f ~/.local/bin/kivi
rm -f ~/.local/share/applications/ai.kivi.Linux.desktop
rm -f ~/.local/share/icons/hicolor/scalable/apps/kivi.svg ~/.local/share/icons/hicolor/*/apps/kivi.png
rm -rf ~/.local/share/fonts/kivi
rm -rf "${XDG_RUNTIME_DIR:-/nonexistent}/kivi" "${XDG_RUNTIME_DIR:-/nonexistent}/kivi.sock"

update-desktop-database -q ~/.local/share/applications 2>/dev/null || true
gtk-update-icon-cache -q -f -t ~/.local/share/icons/hicolor 2>/dev/null || true
command -v kbuildsycoca6 >/dev/null && kbuildsycoca6 --noincremental >/dev/null 2>&1 || true
fc-cache -f ~/.local/share/fonts >/dev/null 2>&1 || true

if [ -n "$purge" ]; then
  rm -rf ~/.config/kivi ~/.local/share/kivi
  echo "kivi removed, including your settings, api key, learned words and history."
else
  echo "kivi removed. kept ~/.config/kivi (settings, api key, words) and ~/.local/share/kivi (history)."
  echo "run ./uninstall.sh --purge to delete those too."
fi

echo
echo "system-wide bits install.sh asked you to add (only if you did, and nothing else needs them):"
[ -f /etc/udev/rules.d/60-kivi-uinput.rules ] && \
  echo "  sudo rm /etc/udev/rules.d/60-kivi-uinput.rules /etc/modules-load.d/uinput.conf && sudo udevadm control --reload"
id -nG "$USER" | grep -qw input && echo "  sudo gpasswd -d $USER input"
echo "then delete this folder: rm -rf \"$(cd "$(dirname "$0")" && pwd)\""
