import os

from dotenv import load_dotenv

load_dotenv()


def _normalize_db_url(url: str) -> str:
    # Supabase y otros proveedores entregan "postgres://"; SQLAlchemy 2 exige "postgresql://".
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return url


class Settings:
    database_url: str = _normalize_db_url(
        os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/kof_smart")
    )
    # Orígenes permitidos separados por coma, p. ej. "http://localhost:5173,https://mi-app.vercel.app"
    cors_origins: list[str] = [
        o.strip()
        for o in os.getenv("CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",")
        if o.strip()
    ]


settings = Settings()
