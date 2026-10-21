"""Unit normalisation and conversion shared by the reader and the toolbox."""

from __future__ import annotations

import math
from typing import Optional

import numpy as np

# canonical unit -> (dimension, factor to the dimension's base unit)
_UNITS: dict[str, tuple[str, float]] = {
    "m/s": ("speed", 1.0),
    "km/h": ("speed", 1.0 / 3.6),
    "mph": ("speed", 0.44704),
    "m/s^2": ("accel", 1.0),
    "g": ("accel", 9.80665),
    "m/s^3": ("jerk", 1.0),
    "rad": ("angle", 1.0),
    "deg": ("angle", math.pi / 180.0),
    "rad/s": ("angular_rate", 1.0),
    "deg/s": ("angular_rate", math.pi / 180.0),
    "s": ("time", 1.0),
    "ms": ("time", 1e-3),
    "m": ("length", 1.0),
    "cm": ("length", 1e-2),
    "mm": ("length", 1e-3),
    "km": ("length", 1e3),
    "pa": ("pressure", 1.0),
    "hpa": ("pressure", 1e2),
    "kpa": ("pressure", 1e3),
    "mbar": ("pressure", 1e2),
    "bar": ("pressure", 1e5),
    "%": ("ratio", 1.0),
    "%/s": ("ratio_rate", 1.0),
    "1/m": ("curvature", 1.0),
    "nm": ("torque", 1.0),
}

_ALIASES = {
    "kph": "km/h", "kmh": "km/h", "km/hr": "km/h",
    "mps": "m/s", "m/sec": "m/s",
    "m/s2": "m/s^2", "m/s/s": "m/s^2", "m/s²": "m/s^2", "mps2": "m/s^2", "ms-2": "m/s^2",
    "m/s3": "m/s^3", "m/s³": "m/s^3",
    "°": "deg", "degree": "deg", "degrees": "deg",
    "°/s": "deg/s", "deg/sec": "deg/s",
    "rad/sec": "rad/s",
    "sec": "s", "seconds": "s",
    "perc": "%", "percent": "%",
    "n.m": "nm", "n*m": "nm",
}


def normalize_unit(unit: Optional[str]) -> Optional[str]:
    """Return a canonical unit string, or None when empty/unknown-dimensionless."""
    if unit is None:
        return None
    u = str(unit).strip()
    if u.startswith("[") and u.endswith("]"):
        u = u[1:-1].strip()
    if u in {"", "-", "u[1]", "1", "none"}:
        return None
    low = u.lower().replace(" ", "")
    low = _ALIASES.get(low, low)
    return low


def conversion_factor(from_unit: Optional[str], to_unit: Optional[str]) -> Optional[float]:
    """Multiplicative factor converting from_unit to to_unit, None if not convertible."""
    a, b = normalize_unit(from_unit), normalize_unit(to_unit)
    if a == b:
        return 1.0
    if a not in _UNITS or b not in _UNITS:
        return None
    dim_a, fa = _UNITS[a]
    dim_b, fb = _UNITS[b]
    if dim_a != dim_b:
        return None
    return fa / fb


def convert(values: np.ndarray, from_unit: str, to_unit: str) -> np.ndarray:
    factor = conversion_factor(from_unit, to_unit)
    if factor is None:
        raise ValueError(f"cannot convert '{from_unit}' to '{to_unit}'")
    return np.asarray(values, dtype=float) * factor


def known_units() -> list[str]:
    return sorted(_UNITS)


_SUP = str.maketrans("0123456789-", "⁰¹²³⁴⁵⁶⁷⁸⁹⁻")
_UNSUP = {v: k for k, v in zip("0123456789-", "⁰¹²³⁴⁵⁶⁷⁸⁹⁻")}


def pretty(unit: str) -> str:
    """A unit as shown to people: powers as superscripts, e.g. m/s^2 -> m/s²."""
    import re

    return re.sub(r"\^(-?\d+)", lambda m: m.group(1).translate(_SUP), unit or "")


def plain(unit: str) -> str:
    """The stored form of a unit typed or picked with superscripts, e.g. m/s² -> m/s^2."""
    import re

    return re.sub(r"[⁰¹²³⁴⁵⁶⁷⁸⁹⁻]+", lambda m: "^" + "".join(_UNSUP[c] for c in m.group()), unit or "")
