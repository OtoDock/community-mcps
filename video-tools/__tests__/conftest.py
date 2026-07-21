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
