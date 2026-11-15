"""Configuration management for PHONEINTEL CLI."""

from __future__ import annotations

import contextlib
import json
import logging
import os
from dataclasses import asdict, dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class CLIConfig:
    api_url: str = "http://localhost:8000"
    api_key: str | None = None
    access_token: str | None = None
    default_purpose: str = "Legitimate investigative triage and fraud prevention"
    default_basis: str = "fraud_prevention"

    @classmethod
    def config_dir(cls) -> Path:
        base = os.environ.get("PHONEINTEL_HOME")
        path = Path(base) if base else Path.home() / ".phoneintel"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @classmethod
    def config_file(cls) -> Path:
        return cls.config_dir() / "config.json"

    @classmethod
    def load(cls) -> CLIConfig:
        cfg = cls()
        file_path = cls.config_file()
        if file_path.is_file():
            with contextlib.suppress(Exception):
                data = json.loads(file_path.read_text(encoding="utf-8"))
                for k, v in data.items():
                    if hasattr(cfg, k) and v is not None:
                        setattr(cfg, k, v)

        # Environment variable overrides
        env_url = os.environ.get("PHONEINTEL_API_URL")
        if env_url:
            cfg.api_url = env_url.rstrip("/")
        env_key = os.environ.get("PHONEINTEL_API_KEY")
        if env_key:
            cfg.api_key = env_key
        env_token = os.environ.get("PHONEINTEL_TOKEN") or os.environ.get("PHONEINTEL_ACCESS_TOKEN")
        if env_token:
            cfg.access_token = env_token

        return cfg

    def save(self) -> None:
        file_path = self.config_file()
        file_path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        # Restrict permissions on Unix-like systems
        with contextlib.suppress(Exception):
            os.chmod(file_path, 0o600)
