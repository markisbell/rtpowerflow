"""Regenerate packaging/windows/netzsim.ico from ui/public/favicon.svg.

Only needed when the favicon changes — the .ico is committed. Rasterises the
SVG with the system Chrome (headless, transparent background) and packs the
usual Windows sizes into one multi-resolution icon.

    pip install pillow
    python packaging/windows/make_icon.py
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
SVG = ROOT / "ui" / "public" / "favicon.svg"
ICO = Path(__file__).with_name("netzsim.ico")
SIZES = [16, 20, 24, 32, 40, 48, 64, 128, 256]
RENDER = 1024  # rasterise large, downscale with a good filter

CHROME_CANDIDATES = [
    shutil.which("chrome"),
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    shutil.which("msedge"),
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
]


def main() -> int:
    browser = next((c for c in CHROME_CANDIDATES if c and Path(c).is_file()), None)
    if browser is None:
        print("no Chrome/Edge found to rasterise the SVG", file=sys.stderr)
        return 1
    with tempfile.TemporaryDirectory() as tmp:
        png = Path(tmp) / "icon.png"
        subprocess.run(
            [browser, "--headless=new", "--disable-gpu", "--hide-scrollbars",
             f"--user-data-dir={Path(tmp) / 'profile'}",
             "--default-background-color=00000000",
             f"--window-size={RENDER},{RENDER}",
             f"--screenshot={png}", SVG.as_uri()],
            check=True, capture_output=True, timeout=120)
        img = Image.open(png).convert("RGBA")
    if img.size != (RENDER, RENDER):
        img = img.resize((RENDER, RENDER), Image.LANCZOS)
    img.save(ICO, format="ICO", sizes=[(s, s) for s in SIZES], bitmap_format="bmp")
    print(f"wrote {ICO} ({ICO.stat().st_size / 1024:.0f} KiB, sizes {SIZES})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
