"""External conversion engines.

* ODA File Converter (Open Design Alliance, free download, proprietary) reads
  and writes every DWG/DXF version. It is the engine that makes DWG -> older
  DWG possible and is the one used whenever it is installed.
* LibreDWG (GPL) reads DWG files (R13-2018 formats) into DXF. It is the
  fallback DWG reader. Its DWG writer is experimental and failed on most
  real drawings in testing, so it is never used for output.
* DXF in, DXF out always works in-process through ezdxf (see converter.py).
"""

from __future__ import annotations

import glob
import os
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


class EngineError(RuntimeError):
    def __init__(self, message: str, broken: bool = False):
        super().__init__(message)
        self.broken = broken  # the engine itself is unusable, not the file


class Cancelled(Exception):
    pass


@dataclass
class CancelToken:
    """Shared between a job and the subprocesses it starts."""

    event: threading.Event = field(default_factory=threading.Event)
    _proc: Optional[subprocess.Popen] = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def cancel(self) -> None:
        self.event.set()
        with self._lock:
            if self._proc and self._proc.poll() is None:
                _kill(self._proc)

    @property
    def cancelled(self) -> bool:
        return self.event.is_set()

    def check(self) -> None:
        if self.event.is_set():
            raise Cancelled()

    def run(self, cmd: list[str], timeout: float, env: Optional[dict] = None) -> tuple[int, str]:
        """Run a command, killing it on cancel or timeout. Returns (code, output)."""
        self.check()
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )
        with self._lock:
            self._proc = proc
        try:
            out, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill(proc)
            proc.communicate()
            raise EngineError(f"{Path(cmd[0]).name} took longer than {int(timeout)} s and was stopped.")
        finally:
            with self._lock:
                self._proc = None
        self.check()
        return proc.returncode, out.decode("utf-8", "replace")


def _kill(proc: subprocess.Popen) -> None:
    # Kill the whole process group: xvfb-run starts the converter as a child.
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, AttributeError):
        proc.kill()


def _timeout() -> float:
    return float(os.environ.get("BACKDATE_ENGINE_TIMEOUT", "900"))


# ── ODA File Converter ─────────────────────────────────────────────────────

_ODA_GLOBS = [
    "/usr/bin/ODAFileConverter",
    "/usr/local/bin/ODAFileConverter",
    "/usr/bin/ODAFileConverter_*/ODAFileConverter",
    "/opt/ODAFileConverter*/ODAFileConverter",
    "/opt/oda/ODAFileConverter*/ODAFileConverter",
    "/Applications/ODAFileConverter.app/Contents/MacOS/ODAFileConverter",
    r"C:\Program Files\ODA\ODAFileConverter*\ODAFileConverter.exe",
]


class Oda:
    name = "ODA File Converter"

    def __init__(self, exe: str):
        self.exe = exe

    @classmethod
    def find(cls) -> Optional["Oda"]:
        configured = os.environ.get("ODA_CONVERTER_PATH")
        if configured:
            return cls(configured) if os.path.isfile(configured) else None
        found = shutil.which("ODAFileConverter")
        if found:
            return cls(found)
        for pattern in _ODA_GLOBS:
            for path in sorted(glob.glob(pattern), reverse=True):
                if os.path.isfile(path):
                    return cls(path)
        return None

    def _command(self, args: list[str]) -> tuple[list[str], dict]:
        env = os.environ.copy()
        cmd = [self.exe, *args]
        if os.name == "posix" and not env.get("DISPLAY") and shutil.which("xvfb-run"):
            # The converter is a Qt GUI app; on a headless server it needs a
            # virtual display even in command-line mode.
            cmd = ["xvfb-run", "-a", *cmd]
        return cmd, env

    def convert(
        self,
        src: Path,
        work: Path,
        oda_version: str,
        out_format: str,
        cancel: CancelToken,
        audit: bool = True,
    ) -> tuple[Path, list[str]]:
        """Convert one file. Returns (output path, audit/error log lines)."""
        in_dir = work / "in"
        out_dir = work / "out"
        for d in (in_dir, out_dir):
            shutil.rmtree(d, ignore_errors=True)
            d.mkdir(parents=True)
        staged = in_dir / src.name
        try:
            os.link(src, staged)
        except OSError:
            shutil.copyfile(src, staged)

        args = [str(in_dir), str(out_dir), oda_version, out_format, "0", "1" if audit else "0", src.name]
        cmd, env = self._command(args)
        code, output = cancel.run(cmd, _timeout(), env)

        ext = "." + out_format.lower()
        produced = [p for p in out_dir.iterdir() if p.suffix.lower() == ext and p.stem == src.stem]
        log: list[str] = []
        for err in out_dir.glob("*.err"):
            log += [ln.strip() for ln in err.read_text("utf-8", "replace").splitlines() if ln.strip()]
        # The Linux build often exits with a crash *after* writing a good file,
        # so success is judged by the output file, not the exit code.
        if "error while loading shared libraries" in output:
            raise EngineError(f"ODA File Converter isn't installed correctly on the server ({output.strip()[-200:]}).", broken=True)
        if not produced or produced[0].stat().st_size == 0:
            detail = "; ".join(log[-3:]) or output.strip()[-300:] or f"exit code {code}"
            raise EngineError(f"ODA File Converter could not convert the file ({detail}).")
        shutil.rmtree(in_dir, ignore_errors=True)
        return produced[0], log


# ── LibreDWG ───────────────────────────────────────────────────────────────

_LIBREDWG_AS = {
    "AC1015": "r2000",
    "AC1018": "r2004",
    "AC1021": "r2007",
    "AC1024": "r2010",
    "AC1027": "r2013",
}  # dwg2dxf can't write r2018 DXF yet; without --as it keeps the DWG's version


class LibreDwg:
    name = "LibreDWG"

    def __init__(self, dwg2dxf: str):
        self.dwg2dxf_exe = dwg2dxf

    @classmethod
    def find(cls) -> Optional["LibreDwg"]:
        if os.environ.get("BACKDATE_DISABLE_LIBREDWG"):
            return None
        reader = shutil.which("dwg2dxf")
        return cls(reader) if reader else None

    def dwg_to_dxf(self, src: Path, dest: Path, code: Optional[str], cancel: CancelToken) -> list[str]:
        """Write the DWG as DXF, keeping its own version where possible."""
        cmd = [self.dwg2dxf_exe, "-y", "-o", str(dest), str(src)]
        if code in _LIBREDWG_AS:
            cmd[1:1] = ["--as", _LIBREDWG_AS[code]]
        code_, output = cancel.run(cmd, _timeout())
        if not dest.exists() or dest.stat().st_size == 0:
            raise EngineError(_tail(output) or f"dwg2dxf failed with exit code {code_}")
        return _warnings(output)


def _tail(text: str) -> str:
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    return "; ".join(lines[-2:])[:300]


def _warnings(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if ln.strip().startswith(("ERROR", "Warning", "WARNING"))]


# ── discovery ──────────────────────────────────────────────────────────────


@dataclass
class Engines:
    oda: Optional[Oda]
    libredwg: Optional[LibreDwg]
    checked_at: float = field(default_factory=time.time)

    @classmethod
    def discover(cls) -> "Engines":
        mode = os.environ.get("BACKDATE_ENGINE", "auto").lower()
        oda = Oda.find() if mode in ("auto", "oda") else None
        if mode == "oda" and oda is None:
            raise EngineError("BACKDATE_ENGINE=oda but ODA File Converter was not found.")
        return cls(oda=oda, libredwg=LibreDwg.find())

    @property
    def can_read_dwg(self) -> bool:
        return self.oda is not None or self.libredwg is not None

    def can_write(self, out_format: str) -> bool:
        return out_format == "DXF" or self.oda is not None

    def describe(self) -> dict:
        return {
            "oda": self.oda is not None,
            "libredwg": self.libredwg is not None,
            "primary": self.oda.name if self.oda else "ezdxf" + (" + LibreDWG" if self.libredwg else ""),
        }
