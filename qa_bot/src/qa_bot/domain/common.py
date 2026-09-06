"""Small shared contract validators."""
import math


def nonempty(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


def confidence(value: float) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("confidence must be numeric")
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError("confidence must be finite and between 0 and 1")
