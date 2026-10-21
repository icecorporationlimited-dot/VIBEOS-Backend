"""Download the small ONNX models used by vision_onnx.py into ./models.

    python tools/fetch_models.py          # run at Docker build time or by hand

Never fails the build: a missing model only means the editor uses its old
heuristics. Re-running skips files that already exist.
"""
import os
import sys
import urllib.request
from pathlib import Path

MODELS_DIR = Path(os.environ.get("VIBEOS_MODELS_DIR")
                  or Path(__file__).resolve().parent.parent / "models")

MODELS = {
    # YuNet face detector (OpenCV Zoo, Apache-2.0 / MIT)
    "face_detection_yunet_2023mar.onnx":
        "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/"
        "face_detection_yunet_2023mar.onnx",
    # U2-Net small (rembg release asset, Apache-2.0)
    "u2netp.onnx":
        "https://github.com/danielgatis/rembg/releases/download/v0.0.0/u2netp.onnx",
}
MIN_BYTES = 100_000          # anything smaller is an error page, not a model


def fetch(name, url):
    dest = MODELS_DIR / name
    if dest.is_file() and dest.stat().st_size >= MIN_BYTES:
        print(f"ok (cached)  {name}")
        return True
    tmp = dest.with_suffix(".part")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "vibeos-fetch-models"})
        with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as fh:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                fh.write(chunk)
        if tmp.stat().st_size < MIN_BYTES:
            raise ValueError(f"only {tmp.stat().st_size} bytes")
        tmp.replace(dest)
        print(f"ok           {name} ({dest.stat().st_size // 1024} KB)")
        return True
    except Exception as exc:
        print(f"FAILED       {name}: {exc!r}", file=sys.stderr)
        tmp.unlink(missing_ok=True)
        return False


def main():
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    results = [fetch(n, u) for n, u in MODELS.items()]
    return 0 if all(results) else 0       # soft-fail on purpose (see docstring)


if __name__ == "__main__":
    raise SystemExit(main())
