"""FastAPI app: the planner API under /api and the workbench at /.

Run locally:  uvicorn planner.api.main:app --reload
"""

import base64
import hmac
import os
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles

from planner.api import control_routes
from planner.api.routes import router

WEB_DIR = Path(__file__).resolve().parents[1] / "web"

# On Vercel with no database configured, run the seeded demo (planner/demo.py).
if os.environ.get("VERCEL") and "DATABASE_URL" not in os.environ:
    os.environ["DATABASE_URL"] = "sqlite:////tmp/freshroots_demo.db"
    os.environ["PLANNER_DEMO"] = "1"
    from planner.demo import bootstrap

    bootstrap()

app = FastAPI(title="Freshroots demand planner", version="0.1.0")


@app.middleware("http")
async def require_access_password(request: Request, call_next):
    """Site-wide gate for hosted deployments: HTTP Basic auth when PLANNER_ACCESS_PASSWORD is set.

    Any username works; the password must match. Vercel's own protection does not cover the
    production domain on the Hobby plan, so the app guards itself.
    """
    expected = os.environ.get("PLANNER_ACCESS_PASSWORD")
    if expected:
        given = ""
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() == "basic":
            try:
                given = base64.b64decode(token).decode("utf-8").partition(":")[2]
            except (ValueError, UnicodeDecodeError):
                given = ""
        if not hmac.compare_digest(given.encode(), expected.encode()):
            return Response(
                "Freshroots planner: sign in to continue.",
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="Freshroots planner", charset="UTF-8"'},
            )
    return await call_next(request)


app.include_router(router, prefix="/api")
app.include_router(control_routes.router, prefix="/api")
app.add_api_route("/status", control_routes.get_status, methods=["GET"], include_in_schema=False)
app.add_api_route("/health", control_routes.health, methods=["GET"], include_in_schema=False)
app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
