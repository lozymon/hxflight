# hxflight

A control panel for the HyperX Cloud Flight wireless headset on Linux: a tray
icon and a small window that replace what HyperX NGENUITY does on Windows,
plus sound and microphone effects built on PipeWire.

This is an unofficial project and is not affiliated with HP or HyperX.

## Features

**Headset**

- Battery level in the tray icon, charging state, and a low-battery
  notification
- Estimated time remaining and a 12-hour battery history graph
- Headset on/off and microphone mute state, with optional notifications

**Sound**

- Headphone volume and mute
- 10-band equalizer with presets (Flat, Bass boost, FPS footsteps, Voice,
  Treble boost, Movie) and your own saved presets
- Virtual 7.1 surround

**Microphone**

- Volume, live input level meter, and a record-and-play-back test
- Noise suppression in two modes:
  - *Standard* reduces steady background noise
  - *Voice only* silences everything that is not speech
- Mic monitoring (hear yourself in the headset)
- The headset's mute button can also mute the microphone in the system, so
  apps show you as muted

**Automation**

- Switches the default output and microphone to the headset when it turns
  on, and back when it turns off
- Pauses media players when the headset turns off
- Start on login

## Supported headsets

Developed and tested with the **HyperX Cloud Flight for PS** (USB ID
`03f0:0c8c`, model 4P5H6AA).

These IDs use the same protocol in other open-source projects and are
accepted too, but have not been tested here: `03f0:0e90`, `0951:1749`,
`0951:16c4`, `0951:1723`.

## Requirements

- Linux with PipeWire and its PulseAudio compatibility layer
- Python 3 with GTK 3 bindings

On Linux Mint 22 and Ubuntu 24.04 everything needed is normally already
installed. If something is missing:

```sh
sudo apt install python3-gi python3-gi-cairo python3-cairo gir1.2-gtk-3.0 \
    gir1.2-ayatanaappindicator3-0.1 gir1.2-notify-0.7 \
    pipewire-bin pipewire-pulse pulseaudio-utils libmysofa1
```

## Install

```sh
git clone https://github.com/lozymon/hxflight.git
cd hxflight
./install.sh
```

`install.sh` asks for your password once to install a udev rule that lets
your user talk to the headset's USB dongle, and adds a menu entry. The app
itself runs from the cloned folder, so keep it where it is.

## Usage

Start **HyperX Cloud Flight** from the menu, or run:

```sh
./hxflight.py            # open the window
./hxflight.py --hidden   # start in the tray only
./hxflight.py --status   # print the battery level and exit
./hxflight.py --dump     # print raw HID reports, for debugging
```

Closing the window keeps the app running in the tray. Use **Quit** in the
tray menu to stop it.

## Things to know

- **Effects are software.** The headset has no equalizer or microphone
  processing of its own, so the effects run in PipeWire on your computer and
  only while the app is running. They appear as extra devices ("HyperX
  Equalizer", "HyperX Virtual Surround 7.1", a filtered HyperX microphone).
- **Battery is an estimate.** The headset reports a voltage, which is mapped
  to a percentage in 5% steps and averaged over about ten minutes. While
  charging it only shows "Charging".
- **Voice-only mode downloads a plugin.** The first time you select it, the
  app downloads the RNNoise plugin (37 MB) from the
  [noise-suppression-for-voice](https://github.com/werman/noise-suppression-for-voice)
  releases into `~/.local/share/hxflight/` and checks it against a fixed
  checksum. It passes any human voice, not only yours.
- **Virtual surround** needs the game or player set to 7.1 output.
- **LEDs cannot be controlled.** The earcup LEDs are red only; a short press
  of the headset's power button switches between solid, breathing and off.

## Files

| Path | Contents |
| --- | --- |
| `~/.config/hxflight/config.json` | Settings and saved equalizer presets |
| `~/.local/share/hxflight/` | Battery history, voice plugin |
| `~/.cache/hxflight/` | Generated tray icons and filter configs |

## Credits

The headset protocol and the battery voltage table come from
[HyperHeadset](https://github.com/LennardKittner/HyperXCloudIIWireless) and
[HeadsetControl](https://github.com/Sapd/HeadsetControl). Virtual surround
uses the MIT KEMAR dummy-head measurements shipped with libmysofa.

## License

[MIT](LICENSE)
