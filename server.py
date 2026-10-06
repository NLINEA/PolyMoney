"""Loopback dashboard for a static synthetic replay; no background bot."""
from pathlib import Path
from functools import lru_cache

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from research.replay import replay

ROOT = Path(__file__).resolve().parent
app = FastAPI(title="PolyMoney Research", docs_url="/docs", redoc_url=None)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@lru_cache(maxsize=1)
def demo_result():
    return replay()


@app.get("/", include_in_schema=False)
def dashboard():
    return FileResponse(ROOT / "static" / "dashboard.html")


@app.get("/api/demo")
def demo():
    return demo_result()


@app.get("/api/health")
def health():
    return {"status": "ok", "mode": "offline_replay", "data_kind": "synthetic", "live_execution": False}


def main():
    import argparse
    import uvicorn
    parser = argparse.ArgumentParser(description="Open the local PolyMoney research dashboard")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
