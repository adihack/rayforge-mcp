"""Persistent settings of the MCP bridge, stored in the addon data dir."""

import logging
import os
from dataclasses import asdict, dataclass, fields
from gettext import gettext as _
from pathlib import Path

import yaml

from rayforge.config import get_addon_data_dir

from . import ADDON_NAME

logger = logging.getLogger(__name__)

MCP_PATH = "/mcp"
MIN_PORT = 1024
MAX_PORT = 65535


@dataclass
class BridgeSettings:
    auto_start: bool = True
    port: int = 8765
    token: str = ""
    allow_lan: bool = False
    machine_tools: bool = True
    confirm_timeout: int = 60

    @property
    def host(self) -> str:
        return "0.0.0.0" if self.allow_lan else "127.0.0.1"

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}{MCP_PATH}"

    def problem(self) -> str | None:
        """Return why the server cannot start with these settings."""
        if not MIN_PORT <= self.port <= MAX_PORT:
            return _("Port must be between {lo} and {hi}.").format(
                lo=MIN_PORT, hi=MAX_PORT
            )
        if self.allow_lan and not self.token:
            return _(
                "Set an access token before allowing connections from "
                "other computers."
            )
        return None


_current: BridgeSettings | None = None


def settings_path() -> Path:
    return get_addon_data_dir(ADDON_NAME) / "settings.yaml"


def load_settings() -> BridgeSettings:
    path = settings_path()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return BridgeSettings()
    except (OSError, yaml.YAMLError) as e:
        logger.warning(f"Could not read {path}, using defaults: {e}")
        return BridgeSettings()

    settings = BridgeSettings()
    for field in fields(BridgeSettings):
        if field.name not in data:
            continue
        try:
            value = type(field.default)(data[field.name])
        except (TypeError, ValueError):
            logger.warning(f"Ignoring invalid {field.name!r} in {path}")
            continue
        setattr(settings, field.name, value)
    return settings


def save_settings(settings: BridgeSettings) -> None:
    path = settings_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(yaml.safe_dump(asdict(settings)), encoding="utf-8")
    # The file holds the access token.
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def get_settings() -> BridgeSettings:
    global _current
    if _current is None:
        _current = load_settings()
    return _current
