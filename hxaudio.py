"""PipeWire/PulseAudio side of hxflight: volumes, routing and effects.

Volumes and routing go through pactl. The equalizer, virtual surround and
microphone noise suppression each run as a small `pipewire -c` filter
process that this module starts and stops.
"""

import ctypes
import hashlib
import io
import math
import os
import re
import signal
import subprocess
import time
import urllib.request
import wave
import zipfile

APP_ID = "hxflight"
CACHE_DIR = os.path.join(
    os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")), APP_ID)

DATA_DIR = os.path.join(
    os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")),
    APP_ID)

# RNNoise voice filter (github.com/werman/noise-suppression-for-voice),
# fetched on first use because distributions do not package it
VOICE_PLUGIN = os.path.join(DATA_DIR, "librnnoise_ladspa.so")
VOICE_URL = ("https://github.com/werman/noise-suppression-for-voice/releases/"
             "download/v1.10/linux-rnnoise.zip")
VOICE_ZIP_SHA256 = (
    "811390b6eb6e28dde023c70590c74d26e74ebb2e595bcf4b95af2341db160e99")
VOICE_MEMBER = "linux-rnnoise/ladspa/librnnoise_ladspa.so"

EQ_SINK = "hxflight_eq"
SURROUND_SINK = "hxflight_surround"
NOISE_SOURCE = "hxflight_mic"

EQ_FREQS = [31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]
EQ_RANGE = 12
EQ_PRESETS = {
    "Flat": [0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    "Bass boost": [6, 5, 4, 2, 0, 0, 0, 0, 0, 0],
    "FPS footsteps": [-4, -3, -2, 0, 1, 3, 5, 5, 3, 1],
    "Voice": [-4, -3, -1, 1, 3, 4, 3, 1, 0, -1],
    "Treble boost": [0, 0, 0, 0, 0, 1, 2, 4, 5, 6],
    "Movie": [4, 4, 2, 0, -1, 0, 2, 3, 3, 2],
}

SOFA_FILE = "/usr/share/libmysofa/MIT_KEMAR_normal_pinna.sofa"
SAMPLE_RATE = 48000
# 7.1 channel order with each virtual speaker's angle (degrees, left positive)
SPEAKERS = [("FL", 30), ("FR", -30), ("FC", 0), ("LFE", 0),
            ("RL", 140), ("RR", -140), ("SL", 90), ("SR", -90)]

BASE_CONF = """\
context.properties = { log.level = 0 }
context.spa-libs = {
    audio.convert.* = audioconvert/libspa-audioconvert
    support.*       = support/libspa-support
}
context.modules = [
    { name = libpipewire-module-rt flags = [ ifexists nofail ] }
    { name = libpipewire-module-protocol-native }
    { name = libpipewire-module-client-node }
    { name = libpipewire-module-adapter }
%s
]
"""


def pactl(*args):
    try:
        return subprocess.run(["pactl", *args], capture_output=True, text=True,
                              timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def eq_preamp(gains):
    """Negative gain that keeps the loudest boosted band from clipping."""
    return -max(0.0, max(gains))


def eq_conf(gains, target):
    nodes = ['{ type = builtin name = preamp label = bq_highshelf control = '
             '{ "Freq" = 0.0 "Q" = 1.0 "Gain" = %.1f } }' % eq_preamp(gains)]
    links = []
    previous = "preamp"
    for i, (freq, gain) in enumerate(zip(EQ_FREQS, gains), 1):
        nodes.append('{ type = builtin name = band_%d label = bq_peaking '
                     'control = { "Freq" = %.1f "Q" = 1.41 "Gain" = %.1f } }'
                     % (i, freq, gain))
        links.append('{ output = "%s:Out" input = "band_%d:In" }'
                     % (previous, i))
        previous = "band_%d" % i
    return """\
    { name = libpipewire-module-filter-chain
        args = {
            node.description = "HyperX Equalizer"
            media.name = "HyperX Equalizer"
            filter.graph = {
                nodes = [ %s ]
                links = [ %s ]
            }
            audio.channels = 2
            audio.position = [ FL FR ]
            capture.props = { node.name = "%s" media.class = Audio/Sink }
            playback.props = {
                node.name = "%s_out" node.passive = true
                node.dont-fallback = true target.object = "%s"
            }
        }
    }""" % (" ".join(nodes), " ".join(links), EQ_SINK, EQ_SINK, target)


def surround_conf(hrir_dir, target):
    nodes, links, inputs = [], [], []
    for i, (name, _angle) in enumerate(SPEAKERS, 1):
        path = os.path.join(hrir_dir, f"hrir-{name}.wav")
        nodes.append("{ type = builtin label = copy name = copy%s }" % name)
        inputs.append('"copy%s:In"' % name)
        for channel, ear in enumerate("LR"):
            nodes.append('{ type = builtin label = convolver name = conv%s_%s '
                         'config = { filename = "%s" channel = %d } }'
                         % (name, ear, path, channel))
            links.append('{ output = "copy%s:Out" input = "conv%s_%s:In" }'
                         % (name, name, ear))
            links.append('{ output = "conv%s_%s:Out" input = "mix%s:In %d" }'
                         % (name, ear, ear, i))
    nodes.append("{ type = builtin label = mixer name = mixL }")
    nodes.append("{ type = builtin label = mixer name = mixR }")
    return """\
    { name = libpipewire-module-filter-chain
        args = {
            node.description = "HyperX Virtual Surround 7.1"
            media.name = "HyperX Virtual Surround 7.1"
            filter.graph = {
                nodes = [ %s ]
                links = [ %s ]
                inputs = [ %s ]
                outputs = [ "mixL:Out" "mixR:Out" ]
            }
            capture.props = {
                node.name = "%s" media.class = Audio/Sink
                audio.channels = 8
                audio.position = [ %s ]
            }
            playback.props = {
                node.name = "%s_out" node.passive = true
                node.dont-fallback = true target.object = "%s"
                audio.channels = 2 audio.position = [ FL FR ]
            }
        }
    }""" % (" ".join(nodes), " ".join(links), " ".join(inputs), SURROUND_SINK,
            " ".join(name for name, _ in SPEAKERS), SURROUND_SINK, target)


def noise_conf(target):
    return """\
    { name = libpipewire-module-echo-cancel
        args = {
            library.name = aec/libspa-aec-webrtc
            aec.args = { webrtc.gain_control = false }
            # The module wants a playback side for echo cancelling, which
            # headphones do not need. Listening on the real output for it
            # caused dropouts there, so it gets an internal sink that
            # applications cannot see and that plays nowhere.
            sink.props = {
                node.name = "%s_ref" media.class = "Audio/Sink/Internal"
            }
            playback.props = {
                node.name = "%s_ref_out" node.passive = true
                node.dont-fallback = true target.object = "%s_unused"
            }
            capture.props = {
                node.name = "%s_in" node.passive = true
                node.dont-fallback = true target.object = "%s"
            }
            source.props = {
                node.name = "%s"
                node.description = "HyperX Microphone (noise suppression)"
            }
        }
    }""" % (NOISE_SOURCE, NOISE_SOURCE, NOISE_SOURCE, NOISE_SOURCE, target,
            NOISE_SOURCE)


def voice_conf(target, threshold):
    """Neural-network filter that only lets speech through: anything it does
    not recognise as a voice is silenced."""
    return """\
    { name = libpipewire-module-filter-chain
        args = {
            node.description = "HyperX Microphone (voice only)"
            media.name = "HyperX Microphone (voice only)"
            filter.graph = {
                nodes = [
                    { type = ladspa name = rnnoise plugin = "%s"
                      label = noise_suppressor_mono
                      control = {
                          "VAD Threshold (%%)" = %.1f
                          "VAD Grace Period (ms)" = 200
                          "Retroactive VAD Grace (ms)" = 0
                      } }
                ]
            }
            audio.rate = 48000
            audio.position = [ MONO ]
            capture.props = {
                node.name = "%s_in" node.passive = true
                node.dont-fallback = true target.object = "%s"
            }
            playback.props = { node.name = "%s" media.class = Audio/Source }
        }
    }""" % (VOICE_PLUGIN, threshold, NOISE_SOURCE, target, NOISE_SOURCE)


def install_voice_plugin():
    """Download the voice filter and check it is the file we expect."""
    with urllib.request.urlopen(VOICE_URL, timeout=60) as response:
        data = response.read()
    if hashlib.sha256(data).hexdigest() != VOICE_ZIP_SHA256:
        raise OSError("downloaded file does not match the expected checksum")
    os.makedirs(DATA_DIR, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        plugin = archive.read(VOICE_MEMBER)
    with open(VOICE_PLUGIN + ".part", "wb") as f:
        f.write(plugin)
    os.replace(VOICE_PLUGIN + ".part", VOICE_PLUGIN)


def write_hrir(directory):
    """Write one stereo impulse response per virtual speaker from the KEMAR
    dummy-head measurements shipped with libmysofa."""
    lib = ctypes.CDLL("libmysofa.so.1")
    lib.mysofa_open.restype = ctypes.c_void_p
    lib.mysofa_open.argtypes = [ctypes.c_char_p, ctypes.c_float,
                                ctypes.POINTER(ctypes.c_int),
                                ctypes.POINTER(ctypes.c_int)]
    float_p = ctypes.POINTER(ctypes.c_float)
    lib.mysofa_getfilter_float.argtypes = (
        [ctypes.c_void_p] + [ctypes.c_float] * 3 + [float_p] * 4)
    lib.mysofa_close.argtypes = [ctypes.c_void_p]

    length, error = ctypes.c_int(), ctypes.c_int()
    handle = lib.mysofa_open(SOFA_FILE.encode(), float(SAMPLE_RATE),
                             ctypes.byref(length), ctypes.byref(error))
    if not handle:
        raise OSError(f"cannot read {SOFA_FILE} (libmysofa error {error.value})")
    try:
        responses = {}
        for name, angle in SPEAKERS:
            left = (ctypes.c_float * length.value)()
            right = (ctypes.c_float * length.value)()
            delay_l, delay_r = ctypes.c_float(), ctypes.c_float()
            rad = math.radians(angle)
            lib.mysofa_getfilter_float(
                handle, 1.4 * math.cos(rad), 1.4 * math.sin(rad), 0.0,
                left, right, ctypes.byref(delay_l), ctypes.byref(delay_r))
            pad_l = int(round(delay_l.value * SAMPLE_RATE))
            pad_r = int(round(delay_r.value * SAMPLE_RATE))
            responses[name] = ([0.0] * pad_l + list(left),
                               [0.0] * pad_r + list(right))
    finally:
        lib.mysofa_close(handle)

    # front-left into the left ear ends up 3 dB down, leaving headroom for
    # several speakers playing at once
    scale = math.sqrt(0.5 / sum(x * x for x in responses["FL"][0]))
    os.makedirs(directory, exist_ok=True)
    for name, (left, right) in responses.items():
        size = max(len(left), len(right))
        frames = bytearray()
        for i in range(size):
            for ear in (left, right):
                value = ear[i] * scale if i < len(ear) else 0.0
                value = max(-1.0, min(1.0, value))
                frames += int(value * 8388607).to_bytes(3, "little", signed=True)
        with wave.open(os.path.join(directory, f"hrir-{name}.wav"), "wb") as f:
            f.setnchannels(2)
            f.setsampwidth(3)
            f.setframerate(SAMPLE_RATE)
            f.writeframes(bytes(frames))


def _die_with_parent():
    ctypes.CDLL("libc.so.6").prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG


class Audio:
    def __init__(self):
        self.loopback = None
        self.filters = {}  # name -> (process, config text)

    # --- devices and volumes ------------------------------------------------

    def _names(self, kind):
        names = []
        for line in pactl("list", "short", kind + "s").splitlines():
            cols = line.split("\t")
            if len(cols) > 1 and not cols[1].endswith(".monitor"):
                names.append((cols[1], cols[0]))
        return names

    def _find(self, kind):
        """Name of the headset's own sink or source."""
        for name, _index in self._names(kind):
            if "HyperX" in name:
                return name
        return None

    def _index(self, kind, wanted):
        for name, index in self._names(kind):
            if name == wanted:
                return index
        return None

    def present(self):
        return self._find("sink") is not None

    def get(self, kind):
        """Return (volume percent, muted) of the device applications use, the
        same one the system volume controls change, or None when absent."""
        name = self.preferred(kind)
        if not name:
            return None
        m = re.search(r"(\d+)%", pactl(f"get-{kind}-volume", name))
        if not m:
            return None
        return int(m.group(1)), "yes" in pactl(f"get-{kind}-mute", name)

    def set_volume(self, kind, percent, name=None):
        name = name or self.preferred(kind)
        if name:
            pactl(f"set-{kind}-volume", name, f"{int(percent)}%")

    def set_mute(self, kind, muted, raw=False):
        """Mute the device applications use, or with `raw` the headset's own
        device, which silences everything built on top of it."""
        name = self._find(kind) if raw else self.preferred(kind)
        if name:
            pactl(f"set-{kind}-mute", name, "1" if muted else "0")

    def set_monitoring(self, enabled, latency):
        """Route the headset mic back into the headset (software sidetone).

        Uses the filtered microphone while noise suppression is running, so
        you hear what others hear."""
        if self.loopback:
            pactl("unload-module", self.loopback)
            self.loopback = None
        if not enabled:
            return True
        source, sink = self.preferred("source"), self._find("sink")
        if not (source and sink):
            return False
        out = pactl("load-module", "module-loopback", f"source={source}",
                    f"sink={sink}", f"latency_msec={int(latency)}").strip()
        self.loopback = out if out.isdigit() else None
        return self.loopback is not None

    # --- effect processes ---------------------------------------------------

    def _stop(self, name):
        process, _conf = self.filters.pop(name, (None, None))
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()

    def _ensure(self, name, conf, node, kind):
        """Run filter `name` with `conf`; conf None stops it."""
        process, running = self.filters.get(name, (None, None))
        alive = process is not None and process.poll() is None
        if conf is None:
            self._stop(name)
            return
        if alive and running == conf:
            return
        self._stop(name)
        os.makedirs(CACHE_DIR, exist_ok=True)
        path = os.path.join(CACHE_DIR, name + ".conf")
        with open(path, "w") as f:
            f.write(BASE_CONF % conf)
        process = subprocess.Popen(
            ["pipewire", "-c", path], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, preexec_fn=_die_with_parent)
        self.filters[name] = (process, conf)
        for _ in range(30):  # wait for the node so it can be routed to
            if self._index(kind, node) or process.poll() is not None:
                break
            time.sleep(0.1)

    def apply_effects(self, config):
        """Start or stop the effect processes to match the settings.

        Returns a dict naming which effects are actually running."""
        sink, source = self._find("sink"), self._find("source")
        eq = bool(config["eq_enabled"] and sink)
        surround = bool(config["surround"] and sink)
        noise = bool(config["noise_suppression"] and source)

        # live gain changes go through set_eq_gains, so the stored config only
        # has to differ when the target changes
        if eq and "eq" in self.filters and self.filter_alive("eq"):
            pass
        else:
            self._ensure("eq", eq_conf(config["eq_gains"], sink) if eq else None,
                         EQ_SINK, "sink")
        conf = None
        if surround:
            try:
                if not os.path.exists(os.path.join(CACHE_DIR, "hrir-FL.wav")):
                    write_hrir(CACHE_DIR)
                conf = surround_conf(CACHE_DIR, EQ_SINK if eq else sink)
            except OSError:
                conf = None
        self._ensure("surround", conf, SURROUND_SINK, "sink")
        conf = None
        if noise:
            if (config["noise_mode"] == "voice"
                    and os.path.exists(VOICE_PLUGIN)):
                conf = voice_conf(source, config["voice_threshold"])
            else:
                conf = noise_conf(source)
        self._ensure("noise", conf, NOISE_SOURCE, "source")
        return {name: self.filter_alive(name)
                for name in ("eq", "surround", "noise")}

    def filter_alive(self, name):
        process, _conf = self.filters.get(name, (None, None))
        return process is not None and process.poll() is None

    def stop_effects(self):
        for name in list(self.filters):
            self._stop(name)

    def set_eq_gains(self, gains):
        """Change the running equalizer's bands without restarting it."""
        node = self._node_id(EQ_SINK)
        if not node:
            return False
        params = ['"preamp:Gain" %.1f' % eq_preamp(gains)]
        params += ['"band_%d:Gain" %.1f' % (i, gain)
                   for i, gain in enumerate(gains, 1)]
        try:
            result = subprocess.run(
                ["pw-cli", "set-param", node, "Props",
                 "{ params = [ %s ] }" % " ".join(params)],
                capture_output=True, text=True, timeout=3)
        except (OSError, subprocess.SubprocessError):
            return False
        return "Error" not in result.stdout + result.stderr

    def _node_id(self, wanted):
        """PipeWire's own id for a node; pactl's index is a different number."""
        try:
            listing = subprocess.run(["pw-cli", "ls", "Node"], text=True,
                                     capture_output=True, timeout=3).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        node = None
        for line in listing.splitlines():
            m = re.match(r"\s*id (\d+),", line)
            if m:
                node = m.group(1)
            elif re.search(r'node\.name = "%s"$' % re.escape(wanted),
                           line.strip()):
                return node
        return None

    # --- routing ------------------------------------------------------------

    def family(self, kind):
        """Every sink or source that leads to the headset."""
        if kind == "sink":
            return {self._find("sink"), EQ_SINK, SURROUND_SINK} - {None}
        return {self._find("source"), NOISE_SOURCE} - {None}

    def preferred(self, kind):
        """Where applications should play to / record from."""
        if kind == "sink":
            for name, node in (("surround", SURROUND_SINK), ("eq", EQ_SINK)):
                if self.filter_alive(name) and self._index("sink", node):
                    return node
            return self._find("sink")
        if self.filter_alive("noise") and self._index("source", NOISE_SOURCE):
            return NOISE_SOURCE
        return self._find("source")

    def default(self, kind):
        return pactl(f"get-default-{kind}").strip() or None

    def set_default(self, kind, name):
        if name:
            pactl(f"set-default-{kind}", name)

    def reroute(self):
        """Keep the default on the right node after effects changed, but only
        when the headset is what is currently in use."""
        for kind in ("sink", "source"):
            current, wanted = self.default(kind), self.preferred(kind)
            if wanted and current != wanted and current in self.family(kind):
                if kind == "sink":
                    self._carry_volume(current, wanted)
                self.set_default(kind, wanted)

    def _carry_volume(self, old, new):
        """Move the listening volume onto the sink that is about to become
        the default, so there is one volume instead of two stacked ones."""
        m = re.search(r"(\d+)%", pactl("get-sink-volume", old))
        if not m:
            return
        self.set_volume("sink", m.group(1), new)
        for name, _index in self._names("sink"):
            if name in self.family("sink") and name != new:
                self.set_volume("sink", 100, name)

    def switch_to_headset(self):
        """Make the headset the default; returns the defaults it replaced."""
        replaced = {}
        for kind in ("sink", "source"):
            current, wanted = self.default(kind), self.preferred(kind)
            if not wanted:
                continue
            if current and current not in self.family(kind):
                replaced[kind] = current
            self.set_default(kind, wanted)
        return replaced

    def switch_away(self, fallback):
        """Move the default off the headset, to `fallback` when it exists."""
        for kind in ("sink", "source"):
            if self.default(kind) not in self.family(kind):
                continue
            others = [name for name, _index in self._names(kind)
                      if name not in self.family(kind)]
            wanted = fallback.get(kind)
            if wanted not in others:
                wanted = others[0] if others else None
            self.set_default(kind, wanted)
