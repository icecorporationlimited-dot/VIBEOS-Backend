"""Downloads the free Google Fonts used by the collage styles into assets/collage/fonts/ (OFL licence).

    python tools/fetch_collage_fonts.py            # best effort: prints what worked, always exits 0
    COLLAGE_FONTS_STRICT=1 python tools/fetch_collage_fonts.py   # exit 1 if any font is missing

Run at image build time (Dockerfile.collage). The renderer loads fonts by the file names below; a missing font falls
back to Poppins / Lora, so a failed download never breaks a render - it only loses the style's typography.
You can also just drop the .ttf files into assets/collage/fonts/ by hand (same names) and commit them.

  handwriting : Caveat, Reenie Beanie, Gaegu        typewriter : Special Elite
  magazine    : Playfair Display, Bodoni Moda       street     : Anton, Bebas Neue, Permanent Marker
"""
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEST = Path(os.getenv("COLLAGE_ASSETS") or (ROOT / "assets" / "collage")) / "fonts"
BASES = ("https://raw.githubusercontent.com/google/fonts/main/ofl/",
         "https://github.com/google/fonts/raw/main/ofl/")

# local name -> [folder/file candidates in github.com/google/fonts]
FONTS = {
    "Caveat.ttf": ["caveat/Caveat[wght].ttf", "caveat/Caveat-Regular.ttf"],
    "ReenieBeanie.ttf": ["reeniebeanie/ReenieBeanie.ttf", "reeniebeanie/ReenieBeanie-Regular.ttf"],
    "Gaegu-Bold.ttf": ["gaegu/Gaegu-Bold.ttf"],
    "SpecialElite.ttf": ["specialelite/SpecialElite-Regular.ttf"],
    "PlayfairDisplay.ttf": ["playfairdisplay/PlayfairDisplay[wght].ttf", "playfairdisplay/PlayfairDisplay-Regular.ttf"],
    "PlayfairDisplay-Italic.ttf": ["playfairdisplay/PlayfairDisplay-Italic[wght].ttf", "playfairdisplay/PlayfairDisplay-Italic.ttf"],
    "BodoniModa.ttf": ["bodonimoda/BodoniModa[opsz,wght].ttf", "bodonimoda/BodoniModa-Regular.ttf"],
    "BodoniModa-Italic.ttf": ["bodonimoda/BodoniModa-Italic[opsz,wght].ttf", "bodonimoda/BodoniModa-Italic.ttf"],
    "Anton.ttf": ["anton/Anton-Regular.ttf"],
    "BebasNeue.ttf": ["bebasneue/BebasNeue-Regular.ttf"],
    "PermanentMarker.ttf": ["permanentmarker/PermanentMarker-Regular.ttf"],
}


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "vibeos-collage-assets"})
    with urllib.request.urlopen(req, timeout=40) as r:
        return r.read()


def _valid(data):
    if len(data) < 20_000:
        return False
    try:
        import io
        from PIL import ImageFont
        ImageFont.truetype(io.BytesIO(data), 40)
        return True
    except Exception:
        return False


def main():
    DEST.mkdir(parents=True, exist_ok=True)
    missing = []
    for local, candidates in FONTS.items():
        target = DEST / local
        if target.is_file() and target.stat().st_size > 20_000:
            print("have   ", local); continue
        ok = False
        for cand in candidates:
            for base in BASES:
                url = base + urllib.parse.quote(cand, safe="/")
                try:
                    data = _get(url)
                except Exception as e:
                    print("  fail ", url, "->", type(e).__name__); continue
                if _valid(data):
                    target.write_bytes(data); print("fetched", local, len(data) // 1024, "KB"); ok = True; break
            if ok:
                break
        if not ok:
            missing.append(local); print("MISSING", local, "(renderer will fall back to Poppins / Lora)")
    print("fonts ready: %d/%d" % (len(FONTS) - len(missing), len(FONTS)))
    if missing and os.getenv("COLLAGE_FONTS_STRICT", "").lower() in ("1", "true", "yes"):
        sys.exit(1)


if __name__ == "__main__":
    main()
