#!/usr/bin/env python3
import fcntl
import sys
import os
import time
import signal
import shutil
import subprocess
import configparser
import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
gi.require_version('Gio', '2.0')
from gi.repository import Gtk, Adw, Gio

COUNTRIES = [
    ("DE", "Deutschland (DE)"),
    ("AT", "Österreich (AT)"),
    ("CH", "Schweiz (CH)"),
    ("GB", "United Kingdom (GB)"),
    ("US", "United States (US)"),
    ("CA", "Canada (CA)"),
    ("AU", "Australia (AU)"),
    ("ES", "España (ES)"),
    ("FR", "France (FR)"),
    ("IT", "Italia (IT)"),
    ("NL", "Nederland (NL)"),
    ("BE", "België / Belgique (BE)"),
    ("PL", "Polska (PL)"),
    ("SE", "Sverige (SE)"),
    ("NO", "Norge (NO)"),
    ("DK", "Danmark (DK)"),
    ("FI", "Suomi (FI)"),
    ("JP", "Japan (JP)"),
    ("NZ", "New Zealand (NZ)"),
]

import gettext
import locale

LOCALE_DIR = "/app/share/locale" if os.path.exists("/.flatpak-info") else os.path.join(os.path.dirname(os.path.abspath(__file__)), "locale")
try:
    locale.setlocale(locale.LC_ALL, "")
except locale.Error:
    pass
gettext.bindtextdomain("heic_for_linux", LOCALE_DIR)
gettext.textdomain("heic_for_linux")
_ = gettext.gettext

APP_ID = "io.github.sudo_net.heic_for_linux"
APP_VERSION = "0.2.0"
IS_FLATPAK = os.path.exists("/.flatpak-info")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.expanduser("~/.local/share/heic_for_linux")
WALLPAPER_STORE = os.path.join(DATA_DIR, "wallpapers")
CONFIG_FILE = os.path.join(DATA_DIR, "config.ini")
PID_FILE = os.path.join(DATA_DIR, "daemon.pid")
LOCK_FILE = os.path.join(DATA_DIR, "daemon.lock")
STOP_FILE = os.path.join(DATA_DIR, "daemon.stop")
DAEMON_SCRIPT = "/app/bin/heic-daemon" if IS_FLATPAK else os.path.join(BASE_DIR, "daemon.py")
ICON_PATH = APP_ID if IS_FLATPAK else os.path.join(BASE_DIR, "icon.svg")

AUTOSTART_DIR = os.path.expanduser("~/.config/autostart")
AUTOSTART_FILE = os.path.join(AUTOSTART_DIR, f"{APP_ID}.daemon.desktop")


class HeicForLinuxApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID,
                         flags=Gio.ApplicationFlags.FLAGS_NONE)

    def do_activate(self):
        self.win = HeicWindow(application=self)
        self.win.present()


class HeicWindow(Adw.ApplicationWindow):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        os.makedirs(DATA_DIR, exist_ok=True)
        os.makedirs(WALLPAPER_STORE, exist_ok=True)

        self.set_title(f"HEIC for Linux (v{APP_VERSION})")
        self.set_default_size(540, 680)

        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        header = Adw.HeaderBar()
        content_box.append(header)

        self.toast_overlay = Adw.ToastOverlay()
        content_box.append(self.toast_overlay)

        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        self.toast_overlay.set_child(scroll)

        clamp = Adw.Clamp()
        clamp.set_maximum_size(480)
        clamp.set_margin_top(24)
        clamp.set_margin_bottom(24)
        clamp.set_margin_start(12)
        clamp.set_margin_end(12)
        scroll.set_child(clamp)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        clamp.set_child(box)

        daemon_group = Adw.PreferencesGroup(
            title=_("Background Service"),
            description="Steuert den automatischen Wechsel von Wallpaper und Dark/Light-Mode."
        )
        box.append(daemon_group)

        self.row_daemon = Adw.ActionRow(title=_("Automatic Switching"), subtitle=_("Checking status..."))
        daemon_group.add(self.row_daemon)

        self.switch_daemon = Gtk.Switch()
        self.switch_daemon.set_valign(Gtk.Align.CENTER)
        self.row_daemon.add_suffix(self.switch_daemon)
        self.row_daemon.set_activatable_widget(self.switch_daemon)

        self.row_autostart = Adw.ActionRow(
            title=_("Start automatically on login"),
            subtitle=_("Starts the background service automatically on login")
        )
        daemon_group.add(self.row_autostart)

        self.switch_autostart = Gtk.Switch()
        self.switch_autostart.set_valign(Gtk.Align.CENTER)
        self.row_autostart.add_suffix(self.switch_autostart)
        self.row_autostart.set_activatable_widget(self.switch_autostart)

        pref_group = Adw.PreferencesGroup(
            title=_("Configuration"),
            description="Wähle dein dynamisches Apple HEIC-Hintergrundbild und deinen Standort."
        )
        box.append(pref_group)

        self.row_file = Adw.ActionRow(title=_("HEIC Wallpaper"), subtitle=_("No file selected"))
        pref_group.add(self.row_file)

        btn_file = Gtk.Button(label=_("Browse..."))
        btn_file.set_valign(Gtk.Align.CENTER)
        btn_file.connect("clicked", self.on_select_file)
        self.row_file.add_suffix(btn_file)

        self.row_zip = Adw.ActionRow(title=_("Postal Code"), subtitle=_("For astronomical solar position calculation"))
        pref_group.add(self.row_zip)

        self.entry_zip = Gtk.Entry()
        self.entry_zip.set_valign(Gtk.Align.CENTER)
        self.entry_zip.set_max_length(12)
        self.row_zip.add_suffix(self.entry_zip)

        self.row_country = Adw.ComboRow(title=_("Country"), subtitle=_("For international postal code lookup"))
        self.row_country.set_model(Gtk.StringList.new([label for _, label in COUNTRIES]))
        self.row_country.set_selected(0)
        pref_group.add(self.row_country)

        btn_save = Gtk.Button(label=_("Save and Apply Settings"))
        btn_save.add_css_class("pill")
        btn_save.add_css_class("suggested-action")
        btn_save.connect("clicked", self.on_save_config)

        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, halign=Gtk.Align.CENTER)
        btn_box.append(btn_save)
        box.append(btn_box)

        self.set_content(content_box)

        self.load_config()
        self.refresh_daemon_status()
        self.switch_autostart.set_active(os.path.exists(AUTOSTART_FILE))

        self.switch_daemon.connect("notify::active", self.on_daemon_toggled)
        self.switch_autostart.connect("notify::active", self.on_autostart_toggled)

    def show_toast(self, message):
        toast = Adw.Toast.new(message)
        toast.set_timeout(3)
        self.toast_overlay.add_toast(toast)

    def get_python_executable(self):
        if IS_FLATPAK:
            return sys.executable
        venv_python = os.path.join(BASE_DIR, "venv", "bin", "python3")
        if os.path.exists(venv_python):
            return venv_python
        return sys.executable

    def get_running_pid(self):
        if not os.path.exists(LOCK_FILE):
            return None
        try:
            with open(LOCK_FILE, "r+") as f:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(f, fcntl.LOCK_UN)
                return None
        except BlockingIOError:
            return True
        except OSError:
            return None

    def refresh_daemon_status(self):
        pid = self.get_running_pid()
        if pid:
            self.switch_daemon.set_active(True)
            self.row_daemon.set_subtitle(_("Running in background"))
        else:
            self.switch_daemon.set_active(False)
            self.row_daemon.set_subtitle(_("Stopped"))

    def start_daemon(self):
        if self.get_running_pid():
            return True

        wallpaper = self.row_file.get_subtitle()
        if not wallpaper or not os.path.isfile(wallpaper):
            self.show_toast("Bitte zuerst eine gültige HEIC-Datei auswählen!")
            return False

        if not os.path.exists(CONFIG_FILE):
            self.save_config_file()

        python_bin = self.get_python_executable()
        proc = subprocess.Popen(
            [python_bin, DAEMON_SCRIPT],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True
        )
        time.sleep(0.15)
        self.row_daemon.set_subtitle(_("Running in background"))
        return True

    def stop_daemon(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(STOP_FILE, "w") as f:
            f.write("stop")
        for i in range(15):
            if not self.get_running_pid():
                break
            time.sleep(0.1)
        if os.path.exists(PID_FILE):
            try:
                os.remove(PID_FILE)
            except OSError:
                pass
        self.row_daemon.set_subtitle(_("Stopped"))

    def on_daemon_toggled(self, switch, gparam):
        if switch.get_active():
            if not self.start_daemon():
                switch.set_active(False)
            else:
                self.show_toast("Hintergrunddienst gestartet")
        else:
            self.stop_daemon()
            self.show_toast("Hintergrunddienst gestoppt")

    def on_autostart_toggled(self, switch, gparam):
        if switch.get_active():
            os.makedirs(AUTOSTART_DIR, exist_ok=True)
            if IS_FLATPAK:
                exec_cmd = f"flatpak run --command=heic-daemon {APP_ID}"
            else:
                python_bin = self.get_python_executable()
                exec_cmd = f"{python_bin} {DAEMON_SCRIPT}"

            desktop_content = (
                "[Desktop Entry]\n"
                "Type=Application\n"
                "Name=HEIC for Linux Daemon\n"
                "Comment=Dynamischer HEIC-Hintergrunddienst\n"
                f"Exec={exec_cmd}\n"
                f"Icon={ICON_PATH}\n"
                "Terminal=false\n"
                "X-GNOME-Autostart-enabled=true\n"
            )
            with open(AUTOSTART_FILE, "w") as f:
                f.write(desktop_content)
            self.show_toast("Autostart beim Login aktiviert")
        else:
            if os.path.exists(AUTOSTART_FILE):
                os.remove(AUTOSTART_FILE)
            self.show_toast("Autostart deaktiviert")

    def load_config(self):
        if os.path.exists(CONFIG_FILE):
            config = configparser.ConfigParser()
            config.read(CONFIG_FILE)
            if config.has_section("General") and config.has_option("General", "wallpaper_path"):
                self.row_file.set_subtitle(config.get("General", "wallpaper_path"))
            if config.has_section("Location"):
                if config.has_option("Location", "zip_code"):
                    self.entry_zip.set_text(config.get("Location", "zip_code"))
                if config.has_option("Location", "country"):
                    saved_country = config.get("Location", "country").strip().upper()
                    codes = [c for c, _ in COUNTRIES]
                    self.row_country.set_selected(codes.index(saved_country) if saved_country in codes else 0)

    def on_select_file(self, button):
        dialog = Gtk.FileDialog.new()
        dialog.set_title(_("Select HEIC Wallpaper"))

        filter_heic = Gtk.FileFilter()
        filter_heic.set_name("Apple HEIC Wallpaper (*.heic)")
        filter_heic.add_pattern("*.heic")
        filter_heic.add_pattern("*.HEIC")

        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(filter_heic)
        dialog.set_filters(filters)

        dialog.open(self, None, self.on_file_selected)

    def on_file_selected(self, dialog, result):
        try:
            file = dialog.open_finish(result)
            if file:
                selected_path = file.get_path()
                if selected_path and os.path.isfile(selected_path):
                    dest_path = os.path.join(WALLPAPER_STORE, os.path.basename(selected_path))
                    if os.path.abspath(selected_path) != os.path.abspath(dest_path):
                        shutil.copy2(selected_path, dest_path)
                    self.row_file.set_subtitle(dest_path)
        except Exception:
            pass

    def save_config_file(self):
        config = configparser.ConfigParser()
        config["General"] = {
            "wallpaper_path": self.row_file.get_subtitle()
        }
        config["Location"] = {
            "zip_code": self.entry_zip.get_text().strip(),
            "country": COUNTRIES[self.row_country.get_selected()][0]
        }
        with open(CONFIG_FILE, "w") as f:
            config.write(f)

    def on_save_config(self, button):
        self.save_config_file()
        if self.get_running_pid():
            self.stop_daemon()
            self.start_daemon()
            self.show_toast("Gespeichert und Hintergrundbild aktualisiert")
        else:
            self.show_toast("Einstellungen gespeichert")


if __name__ == "__main__":
    app = HeicForLinuxApp()
    app.run(sys.argv)
