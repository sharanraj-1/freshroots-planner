"""Request dependencies: database session, business config and the deciding user."""

from fastapi import Header, HTTPException

from planner.config import load_config
from planner.db import make_session_factory

USER_HEADER = "X-Planner-User"
_factory = None


def get_session():
    global _factory
    if _factory is None:
        _factory = make_session_factory()
    with _factory() as session:
        yield session


def get_config():
    return load_config()


def current_user(x_planner_user: str | None = Header(None)):
    """Simple header-based identity until the company SSO is wired in."""
    user = (x_planner_user or "").strip()
    if not user:
        raise HTTPException(401, f"missing {USER_HEADER} header")
    if len(user) > 64:
        raise HTTPException(400, f"{USER_HEADER} is longer than 64 characters")
    return user
