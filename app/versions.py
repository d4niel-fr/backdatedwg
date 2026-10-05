"""AutoCAD file-format versions and detection.

AutoCAD does not change its file format every release. A drawing saved by
AutoCAD 2026 uses the same "AC1032" format introduced with AutoCAD 2018, so a
file can only ever be identified by its format family, never by the exact
release that wrote it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class FormatVersion:
    code: str  # $ACADVER / DWG magic, e.g. "AC1024"
    year: int  # first release that wrote this format
    label: str  # human label for the format family
    short: str  # compact label used in "2018+ -> 2010"
    oda: str  # ODA File Converter version argument


VERSIONS: list[FormatVersion] = [
    FormatVersion("AC1009", 1992, "AutoCAD R11/R12", "R12", "ACAD12"),
    FormatVersion("AC1012", 1994, "AutoCAD R13", "R13", "ACAD13"),
    FormatVersion("AC1014", 1997, "AutoCAD R14", "R14", "ACAD14"),
    FormatVersion("AC1015", 2000, "AutoCAD 2000–2002", "2000", "ACAD2000"),
    FormatVersion("AC1018", 2004, "AutoCAD 2004–2006", "2004", "ACAD2004"),
    FormatVersion("AC1021", 2007, "AutoCAD 2007–2009", "2007", "ACAD2007"),
    FormatVersion("AC1024", 2010, "AutoCAD 2010–2012", "2010", "ACAD2010"),
    FormatVersion("AC1027", 2013, "AutoCAD 2013–2017", "2013", "ACAD2013"),
    FormatVersion("AC1032", 2018, "AutoCAD 2018–2026", "2018+", "ACAD2018"),
]

BY_CODE = {v.code: v for v in VERSIONS}
BY_YEAR = {v.year: v for v in VERSIONS}

# Versions offered as conversion targets (the chips in the UI).
TARGET_YEARS = [2000, 2004, 2007, 2010, 2013, 2018]
DEFAULT_TARGET_YEAR = 2010


def order(code: str) -> int:
    """Sort key for a format code; unknown codes sort as newest."""
    for i, v in enumerate(VERSIONS):
        if v.code == code:
            return i
    return len(VERSIONS)


def target(year: int) -> FormatVersion:
    if year not in TARGET_YEARS:
        raise ValueError(f"Unsupported target version {year}")
    return BY_YEAR[year]


@dataclass
class Detected:
    kind: str  # "DWG" | "DXF"
    code: Optional[str]  # None when the version could not be read
    binary_dxf: bool = False

    @property
    def version(self) -> Optional[FormatVersion]:
        return BY_CODE.get(self.code or "")


class DetectError(ValueError):
    pass


_BINARY_DXF = b"AutoCAD Binary DXF\r\n\x1a\x00"
_ACADVER_TEXT = re.compile(rb"\$ACADVER\s*\r?\n\s*1\s*\r?\n\s*(AC\d{4})")


def detect(head: bytes, filename: str) -> Detected:
    """Identify a DWG/DXF file from its first bytes (64 KB is plenty).

    Raises DetectError when the content does not look like either format.
    """
    name = filename.lower()
    if re.fullmatch(rb"AC\d{4}", head[:6]):
        return Detected("DWG", head[:6].decode("ascii"))
    # Very old DWG files start with AC1.40, AC1.50, AC2.10 ...
    if head.startswith(b"AC1.") or head.startswith(b"AC2."):
        return Detected("DWG", None)
    if head.startswith(_BINARY_DXF):
        idx = head.find(b"$ACADVER")
        code = None
        if idx >= 0:
            m = re.search(rb"(AC\d{4})", head[idx : idx + 64])
            code = m.group(1).decode() if m else None
        return Detected("DXF", code, binary_dxf=True)
    m = _ACADVER_TEXT.search(head)
    if m:
        return Detected("DXF", m.group(1).decode())
    # A DXF without $ACADVER is an R12-or-older file.
    if name.endswith(".dxf") and re.search(rb"^\s*0\s*\r?\n\s*SECTION", head, re.M):
        return Detected("DXF", "AC1009")
    raise DetectError("This doesn't look like a DWG or DXF file.")
