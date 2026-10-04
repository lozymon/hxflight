#!/usr/bin/env python3
"""HyperX Cloud Flight control panel for Linux.

Talks to the wireless dongle over hidraw (battery, charging, power and
mic-mute state) and to PipeWire/PulseAudio through pactl (volumes and
mic monitoring).
"""

import argparse
import bisect
import collections
import glob
import json
import os
import re
import select
import subprocess
import sys
import time

APP_ID = "hxflight"
APP_NAME = "HyperX Cloud Flight"

# (vendor, product) pairs that share the Cloud Flight protocol
DEVICE_IDS = {
    (0x03F0, 0x0C8C),  # Cloud Flight for PS (HP)
    (0x03F0, 0x0E90),
    (0x0951, 0x1749),
    (0x0951, 0x16C4),  # Cloud Flight (old)
    (0x0951, 0x1723),  # Cloud Flight (new)
}

REPORT_CMD = 0x21
REPORT_POWER = 0x64
REPORT_MUTE = 0x65
CMD_BATTERY = 0x05
CMD_LEN = 20

# Battery voltage (mV) -> percent, in 5% steps
VOLTAGES = [3328, 3584, 3674, 3704, 3732, 3744, 3754, 3764, 3774, 3784,
            3794, 3804, 3824, 3840, 3860, 3890, 3910, 3940, 3960, 3970]
CHARGING_MV = 0x1014

POLL_SECONDS = 60
REPLY_TIMEOUT = 3
RETRY_SECONDS = 3
SMOOTH_SAMPLES = 10  # battery readings averaged, one per poll

CONFIG_PATH = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
    APP_ID, "config.json")
AUTOSTART_PATH = os.path.expanduser("~/.config/autostart/hxflight.desktop")
ICON_DIR = os.path.join(
    os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")), APP_ID)
ICON_SIZE = 64
MAX_VOLUME = 150
DEFAULT_CONFIG = {"low_battery": 20, "notify_mute": True, "monitor_latency": 20}


def find_hidraw():
    """Return /dev/hidraw* paths belonging to a supported headset."""
    paths = []
    for sysdir in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        try:
            with open(os.path.join(sysdir, "device/uevent")) as f:
                uevent = f.read()
        except OSError:
            continue
        m = re.search(r"HID_ID=\w+:(\w+):(\w+)", uevent)
        if m and (int(m.group(1), 16), int(m.group(2), 16)) in DEVICE_IDS:
            paths.append("/dev/" + os.path.basename(sysdir))
    return paths


def battery_request():
    return bytes([REPORT_CMD, 0xFF, CMD_BATTERY]) + bytes(CMD_LEN - 3)


def voltage_to_percent(mv):
    return (max(bisect.bisect_right(VOLTAGES, mv) - 1, 0) + 1) * 5


def parse_report(data):
    """Decode one input report into a dict of state changes."""
    if len(data) < 2:
        return {}
    if data[0] == REPORT_POWER:
        if data[1] == 0x01:
            return {"power": True}
        if data[1] == 0x03:
            return {"power": False}
    elif data[0] == REPORT_MUTE:
        return {"muted": data[1] == 0x04}
    elif (data[0] == REPORT_CMD and len(data) >= 5 and data[1] == 0xFF
          and data[2] == CMD_BATTERY):
        mv = (data[3] << 8) | data[4]
        if mv == 0:
            return {}
        if mv >= CHARGING_MV:
            return {"power": True, "charging": True, "voltage": mv}
        return {"power": True, "charging": False, "voltage": mv,
                "battery": voltage_to_percent(mv)}
    return {}


def pactl(*args):
    try:
        return subprocess.run(["pactl", *args], capture_output=True, text=True,
                              timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


class Audio:
    """Headset sink/source volume control through pactl."""

    def __init__(self):
        self.loopback = None

    def _find(self, kind):
        for line in pactl("list", "short", kind + "s").splitlines():
            cols = line.split("\t")
            if (len(cols) > 1 and "HyperX" in cols[1]
                    and not cols[1].endswith(".monitor")):
                return cols[1]
        return None

    def get(self, kind):
        """Return (volume percent, muted) or None when the device is absent."""
        name = self._find(kind)
        if not name:
            return None
        m = re.search(r"(\d+)%", pactl(f"get-{kind}-volume", name))
        if not m:
            return None
        return int(m.group(1)), "yes" in pactl(f"get-{kind}-mute", name)

    def set_volume(self, kind, percent):
        name = self._find(kind)
        if name:
            pactl(f"set-{kind}-volume", name, f"{int(percent)}%")

    def set_mute(self, kind, muted):
        name = self._find(kind)
        if name:
            pactl(f"set-{kind}-mute", name, "1" if muted else "0")

    def set_monitoring(self, enabled, latency):
        """Route the headset mic back into the headset (software sidetone)."""
        if self.loopback:
            pactl("unload-module", self.loopback)
            self.loopback = None
        if not enabled:
            return True
        source, sink = self._find("source"), self._find("sink")
        if not (source and sink):
            return False
        out = pactl("load-module", "module-loopback", f"source={source}",
                    f"sink={sink}", f"latency_msec={int(latency)}").strip()
        self.loopback = out if out.isdigit() else None
        return self.loopback is not None


def load_config():
    config = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH) as f:
            config.update(json.load(f))
    except (OSError, ValueError):
        pass
    return config


def save_config(config):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)


def render_icon(battery, charging):
    """Draw a tray badge showing the battery percentage; returns its path."""
    import cairo
    name = "charging" if charging else str(battery)
    path = os.path.join(ICON_DIR, f"battery-{name}.png")
    if os.path.exists(path):
        return path
    if charging or battery > 50:
        color = (0.18, 0.62, 0.30)
    elif battery > 20:
        color = (0.90, 0.56, 0.10)
    else:
        color = (0.83, 0.18, 0.18)
    size = ICON_SIZE
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    ctx = cairo.Context(surface)
    radius, top, bottom = 12, 8, size - 8
    ctx.new_sub_path()
    ctx.arc(size - radius, top + radius, radius, -1.5708, 0)
    ctx.arc(size - radius, bottom - radius, radius, 0, 1.5708)
    ctx.arc(radius, bottom - radius, radius, 1.5708, 3.1416)
    ctx.arc(radius, top + radius, radius, 3.1416, 4.7124)
    ctx.close_path()
    ctx.set_source_rgb(*color)
    ctx.fill()
    ctx.set_source_rgb(1, 1, 1)
    if charging:
        for i, (x, y) in enumerate(((36, 12), (18, 34), (30, 34), (26, 52),
                                    (46, 28), (34, 28))):
            (ctx.line_to if i else ctx.move_to)(x, y)
        ctx.close_path()
        ctx.fill()
    else:
        text = str(battery)
        ctx.select_font_face("sans-serif", cairo.FONT_SLANT_NORMAL,
                             cairo.FONT_WEIGHT_BOLD)
        ctx.set_font_size(40 if len(text) < 3 else 30)
        ext = ctx.text_extents(text)
        ctx.move_to((size - ext.width) / 2 - ext.x_bearing,
                    (size - ext.height) / 2 - ext.y_bearing)
        ctx.show_text(text)
    os.makedirs(ICON_DIR, exist_ok=True)
    surface.write_to_png(path)
    return path


def open_device():
    """Open the first usable hidraw node. Returns (fd, error message)."""
    paths = find_hidraw()
    if not paths:
        return None, "Dongle not plugged in"
    error = None
    for path in paths:
        try:
            return os.open(path, os.O_RDWR | os.O_NONBLOCK), None
        except PermissionError:
            error = f"No permission for {path} - run ./install.sh"
        except OSError as e:
            error = f"{path}: {e.strerror}"
    return None, error


def cli(dump):
    fd, error = open_device()
    if fd is None:
        print(error, file=sys.stderr)
        return 1
    os.write(fd, battery_request())
    deadline = None if dump else time.time() + REPLY_TIMEOUT
    while True:
        wait = None if deadline is None else max(deadline - time.time(), 0)
        if not select.select([fd], [], [], wait)[0]:
            print("No reply - headset is switched off or out of range")
            return 2
        data = os.read(fd, 64)
        state = parse_report(data)
        if dump:
            print(data.hex(" "), state, flush=True)
        elif "voltage" in state:
            level = "charging" if state["charging"] else f"{state['battery']}%"
            print(f"Battery: {level} ({state['voltage']} mV)")
            return 0


def gui():
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("Notify", "0.7")
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3 as AppIndicator
    from gi.repository import Gio, GLib, Gtk, Notify

    class App(Gtk.Application):
        def __init__(self):
            super().__init__(application_id="io.github.hxflight")
            self.config = load_config()
            self.audio = Audio()
            self.fd = None
            self.watch = None
            self.error = None
            self.power = None
            self.battery = None
            self.samples = collections.deque(maxlen=SMOOTH_SAMPLES)
            self.charging = False
            self.muted = None
            self.pending_since = None
            self.warned_low = False
            self.window = None
            self.syncing = False

        # --- device -----------------------------------------------------

        def connect_device(self):
            if self.fd is None:
                self.fd, self.error = open_device()
                if self.fd is not None:
                    self.watch = GLib.io_add_watch(
                        self.fd, GLib.IO_IN | GLib.IO_HUP | GLib.IO_ERR,
                        self.on_readable)
                    self.request_battery()
                self.refresh()
            return True

        def disconnect_device(self):
            if self.watch:
                GLib.source_remove(self.watch)
            if self.fd is not None:
                os.close(self.fd)
            self.fd = self.watch = self.pending_since = None
            self.power = self.battery = self.muted = None
            self.charging = False
            self.samples.clear()
            self.refresh()

        def request_battery(self):
            if self.fd is None:
                return True
            if (self.pending_since
                    and time.time() - self.pending_since > REPLY_TIMEOUT):
                # dongle is present but the headset did not answer
                self.power, self.battery, self.charging = False, None, False
                self.samples.clear()
                self.refresh()
            try:
                os.write(self.fd, battery_request())
                self.pending_since = self.pending_since or time.time()
            except OSError:
                self.disconnect_device()
            return True

        def on_readable(self, fd, condition):
            if condition & (GLib.IO_HUP | GLib.IO_ERR):
                self.watch = None
                self.disconnect_device()
                return False
            try:
                data = os.read(fd, 64)
            except BlockingIOError:
                return True
            except OSError:
                self.watch = None
                self.disconnect_device()
                return False
            self.apply(parse_report(data))
            return True

        def apply(self, state):
            if not state:
                return
            if "voltage" in state:
                self.pending_since = None
                self.charging = state["charging"]
                if self.charging:
                    self.samples.clear()
                else:
                    self.samples.append(state["voltage"])
                    self.battery = voltage_to_percent(
                        sum(self.samples) / len(self.samples))
            if "power" in state:
                was = self.power
                self.power = state["power"]
                if not self.power:
                    self.battery, self.charging, self.muted = None, False, None
                    self.samples.clear()
                elif was is False:
                    GLib.timeout_add_seconds(1, self.request_once)
            if "muted" in state:
                self.power = True
                if state["muted"] != self.muted and self.config["notify_mute"]:
                    self.notify("Microphone muted" if state["muted"]
                                else "Microphone on",
                                "microphone-sensitivity-muted" if state["muted"]
                                else "audio-input-microphone")
                self.muted = state["muted"]
            low = self.config["low_battery"]
            if self.battery is not None and not self.charging:
                if self.battery <= low and not self.warned_low:
                    self.warned_low = True
                    self.notify(f"Headset battery low ({self.battery}%)",
                                "battery-caution")
                elif self.battery > low:
                    self.warned_low = False
            self.refresh()

        def request_once(self):
            self.request_battery()
            return False

        def notify(self, text, icon):
            try:
                Notify.Notification.new(APP_NAME, text, icon).show()
            except GLib.Error:
                pass

        # --- presentation -----------------------------------------------

        def status_text(self):
            if self.fd is None:
                return self.error or "Dongle not plugged in"
            if self.power is None:
                return "Waiting for headset..."
            if not self.power:
                return "Headset is off"
            if self.charging:
                return "Charging"
            if self.battery is None:
                return "Connected"
            return f"Battery {self.battery}%"

        def icon_name(self):
            if self.fd is None or not self.power:
                return "audio-headset"
            if not self.charging and self.battery is None:
                return "audio-headset"
            try:
                return render_icon(self.battery, self.charging)
            except (ImportError, OSError):
                return "battery-good-charging" if self.charging else "battery"

        def mic_text(self):
            if self.muted is None:
                return "Microphone: unknown (press the mute button once)"
            return "Microphone: muted" if self.muted else "Microphone: on"

        def refresh(self):
            status = self.status_text()
            self.indicator.set_icon_full(self.icon_name(), status)
            self.indicator.set_title(f"{APP_NAME} - {status}")
            self.menu_status.set_label(status)
            self.menu_mic.set_label(self.mic_text())
            if self.window:
                self.status_label.set_text(status)
                self.mic_label.set_text(self.mic_text())
                self.level.set_value(self.battery or 0)
                self.level.set_sensitive(self.battery is not None)

        def sync_audio(self):
            if not (self.window and self.window.get_visible()):
                return True
            self.syncing = True
            for kind, scale, mute in (("sink", self.out_scale, self.out_mute),
                                      ("source", self.mic_scale, self.mic_mute)):
                state = self.audio.get(kind)
                scale.set_sensitive(state is not None)
                mute.set_sensitive(state is not None)
                if state:
                    scale.set_value(state[0])
                    mute.set_active(state[1])
            self.syncing = False
            return True

        def volume_row(self, grid, row, title, kind):
            label = Gtk.Label(label=title, xalign=0)
            scale = Gtk.Scale.new_with_range(
                Gtk.Orientation.HORIZONTAL, 0, MAX_VOLUME, 1)
            scale.add_mark(100, Gtk.PositionType.BOTTOM, None)
            scale.set_hexpand(True)
            scale.set_value_pos(Gtk.PositionType.RIGHT)
            mute = Gtk.ToggleButton(label="Mute")

            def on_volume(widget):
                if not self.syncing:
                    self.audio.set_volume(kind, widget.get_value())

            def on_mute(widget):
                if not self.syncing:
                    self.audio.set_mute(kind, widget.get_active())

            scale.connect("value-changed", on_volume)
            mute.connect("toggled", on_mute)
            grid.attach(label, 0, row, 1, 1)
            grid.attach(scale, 1, row, 1, 1)
            grid.attach(mute, 2, row, 1, 1)
            return scale, mute

        def switch_row(self, grid, row, title, active, handler):
            label = Gtk.Label(label=title, xalign=0)
            switch = Gtk.Switch(active=active, halign=Gtk.Align.END)
            switch.connect("notify::active", handler)
            grid.attach(label, 0, row, 2, 1)
            grid.attach(switch, 2, row, 1, 1)
            return switch

        def build_window(self):
            self.window = Gtk.ApplicationWindow(application=self, title=APP_NAME)
            self.window.set_icon_name("audio-headset")
            self.window.set_default_size(460, -1)
            self.window.connect("delete-event",
                                lambda w, e: w.hide() or True)

            grid = Gtk.Grid(row_spacing=12, column_spacing=12, margin=18)
            self.window.add(grid)

            self.status_label = Gtk.Label(xalign=0)
            self.status_label.get_style_context().add_class("title-2")
            self.level = Gtk.LevelBar(min_value=0, max_value=100)
            self.mic_label = Gtk.Label(xalign=0)
            grid.attach(self.status_label, 0, 0, 3, 1)
            grid.attach(self.level, 0, 1, 3, 1)
            grid.attach(self.mic_label, 0, 2, 3, 1)
            grid.attach(Gtk.Separator(), 0, 3, 3, 1)

            self.out_scale, self.out_mute = self.volume_row(
                grid, 4, "Headphone volume", "sink")
            self.mic_scale, self.mic_mute = self.volume_row(
                grid, 5, "Microphone volume", "source")
            self.switch_row(grid, 6, "Mic monitoring (hear yourself)",
                            False, self.on_monitoring)
            grid.attach(Gtk.Separator(), 0, 7, 3, 1)

            grid.attach(Gtk.Label(label="Low battery warning at", xalign=0),
                        0, 8, 2, 1)
            spin = Gtk.SpinButton.new_with_range(5, 50, 5)
            spin.set_value(self.config["low_battery"])
            spin.connect("value-changed", self.on_low_battery)
            grid.attach(spin, 2, 8, 1, 1)
            self.switch_row(grid, 9, "Notify when mic is muted/unmuted",
                            self.config["notify_mute"], self.on_notify_mute)
            self.switch_row(grid, 10, "Start on login",
                            os.path.exists(AUTOSTART_PATH), self.on_autostart)
            grid.attach(Gtk.Separator(), 0, 11, 3, 1)
            led = Gtk.Label(xalign=0, wrap=True, max_width_chars=50, label=(
                "Earcup LEDs: press the headset's power button briefly to "
                "switch between solid, breathing and off. They are red only "
                "and cannot be set from the computer."))
            led.get_style_context().add_class("dim-label")
            grid.attach(led, 0, 12, 3, 1)
            grid.show_all()

        def on_monitoring(self, switch, _param):
            if self.syncing:
                return
            ok = self.audio.set_monitoring(switch.get_active(),
                                           self.config["monitor_latency"])
            if switch.get_active() and not ok:
                self.syncing = True
                switch.set_active(False)
                self.syncing = False

        def on_low_battery(self, spin):
            self.config["low_battery"] = spin.get_value_as_int()
            save_config(self.config)

        def on_notify_mute(self, switch, _param):
            self.config["notify_mute"] = switch.get_active()
            save_config(self.config)

        def on_autostart(self, switch, _param):
            if switch.get_active():
                os.makedirs(os.path.dirname(AUTOSTART_PATH), exist_ok=True)
                with open(AUTOSTART_PATH, "w") as f:
                    f.write("[Desktop Entry]\nType=Application\n"
                            f"Name={APP_NAME}\nIcon=audio-headset\n"
                            f"Exec={os.path.abspath(__file__)} --hidden\n")
            elif os.path.exists(AUTOSTART_PATH):
                os.remove(AUTOSTART_PATH)

        def show_window(self, *_args):
            self.sync_audio()
            self.window.present()
            self.sync_audio()

        def quit_app(self, *_args):
            self.audio.set_monitoring(False, 0)
            self.quit()

        # --- lifecycle --------------------------------------------------

        def do_startup(self):
            Gtk.Application.do_startup(self)
            Notify.init(APP_NAME)

            menu = Gtk.Menu()
            self.menu_status = Gtk.MenuItem(label="", sensitive=False)
            self.menu_mic = Gtk.MenuItem(label="", sensitive=False)
            open_item = Gtk.MenuItem(label="Open")
            open_item.connect("activate", self.show_window)
            quit_item = Gtk.MenuItem(label="Quit")
            quit_item.connect("activate", self.quit_app)
            for item in (self.menu_status, self.menu_mic,
                         Gtk.SeparatorMenuItem(), open_item, quit_item):
                menu.append(item)
            menu.show_all()

            self.indicator = AppIndicator.Indicator.new(
                APP_ID, "audio-headset",
                AppIndicator.IndicatorCategory.HARDWARE)
            self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
            self.indicator.set_menu(menu)
            self.indicator.set_secondary_activate_target(open_item)

            self.build_window()
            self.connect_device()
            GLib.timeout_add_seconds(RETRY_SECONDS, self.connect_device)
            GLib.timeout_add_seconds(POLL_SECONDS, self.request_battery)
            GLib.timeout_add_seconds(2, self.sync_audio)
            self.hold()

        def do_activate(self):
            if not self.start_hidden:
                self.show_window()
            self.start_hidden = False

    app = App()
    app.start_hidden = "--hidden" in sys.argv
    return app.run([a for a in sys.argv if a != "--hidden"])


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--status", action="store_true",
                        help="print the battery level and exit")
    parser.add_argument("--dump", action="store_true",
                        help="print raw HID reports as they arrive")
    parser.add_argument("--hidden", action="store_true",
                        help="start in the tray without opening the window")
    args = parser.parse_args()
    if args.status or args.dump:
        return cli(args.dump)
    return gui()


if __name__ == "__main__":
    sys.exit(main())
