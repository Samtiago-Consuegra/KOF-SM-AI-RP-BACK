import logging
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from sqlalchemy import text

from .config import settings
from .db import Base, engine
from . import motor_service
from .prediction_service import mark_interrupted_runs
from .routers import auth, eda, motor, paradas, predicciones, upload

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("kof.startup")

TABLES = ["users", "failure_events", "data_loads", "model_artifacts", "prediction_runs",
          "motor_archivos", "motor_runs", "motor_snapshots", "motor_salidas",
          "historia_eventos", "paradas", "decision_tareas"]


def _startup_worker() -> None:
    """Inicialización de BD fuera del arranque: no bloquea /health ni el resto de la API."""
    try:
        Base.metadata.create_all(bind=engine)
        # Supabase publica las tablas del esquema public por su API REST. Activar RLS sin políticas las
        # cierra a esa API; el backend se conecta como 'postgres', que ignora RLS, así que sigue funcionando.
        with engine.begin() as conn:
            for t in TABLES:
                conn.execute(text(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY"))
        mark_interrupted_runs()
        motor_service.mark_interrupted_runs()
        motor_service.requeue_pending_runs()
        motor_service.start_worker()
        log.info("Inicialización de BD completada")
    except Exception:  # noqa: BLE001 - la API debe seguir arriba aunque falle la BD
        log.exception("Fallo al inicializar la base de datos en segundo plano")


@asynccontextmanager
async def lifespan(app: FastAPI):
    threading.Thread(target=_startup_worker, name="kof-startup", daemon=True).start()
    yield


app = FastAPI(
    title="KOF-SMART Maintenance AI — API (Línea 4 BAQ)",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(GZipMiddleware, minimum_size=1000)


@app.get("/health")
def health():
    return {"status": "ok"}


app.include_router(auth.router, prefix="/auth", tags=["auth"])
app.include_router(upload.router)
app.include_router(eda.router)
app.include_router(predicciones.router)
app.include_router(motor.router)
app.include_router(paradas.router)
