"""Test único de órdenes SAP por semana y semanas pasadas sin motor (SQLite temporal, sin Playwright).

Verifica:
  - Carga masiva de ordenes_sap desde un ot_limpias.csv de ejemplo (solo En_alcance).
  - GET /motor/ordenes-sap: filtro por rango y máquina, orden, resumen por tipo y estado, 422 > 31 días.
  - GET /motor/plan-semanal de una semana pasada no calculada: 200 pasada=true sin encolar corridas.
"""
import datetime as dt
import os
import tempfile
from pathlib import Path

_TMPDIR = tempfile.mkdtemp(prefix="kof-ordenes-test-")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(_TMPDIR, "test.db").replace("\\", "/")

from fastapi.testclient import TestClient  # noqa: E402

import app.main as main_module  # noqa: E402
from app import motor_service  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.deps import get_current_user  # noqa: E402
from app.models import MotorRun, MotorSalida, OrdenSap, User  # noqa: E402

CSV = """Orden,Maquina,Fecha_programada,Tipo_orden,Plan,Texto,Estado,Estado_SAP,Equipo,Costo_plan,Costo_real,En_alcance
4001,LLENADORA,2026-11-03,Preventiva,P1,Lubricar,Ejecutada,CTEC,10001,100.5,90,True
4002,ETIQUETADORA,2026-11-03,Correctiva,,Cambio banda,No ejecutada,LIB,10002,50,,True
4003,LLENADORA,2026-11-05,Predictiva,P2,Vibraciones,Abierta,LIB,10001,,,True
4004,LLENADORA,2026-11-06,Mejora,,Proyecto,Ejecutada,CTEC,10001,,,True
4005,LLENADORA,2026-11-04,Preventiva,P1,Fuera de alcance,Ejecutada,CTEC,10001,,,False
4006,LLENADORA,2026-11-20,Emergencia,,Fuga,Abierta,LIB,10001,,,True
"""


def test_ordenes_sap_y_semana_pasada(tmp_path: Path, monkeypatch):
    Base.metadata.create_all(bind=engine, tables=[User.__table__, MotorRun.__table__,
                                                  MotorSalida.__table__, OrdenSap.__table__])
    with SessionLocal() as db:
        usuario = User(email="planner@kof.com", full_name="Planner", hashed_password="x")
        db.add(usuario)
        db.add(MotorRun(status="ok", trigger="manual", fases=[13], semana="2026-W47"))
        db.commit()
        db.refresh(usuario)

    # 1. Carga masiva desde ot_limpias.csv: solo En_alcance verdadero.
    (tmp_path / "ot_limpias.csv").write_text(CSV, encoding="utf-8")
    with SessionLocal() as db:
        motor_service._reemplazar_ordenes_sap(db, tmp_path)
        db.commit()
        ordenes = {o.orden: o for o in db.query(OrdenSap).all()}
    assert set(ordenes) == {"4001", "4002", "4003", "4004", "4006"}
    assert ordenes["4004"].tipo_orden == "otro"
    assert ordenes["4001"].fecha_programada == dt.date(2026, 11, 3)
    assert float(ordenes["4001"].costo_plan) == 100.5 and ordenes["4001"].estado_sap == "CTEC"
    assert ordenes["4001"].equipo == "10001" and ordenes["4002"].costo_real is None
    motor_service.invalidate_cache()

    app = main_module.app
    app.dependency_overrides[get_current_user] = lambda: usuario
    encoladas = []
    monkeypatch.setattr(motor_service, "encolar", lambda *a, **k: encoladas.append((a, k)))
    monkeypatch.setattr(motor_service, "hoy_bogota", lambda: dt.date(2026, 11, 18))
    client = TestClient(app)
    try:
        # 2. Rango de la semana 2026-W45 (2-8 nov).
        r = client.get("/motor/ordenes-sap", params={"desde": "2026-11-02", "hasta": "2026-11-08"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert [o["orden"] for o in body["ordenes"]] == ["4002", "4001", "4003", "4004"]
        assert body["resumen"] == {
            "total": 4,
            "por_tipo": {"Correctiva": 1, "Preventiva": 1, "Predictiva": 1, "otro": 1},
            "por_estado": {"No ejecutada": 1, "Ejecutada": 2, "Abierta": 1},
        }
        # Segunda llamada: misma respuesta (caché).
        assert client.get("/motor/ordenes-sap",
                          params={"desde": "2026-11-02", "hasta": "2026-11-08"}).json() == body

        # Filtro por máquina.
        r = client.get("/motor/ordenes-sap",
                       params={"desde": "2026-11-02", "hasta": "2026-11-08", "maquina": "LLENADORA"})
        assert [o["orden"] for o in r.json()["ordenes"]] == ["4001", "4003", "4004"]
        assert r.json()["resumen"]["total"] == 3

        # 31 días se permite; más de 31 → 422.
        assert client.get("/motor/ordenes-sap",
                          params={"desde": "2026-11-01", "hasta": "2026-12-01"}).status_code == 200
        assert client.get("/motor/ordenes-sap",
                          params={"desde": "2026-11-01", "hasta": "2026-12-02"}).status_code == 422

        # 3. Semana pasada no calculada: 200 pasada=true y sin encolar.
        r = client.get("/motor/plan-semanal", params={"semana": "2026-W45"})
        assert r.status_code == 200, r.text
        assert r.json() == {"pasada": True, "semana": "2026-W45",
                            "desde": "2026-11-02", "hasta": "2026-11-08"}
        r = client.get("/motor/alertas", params={"semana": "2026-W45"})
        assert r.status_code == 200 and r.json()["pasada"] is True
        assert encoladas == []

        # Semana pasada ya calculada: se devuelve como hoy.
        with SessionLocal() as db:
            db.add(MotorSalida(nombre="alertas", semana="2026-W44", run_id=1, contenido={"alertas": []}))
            db.commit()
        assert client.get("/motor/alertas", params={"semana": "2026-W44"}).json() == {"alertas": []}
        assert encoladas == []
    finally:
        app.dependency_overrides.clear()
