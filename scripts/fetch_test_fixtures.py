#!/usr/bin/env python3
"""Download CC0 camera RAW samples from raw.pixls.us for the format tests (dev only).

    python scripts/fetch_test_fixtures.py

Files land in tests/fixtures/raw/ (git-ignored, ~350 MB). Tests that need them are skipped
when absent. The app itself never downloads anything.
"""

from __future__ import annotations

import sys
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "https://raw.pixls.us/data/"
SAMPLES = {
    "cr2": "Canon/EOS 5D Mark III/5G4A9394.CR2",
    "cr3": "Canon/EOS R5/Canon_EOS_R5_CRAW_ISO_100_nocrop_dual.CR3",   # 45 MP
    "nef": "Nikon/Z 7/3-Nikon-Z7-RAW-14bit-lossless-compressed-L.NEF",  # 45.7 MP
    "arw": "Sony/ILCE-7M3/_DSC0009.ARW",
    "dng": "Google/Pixel 3a/IMG_20190918_164153.dng",
    "raf": "Fujifilm/X-T3/AFXT2720.RAF",
    "orf": "Olympus/E-M1MarkII/Olympus_EM1mk2_Standard_20MP.ORF",
}
DEST = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "raw"


def main() -> int:
    DEST.mkdir(parents=True, exist_ok=True)
    for ext, remote in SAMPLES.items():
        target = DEST / f"sample.{ext}"
        if target.is_file() and target.stat().st_size > 0:
            print(f"{target.name}: present")
            continue
        url = BASE + urllib.parse.quote(remote)
        print(f"{target.name} <- {url}")
        part = target.with_suffix(target.suffix + ".part")
        with urllib.request.urlopen(url, timeout=120) as response, part.open("wb") as out:
            while chunk := response.read(1 << 20):
                out.write(chunk)
        part.rename(target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
