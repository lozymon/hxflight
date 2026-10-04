#!/usr/bin/env python3
"""HyperX Cloud Flight control panel for Linux.

Talks to the wireless dongle over hidraw (battery, charging, power and
mic-mute state); hxaudio handles volumes, routing and sound effects
through PipeWire.
"""

import argparse
import bisect
import collections
import glob
import json
import math
import os
import re
import select
import signal
import struct
import subprocess
import sys
import threading
import time

import hxaudio

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
OFF_POLL_SECONDS = 10  # while the headset is switched off
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
DATA_DIR = os.path.join(
    os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")),
    APP_ID)
HISTORY_HOURS = 12
RATED_HOURS = 30  # battery life the manufacturer quotes
MIC_TEST_SECONDS = 5
DEFAULT_CONFIG = {
    "low_battery": 20,
    "notify_mute": True,
    "monitor_latency": 20,
    "sync_mute": True,
    "auto_switch": True,
    "pause_on_off": True,
    "fallback": {},  # output/microphone to return to when the headset is off
    "eq_enabled": False,
    "eq_preset": "Flat",
    "eq_gains": [0.0] * len(hxaudio.EQ_FREQS),
    "eq_presets": {},
    "surround": False,
    "noise_suppression": False,
    "noise_mode": "standard",  # or "voice"
    "voice_threshold": 60,
}


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


def voltage_to_fraction(mv):
    """Like voltage_to_percent but interpolated, for graphs and estimates."""
    i = bisect.bisect_right(VOLTAGES, mv) - 1
    if i < 0:
        return 0.0
    if i >= len(VOLTAGES) - 1:
        return 100.0
    return 5.0 * (i + 1 + (mv - VOLTAGES[i]) / (VOLTAGES[i + 1] - VOLTAGES[i]))


class History:
    """Battery voltage readings over time, kept across restarts."""

    GAP = 300  # seconds without a reading that separate two runs
    KEEP = 48 * 3600

    def __init__(self):
        self.path = os.path.join(DATA_DIR, "battery.csv")
        self.points = []
        cutoff = time.time() - self.KEEP
        try:
            with open(self.path) as f:
                for line in f:
                    ts, mv = line.split(",")
                    if float(ts) >= cutoff:
                        self.points.append((float(ts), float(mv)))
            with open(self.path, "w") as f:
                f.writelines(f"{ts:.0f},{mv:.1f}\n" for ts, mv in self.points)
        except (OSError, ValueError):
            pass

    def add(self, ts, mv):
        self.points.append((ts, mv))
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(self.path, "a") as f:
                f.write(f"{ts:.0f},{mv:.1f}\n")
        except OSError:
            pass

    def runs(self, since):
        """Readings newer than `since`, split where the headset was off."""
        runs = []
        for point in self.points:
            if point[0] < since:
                continue
            if runs and point[0] - runs[-1][-1][0] <= self.GAP:
                runs[-1].append(point)
            else:
                runs.append([point])
        return runs

    def hours_left(self):
        """Return (hours, measured). `measured` is False when there is too
        little data and the figure comes from the rated battery life."""
        now = time.time()
        runs = self.runs(now - 2 * 3600)
        if not runs or now - runs[-1][-1][0] > self.GAP:
            return None, False
        run = runs[-1]
        level = voltage_to_fraction(run[-1][1])
        if run[-1][0] - run[0][0] >= 1800:
            xs = [(ts - run[0][0]) / 3600 for ts, _mv in run]
            ys = [voltage_to_fraction(mv) for _ts, mv in run]
            mean_x, mean_y = sum(xs) / len(xs), sum(ys) / len(ys)
            slope = (sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
                     / sum((x - mean_x) ** 2 for x in xs))
            if slope <= -1:  # percent per hour
                return min(level / -slope, RATED_HOURS), True
        return level / 100 * RATED_HOURS, False


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

    MIC_PAGE = 2
    CUSTOM = "Custom"

    class App(Gtk.Application):
        def __init__(self):
            super().__init__(application_id="io.github.hxflight")
            self.config = load_config()
            self.audio = hxaudio.Audio()
            self.history = History()
            self.fd = None
            self.watch = None
            self.error = None
            self.power = None
            self.battery = None
            self.samples = collections.deque(maxlen=SMOOTH_SAMPLES)
            self.charging = False
            self.muted = None
            self.pending_since = None
            self.misses = 0
            self.last_request = 0
            self.warned_low = False
            self.window = None
            self.syncing = False
            self.headset_present = None
            self.eq_timer = None
            self.noise_timer = None
            self.downloading = False
            self.events = None
            self.sync_timer = None
            self.meter = None
            self.meter_watch = None
            self.test_process = None

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
            self.misses = 0
            self.set_power(None)
            self.refresh()

        def poll(self):
            """Runs every few seconds; asks more often while the headset is
            off so switching it on is noticed quickly."""
            interval = OFF_POLL_SECONDS if self.power is False else POLL_SECONDS
            if time.time() - self.last_request >= interval:
                self.request_battery()
            return True

        def request_battery(self):
            if self.fd is None:
                return
            try:
                os.write(self.fd, battery_request())
            except OSError:
                self.disconnect_device()
                return
            self.last_request = time.time()
            if self.pending_since is None:
                self.pending_since = self.last_request
                GLib.timeout_add_seconds(REPLY_TIMEOUT, self.check_reply)

        def check_reply(self):
            """A request went unanswered: retry once, then treat the headset
            as switched off."""
            if self.fd is None or self.pending_since is None:
                return False
            if time.time() - self.pending_since < REPLY_TIMEOUT - 0.5:
                return False  # a newer request has its own check scheduled
            self.pending_since = None
            self.misses += 1
            if self.misses >= 2:
                if self.power is not False:
                    self.set_power(False)
                    self.refresh()
            else:
                self.request_battery()
            return False

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

        def set_power(self, value):
            was, self.power = self.power, value
            if not value:
                self.battery, self.charging, self.muted = None, False, None
                self.samples.clear()
            if value and was is False:
                self.on_headset_on()
            elif value is False and was:
                self.on_headset_off()

        def on_headset_on(self):
            if self.config["auto_switch"]:
                replaced = self.audio.switch_to_headset()
                if replaced:
                    self.config["fallback"].update(replaced)
                    save_config(self.config)

        def on_headset_off(self):
            if self.config["pause_on_off"]:
                self.pause_media()
            if self.config["auto_switch"]:
                self.audio.switch_away(self.config["fallback"])

        def pause_media(self):
            """Pause every MPRIS media player on the session bus."""
            try:
                bus = Gio.bus_get_sync(Gio.BusType.SESSION)
                names = bus.call_sync(
                    "org.freedesktop.DBus", "/org/freedesktop/DBus",
                    "org.freedesktop.DBus", "ListNames", None,
                    GLib.VariantType("(as)"), Gio.DBusCallFlags.NONE, 1000,
                    None).unpack()[0]
                for name in names:
                    if name.startswith("org.mpris.MediaPlayer2."):
                        bus.call(name, "/org/mpris/MediaPlayer2",
                                 "org.mpris.MediaPlayer2.Player", "Pause",
                                 None, None, Gio.DBusCallFlags.NONE, 1000,
                                 None, None, None)
            except GLib.Error:
                pass

        def apply(self, state):
            if not state:
                return
            if "voltage" in state:
                self.pending_since = None
                self.misses = 0
                self.charging = state["charging"]
                if self.charging:
                    self.samples.clear()
                else:
                    self.samples.append(state["voltage"])
                    smoothed = sum(self.samples) / len(self.samples)
                    self.battery = voltage_to_percent(smoothed)
                    self.history.add(time.time(), smoothed)
            if "power" in state:
                was_off = self.power is False
                self.set_power(state["power"])
                if state["power"] and was_off:
                    GLib.timeout_add_seconds(1, self.request_once)
            if "muted" in state:
                if self.power is not True:
                    self.set_power(True)
                if state["muted"] != self.muted and self.config["notify_mute"]:
                    self.notify("Microphone muted" if state["muted"]
                                else "Microphone on",
                                "microphone-sensitivity-muted" if state["muted"]
                                else "audio-input-microphone")
                self.muted = state["muted"]
                if self.config["sync_mute"]:
                    self.audio.set_mute("source", self.muted, raw=True)
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

        def remaining_text(self):
            if not self.power or self.charging or self.battery is None:
                return ""
            hours, measured = self.history.hours_left()
            if hours is None:
                return ""
            amount = (f"{hours:.0f} hours" if hours >= 2
                      else f"{max(hours * 60, 5):.0f} minutes")
            basis = "at the current drain" if measured else "at typical use"
            return f"About {amount} left {basis}"

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
            remaining = self.remaining_text()
            self.indicator.set_icon_full(self.icon_name(), status)
            self.indicator.set_title(f"{APP_NAME} - {status}")
            self.menu_status.set_label(status)
            self.menu_remaining.set_label(remaining)
            self.menu_remaining.set_visible(bool(remaining))
            self.menu_mic.set_label(self.mic_text())
            if self.window:
                self.status_label.set_text(status)
                self.remaining_label.set_text(remaining)
                self.mic_label.set_text(self.mic_text())
                self.level.set_value(self.battery or 0)
                self.level.set_sensitive(self.battery is not None)
                self.graph.queue_draw()

        def draw_history(self, area, cr):
            width = area.get_allocated_width()
            height = area.get_allocated_height()
            color = area.get_style_context().get_color(Gtk.StateFlags.NORMAL)
            cr.set_line_width(1)
            cr.set_source_rgba(color.red, color.green, color.blue, 0.15)
            for step in range(5):
                y = round(height * step / 4) + 0.5
                cr.move_to(0, min(y, height - 0.5))
                cr.line_to(width, min(y, height - 0.5))
            cr.stroke()
            cr.set_source_rgba(0.18, 0.62, 0.30, 1)
            cr.set_line_width(2)
            now = time.time()
            for run in self.history.runs(now - HISTORY_HOURS * 3600):
                for i, (ts, mv) in enumerate(run):
                    x = width * (1 - (now - ts) / (HISTORY_HOURS * 3600))
                    y = height * (1 - voltage_to_fraction(mv) / 100)
                    (cr.line_to if i else cr.move_to)(x, y)
                if len(run) == 1:
                    cr.rel_line_to(2, 0)
                cr.stroke()

        def tick(self):
            """Follow the headset's audio devices appearing or vanishing and
            keep the sliders in step with the system."""
            present = self.audio.present()
            if present != self.headset_present:
                self.headset_present = present
                self.apply_effects()
            self.sync_audio()
            return True

        def watch_audio(self):
            """Follow volume changes made outside the app as they happen."""
            self.events = subprocess.Popen(
                ["pactl", "subscribe"], stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, preexec_fn=hxaudio._die_with_parent)
            fd = self.events.stdout.fileno()
            os.set_blocking(fd, False)
            GLib.io_add_watch(fd, GLib.IO_IN | GLib.IO_HUP, self.on_audio_event)

        def on_audio_event(self, fd, condition):
            try:
                data = os.read(fd, 65536)
            except BlockingIOError:
                return True
            except OSError:
                data = b""
            if not data:
                return False
            if ((b"on sink #" in data or b"on source #" in data
                 or b"on server" in data) and self.sync_timer is None):
                self.sync_timer = GLib.timeout_add(100, self.sync_soon)
            return True

        def sync_soon(self):
            self.sync_timer = None
            self.sync_audio()
            return False

        def sync_audio(self):
            if not (self.window and self.window.get_visible()):
                return
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

        # --- effects ----------------------------------------------------

        def apply_effects(self):
            running = self.audio.apply_effects(self.config)
            self.audio.reroute()
            if self.audio.loopback:
                # the microphone it was listening to may have been replaced
                self.audio.set_monitoring(True, self.config["monitor_latency"])
            return running

        def on_effect(self, switch, _param, key, name):
            if self.syncing:
                return
            self.config[key] = switch.get_active()
            running = self.apply_effects()
            if switch.get_active() and not running[name]:
                self.config[key] = False
                self.syncing = True
                switch.set_active(False)
                self.syncing = False
                self.notify("Could not start this effect - is the headset "
                            "dongle plugged in?", "dialog-warning")
            save_config(self.config)
            self.eq_box.set_sensitive(self.config["eq_enabled"])
            if name == "noise":
                self.update_meter(restart=True)
                self.need_voice_plugin()

        def need_voice_plugin(self):
            """Fetch the voice filter the first time voice-only mode is used.
            Returns True when a download was started."""
            if (self.downloading or not self.config["noise_suppression"]
                    or self.config["noise_mode"] != "voice"
                    or os.path.exists(hxaudio.VOICE_PLUGIN)):
                return False
            self.downloading = True
            self.noise_status.set_text(
                "Downloading the voice filter (37 MB). Standard suppression "
                "is used until it is ready.")

            def work():
                try:
                    hxaudio.install_voice_plugin()
                    error = None
                except (OSError, KeyError, ValueError) as e:
                    error = str(e)
                GLib.idle_add(done, error)

            def done(error):
                self.downloading = False
                self.noise_status.set_text(
                    f"Could not get the voice filter: {error}" if error else "")
                self.restart_noise()
                return False

            threading.Thread(target=work, daemon=True).start()
            return True

        def restart_noise(self):
            self.noise_timer = None
            save_config(self.config)
            self.apply_effects()
            self.update_meter(restart=True)
            return False

        def on_noise_mode(self, combo):
            if self.syncing:
                return
            self.config["noise_mode"] = combo.get_active_id()
            self.threshold_scale.set_sensitive(
                self.config["noise_mode"] == "voice")
            if not self.need_voice_plugin():
                self.restart_noise()

        def on_threshold(self, scale):
            self.config["voice_threshold"] = int(scale.get_value())
            if self.noise_timer is None:
                self.noise_timer = GLib.timeout_add(600, self.restart_noise)

        def presets(self):
            return {**hxaudio.EQ_PRESETS, **self.config["eq_presets"]}

        def fill_presets(self):
            self.syncing = True
            self.preset_combo.remove_all()
            for name in list(self.presets()) + [CUSTOM]:
                self.preset_combo.append(name, name)
            current = self.config["eq_preset"]
            if current not in self.presets():
                current = CUSTOM
            self.preset_combo.set_active_id(current)
            self.delete_button.set_sensitive(
                current in self.config["eq_presets"])
            self.syncing = False

        def on_preset(self, combo):
            name = combo.get_active_id()
            if self.syncing or name is None or name == CUSTOM:
                return
            self.config["eq_preset"] = name
            self.config["eq_gains"] = list(self.presets()[name])
            self.syncing = True
            for scale, gain in zip(self.eq_scales, self.config["eq_gains"]):
                scale.set_value(gain)
            self.syncing = False
            self.delete_button.set_sensitive(name in self.config["eq_presets"])
            self.push_eq()

        def on_band(self, scale, band):
            if self.syncing:
                return
            self.config["eq_gains"][band] = scale.get_value()
            self.config["eq_preset"] = CUSTOM
            self.syncing = True
            self.preset_combo.set_active_id(CUSTOM)
            self.syncing = False
            self.delete_button.set_sensitive(False)
            if self.eq_timer is None:
                self.eq_timer = GLib.timeout_add(150, self.push_eq)

        def push_eq(self):
            self.eq_timer = None
            self.audio.set_eq_gains(self.config["eq_gains"])
            save_config(self.config)
            return False

        def on_save_preset(self, _button):
            dialog = Gtk.Dialog(title="Save preset", transient_for=self.window,
                                modal=True)
            dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL,
                               "Save", Gtk.ResponseType.OK)
            dialog.set_default_response(Gtk.ResponseType.OK)
            entry = Gtk.Entry(placeholder_text="Preset name", margin=12,
                              activates_default=True)
            dialog.get_content_area().add(entry)
            dialog.show_all()
            response = dialog.run()
            name = entry.get_text().strip()
            dialog.destroy()
            if (response != Gtk.ResponseType.OK or not name or name == CUSTOM
                    or name in hxaudio.EQ_PRESETS):
                return
            self.config["eq_presets"][name] = list(self.config["eq_gains"])
            self.config["eq_preset"] = name
            save_config(self.config)
            self.fill_presets()

        def on_delete_preset(self, _button):
            self.config["eq_presets"].pop(self.config["eq_preset"], None)
            self.config["eq_preset"] = CUSTOM
            save_config(self.config)
            self.fill_presets()

        # --- microphone -------------------------------------------------

        def update_meter(self, *_args, restart=False):
            wanted = (self.window.get_visible()
                      and self.notebook.get_current_page() == MIC_PAGE)
            if self.meter and (restart or not wanted):
                if self.meter_watch:
                    GLib.source_remove(self.meter_watch)
                self.meter.kill()
                self.meter.wait()
                self.meter = self.meter_watch = None
                self.mic_level.set_value(0)
            source = self.audio.preferred("source")
            if wanted and not self.meter and source:
                self.meter = subprocess.Popen(
                    ["parec", "--device=" + source, "--format=s16le",
                     "--rate=8000", "--channels=1", "--latency-msec=40",
                     "--raw"],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                fd = self.meter.stdout.fileno()
                os.set_blocking(fd, False)
                self.meter_watch = GLib.io_add_watch(
                    fd, GLib.IO_IN | GLib.IO_HUP, self.on_meter)

        def on_page(self, _notebook, _page, _number):
            GLib.idle_add(self.update_meter)

        def on_meter(self, fd, condition):
            try:
                data = os.read(fd, 65536)
            except BlockingIOError:
                return True
            except OSError:
                data = b""
            if not data:
                self.meter_watch = None
                return False
            count = len(data) // 2
            if count:
                peak = max(map(abs, struct.unpack(f"<{count}h",
                                                  data[:count * 2])))
                decibel = 20 * math.log10(max(peak / 32768, 1e-5))
                level = max(0.0, min(1.0, 1 + decibel / 50))
                self.mic_level.set_value(
                    max(level, self.mic_level.get_value() * 0.8))
            return True

        def on_mic_test(self, button):
            source = self.audio.preferred("source")
            sink = self.audio.preferred("sink")
            if self.test_process or not (source and sink):
                return
            path = os.path.join(hxaudio.CACHE_DIR, "mic-test.wav")
            os.makedirs(hxaudio.CACHE_DIR, exist_ok=True)
            self.test_process = subprocess.Popen(
                ["pw-record", "--target", source, path],
                stderr=subprocess.DEVNULL)
            button.set_sensitive(False)
            button.set_label(f"Recording {MIC_TEST_SECONDS} seconds...")

            def finish():
                if self.test_process.poll() is None:
                    return True
                self.test_process = None
                button.set_label(self.test_label)
                button.set_sensitive(True)
                return False

            def play():
                # pw-record only finishes the file on SIGINT
                self.test_process.send_signal(signal.SIGINT)
                self.test_process.wait()
                button.set_label("Playing back...")
                self.test_process = subprocess.Popen(
                    ["paplay", "--device=" + sink, path],
                    stderr=subprocess.DEVNULL)
                GLib.timeout_add(200, finish)
                return False

            GLib.timeout_add_seconds(MIC_TEST_SECONDS, play)

        def on_monitoring(self, switch, _param):
            if self.syncing:
                return
            ok = self.audio.set_monitoring(switch.get_active(),
                                           self.config["monitor_latency"])
            if switch.get_active() and not ok:
                self.syncing = True
                switch.set_active(False)
                self.syncing = False

        # --- window -----------------------------------------------------

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

        def switch_row(self, grid, row, title, active, handler, *args,
                       hint=None):
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            box.add(Gtk.Label(label=title, xalign=0))
            if hint:
                box.add(self.hint(hint))
            switch = Gtk.Switch(active=active, halign=Gtk.Align.END,
                                valign=Gtk.Align.CENTER)
            switch.connect("notify::active", handler, *args)
            grid.attach(box, 0, row, 2, 1)
            grid.attach(switch, 2, row, 1, 1)
            return switch

        def hint(self, text):
            label = Gtk.Label(label=text, xalign=0, wrap=True,
                              max_width_chars=52)
            label.get_style_context().add_class("dim-label")
            return label

        def page(self, title):
            grid = Gtk.Grid(row_spacing=12, column_spacing=12, margin=18)
            self.notebook.append_page(grid, Gtk.Label(label=title))
            return grid

        def config_switch(self, grid, row, title, key, hint=None):
            def on_toggle(switch, _param):
                self.config[key] = switch.get_active()
                save_config(self.config)
            return self.switch_row(grid, row, title, self.config[key],
                                   on_toggle, hint=hint)

        def build_window(self):
            self.window = Gtk.ApplicationWindow(application=self, title=APP_NAME)
            self.window.set_icon_name("audio-headset")
            self.window.set_default_size(520, -1)
            self.window.connect("delete-event", lambda w, e: w.hide() or True)
            self.window.connect("show", self.update_meter)
            self.window.connect("hide", self.update_meter)
            self.notebook = Gtk.Notebook()
            self.window.add(self.notebook)

            # Status
            grid = self.page("Status")
            self.status_label = Gtk.Label(xalign=0)
            self.level = Gtk.LevelBar(min_value=0, max_value=100, hexpand=True)
            self.remaining_label = self.hint("")
            self.mic_label = Gtk.Label(xalign=0)
            self.graph = Gtk.DrawingArea(hexpand=True)
            self.graph.set_size_request(-1, 110)
            self.graph.connect("draw", self.draw_history)
            grid.attach(self.status_label, 0, 0, 3, 1)
            grid.attach(self.level, 0, 1, 3, 1)
            grid.attach(self.remaining_label, 0, 2, 3, 1)
            grid.attach(self.mic_label, 0, 3, 3, 1)
            grid.attach(Gtk.Separator(), 0, 4, 3, 1)
            grid.attach(Gtk.Label(
                label=f"Battery over the last {HISTORY_HOURS} hours",
                xalign=0), 0, 5, 3, 1)
            grid.attach(self.graph, 0, 6, 3, 1)

            # Sound
            grid = self.page("Sound")
            self.out_scale, self.out_mute = self.volume_row(
                grid, 0, "Headphone volume", "sink")
            grid.attach(Gtk.Separator(), 0, 1, 3, 1)
            self.switch_row(grid, 2, "Equalizer", self.config["eq_enabled"],
                            self.on_effect, "eq_enabled", "eq")
            self.eq_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                                  spacing=8)
            self.eq_box.set_sensitive(self.config["eq_enabled"])
            presets = Gtk.Box(spacing=8)
            self.preset_combo = Gtk.ComboBoxText(hexpand=True)
            self.preset_combo.connect("changed", self.on_preset)
            save_button = Gtk.Button(label="Save as...")
            save_button.connect("clicked", self.on_save_preset)
            self.delete_button = Gtk.Button(label="Delete")
            self.delete_button.connect("clicked", self.on_delete_preset)
            for widget in (Gtk.Label(label="Preset"), self.preset_combo,
                           save_button, self.delete_button):
                presets.add(widget)
            bands = Gtk.Box(spacing=4, homogeneous=True)
            self.eq_scales = []
            for band, freq in enumerate(hxaudio.EQ_FREQS):
                column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
                scale = Gtk.Scale.new_with_range(
                    Gtk.Orientation.VERTICAL, -hxaudio.EQ_RANGE,
                    hxaudio.EQ_RANGE, 1)
                scale.set_inverted(True)
                scale.set_size_request(-1, 150)
                scale.add_mark(0, Gtk.PositionType.LEFT, None)
                scale.set_value(self.config["eq_gains"][band])
                scale.connect("value-changed", self.on_band, band)
                name = f"{freq // 1000}k" if freq >= 1000 else str(freq)
                column.add(scale)
                column.add(Gtk.Label(label=name))
                bands.add(column)
                self.eq_scales.append(scale)
            self.eq_box.add(presets)
            self.eq_box.add(bands)
            self.eq_box.add(self.hint(
                "Gain in dB per frequency band. Boosting a band lowers the "
                "overall level a little so the sound does not distort."))
            grid.attach(self.eq_box, 0, 3, 3, 1)
            self.fill_presets()
            grid.attach(Gtk.Separator(), 0, 4, 3, 1)
            self.switch_row(
                grid, 5, "Virtual surround 7.1", self.config["surround"],
                self.on_effect, "surround", "surround",
                hint="Set the game or player to 7.1 output. Stereo music "
                     "sounds wider but less direct with this on.")

            # Microphone
            grid = self.page("Microphone")
            self.mic_scale, self.mic_mute = self.volume_row(
                grid, 0, "Microphone volume", "source")
            grid.attach(Gtk.Label(label="Input level", xalign=0), 0, 1, 1, 1)
            self.mic_level = Gtk.LevelBar(min_value=0, max_value=1,
                                          hexpand=True, valign=Gtk.Align.CENTER)
            grid.attach(self.mic_level, 1, 1, 2, 1)
            self.test_label = f"Record {MIC_TEST_SECONDS} seconds and play back"
            test = Gtk.Button(label=self.test_label)
            test.connect("clicked", self.on_mic_test)
            grid.attach(test, 0, 2, 3, 1)
            grid.attach(Gtk.Separator(), 0, 3, 3, 1)
            self.switch_row(
                grid, 4, "Noise suppression",
                self.config["noise_suppression"], self.on_effect,
                "noise_suppression", "noise",
                hint="Adds a second, filtered HyperX microphone for apps "
                     "to use.")
            grid.attach(Gtk.Label(label="Mode", xalign=0), 0, 5, 1, 1)
            mode = Gtk.ComboBoxText(hexpand=True)
            mode.append("standard", "Standard - reduces steady background noise")
            mode.append("voice", "Voice only - silences everything but speech")
            mode.set_active_id(self.config["noise_mode"])
            mode.connect("changed", self.on_noise_mode)
            grid.attach(mode, 1, 5, 2, 1)
            grid.attach(Gtk.Label(label="Strictness", xalign=0), 0, 6, 1, 1)
            self.threshold_scale = Gtk.Scale.new_with_range(
                Gtk.Orientation.HORIZONTAL, 10, 95, 5)
            self.threshold_scale.set_value(self.config["voice_threshold"])
            self.threshold_scale.set_value_pos(Gtk.PositionType.RIGHT)
            self.threshold_scale.set_sensitive(
                self.config["noise_mode"] == "voice")
            self.threshold_scale.connect("value-changed", self.on_threshold)
            grid.attach(self.threshold_scale, 1, 6, 2, 1)
            self.noise_status = self.hint("")
            grid.attach(self.hint(
                "Voice only: higher strictness blocks more, but can cut the "
                "start of words. It passes any human voice, not just yours."),
                0, 7, 3, 1)
            grid.attach(self.noise_status, 0, 8, 3, 1)
            grid.attach(Gtk.Separator(), 0, 9, 3, 1)
            self.switch_row(grid, 10, "Mic monitoring (hear yourself)",
                            False, self.on_monitoring)
            self.config_switch(
                grid, 11, "Mute button also mutes the system microphone",
                "sync_mute",
                hint="Apps then show you as muted when you press the "
                     "headset's mute button.")
            self.config_switch(grid, 12, "Notify when mic is muted/unmuted",
                               "notify_mute")

            # Settings
            grid = self.page("Settings")
            self.config_switch(
                grid, 0, "Switch audio to the headset when it turns on",
                "auto_switch",
                hint="And back to the previous output and microphone when "
                     "it turns off.")
            self.config_switch(grid, 1, "Pause media when the headset turns off",
                               "pause_on_off")
            grid.attach(Gtk.Separator(), 0, 2, 3, 1)
            grid.attach(Gtk.Label(label="Low battery warning at (%)", xalign=0,
                                  hexpand=True), 0, 3, 2, 1)
            spin = Gtk.SpinButton.new_with_range(5, 50, 5)
            spin.set_value(self.config["low_battery"])
            spin.connect("value-changed", self.on_low_battery)
            grid.attach(spin, 2, 3, 1, 1)
            self.switch_row(grid, 4, "Start on login",
                            os.path.exists(AUTOSTART_PATH), self.on_autostart)
            grid.attach(Gtk.Separator(), 0, 5, 3, 1)
            grid.attach(self.hint(
                "Earcup LEDs: press the headset's power button briefly to "
                "switch between solid, breathing and off. They are red only "
                "and cannot be set from the computer."), 0, 6, 3, 1)

            self.notebook.connect("switch-page", self.on_page)
            self.notebook.show_all()

        def on_low_battery(self, spin):
            self.config["low_battery"] = spin.get_value_as_int()
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
            self.window.present()
            self.sync_audio()

        def quit_app(self, *_args):
            for process in (self.meter, self.events):
                if process:
                    process.kill()
            self.audio.set_monitoring(False, 0)
            # leave the default on a device that still exists once the
            # effect processes are gone
            for kind in ("sink", "source"):
                raw = self.audio._find(kind)
                current = self.audio.default(kind)
                if raw and current in self.audio.family(kind) - {raw}:
                    if kind == "sink":
                        self.audio._carry_volume(current, raw)
                    self.audio.set_default(kind, raw)
            self.audio.stop_effects()
            self.quit()
            return False

        # --- lifecycle --------------------------------------------------

        def do_startup(self):
            Gtk.Application.do_startup(self)
            Notify.init(APP_NAME)

            menu = Gtk.Menu()
            self.menu_status = Gtk.MenuItem(label="", sensitive=False)
            self.menu_remaining = Gtk.MenuItem(label="", sensitive=False)
            self.menu_mic = Gtk.MenuItem(label="", sensitive=False)
            open_item = Gtk.MenuItem(label="Open")
            open_item.connect("activate", self.show_window)
            quit_item = Gtk.MenuItem(label="Quit")
            quit_item.connect("activate", self.quit_app)
            for item in (self.menu_status, self.menu_remaining, self.menu_mic,
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
            self.tick()
            self.watch_audio()
            GLib.timeout_add_seconds(RETRY_SECONDS, self.connect_device)
            GLib.timeout_add_seconds(OFF_POLL_SECONDS, self.poll)
            GLib.timeout_add_seconds(3, self.tick)
            for signum in (signal.SIGINT, signal.SIGTERM):
                GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signum,
                                     self.quit_app)
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
