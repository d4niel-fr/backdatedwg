"""Drawing units: what one unit in the file means, and how to read "3.5 m"."""

from __future__ import annotations

import re
from dataclasses import dataclass

# $INSUNITS code -> (name, short label, metres per unit)
INSUNITS: dict[int, tuple[str, str, float | None]] = {
    0: ("unitless", "units", None),
    1: ("inches", "in", 0.0254),
    2: ("feet", "ft", 0.3048),
    3: ("miles", "mi", 1609.344),
    4: ("millimetres", "mm", 0.001),
    5: ("centimetres", "cm", 0.01),
    6: ("metres", "m", 1.0),
    7: ("kilometres", "km", 1000.0),
    8: ("microinches", "µin", 2.54e-8),
    9: ("mils", "mil", 2.54e-5),
    10: ("yards", "yd", 0.9144),
    14: ("decimetres", "dm", 0.1),
}

# Spellings accepted after a number: "3500mm", "3.5 m", "12ft", '10"'.
_SUFFIX_M: dict[str, float] = {
    "mm": 0.001, "millimetre": 0.001, "millimetres": 0.001, "millimeter": 0.001, "millimeters": 0.001,
    "cm": 0.01, "centimetre": 0.01, "centimetres": 0.01, "centimeter": 0.01, "centimeters": 0.01,
    "m": 1.0, "metre": 1.0, "metres": 1.0, "meter": 1.0, "meters": 1.0,
    "km": 1000.0,
    "in": 0.0254, "inch": 0.0254, "inches": 0.0254, '"': 0.0254,
    "ft": 0.3048, "foot": 0.3048, "feet": 0.3048, "'": 0.3048,
    "yd": 0.9144, "yard": 0.9144, "yards": 0.9144,
}

_NUMBER = re.compile(r"^\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)\s*([a-zA-Z\"']*)\s*$")


class UnitError(ValueError):
    pass


@dataclass(frozen=True)
class Units:
    code: int
    name: str
    short: str
    to_m: float  # metres per drawing unit
    guessed: bool = False

    def describe(self) -> dict:
        return {"code": self.code, "name": self.name, "short": self.short, "toMetres": self.to_m, "guessed": self.guessed}

    def length(self, value) -> float:
        """A number (already in drawing units) or a string like ``"3.5 m"``."""
        if isinstance(value, bool):
            raise UnitError("Expected a length, got true/false.")
        if isinstance(value, (int, float)):
            if value != value or value in (float("inf"), float("-inf")):
                raise UnitError("A length can't be NaN or infinite.")
            return float(value)
        if isinstance(value, str):
            m = _NUMBER.match(value)
            if not m:
                raise UnitError(f"Can't read {value!r} as a length.")
            number, suffix = float(m.group(1)), m.group(2).lower()
            if not suffix:
                return number
            metres = _SUFFIX_M.get(suffix)
            if metres is None:
                raise UnitError(f"Unknown unit {m.group(2)!r} in {value!r}.")
            return number * metres / self.to_m
        raise UnitError(f"Expected a length, got {value!r}.")

    def show(self, value: float) -> str:
        """A length for people, in the drawing's own unit."""
        return f"{value:,.4g} {self.short}" if abs(value) < 1e6 else f"{value:,.0f} {self.short}"


def detect_units(code: int | None, extent_max: float | None) -> Units:
    """Units from ``$INSUNITS``; for unitless drawings, a labelled guess."""
    entry = INSUNITS.get(int(code or 0))
    if entry and entry[2] is not None:
        return Units(int(code), entry[0], entry[1], entry[2])
    # Unitless: building plans are drawn in millimetres far more often than in
    # kilometres, so a big extent means mm and a small one means metres.
    if extent_max is not None and extent_max >= 1000:
        return Units(0, "millimetres", "mm", 0.001, guessed=True)
    return Units(0, "metres", "m", 1.0, guessed=True)
