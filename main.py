# main.py — launcher: Telegram bot + web API in one Render service.
#
# Public $PORT  -> FastAPI (api.py): /api/* for the web app, and a proxy that
#                  forwards every other path to the bot's built-in server.
# Bot's server  -> moved to INTERNAL_PORT (bot.py reads PORT from the env).
#
# If FastAPI/uvicorn are missing, falls back to the old behaviour (bot on $PORT).
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
BOT_FILE = BASE_DIR / "bot.py"


def _stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def main() -> int:
    if not BOT_FILE.is_file():
        raise FileNotFoundError(f"Bot file not found: {BOT_FILE}")

    public_port = int(os.environ.get("PORT", "8000"))
    internal_port = int(os.environ.get("INTERNAL_PORT", str(public_port + 1)))

    try:
        import uvicorn
        import api  # noqa: F401  (import check; also validates deps)
        web_mode = True
    except Exception as exc:
        print(f"[main] Web API disabled ({exc!r}); running bot only.", flush=True)
        web_mode = False

    env = dict(os.environ)
    if web_mode:
        env["PORT"] = str(internal_port)          # bot's server goes internal
        os.environ["INTERNAL_PORT"] = str(internal_port)

    process = subprocess.Popen([sys.executable, str(BOT_FILE)], cwd=str(BASE_DIR), env=env)

    def _forward(signum, _frame):
        _stop(process)

    signal.signal(signal.SIGTERM, _forward)
    signal.signal(signal.SIGINT, _forward)

    if not web_mode:
        try:
            return process.wait()
        finally:
            _stop(process)

    # If the bot dies, stop the web side too so Render restarts the service.
    def _watch():
        process.wait()
        os.kill(os.getpid(), signal.SIGTERM)

    threading.Thread(target=_watch, daemon=True).start()
    try:
        uvicorn.run("api:app", host="0.0.0.0", port=public_port, log_level="info")
    finally:
        _stop(process)
    return process.returncode or 0


if __name__ == "__main__":
    raise SystemExit(main())
