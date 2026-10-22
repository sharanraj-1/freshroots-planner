"""FastAPI app: the planner API under /api and the workbench at /.

Run locally:  uvicorn planner.api.main:app --reload
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from planner.api import control_routes
from planner.api.routes import router

WEB_DIR = Path(__file__).resolve().parents[1] / "web"

app = FastAPI(title="Freshroots demand planner", version="0.1.0")
app.include_router(router, prefix="/api")
app.include_router(control_routes.router, prefix="/api")
app.add_api_route("/status", control_routes.get_status, methods=["GET"], include_in_schema=False)
app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
