# Low-memory patches (Render 512 MB)

- limits.py (new): one shared gate. Only HEAVY_CONCURRENCY (default 1) photo edits / Reel renders run at once; others wait.
- editor.py: edits go through the gate; images are loaded at max 1920 px long side (was 2600; EDIT_MAX_SIDE),
  JPEGs are shrunk while decoding, and images over 25 MP (EDIT_MAX_PIXELS) are refused.
- creative_studio/reel_engine.py: Reel render goes through the gate; ffmpeg uses -preset veryfast -threads 2.
- publishing/web_studio.py: max 5 photos per upload (was 10), max 25 MP per photo (was 50).
- main.py: bot + web API now run in ONE process (SINGLE_PROCESS=1, default). Set SINGLE_PROCESS=0 for the old two-process mode.
- Dockerfile: MALLOC_ARENA_MAX=2 (less memory bloat from threads).

Bigger instance later? Raise HEAVY_CONCURRENCY (e.g. 3) and EDIT_MAX_SIDE if you want.
