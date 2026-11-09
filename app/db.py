from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from .config import settings

# pool_pre_ping: descarta conexiones que el pooler de Supabase cerró por inactividad.
# pool_size pequeño: el pooler gratuito de Supabase limita las conexiones simultáneas.
# pool_recycle: renueva conexiones viejas antes de que el pooler las corte.
# connect_timeout: evita que una conexión caída bloquee una petición indefinidamente.
_is_sqlite = settings.database_url.startswith("sqlite")
engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=5,
    pool_recycle=300,
    connect_args={"check_same_thread": False} if _is_sqlite else {"connect_timeout": 10},
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
