"""Alembic environment. The database URL comes from DATABASE_URL (see planner/db.py)."""

from alembic import context

from planner.db import database_url, make_engine
from planner.models import Base

target_metadata = Base.metadata


def run_migrations_offline():
    context.configure(url=database_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    connection = context.config.attributes.get("connection")
    if connection is not None:  # tests pass an open connection
        _run(connection)
        return
    with make_engine().connect() as connection:
        _run(connection)


def _run(connection):
    context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
