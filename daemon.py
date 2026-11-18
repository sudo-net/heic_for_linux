import configparser
import os
import sys
import signal
import shutil
import subprocess
import base64
import plistlib
from datetime import datetime
import time

import pgeocode
from astral import LocationInfo
from astral.sun import elevation, azimuth, noon

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.ini")
PID_FILE = os.path.join(BASE_DIR, "daemon.pid")
LOG_FILE = os.path.join(BASE_DIR, "daemon.log")


def log(msg):
    timestamp = datetime.now().strftime("%H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def check_already_running():
    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE, "r") as f:
                old_pid = int(f.read().strip())
            if old_pid != os.getpid():
                os.kill(old_pid, 0)
                print(f"Daemon läuft bereits (PID {old_pid}). Beende zweiten Start.")
                sys.exit(0)
        except (ValueError, ProcessLookupError, PermissionError):
            pass


def write_pid():
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))


def cleanup_pid(*args):
    if os.path.exists(PID_FILE):
        try:
            os.remove(PID_FILE)
        except OSError:
            pass
    sys.exit(0)


def prepare_wallpaper_images(wallpaper_path):
    if os.path.isfile(wallpaper_path) and wallpaper_path.lower().endswith(".heic"):
        folder_name = wallpaper_path.rsplit('.', 1)[0] + "_extracted"
        if os.path.exists(folder_name):
            return folder_name

        log(f"-> Creating extraction folder: {folder_name}")
        os.makedirs(folder_name)
        log("-> Extracting images from HEIC...")
        output_pattern = os.path.join(folder_name, "frame.jpg")

        try:
            subprocess.run(["heif-convert", wallpaper_path, output_pattern], check=True, stdout=subprocess.DEVNULL)
            return folder_name
        except subprocess.CalledProcessError:
            log("Error: Could not extract images.")
            if os.path.exists(folder_name):
                shutil.rmtree(folder_name)
            cleanup_pid()
    return wallpaper_path


def extract_apple_metadata(wallpaper_path):
    try:
        result = subprocess.run(
            ["exiftool", "-b", "-Solar", wallpaper_path],
            capture_output=True, text=False, check=True
        )
        b64_data = result.stdout.strip()
        if not b64_data:
            return None

        plist_bytes = base64.b64decode(b64_data)
        return plistlib.loads(plist_bytes)
    except Exception:
        return None


def init_location_observer(zip_code, country="DE"):
    nomi = pgeocode.Nominatim(country.lower())
    location = nomi.query_postal_code(zip_code)

    if location.latitude != location.latitude:
        log("-> Error: ZIP code not found.")
        cleanup_pid()

    lat, lon = float(location.latitude), float(location.longitude)
    loc = LocationInfo("Local", "Region", "Europe/Berlin", lat, lon)
    log(f"-> Location initialized: ZIP {zip_code} ({lat:.4f}, {lon:.4f})")
    return loc.observer


def calculate_sun_position(observer):
    now = datetime.now().astimezone()
    current_alt = elevation(observer, now)
    current_az = azimuth(observer, now)
    solar_noon = noon(observer, date=now.date(), tzinfo=now.tzinfo)
    max_alt_today = elevation(observer, solar_noon)
    return current_alt, current_az, max_alt_today


def find_best_image_match(current_alt, current_az, max_alt_today, metadata):
    best_image_index = 0
    smallest_difference = float('inf')

    # Höchsten Sonnenstand in den HEIC-Metadaten ermitteln (meist Sommer ~65-75°)
    max_meta_alt = max((item['a'] for item in metadata['si']), default=65.0)

    # Jahreszeiten-Skalierung: Im Herbst/Winter steigt die Sonne mittags deutlich
    # flacher als in Apples Sommer-Metadaten. Wir skalieren positive Werte proportional,
    # damit mittags auch im Winter die echten Mittagsbilder erreicht werden.
    if current_alt > 0.0 and max_alt_today > 5.0 and max_meta_alt > max_alt_today:
        effective_alt = current_alt * (max_meta_alt / max_alt_today)
    else:
        effective_alt = current_alt

    az_weight = 0.02 if current_alt < -12.0 else 0.4
    is_morning = current_az < 180.0

    for item in metadata['si']:
        img_index = item['i']
        img_alt = item['a']
        img_az = item['z']

        diff_alt = abs(effective_alt - img_alt)
        diff_az = min(abs(current_az - img_az), 360 - abs(current_az - img_az))

        # 1. Tageshälften-Sperre (Ost vs. West):
        # Verhindert, dass am Morgen (Azimut < 180°) ein Abendbild (img_az > 200°)
        # gewählt wird oder am Abend (Azimut >= 180°) ein Morgenbild (img_az < 160°).
        hemisphere_penalty = 0
        if current_alt > -12.0 and img_alt > -12.0:
            if is_morning and img_az > 200.0:
                hemisphere_penalty = 500
            elif not is_morning and img_az < 160.0:
                hemisphere_penalty = 500

        # 2. Horizont-Sperre:
        # Erst nach Ende der Dämmerung (unter -8°) werden Tag-/Dämmerungsbilder
        # (über -2°) blockiert; tagsüber (über 0°) werden tiefe Nachtbilder blockiert.
        horizon_penalty = 0
        if current_alt < -8.0 and img_alt > -2.0:
            horizon_penalty = 1000
        elif current_alt > 0.0 and img_alt < -6.0:
            horizon_penalty = 1000

        total_diff = diff_alt + (diff_az * az_weight) + hemisphere_penalty + horizon_penalty

        if total_diff < smallest_difference:
            smallest_difference = total_diff
            best_image_index = img_index

    return best_image_index + 1


def set_gnome_wallpaper(image_path):
    uri = f"file://{os.path.abspath(image_path)}"
    try:
        subprocess.run(["gsettings", "set", "org.gnome.desktop.background", "picture-uri", uri], check=True)
        subprocess.run(["gsettings", "set", "org.gnome.desktop.background", "picture-uri-dark", uri], check=True)
    except subprocess.CalledProcessError:
        log("-> Error: Could not change GNOME wallpaper.")


def set_gnome_theme(current_alt):
    is_light = current_alt >= 0.0
    try:
        if is_light:
            subprocess.run(["gsettings", "set", "org.gnome.desktop.interface", "color-scheme", "default"], check=True)
            log("-> Theme: GNOME set to LIGHT mode.")
        else:
            subprocess.run(["gsettings", "set", "org.gnome.desktop.interface", "color-scheme", "prefer-dark"], check=True)
            log("-> Theme: GNOME set to DARK mode.")
    except subprocess.CalledProcessError:
        log("-> Error: Could not change GNOME theme.")


def load_configuration():
    if not os.path.exists(CONFIG_FILE):
        log(f"Error: {CONFIG_FILE} not found!")
        cleanup_pid()
    config = configparser.ConfigParser()
    config.read(CONFIG_FILE)
    return config


if __name__ == "__main__":
    check_already_running()
    signal.signal(signal.SIGTERM, cleanup_pid)
    signal.signal(signal.SIGINT, cleanup_pid)

    with open(LOG_FILE, "w") as f:
        f.write("")

    write_pid()
    my_config = load_configuration()

    path = my_config.get("General", "wallpaper_path")
    zip_code = my_config.get("Location", "zip_code")
    country = my_config.get("Location", "country", fallback="DE")

    log("--- heic_for_linux: Daemon started ---")

    final_folder = prepare_wallpaper_images(path)
    metadata = extract_apple_metadata(path)

    if not metadata or 'si' not in metadata:
        log("-> Error: No solar metadata found in HEIC file.")
        cleanup_pid()

    observer = init_location_observer(zip_code, country)

    last_active_frame = None
    last_theme_is_light = None

    while True:
        current_alt, current_az, max_alt_today = calculate_sun_position(observer)
        best_frame = find_best_image_match(current_alt, current_az, max_alt_today, metadata)

        if best_frame != last_active_frame:
            final_image = os.path.join(final_folder, f"frame-{best_frame}.jpg")
            log(f"-> New wallpaper: {final_image} (Sun alt: {current_alt:.1f}°, max today: {max_alt_today:.1f}°, az: {current_az:.1f}°)")
            set_gnome_wallpaper(final_image)
            last_active_frame = best_frame

        is_currently_light = current_alt >= 0.0
        if is_currently_light != last_theme_is_light:
            set_gnome_theme(current_alt)
            last_theme_is_light = is_currently_light

        time.sleep(300)

