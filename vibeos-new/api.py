
from fastapi import FastAPI

app = FastAPI(title="VibeOS API")

@app.get("/")
def home():
    return {
        "app": "VibeOS",
        "status": "online"
    }

@app.get("/health")
def health():
    return {"ok": True}
