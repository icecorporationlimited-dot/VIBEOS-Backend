# VIBEOS Automatic Photo Style Tagger

A small, stateless service that analyzes image pixels with OpenCV and chooses one internal style.
It does **not** edit or return photos. The VIBEOS photo worker uses the selected style in `editor.py`
and returns only the final edited image.

## Deploy
Deploy `render.tagger.yaml` as a separate Render Blueprint/service from the VIBEOS-Backend repository.
Render generates `TAGGER_API_KEY`; copy the generated value into the main VIBEOS web service and
the photo worker as the `TAGGER_API_KEY` environment variable. Set `TAGGER_URL` in both to the
tagger service's public HTTPS URL (no trailing slash).

## API
- `GET /` — health check
- `POST /analyze` — multipart field `file`, requires `X-API-Key`
- `POST /analyze/batch` — repeated multipart `files`, maximum 12

The service is stateless and does not save uploaded photos. If it is unavailable, VIBEOS falls back
to its built-in adaptive pixel-based style selection.
