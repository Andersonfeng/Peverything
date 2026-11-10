"""User preferences, stored next to the index as ``settings.json``."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import util

CLOSE_ACTIONS: dict[str, str] = {
    "tray": "最小化到托盘",
    "taskbar": "最小化到任务栏",
    "exit": "退出程序",
}

DEFAULT_HOTKEY = "Ctrl+Alt+E"


@dataclass
class Settings:
    hotkey: str = DEFAULT_HOTKEY
    hotkey_enabled: bool = True
    tray_icon: bool = True
    close_action: str = "tray"
    start_hidden: bool = False
    extras: dict = field(default_factory=dict)

    # ------------------------------------------------------------------ io ---
    @classmethod
    def path(cls) -> Path:
        return util.settings_path()

    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        target = path or cls.path()
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        if not isinstance(raw, dict):
            return cls()
        settings = cls()
        settings.hotkey = str(raw.get("hotkey", settings.hotkey) or "")
        settings.hotkey_enabled = bool(raw.get("hotkey_enabled", settings.hotkey_enabled))
        settings.tray_icon = bool(raw.get("tray_icon", settings.tray_icon))
        action = str(raw.get("close_action", settings.close_action))
        settings.close_action = action if action in CLOSE_ACTIONS else "tray"
        settings.start_hidden = bool(raw.get("start_hidden", settings.start_hidden))
        return settings

    def save(self, path: Path | None = None) -> Path:
        target = path or self.path()
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = asdict(self)
        payload.pop("extras", None)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(target)
        return target

    def copy(self) -> "Settings":
        return Settings(**{key: value for key, value in asdict(self).items() if key != "extras"})
