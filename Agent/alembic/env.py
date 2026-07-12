from __future__ import annotations

from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool
from sqlalchemy.engine import URL

from utils.config_handler import mysql_conf

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

database_url = URL.create(
    "mysql+mysqlconnector",
    username=mysql_conf.get("user", "root"),
    password=str(mysql_conf.get("password", "")),
    host=mysql_conf.get("host", "localhost"),
    port=int(mysql_conf.get("port", 3306)),
    database=mysql_conf.get("database", "furniture_agent"),
)
config.set_main_option("sqlalchemy.url", str(database_url))

target_metadata = None


def run_migrations_offline() -> None:
    context.configure(
        url=str(database_url),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

