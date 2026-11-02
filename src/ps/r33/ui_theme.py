"""Industrial vision HMI theme: palette, fonts and helpers shared by all pages."""
from pathlib import Path

import numpy as np

WIDTH, HEIGHT = 1280, 720
TITLE = '智能线激光三维测量系统'

BG = (24, 27, 31)
PANEL = (32, 36, 41)
BAR = (45, 51, 58)
BORDER = (62, 69, 78)
GRID = (52, 58, 66)
TEXT = (230, 234, 238)
DIM = (150, 158, 168)
ACCENT = (47, 129, 247)
OK = (46, 160, 67)
NG = (218, 54, 51)
WARN = (210, 153, 34)
CENTRE = (80, 220, 120)
BAND = (36, 66, 48)            # tolerance band fill

PL_WINDOW = (448, 480)         # size of the hardware point-cloud window

FONT_DIR = Path(__file__).resolve().parents[1] / 'assets' / 'fonts'
CJK_FONTS = ['NotoSansCJKsc-pages-subset.otf', 'NotoSansCJKsc-UI13-subset.otf']
MONO_FONTS = ['DejaVuSansMono-pages-subset.ttf']


class Fonts:
    """Truetype cache; explicit paths override the bundled subsets (PC mockups)."""

    def __init__(self, cjk=None, mono=None):
        from PIL import ImageFont
        self._font = ImageFont.truetype
        self.cjk = cjk or _first(CJK_FONTS)
        self.mono = mono or _first(MONO_FONTS) or self.cjk
        self._cache = {}

    def get(self, size, mono=False):
        key = (size, mono)
        if key not in self._cache:
            self._cache[key] = self._font(self.mono if mono else self.cjk, size)
        return self._cache[key]


def _first(names):
    return next((str(FONT_DIR / n) for n in names if (FONT_DIR / n).is_file()), None)


def height_colours(levels):
    """Soft height palette of the PL view (blue -> yellow); index 0 is the background."""
    t = np.linspace(0.0, 1.0, levels)[:, None]
    lo, hi = np.array((105, 150, 210), float), np.array((220, 225, 100), float)
    return np.vstack([PANEL, lo + (hi - lo) * t]).astype(np.uint8)


def nice_step(span, ticks=6):
    raw = max(span, 1e-9) / ticks
    base = 10 ** np.floor(np.log10(raw))
    return float(next(m * base for m in (1, 2, 5, 10) if m * base >= raw))
