"""Test de rendimiento del backend (sin Playwright).

Verifica en un solo test:
  - /health responde 200.
  - Dos llamadas idénticas a /eda/summary devuelven el mismo JSON y la segunda no vuelve a
    traer las filas de failure_events (solo consulta la versión ligera count/max).
  - Tras cache.invalidate() la caché se reconstruye desde la BD.

Se usa SQLite en archivo temporal para que el hilo de startup y las peticiones compartan la BD.
"""
import datetime as dt
import os
import tempfile

_TMPDIR = tempfile.mkdtemp(prefix="kof-perf-")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(_TMPDIR, "test.db").replace("\\", "/")

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import event  # noqa: E402

import app.main as main_module  # noqa: E402
from app import cache  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.models import FailureEvent  # noqa: E402


def _startup_noop() -> None:
    """En SQLite no se puede crear el esquema completo (JSONB); el test crea su propia tabla."""
    return None


def _seed() -> None:
    rows = [
        dict(row_key="perf-1", machine="Llenadora", subsystem="Llenado", failure_type="Fuga",
             shift=1, interval="06:00-10:00", event_date=dt.date(2024, 8, 1),
             stop_minutes=10.5, efficiency_points_lost=1.25),
        dict(row_key="perf-2", machine="Empacadora", subsystem="Empaque", failure_type="Atasco",
             shift=2, interval="10:00-14:00", event_date=dt.date(2024, 8, 2),
             stop_minutes=5.25, efficiency_points_lost=0.5),
        dict(row_key="perf-3", machine="Paletizadora", subsystem="Paletizado", failure_type="Sensor",
             shift=3, interval="14:00-18:00", event_date=dt.date(2024, 8, 3),
             stop_minutes=7.75, efficiency_points_lost=0.75),
    ]
    with SessionLocal() as db:
        db.add_all([FailureEvent(**row) for row in rows])
        db.commit()


def test_health_and_eda_cache() -> None:
    main_module._startup_worker = _startup_noop
    Base.metadata.create_all(bind=engine, tables=[FailureEvent.__table__])
    _seed()

    # Solo cuentan las consultas "pesadas" que traen columnas de eventos. La consulta de versión
    # (count(id), max(id)) también toca failure_events, pero es ligera y no incluye .machine.
    heavy_queries: list[str] = []

    def before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        if "failure_events.machine" in statement.lower():
            heavy_queries.append(statement)

    event.listen(engine, "before_cursor_execute", before_cursor_execute)
    try:
        with TestClient(main_module.app) as client:
            assert client.get("/health").status_code == 200

            r1 = client.get("/eda/summary", params={"period": "todo"})
            r2 = client.get("/eda/summary", params={"period": "todo"})
            assert r1.status_code == 200 and r2.status_code == 200
            assert r1.json() == r2.json()
            assert len(heavy_queries) == 1, "la segunda llamada no debe releer failure_events"

            cache.invalidate()
            r3 = client.get("/eda/summary", params={"period": "todo"})
            assert r3.status_code == 200
            assert r3.json() == r1.json()
            assert len(heavy_queries) == 2, "invalidate() debe forzar la reconstrucción de la caché"
    finally:
        event.remove(engine, "before_cursor_execute", before_cursor_execute)
