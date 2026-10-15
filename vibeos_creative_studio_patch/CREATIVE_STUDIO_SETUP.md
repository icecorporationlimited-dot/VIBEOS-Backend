# VibeOS Creative Studio patch (photo-first, Reel-ready architecture)

## What this patch does

- Adds `creative_studio/analyzer.py`: measurable pixel analysis (exposure, contrast, clipping, sharpness proxy) and conservative edit suggestions.
- Adds `creative_studio/photo_engine.py`: runs the existing `editor.py` look, then adds bounded per-image exposure/contrast/color corrections. It can return an analysis/edit report.
- Adds `creative_studio/reel_engine.py`: checks for ffmpeg/ffprobe, probes video metadata and defines a future Reel planning contract. It does **not** yet auto-select scenes, sync music or render Reels.

## Install

1. Extract the patch ZIP.
2. Copy the `creative_studio` folder into `Internal Storage/Download/vibeos/` so the layout is:

   ```text
   vibeos/
     editor.py
     media_quality.py
     creative_studio/
       __init__.py
       analyzer.py
       photo_engine.py
       reel_engine.py
   ```

3. Do not overwrite `editor.py`, `settings.py`, or other existing files with this patch.
4. Existing project dependencies (`Pillow`, `numpy`) are enough for the photo pipeline.

## Test on Termux

```bash
cd /storage/emulated/0/Download/vibeos
python -m compileall -q creative_studio
python - <<'PY'
import io
from PIL import Image
from creative_studio import analyze_photo, build_edit_plan
from creative_studio.photo_engine import edit_photo_adaptive

buf = io.BytesIO()
Image.new('RGB', (1200, 1600), (120, 95, 80)).save(buf, 'JPEG')
data = buf.getvalue()
print(analyze_photo(data))
print(build_edit_plan(analyze_photo(data), 'natural'))
# Requires the existing editor.py and its fonts/presets.
out, report = edit_photo_adaptive(data, vibe='natural', size='post', return_report=True)
print('output bytes:', len(out.getvalue()))
print('edit plan:', report['edit_plan'])
PY
```

The test image is synthetic; it only checks software execution, not aesthetic quality.

## Integration into ContentBuffer

For the next integration step, update `ContentBuffer.prepare_ready()` to call `creative_studio.photo_engine.edit_photo_adaptive` instead of the base `editor.edit_photo`. Keep the existing `edit_fn` injection for tests. Store the returned report in `asset.creative.edit_report` so the scheduler can audit why an edit was applied.

Example direct call:

```python
from creative_studio.photo_engine import edit_photo_adaptive
out, report = edit_photo_adaptive(image_bytes, vibe="baddie", size="post", return_report=True)
```

## Honest limitations

- Pixel metrics do not identify semantic content such as Diwali, a birthday, a person, or a specific location. Use a vision-capable model for scene understanding and validate its confidence.
- This is adaptive finishing, not Photoshop-level generative retouching. It does not remove objects, perform face-aware skin retouching, relight subjects, or replace backgrounds.
- Reel module is a scaffold only. Actual scene detection, clip ranking, audio rights checks, editing, rendering, and output validation must be implemented and tested separately.
- Preserve the original blob; use the edited output as a derived asset. Do not overwrite source photos.

## Optional direct integration (recommended)

This ZIP also includes a patched `content_buffer.py` that changes the default editor used by `ContentBuffer.prepare_ready()` to the adaptive editor. After making a backup, replace the existing `content_buffer.py` with the ZIP's `content_buffer.py`. The injected `edit_fn` argument remains supported for tests and custom pipelines.
