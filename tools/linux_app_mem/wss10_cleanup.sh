#!/bin/bash
# Undo the test settings after the wss10 queue (same state as after the 2026-10-22 afternoon cleanup).
for k in show-startup-wizard show-to-do-bar show-attachment-bar; do gsettings reset org.gnome.evolution.mail $k; done
for k in height width x y; do gsettings reset org.gnome.evolution.window:/org/gnome/evolution/shell/window/ $k; done
gsettings reset org.gnome.evolution version; gsettings reset org.gnome.evolution.mail paned-size; gsettings reset org.gnome.evolution.mail to-do-bar-width
gsettings reset org.gnome.rhythmbox.rhythmdb locations; gsettings reset org.gnome.rhythmbox.rhythmdb monitor-library
X=$(mktemp -d); tar xzf "$HOME/linux_app_mem/evolution_cleanup/evolution_dirs_before.tgz" -C "$X"
for d in .local/share/evolution .config/evolution .cache/evolution; do rsync -a --delete "$X/$d/" "$HOME/$d/"; done
rm -r "$X"
find "$HOME/.local/share/gnote" "$HOME/.local/share/rhythmbox" -mindepth 1 -delete 2>/dev/null
rm -f "$HOME/.asoundrc"
for p in $(pgrep -x evolution-alarm); do tr '\0' '\n' < /proc/"$p"/environ | grep -qx 'DISPLAY=:0' && kill "$p"; done
echo "cleanup done $(date +%T)"
