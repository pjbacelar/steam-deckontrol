# Setup & Hardware Bring-Up Guide

How to turn an AMD PC into a SteamOS "Steam Machine" host and stream your real
Steam Deck's input to it with this project.

The order matters. We validate the risky parts (reading the Deck controller,
the network→virtual-controller path) with a plain CLI **before** dealing with
SteamOS quirks and Decky packaging.

---

## Part 0 — Hardware check (do this first)

SteamOS 3.8's DIY installer (June 2026) supports **any AMD desktop PC, but the
GPU must be AMD** (Radeon, including discrete cards). Nvidia is **not** supported.

- **AMD GPU?** → SteamOS, follow Part 1.
- **Nvidia GPU (or want broader support)?** → use **Bazzite** instead. It's a
  SteamOS-like Game-Mode distro with AMD *and* Nvidia images and one-click Decky.
  Everything in Parts 3–5 still applies; only the OS install (Part 1) differs —
  flash Bazzite from <https://bazzite.gg> with the same USB process.

You also need: a spare USB stick (≥8 GB), both machines on the **same LAN**
(wired host strongly preferred for latency), and a way to type on the host.

---

## Part 1 — Install SteamOS on the AMD PC

### 1.1 Make the installer USB

On any computer:

1. Download the SteamOS DIY recovery image from Valve's official page:
   <https://store.steampowered.com/steamos/download> (the "SteamOS 3.8" /
   generic installer, not the Deck-only recovery).
2. Flash it to the USB with **Balena Etcher** or **Rufus** (or `dd` on Linux:
   `sudo dd if=steamos-recovery.img of=/dev/sdX bs=4M status=progress oflag=sync`
   — triple-check `/dev/sdX`).

### 1.2 Boot and install

1. Plug the USB into the AMD PC, enter the boot menu (usually `F12`/`F11`/`F8`/
   `Esc` at power-on), and boot the USB.
2. You land in a SteamOS desktop. Run the **"Install SteamOS"** / "Reimage"
   icon. ⚠️ This **wipes the target drive** — back up first.
3. Finish, remove the USB, reboot. You'll boot into **Game Mode**, run through
   first-time setup, and sign into Steam.
4. **Update everything**: Settings → System → check for updates, reboot.

### 1.3 Make life easier (recommended)

Switch to Desktop Mode (Steam button → Power → Switch to Desktop), open
Konsole, and:

```bash
passwd                      # set a sudo password (blank by default)
sudo systemctl enable --now sshd    # so you can SSH in from your laptop
ip addr | grep 'inet '      # note the host's LAN IP, e.g. 192.168.1.50
```

---

## Part 2 — Install the host daemon on the Steam Machine

SteamOS has an **immutable root filesystem**, so don't fight `pacman`. Use
**distrobox** (ships with SteamOS) to get a normal package environment that can
still create the kernel virtual device.

```bash
# In Desktop Mode on the host:
distrobox create -n deckontrol --image ubuntu:24.04
distrobox enter deckontrol

# --- now inside the container ---
sudo apt update && sudo apt install -y python3-evdev python3-pip git
git clone https://github.com/pjbacelar/steam-deckontrol.git
cd steam-deckontrol
pip install --break-system-packages --no-deps ./host   # evdev already provided by apt
```

The daemon needs write access to `/dev/uinput`. Easiest for a home setup is to
run it as root:

```bash
sudo $(which deckontrol-host)
```

It prints a **pairing token** — copy it. Leave this running. (Production setup:
add the udev rule in `host/packaging/` and run rootless; see `host/README.md`.)

> If `deckontrol-host` can't open `/dev/uinput`, the kernel module may be off.
> On the **host** (not the container): `sudo modprobe uinput` and, to persist,
> `echo uinput | sudo tee /etc/modules-load.d/uinput.conf` (run
> `sudo steamos-readonly disable` first if `/etc` is read-only).

---

## Part 3 — Validate from the Deck with the standalone sender

This is the bring-up test. **Don't install the Decky plugin yet.** On the Deck,
in **Desktop Mode**, open Konsole.

### 3.1 Can you even read the controller?

```bash
distrobox create -n deckontrol --image ubuntu:24.04
distrobox enter deckontrol
sudo apt update && sudo apt install -y python3-evdev git
git clone https://github.com/pjbacelar/steam-deckontrol.git
cd steam-deckontrol
python3 scripts/deck_sender.py --list-devices
```

You should see entries like `Microsoft X-Box 360 pad` and/or `Steam Deck` marked
`[gamepad]`. If you see **nothing readable**, that's the Steam-Input-grab problem
— see Part 5. Getting past this is the make-or-break step.

### 3.2 Stream to the host

```bash
python3 scripts/deck_sender.py --discover --token <TOKEN_FROM_HOST>
# or skip discovery and give the IP directly:
python3 scripts/deck_sender.py --ip 192.168.1.50 --token <TOKEN>
```

You should see `paired with host ...` and a rising frame counter. Now press
buttons / move sticks on the Deck.

### 3.3 Confirm injection on the host

Back on the host (in the distrobox or after `apt install evtest joystick`):

```bash
ls /dev/input/by-id | grep -i network        # the virtual node should appear
evtest                                        # pick "Steam Deck Controller (Network)"
# press Deck buttons -> events scroll on the host. 🎉
```

Or just open **Steam → Settings → Controller** on the host and watch the new
controller light up. If buttons map to the wrong actions, that's expected
pre-calibration — see Part 4.

---

## Part 4 — Calibrate the evdev mapping (gyro / trackpads / back buttons)

The standard gamepad subset (face buttons, sticks, triggers, d-pad) should work
out of the box. The Deck-specific surfaces need their real codes confirmed,
because they vary with which node Steam Input exposes.

On the Deck (in the distrobox), find the richest device and dump live codes:

```bash
sudo apt install -y evtest
sudo evtest                 # choose the "Steam Deck" node if present
# Now press L4/L5/R4/R5, touch each trackpad, rotate the Deck for gyro.
# Note the exact EV_KEY / EV_ABS codes that fire.
```

Then edit the mapping tables (clearly marked) to match:

- Deck → wire: `plugin/py_modules/input_capture.py` (`_key_map()` and the
  `EV_ABS` branches in `_apply_event`).
- Wire → host gamepad: `host/deckontrol_host/virtual_controller.py`
  (`_button_map()` and `apply()`).

Re-run the standalone sender to verify each change before moving on.

---

## Part 5 — Wrap it in the Decky plugin (nice UX)

Once the standalone path works, get the in-Steam UI.

### 5.1 Install Decky Loader on the Deck

Desktop Mode → download and run the installer from <https://decky.xyz> (or the
official GitHub). Reboot to Game Mode; the Decky button (⚙ plug icon) appears in
the Quick Access menu.

### 5.2 Build and sideload this plugin

```bash
# On a dev machine (or the Deck's distrobox with node installed):
cd steam-deckontrol/plugin
npm install && npm run build        # produces dist/index.js
```

Copy the plugin folder (must contain `dist/`, `main.py`, `plugin.json`,
`py_modules/`) onto the Deck:

```bash
# from your dev machine:
scp -r plugin deck@<DECK_IP>:/home/deck/homebrew/plugins/steam-deckontrol
```

Then in Decky → Developer → **Reload**, or use "Install from ZIP". Open the
**Deckontrol** plugin in Quick Access → Scan LAN → pick host → enter token →
Connect, and toggle **Input-only mode**.

### 5.3 evdev for the plugin backend ⚠️

The plugin's Python backend (`main.py`) needs `evdev`, which Decky does **not**
provide by default. Until that's bundled, the standalone sender (Part 3) is the
reliable path. Options to make the plugin self-sufficient (pick one, then I can
wire it up):
- vendor a prebuilt x86-64 `evdev` wheel into `py_modules/`, or
- have the plugin shell out to the standalone sender / a small system service,
  or
- ship a tiny `requirements.txt` and install on first load.

---

## Part 6 — Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `--list-devices` shows no gamepad | Steam Input has grabbed the controller (`EVIOCGRAB`) | Test in Desktop Mode; or in Steam → Settings → Controller, set the Deck controller to disable Steam Input for the test; the kernel `Steam Deck` node may still be readable |
| `host did not respond` | Wrong IP, firewall, different subnet | Confirm both on same LAN; open UDP 27970–27971 on the host; ping the host |
| `pairing rejected: bad token` | Token mismatch | Re-copy from the host's `deckontrol-host` output or `deckontrol-host --print-token` |
| Virtual controller never appears on host | `/dev/uinput` not writable / module off | `sudo modprobe uinput`; run daemon as root or install the udev rule |
| Controller appears but wrong buttons | Mapping not calibrated | Part 4 |
| Works in Desktop, not Game Mode | Steam Input grabbing in Game Mode | Give the virtual controller an explicit Steam Input profile; disable Steam Input on the *source* Deck controller |
| High latency / jitter | Wi-Fi | Wire the host; keep the Deck on 5 GHz; both on the same AP |

---

## Quick reference

```
HOST  (SteamOS, AMD GPU):   distrobox → deckontrol-host         (prints token)
DECK  (bring-up):           scripts/deck_sender.py --discover --token <T>
DECK  (final UX):           Decky plugin "Deckontrol" → Scan → Connect
PORTS: udp/27970 control+input,  udp/27971 discovery
```
