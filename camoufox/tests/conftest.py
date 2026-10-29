"""The entry's modules sit beside this folder, not in an installed package."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
