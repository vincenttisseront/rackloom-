from alembic import context
from sqlalchemy import create_engine, text

config = context.config


def run_migrations_online() -> None:
    url = config.get_main_option("sqlalchemy.url")
    engine = create_engine(url, future=True)
    with engine.connect() as connection:
        # Sérialise les migrations si plusieurs services démarrent en même temps.
        connection.execute(text("SELECT pg_advisory_lock(4242001)"))
        try:
            context.configure(connection=connection, transaction_per_migration=True)
            with context.begin_transaction():
                context.run_migrations()
            connection.commit()
        finally:
            connection.execute(text("SELECT pg_advisory_unlock(4242001)"))
            connection.commit()
    engine.dispose()


run_migrations_online()
