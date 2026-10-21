# VIBEOS Automatic Photo Editor — Integration Notes

## User experience
The consumer editor is fully automatic. Uploading a photo returns one final edited image:
- no style/filter selection
- no manual editing sliders
- no before/after comparison or alternate previews
- output framing is selected automatically from the input aspect ratio

The API `/api/edit` ignores any legacy `style` or `size` form values and enqueues an automatic edit. `/api/options` reports `styles: ["automatic"]` and `sizes: ["automatic"]`.

## Processing path
1. `api.py` accepts and validates the upload.
2. `jobs.py` queues an `edit` job.
3. `worker.py` runs the heavy edit in a child process.
4. `job_handlers.py` calls the optional VIBEOS tagger to rank a style from measured image properties.
5. A confident result is normalized through `media_names.resolve_style`; otherwise `editor.py`'s built-in pixel-based auto selection is used.
6. `editor.py` applies its existing image-aware exposure, white-balance, tone, color, denoise, local subject and finishing operations.
7. The worker returns one final JPEG. Internal candidate styles are never sent to the client.

For autopilot preparation, the selected internal style is chosen once from the original and reused across post/story variants so they remain visually consistent. Those variants are for publishing formats, not alternate user previews.

## Deploy the style selector
A standalone OpenCV service lives in `tagger_service/`. Deploy `render.tagger.yaml` as a separate Render Blueprint/service. It measures exposure, contrast, highlights, shadows, saturation, warmth and noise, then returns a ranked style and a bounded edit plan. It does not store uploaded photos or render edited outputs.

After deploying, set these environment variables on BOTH the main VIBEOS web service and the photo worker:
- `TAGGER_URL`: public HTTPS base URL of the tagger service, without a trailing slash
- `TAGGER_API_KEY`: the same secret configured on the tagger service

The tagger is fail-closed: it requires `TAGGER_API_KEY`. If it is unavailable, VIBEOS logs the failure and falls back to its built-in automatic style choice; editing still works.

## Dependencies and test
`requirements-worker.txt` includes `httpx` for the tagger client. The tagger service installs `opencv-python-headless` in its own container; OpenCV is not added to the main worker.

Run from the backend directory:
```bash
python -m unittest tests.test_auto_editor
python -m compileall -q .
```

## Important implementation notes
- The tagger's style decision is used to select the internal look; the editor's own image measurements still adapt the actual rendering.
- The tagger's returned exposure, contrast, saturation, warmth and grain hints are blended conservatively into `editor.py`'s own adaptive parameters. Highlight/shadow recovery remains measurement-driven inside the renderer, avoiding a second full correction pass.
- Numeric style ranges are initial tuning values. Validate on a diverse, permissioned image set and tune based on output quality, skin-tone stability, clipping, artifacts, latency and memory.
