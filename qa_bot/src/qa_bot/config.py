"""Strict local TOML configuration. No environment or credentials are read."""
from dataclasses import dataclass, fields
from pathlib import Path
import math
import tomllib


@dataclass(frozen=True)
class AppConfig:
    mode: str = "dry-run"
    external_services: bool = False
    browser_enabled: bool = False
    operation_timeout_seconds: float = 30.0
    max_retries: int = 0

    def __post_init__(self) -> None:
        if self.mode != "dry-run":
            raise ValueError("only dry-run mode is available")
        if self.external_services is not False or self.browser_enabled is not False:
            raise ValueError("services and browser must remain disabled")
        value = self.operation_timeout_seconds
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError("operation_timeout_seconds must be finite and positive")
        if type(self.max_retries) is not int or self.max_retries < 0:
            raise ValueError("max_retries must be a non-negative integer")


def parse_config(data: dict) -> AppConfig:
    unknown = set(data) - {item.name for item in fields(AppConfig)}
    if unknown:
        raise ValueError("unknown config fields: " + ", ".join(sorted(unknown)))
    return AppConfig(**data)


def load_config(path: Path | None = None) -> AppConfig:
    if path is None:
        return AppConfig()
    with path.open("rb") as stream:
        return parse_config(tomllib.load(stream))
