# main.py — simple, reliable launcher for the VibeOS bot.
import signal
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
BOT_FILE = BASE_DIR / "bot.py"


def main() -> int:
    if not BOT_FILE.is_file():
        raise FileNotFoundError(f"Bot file not found: {BOT_FILE}")

    process = subprocess.Popen(
        [sys.executable, str(BOT_FILE)],
        cwd=str(BASE_DIR),
    )

    def _forward(signum, _frame):
        # Host (Docker/Railway/systemd) sends SIGTERM: stop the child cleanly.
        if process.poll() is None:
            process.terminate()

    signal.signal(signal.SIGTERM, _forward)
    signal.signal(signal.SIGINT, _forward)

    try:
        return process.wait()
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == "__main__":
    raise SystemExit(main())
