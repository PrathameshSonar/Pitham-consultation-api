"""Alembic env — autogenerate compares Base.metadata against the live DB."""

from logging.config import fileConfig
from pathlib import Path
from dotenv import load_dotenv

from alembic import context
from sqlalchemy import engine_from_config, pool

# Load backend/.env so DATABASE_URL resolves
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# Make `import models` work
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import engine, Base
import models  # noqa: F401 — registers all models with Base

config = context.config
config.set_main_option("sqlalchemy.url", str(engine.url))

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=str(engine.url),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
