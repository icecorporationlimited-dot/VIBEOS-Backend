FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# FFmpeg: Reels. fonts-noto-color-emoji + libraqm0: emoji and Devanagari on Story cards
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-noto-color-emoji libraqm0 \
    && rm -rf /var/lib/apt/lists/*

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
