"""Test único de GET /cobertura (SQLite temporal, sin Playwright).

Verifica rangos y totales por fuente, nulls/0 en fuente vacía, invalidación de caché al crear una
parada, y mide el tiempo en frío y con caché.
"""
import datetime as dt
import json
import os
import tempfile
import time

_TMPDIR = tempfile.mkdtemp(prefix="kof-cobertura-test-")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(_TMPDIR, "test.db").replace("\\", "/")

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

import app.main as main_module  # noqa: E402
from app import cobertura_service  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.deps import get_current_user  # noqa: E402
from app.models import (DataLoad, FailureEvent, HistoriaEvento, MotorArchivo, MotorRun,  # noqa: E402
                        MotorSalida, OrdenSap, Parada, User)

D = dt.date


def _parada(id_, fecha):
    return Parada(id_parada=id_, titulo="Parada", modalidad="Frío", fecha=fecha, inicio="06:00", fin="10:00",
                  maquinas=["LLENADORA"], tareas_manuales=[])


def test_cobertura():
    Base.metadata.create_all(bind=engine, tables=[t.__table__ for t in (
        User, FailureEvent, DataLoad, MotorArchivo, MotorRun, MotorSalida, HistoriaEvento, OrdenSap, Parada)])
    # prediction_runs usa JSONB puro (no compila en SQLite): tabla mínima con las columnas consultadas.
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE prediction_runs (id INTEGER PRIMARY KEY, status VARCHAR(20), "
                          "data_until DATE, created_at DATETIME, finished_at DATETIME, trigger VARCHAR(30), "
                          "error TEXT, artifact_id INTEGER, results TEXT)"))

    with SessionLocal() as db:
        usuario = User(email="planner@kof.com", full_name="Planner", hashed_password="x")
        db.add(usuario)
        for i, f in enumerate([D(2026, 1, 5), D(2026, 3, 10), D(2026, 11, 15)]):
            db.add(FailureEvent(row_key=f"k{i}", machine="LLENADORA", shift=1, interval="06:00-07:00",
                                event_date=f, stop_minutes=10))
        for tipo, f in [("FALLA", D(2025, 6, 1)), ("FALLA", D(2026, 2, 1)), ("AVISO", D(2025, 9, 9)),
                        ("PREVENTIVO", D(2024, 12, 31)), ("CORRECTIVO", D(2026, 10, 1))]:
            db.add(HistoriaEvento(maquina="LLENADORA", fecha=f, tipo=tipo))
        db.add(_parada("P-1", D(2026, 11, 20)))
        db.add(MotorArchivo(tipo="avisos", nombre_archivo="a.xlsx", data=b"x",
                            subido_en=dt.datetime(2026, 4, 2, 12, 0)))
        db.add(DataLoad(file_name="r.xlsx", uploaded_at=dt.datetime(2026, 2, 1, 9, 0)))
        db.add(DataLoad(file_name="r2.xlsx", uploaded_at=dt.datetime(2026, 11, 10, 9, 0)))
        db.add(MotorSalida(nombre="cumplimiento", semana="", contenido={"datos_hasta": "2026-11-12", "x": 1}))
        db.commit()
        db.refresh(usuario)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO prediction_runs (status, data_until) VALUES "
                          "('ok', '2026-11-01'), ('error', '2026-11-16')"))

    app = main_module.app
    app.dependency_overrides[get_current_user] = lambda: usuario
    client = TestClient(app)
    cobertura_service.invalidate()

    t0 = time.perf_counter()
    r = client.get("/cobertura")
    frio = (time.perf_counter() - t0) * 1000
    t0 = time.perf_counter()
    r2 = client.get("/cobertura")
    cache = (time.perf_counter() - t0) * 1000
    print(f"\nGET /cobertura frío: {frio:.1f} ms · con caché: {cache:.1f} ms")
    assert r.status_code == 200 and r2.json() == r.json()
    c = r.json()
    print(json.dumps(c, ensure_ascii=False, indent=2))

    assert c["reporte_diario"] == {"desde": "2026-01-05", "hasta": "2026-11-15", "total": 3}
    assert c["fallas_historia"] == {"desde": "2025-06-01", "hasta": "2026-02-01", "total": 2}
    assert c["eventos_historia"] == {"desde": "2024-12-31", "hasta": "2026-10-01", "total": 5}
    assert c["avisos"] == {"desde": "2025-09-09", "hasta": "2025-09-09", "total": 1}
    assert c["ordenes_sap"] == {"desde": None, "hasta": None, "total": 0}   # fuente vacía
    assert c["paradas"] == {"desde": "2026-11-20", "hasta": "2026-11-20", "total": 1}
    assert c["archivos"] == {"desde": "2026-02-01", "hasta": "2026-11-10", "total": 3}
    assert c["cumplimiento_sap"] == {"desde": None, "hasta": "2026-11-12", "total": 1}
    assert c["predicciones"] == {"desde": None, "hasta": "2026-11-01", "total": 1}
    assert c["generado_en"].endswith("-05:00")

    # Crear una parada invalida la caché (listener de commit).
    with SessionLocal() as db:
        db.add(_parada("P-2", D(2026, 12, 3)))
        db.commit()
    c = client.get("/cobertura").json()
    assert c["paradas"] == {"desde": "2026-11-20", "hasta": "2026-12-03", "total": 2}
    app.dependency_overrides.clear()
