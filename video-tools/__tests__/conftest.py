"""Test bootstrap: import the flat modules, point fftools at a usable ffmpeg,
and bake the built-in looks into a tmp dir before color.py is imported."""

import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

_ffmpeg = (os.environ.get("FFMPEG_PATH")
           or shutil.which("ffmpeg")
           or str(Path.home() / "tools" / "bin" / "ffmpeg"))
_ffprobe = (os.environ.get("FFPROBE_PATH")
            or shutil.which("ffprobe")
            or str(Path.home() / "tools" / "bin" / "ffprobe"))
os.environ["FFMPEG_PATH"] = _ffmpeg
os.environ["FFPROBE_PATH"] = _ffprobe

HAVE_FFMPEG = Path(_ffmpeg).exists() and Path(_ffprobe).exists()

_looks_dir = Path(tempfile.mkdtemp(prefix="vt-looks-"))
os.environ["VIDEO_TOOLS_LOOKS_DIR"] = str(_looks_dir)

import color  # noqa: E402  (env must be set first)

color.emit_builtin_looks(str(_looks_dir))


import pytest  # noqa: E402

import shared  # noqa: E402

# The real judges of a file written or read beside a source, for the tests of
# them; every other test writes and reads beside its sources as its identity
# resolver allows.
REAL_WRITE_BESIDE = shared._write_beside
REAL_READ_BESIDE = shared._read_beside


async def same_path(path, writing=False):
    """A resolver that answers every path as given (the proxy's judge is
    tested on its own): the async shape ``_resolve_path`` has."""
    return path


async def _beside(path):
    return path


@pytest.fixture(scope="session", autouse=True)
def _tmp_files_sit_beneath_the_mount(tmp_path_factory):
    """Every test's tmp tree is the mount the tool opens beneath: a person's
    file is read, and an output written, the way production does it (no
    link followed, the atomic writer, ffmpeg's descriptor door). The tool's
    own temp folders (``tempfile``) stay outside it, as in production."""
    old = os.environ.get("VIDEOTOOLS_WRITE_ROOT")
    os.environ["VIDEOTOOLS_WRITE_ROOT"] = str(tmp_path_factory.getbasetemp())
    yield
    if old is None:
        os.environ.pop("VIDEOTOOLS_WRITE_ROOT", None)
    else:
        os.environ["VIDEOTOOLS_WRITE_ROOT"] = old


@pytest.fixture(autouse=True)
def _writes_beside_allowed(monkeypatch):
    monkeypatch.setattr(shared, "_write_beside", _beside)
    monkeypatch.setattr(shared, "_read_beside", _beside)
