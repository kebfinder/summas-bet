"""Alembic migration environment configuration.

HIGH FIX (F4): Replaces the fragile metadata.create_all() with proper 
schema versioning and rollback capability via Alembic migrations.

Usage:
  alembic init alembic           # Initialize (already done here)
  alembic revision --autogenerate -m "Add index" # Create migration
  alembic upgrade head           # Apply migrations
  alembic downgrade -1           # Rollback one migration
"""

from logging.config import fileConfig
from sqlalchemy import engine_from_config
from sqlalchemy import pool
from alembic import context

# Import your Base and models so Alembic can auto-detect schema changes
from database.database import Base
from database import models  # noqa: F401 - import to register models

# this is the Alembic Config object, which provides the values of the [alembic] section
config = context.config

# Interpret the config file for Python logging.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (for CI/CD, generating SQL scripts)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode (against live database)."""
    configuration = config.get_section(config.config_ini_section)
    configuration["sqlalchemy.url"] = config.get_main_option("sqlalchemy.url")
    
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
