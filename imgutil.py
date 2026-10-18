"""imgutil.py — small image helpers shared by the service and the worker."""
import io

MAX_EDGE = 2048


def downscale(data: bytes, max_edge: int = MAX_EDGE, quality: int = 92) -> bytes:
    """Return `data` unchanged if its longest side is <= max_edge, else a JPEG
    resized to max_edge (EXIF orientation applied). Bytes that Pillow cannot
    read are returned untouched so the normal analysis step rejects them with
    the usual 'unreadable' reason instead of failing here.
    """
    try:
        from PIL import Image, ImageOps
        with Image.open(io.BytesIO(data)) as probe:
            if max(probe.size) <= max_edge:
                return data
        img = Image.open(io.BytesIO(data))
        img = ImageOps.exif_transpose(img).convert("RGB")
        img.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        img.save(out, "JPEG", quality=quality, optimize=True)
        return out.getvalue()
    except Exception:
        return data
