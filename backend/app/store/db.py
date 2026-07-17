from sqlalchemy import Engine, create_engine, text

from app.core.config import Settings


def create_db_engine(settings: Settings) -> Engine:
    return create_engine(settings.database_url.get_secret_value(), pool_pre_ping=True)


def check_db(engine: Engine) -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
