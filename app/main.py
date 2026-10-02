import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from .config import settings
from .db import Base, engine
from .prediction_service import mark_interrupted_runs
from .routers import eda, predicciones, upload

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = FastAPI(title="KOF-SMART Maintenance AI — API (Línea 4 BAQ)", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

TABLES = ["failure_events", "data_loads", "model_artifacts", "prediction_runs"]


@app.on_event("startup")
def startup():
    Base.metadata.create_all(bind=engine)
    # Supabase publica las tablas del esquema public por su API REST. Activar RLS sin políticas las
    # cierra a esa API; el backend se conecta como 'postgres', que ignora RLS, así que sigue funcionando.
    with engine.begin() as conn:
        for t in TABLES:
            conn.execute(text(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY"))
    mark_interrupted_runs()


@app.get("/health")
def health():
    return {"status": "ok"}


app.include_router(upload.router)
app.include_router(eda.router)
app.include_router(predicciones.router)
