"""Loopback dashboard for a static synthetic replay; no background bot."""
from pathlib import Path
from functools import lru_cache

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from research.replay import replay

ROOT = Path(__file__).resolve().parent
# Local API documentation avoids third-party scripts and browser requests.
app = FastAPI(title="PolyMoney Research", docs_url=None, redoc_url=None)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"], www_redirect=False)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.middleware("http")
async def privacy_headers(request, call_next):
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; script-src 'self'; style-src 'self'; "
        "img-src 'self'; connect-src 'self'; base-uri 'none'; "
        "object-src 'none'; frame-ancestors 'none'; form-action 'none'"
    )
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Cache-Control"] = "no-store"
    return response


@lru_cache(maxsize=1)
def demo_result():
    return replay()


@app.get("/", include_in_schema=False)
def dashboard():
    return FileResponse(ROOT / "static" / "dashboard.html")


@app.get("/docs", include_in_schema=False)
def api_docs():
    return FileResponse(ROOT / "static" / "api.html")


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
