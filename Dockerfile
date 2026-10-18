# Web/bot service (main). NO ffmpeg, NO fonts, NO OpenCV here: all heavy media
# work (editing, Reels, Story rendering) runs in Dockerfile.worker.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Run as a non-root user (limits damage if the bot is ever compromised).
RUN useradd --create-home --uid 10001 vibeos && chown -R vibeos:vibeos /app
USER vibeos

# Render injects PORT at runtime; settings.py reads it.
EXPOSE 10000
CMD ["python", "main.py"]
