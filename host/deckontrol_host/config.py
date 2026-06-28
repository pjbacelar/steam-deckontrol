"""Host daemon configuration.

Resolution order (last wins): built-in defaults -> config file -> environment
-> CLI flags. The config file lives at ``$XDG_CONFIG_HOME/deckontrol/host.json``
(falling back to ``~/.config/deckontrol/host.json``) and is created on first
run with a freshly generated pairing token.
"""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass, asdict
from pathlib import Path

DEFAULT_PORT = 27970          # input + control channel
DEFAULT_DISCOVERY_PORT = 27971  # broadcast discovery channel
STALE_FRAME_WINDOW = 1 << 20  # seq wrap tolerance for stale-frame detection
CLIENT_TIMEOUT_S = 5.0        # drop a paired client after this much silence


def _config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "deckontrol"


@dataclass
class HostConfig:
    name: str = os.uname().nodename
    port: int = DEFAULT_PORT
    discovery_port: int = DEFAULT_DISCOVERY_PORT
    token: str = ""
    enable_mouse: bool = True
    controller_name: str = "Steam Deck Controller (Network)"

    @classmethod
    def load(cls) -> "HostConfig":
        path = _config_dir() / "host.json"
        cfg = cls()
        if path.exists():
            try:
                data = json.loads(path.read_text())
                for key, value in data.items():
                    if hasattr(cfg, key):
                        setattr(cfg, key, value)
            except (ValueError, OSError):
                pass
        # Apply environment overrides.
        if env := os.environ.get("DECKONTROL_PORT"):
            cfg.port = int(env)
        if env := os.environ.get("DECKONTROL_TOKEN"):
            cfg.token = env
        if env := os.environ.get("DECKONTROL_NAME"):
            cfg.name = env
        # Generate + persist a token on first run.
        if not cfg.token:
            cfg.token = secrets.token_hex(8)
            cfg.save()
        return cfg

    def save(self) -> None:
        path = _config_dir() / "host.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2))
