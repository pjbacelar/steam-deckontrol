# Deckontrol Host Daemon

Runs on the **Steam Machine / SteamOS PC**. Receives input frames from the Deck
over UDP and injects them as a virtual controller (and optional virtual mouse)
via `uinput`, so Steam Input sees a normal gamepad named *Steam Deck Controller
(Network)*.

## Install

```bash
# from the repo root
pip install --user ./host          # installs the `deckontrol-host` script + evdev

# allow uinput without root (recommended)
sudo cp host/packaging/99-deckontrol-uinput.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger
sudo usermod -aG input "$USER"     # log out/in afterwards
```

## Run

```bash
deckontrol-host                 # foreground, prints the pairing token
deckontrol-host --print-token   # just show the token (for manual pairing)
deckontrol-host -v              # debug logging (per-packet)
```

Or as a user service:

```bash
cp host/packaging/deckontrol-host.service ~/.config/systemd/user/
systemctl --user enable --now deckontrol-host
journalctl --user -u deckontrol-host -f
```

## Configuration

First run writes `~/.config/deckontrol/host.json` with a generated token:

| Key               | Default                              | Notes                                   |
| ----------------- | ------------------------------------ | --------------------------------------- |
| `name`            | hostname                             | advertised over LAN discovery           |
| `port`            | `27970`                              | control + input UDP port                |
| `discovery_port`  | `27971`                              | HELLO/HELLO_ACK broadcast port          |
| `token`           | generated                            | shared secret the Deck must present     |
| `enable_mouse`    | `true`                               | right trackpad drives a virtual mouse   |
| `controller_name` | `Steam Deck Controller (Network)`    | name Steam sees                         |

Env overrides: `DECKONTROL_PORT`, `DECKONTROL_TOKEN`, `DECKONTROL_NAME`.

## Notes / known on-device work

- **Steam Input** may claim or reorder controllers. Give the virtual pad an
  explicit Steam Input config if Steam grabs it ahead of your game.
- The trackpad→mouse mapping uses a fixed sensitivity (`*1000`); tune in
  `virtual_controller.py` once tested on hardware.
