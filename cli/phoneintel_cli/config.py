"""CLI configuration and credential storage.

Location (override with ``PHONEINTEL_HOME``):

* Windows:        %APPDATA%\\phoneintel\\config.json
* macOS:          ~/Library/Application Support/phoneintel/config.json
* Linux / Termux: $XDG_CONFIG_HOME/phoneintel/config.json (default ~/.config/phoneintel)

The file holds tokens, so it is created with mode 0600 on POSIX systems and is
written atomically.  Secrets are never printed; ``config show`` masks them.
Environment variables take precedence over the file:
``PHONEINTEL_API_URL``, ``PHONEINTEL_API_KEY``.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

SECRET_FIELDS = frozenset({"api_key", "access_token", "refresh_token"})
SETTABLE_FIELDS = frozenset({"api_url", "api_key", "timeout_seconds", "verify_tls"})


class ConfigError(Exception):
    pass


def config_dir() -> Path:
    override = os.environ.get("PHONEINTEL_HOME")
    if override:
        return Path(override)
    if sys.platform.startswith("win"):
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "phoneintel"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "phoneintel"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "phoneintel"


def mask(value: str | None) -> str:
    if not value:
        return "(not set)"
    return f"{value[:4]}…{'*' * 6}" if len(value) > 8 else "******"


@dataclass
class CLIConfig:
    api_url: str = "http://localhost:8000"
    api_key: str | None = None
    access_token: str | None = None
    refresh_token: str | None = None
    timeout_seconds: float = 30.0
    verify_tls: bool = True
    last_investigation_id: str | None = None
    warnings: list[str] = field(default_factory=list, repr=False)

    # ------------------------------------------------------------- loading
    @classmethod
    def path(cls) -> Path:
        return config_dir() / "config.json"

    @classmethod
    def load(cls) -> CLIConfig:
        cfg = cls()
        path = cls.path()
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    raise ValueError("config root must be an object")
            except (OSError, ValueError) as exc:
                cfg.warnings.append(f"Ignoring unreadable config file {path}: {exc}")
                data = {}
            known = {f.name for f in fields(cls)} - {"warnings"}
            for key, value in data.items():
                if key in known and value is not None:
                    setattr(cfg, key, value)
        if os.environ.get("PHONEINTEL_API_URL"):
            cfg.api_url = os.environ["PHONEINTEL_API_URL"]
        if os.environ.get("PHONEINTEL_API_KEY"):
            cfg.api_key = os.environ["PHONEINTEL_API_KEY"]
        cfg.api_url = cfg.api_url.rstrip("/")
        return cfg

    # -------------------------------------------------------------- saving
    def save(self) -> None:
        path = self.path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {k: v for k, v in asdict(self).items() if k != "warnings"}
        # Never persist values that only came from the environment.
        if os.environ.get("PHONEINTEL_API_KEY") == payload.get("api_key"):
            payload["api_key"] = self._stored_value("api_key")
        fd, tmp = tempfile.mkstemp(prefix=".config-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2)
            if os.name == "posix":
                os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def _stored_value(self, key: str) -> Any:
        try:
            return json.loads(self.path().read_text(encoding="utf-8")).get(key)
        except (OSError, ValueError):
            return None

    def set_value(self, key: str, value: str) -> None:
        if key not in SETTABLE_FIELDS:
            raise ConfigError(f"Unknown or read-only key '{key}'. Settable: {', '.join(sorted(SETTABLE_FIELDS))}")
        if key == "timeout_seconds":
            try:
                parsed: Any = float(value)
            except ValueError as exc:
                raise ConfigError("timeout_seconds must be a number") from exc
            if not 1 <= parsed <= 600:
                raise ConfigError("timeout_seconds must be between 1 and 600")
        elif key == "verify_tls":
            if value.lower() not in {"true", "false"}:
                raise ConfigError("verify_tls must be true or false")
            parsed = value.lower() == "true"
        elif key == "api_url":
            if not value.startswith(("https://", "http://")):
                raise ConfigError("api_url must start with https:// or http://")
            parsed = value.rstrip("/")
        else:
            parsed = value or None
        setattr(self, key, parsed)

    def redacted(self) -> dict[str, Any]:
        data = {k: v for k, v in asdict(self).items() if k != "warnings"}
        for key in SECRET_FIELDS:
            data[key] = mask(data.get(key))
        data["config_file"] = str(self.path())
        return data

    def clear_session(self) -> None:
        self.access_token = None
        self.refresh_token = None
