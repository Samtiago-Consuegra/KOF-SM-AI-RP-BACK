"""Test único de la integración del motor de planes (sin Playwright).

Verifica:
  - paradas.json / decisiones.json generados desde las tablas cumplen el contrato.
  - El Excel diario generado desde failure_events tiene hoja "BD SAP" y columnas exactas.
  - La fusión de la cola une fases y semanas.
  - GET /motor/plan-semanal responde 202 para una semana sin calcular y 200 available:false
    cuando el motor nunca ha corrido.

El subproceso del motor y el hilo worker se mockean: no se ejecuta nada de motor_planes/.
"""
import datetime as dt
import os
import tempfile
from pathlib import Path

_TMPDIR = tempfile.mkdtemp(prefix="kof-motor-test-")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(_TMPDIR, "test.db").replace("\\", "/")

from fastapi.testclient import TestClient  # noqa: E402

import app.main as main_module  # noqa: E402
from app import motor_service  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.deps import get_current_user  # noqa: E402
from app.models import (DecisionTarea, FailureEvent, HistoriaEvento, MotorArchivo, MotorRun,  # noqa: E402
                        MotorSalida, MotorSnapshot, Parada, User)


def _crear_tablas() -> None:
    tablas = [User.__table__, FailureEvent.__table__, MotorRun.__table__, MotorArchivo.__table__,
              MotorSnapshot.__table__, MotorSalida.__table__, Parada.__table__,
              DecisionTarea.__table__, HistoriaEvento.__table__]
    Base.metadata.create_all(bind=engine, tables=tablas)


def _seed() -> User:
    with SessionLocal() as db:
        usuario = User(email="planner@kof.com", full_name="Planner", hashed_password="x")
        db.add(usuario)
        db.add(FailureEvent(
            row_key="motor-test-1", machine="Llenadora", subsystem="Llenado", failure_type="Fuga",
            shift=2, interval="10:00-14:00", event_date=dt.date(2026, 11, 9),
            stop_minutes=35.5, crew="A", efficiency_points_lost=1.0,
        ))
        db.add(Parada(
            id_parada="PAR-2026-11-11-01", titulo="Cambio de formato 600 ml", modalidad="Frío",
            fecha=dt.date(2026, 11, 11), inicio="05:00", fin="08:00",
            maquinas=["Llenadora", "Empacadora"],
            tareas_manuales=[{
                "Maquina": "Llenadora", "Sistema": "Sistema de Conjunto de llenado",
                "Id_componente": "LLE-001", "Componente": "Válvula de llenado",
                "Tipo_tarea": "Preventiva", "Tarea": "Revisar empaques", "Duracion_h": 1.0,
            }],
            creada_por=usuario.email, estado_base="Pendiente", tareas_estado={},
            historial_reprogramaciones=[], cumplida_parcial=False,
        ))
        db.add(DecisionTarea(id_tarea="REC-EMP-031", decision="Enviada",
                             id_parada="PAR-2026-11-11-01", semana="2026-W46",
                             decidida_por=usuario.email))
        db.add(DecisionTarea(id_tarea="REC-EMP-012", decision="Pospuesta", semana="2026-W46",
                             decidida_por=usuario.email))
        db.commit()
        db.refresh(usuario)
        return usuario


def test_motor_integracion(monkeypatch) -> None:
    main_module._startup_worker = lambda: None
    _crear_tablas()
    usuario = _seed()

    # Nunca se debe ejecutar el motor real ni el hilo worker dentro del test.
    def _subproceso_prohibido(*args, **kwargs):
        raise AssertionError("El subproceso del motor no debe ejecutarse en el test")

    monkeypatch.setattr(motor_service, "_run_subprocess", _subproceso_prohibido)
    monkeypatch.setattr(motor_service, "start_worker", lambda: None)
    main_module.app.dependency_overrides[get_current_user] = lambda: usuario

    # 1) paradas.json y decisiones.json desde las tablas.
    with SessionLocal() as db:
        paradas = motor_service._build_paradas_json(db)
        decisiones = motor_service._build_decisiones_json(db)

    assert paradas == {"paradas": [{
        "Id_parada": "PAR-2026-11-11-01", "Titulo": "Cambio de formato 600 ml",
        "Modalidad": "Frío", "Fecha": "2026-11-11", "Inicio": "05:00", "Fin": "08:00",
        "Maquinas": ["Llenadora", "Empacadora"],
        "Tareas_manuales": [{
            "Maquina": "Llenadora", "Sistema": "Sistema de Conjunto de llenado",
            "Id_componente": "LLE-001", "Componente": "Válvula de llenado",
            "Tipo_tarea": "Preventiva", "Tarea": "Revisar empaques", "Duracion_h": 1.0,
        }],
    }]}
    assert decisiones == {"decisiones": [
        {"Id_tarea": "REC-EMP-012", "Decision": "Pospuesta"},
        {"Id_tarea": "REC-EMP-031", "Decision": "Enviada", "Id_parada": "PAR-2026-11-11-01"},
    ]}

    # 2) Excel diario: hoja y columnas exactas del contrato.
    import pandas as pd

    destino = os.path.join(_TMPDIR, "BD_Reporte_SAP_desde_bd.xlsx")
    with SessionLocal() as db:
        motor_service._build_excel_diario(db, destino)

    hojas = pd.ExcelFile(destino).sheet_names
    assert hojas == ["BD SAP"]
    df = pd.read_excel(destino, sheet_name="BD SAP")
    assert list(df.columns) == [
        "Fecha Contable", "Turno", "Intervalo", "Tipo de Paro", "Clave de Paro",
        "Subclave de Paro", "Clave 1 de Paro", "Minutos de paro", "Tripulación",
    ]
    assert df.loc[0, "Tipo de Paro"] == "P.EQ.LINEA"
    assert df.loc[0, "Clave de Paro"] == "Llenadora"
    assert float(df.loc[0, "Minutos de paro"]) == 35.5

    # 3) Fusión de la cola: unión de fases y semanas.
    lote = motor_service._fusionar(None, [20, 23], "2026-W45")
    lote = motor_service._fusionar(lote, [19, 20], "2026-W46")
    assert lote["fases"] == {19, 20, 23}
    assert lote["semanas"] == {"2026-W45", "2026-W46"}

    # 4) Endpoints: sin ningún run ok -> 200 available:false; con un run ok -> 202.
    with TestClient(main_module.app) as client:
        r = client.get("/motor/plan-semanal", params={"semana": "2026-W50"})
        assert r.status_code == 200
        assert r.json()["available"] is False

        with SessionLocal() as db:
            db.add(MotorRun(status="ok", trigger="test", fases=[12], semana="2026-W45",
                            fecha_ref=dt.date(2026, 11, 9)))
            db.commit()

        r = client.get("/motor/plan-semanal", params={"semana": "2026-W50"})
        assert r.status_code == 202
        cuerpo = r.json()
        assert cuerpo["status"] == "calculando"

        with SessionLocal() as db:
            run = db.get(MotorRun, cuerpo["run_id"])
            assert run is not None
            assert run.status == "en_cola"
            assert run.fases == [19, 20, 23]
            assert run.semana == "2026-W50"

        # La decisión se refleja en el JSON que recibe el motor.
        r = client.post("/motor/decisiones", json={
            "Id_tarea": "REC-EMP-099", "Decision": "Pospuesta", "semana": "2026-W50"})
        assert r.status_code == 200
        assert r.json()["decision"]["decision"] == "Pospuesta"

    # 5) Captura semanal consciente de fases: ACR no reescribe un plan que no calculó.
    salida = os.path.join(_TMPDIR, "salida")
    os.makedirs(salida, exist_ok=True)
    for nombre in ("alertas.json", "plan_semanal.json", "plan_semanal_2026-W50.xlsx"):
        with open(os.path.join(salida, nombre), "w", encoding="utf-8") as fh:
            fh.write("{}")

    captura_acr = os.path.join(_TMPDIR, "captura-acr")
    motor_service._capturar_semana(Path(salida), Path(captura_acr), "2026-W50", [21, 22, 23])
    assert os.path.exists(os.path.join(captura_acr, "alertas.json"))
    assert not os.path.exists(os.path.join(captura_acr, "plan_semanal.json"))
    assert not os.path.exists(os.path.join(captura_acr, "plan_semanal_2026-W50.xlsx"))

    captura_semana = os.path.join(_TMPDIR, "captura-semana")
    motor_service._capturar_semana(Path(salida), Path(captura_semana), "2026-W50", [19, 20, 23])
    assert os.path.exists(os.path.join(captura_semana, "alertas.json"))
    assert os.path.exists(os.path.join(captura_semana, "plan_semanal.json"))
    assert os.path.exists(os.path.join(captura_semana, "plan_semanal_2026-W50.xlsx"))

    main_module.app.dependency_overrides.clear()
