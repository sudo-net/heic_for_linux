import configparser
import os
import sys
import shutil
import subprocess
import base64
import plistlib
from datetime import datetime
import time

try:
    import pgeocode
    from astral import LocationInfo
    from astral.sun import elevation, azimuth
except ImportError:
    # Wird direkt im Anschluss von check_dependencies() sauber abgefangen
    pass

CONFIG_FILE = "config.ini"


def get_os_name():
    if os.path.exists("/etc/os-release"):
        with open("/etc/os-release", "r") as f:
            for line in f:
                if line.startswith("ID="):
                    return line.strip().split("=")[1].strip('"').lower()
    return "unknown"


def check_dependencies():
    os_name = get_os_name()

    if shutil.which("heif-convert") is None or shutil.which("exiftool") is None:
        print(f"System Check: Missing tools on your system ({os_name}).")
        if os_name in ["ubuntu", "debian"]:
            print("-> Please install via terminal: sudo apt install libheif-examples libimage-exiftool-perl libheif-plugin-libde265")
        elif os_name == "alpine":
            print("-> Please install via terminal: sudo apk add libheif exiftool")
        else:
            print("-> Please install the appropriate packages for 'heif-convert' and 'exiftool'.")
        sys.exit(1)

    missing_modules = []
    try:
        import pgeocode
    except ImportError:
        missing_modules.append("pgeocode")
    try:
        import astral
    except ImportError:
        missing_modules.append("astral")

    if missing_modules:
        print(f"Python Check: Missing required modules: {', '.join(missing_modules)}")
        print("Hint: Make sure your virtual environment is activated:")
        print("      source venv/bin/activate")
        print("      Then run: pip install pgeocode astral\n")
        sys.exit(1)


def prepare_wallpaper_images(wallpaper_path):
    if os.path.isfile(wallpaper_path) and wallpaper_path.lower().endswith(".heic"):
        folder_name = wallpaper_path.rsplit('.', 1)[0] + "_extracted"
        if os.path.exists(folder_name):
            return folder_name

        print(f"-> Creating extraction folder: {folder_name}")
        os.makedirs(folder_name)
        print("-> Extracting images from HEIC...")
        output_pattern = os.path.join(folder_name, "frame.jpg")

        try:
            subprocess.run(["heif-convert", wallpaper_path, output_pattern], check=True, stdout=subprocess.DEVNULL)
            return folder_name
        except subprocess.CalledProcessError:
            print("\nError: Could not extract images.")
            if os.path.exists(folder_name):
                shutil.rmtree(folder_name)
            sys.exit(1)
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
        print("-> Error: ZIP code not found.")
        sys.exit(1)

    lat, lon = float(location.latitude), float(location.longitude)
    loc = LocationInfo("Local", "Region", "Europe/Berlin", lat, lon)
    print(f"-> Location initialized: ZIP {zip_code} ({lat:.4f}, {lon:.4f})")
    return loc.observer


def calculate_sun_position(observer):
    now = datetime.now().astimezone()
    current_alt = elevation(observer, now)
    current_az = azimuth(observer, now)
    return current_alt, current_az


def find_best_image_match(current_alt, current_az, metadata):
    best_image_index = 0
    smallest_difference = float('inf')

    # In der tiefen Nacht (Sonne tiefer als -6 Grad) spielt der Azimut
    # optisch kaum eine Rolle. Die geringe Gewichtung verhindert, dass nach
    # Mitternacht (Azimut im Osten) bereits das Sonnenaufgangsbild gewinnt.
    az_weight = 0.02 if current_alt < -6.0 else 0.5

    for item in metadata['si']:
        img_index = item['i']
        img_alt = item['a']
        img_az = item['z']

        diff_alt = abs(current_alt - img_alt)
        diff_az = min(abs(current_az - img_az), 360 - abs(current_az - img_az))

        # Horizont-Sperre: Verhindert, dass nachts (current_alt < -2) Bilder mit
        # Sonne über dem Horizont (img_alt > 0) gewählt werden (und umgekehrt).
        horizon_penalty = 0
        if current_alt < -2.0 and img_alt > 0.0:
            horizon_penalty = 1000
        elif current_alt > 2.0 and img_alt < -2.0:
            horizon_penalty = 1000

        total_diff = diff_alt + (diff_az * az_weight) + horizon_penalty

        if total_diff < smallest_difference:
            smallest_difference = total_diff
            best_image_index = img_index

    frame_number = best_image_index + 1
    return frame_number


def set_gnome_wallpaper(image_path):
    uri = f"file://{os.path.abspath(image_path)}"
    try:
        subprocess.run(["gsettings", "set", "org.gnome.desktop.background", "picture-uri", uri], check=True)
        subprocess.run(["gsettings", "set", "org.gnome.desktop.background", "picture-uri-dark", uri], check=True)
    except subprocess.CalledProcessError:
        print("-> Error: Could not change GNOME wallpaper.")


def set_gnome_theme(current_alt):
    # Schaltet direkt beim echten Sonnenaufgang/Sonnenuntergang (0.0 Grad).
    is_light = current_alt >= 0.0

    try:
        if is_light:
            subprocess.run(["gsettings", "set", "org.gnome.desktop.interface", "color-scheme", "default"], check=True)
            print("-> Theme: GNOME set to LIGHT mode.")
        else:
            subprocess.run(["gsettings", "set", "org.gnome.desktop.interface", "color-scheme", "prefer-dark"], check=True)
            print("-> Theme: GNOME set to DARK mode.")
    except subprocess.CalledProcessError:
        print("-> Error: Could not change GNOME theme.")


def load_configuration():
    if not os.path.exists(CONFIG_FILE):
        print(f"Error: The file {CONFIG_FILE} was not found!")
        sys.exit(1)
    config = configparser.ConfigParser()
    config.read(CONFIG_FILE)
    return config


if __name__ == "__main__":
    check_dependencies()
    my_config = load_configuration()

    path = my_config.get("General", "wallpaper_path")
    zip_code = my_config.get("Location", "zip_code")
    country = my_config.get("Location", "country", fallback="DE")

    print("\n--- heic_for_gnome: Background Daemon starting ---")

    final_folder = prepare_wallpaper_images(path)
    metadata = extract_apple_metadata(path)

    if not metadata or 'si' not in metadata:
        print("-> Error: No solar metadata found in HEIC file.")
        sys.exit(1)

    observer = init_location_observer(zip_code, country)

    last_active_frame = None
    last_theme_is_light = None

    try:
        while True:
            current_alt, current_az = calculate_sun_position(observer)
            best_frame = find_best_image_match(current_alt, current_az, metadata)

            if best_frame != last_active_frame:
                final_image = os.path.join(final_folder, f"frame-{best_frame}.jpg")
                print(f"\n-> Time for a change! New wallpaper: {final_image} (Sun alt: {current_alt:.1f}°, az: {current_az:.1f}°)")
                set_gnome_wallpaper(final_image)
                last_active_frame = best_frame
            else:
                print(f"-> Frame {best_frame} is still the best fit (Sun alt: {current_alt:.1f}°, az: {current_az:.1f}°).")

            is_currently_light = current_alt >= 0.0
            if is_currently_light != last_theme_is_light:
                set_gnome_theme(current_alt)
                last_theme_is_light = is_currently_light

            print("-> Going to sleep for 5 minutes... zZz...")
            time.sleep(300)

    except KeyboardInterrupt:
        print("\n-> Daemon stopped by user. Goodbye!")
        sys.exit(0)
