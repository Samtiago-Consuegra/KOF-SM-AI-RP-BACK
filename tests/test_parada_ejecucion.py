"""Campo Ejecucion de las paradas (propio | contratista) sobre SQLite temporal."""
import os
import tempfile

_TMP = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(_TMP, "paradas.db").replace("\\", "/")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import motor_service  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.deps import get_current_user  # noqa: E402
from app.main import app  # noqa: E402
from app.models import User  # noqa: E402

TABLAS = ("users", "paradas", "motor_runs", "motor_salidas", "decision_tareas")


class _Run:
    def __init__(self, id_):
        self.id = id_


@pytest.fixture()
def cliente(monkeypatch):
    if not engine.url.drivername.startswith("sqlite"):
        pytest.skip("La prueba necesita la BD SQLite temporal")
    tablas = [Base.metadata.tables[t] for t in TABLAS]
    Base.metadata.drop_all(bind=engine, tables=tablas)
    Base.metadata.create_all(bind=engine, tables=tablas)

    cola = {"semanas": [], "despertares": 0}

    def encolar(db, trigger, fases, semana=None, fecha_ref=None, iniciar=True):
        cola["semanas"].append(semana)
        if iniciar:
            cola["despertares"] += 1
        return _Run(len(cola["semanas"]))

    def start_worker():
        cola["despertares"] += 1

    monkeypatch.setattr(motor_service, "encolar", encolar)
    monkeypatch.setattr(motor_service, "start_worker", start_worker)
    app.dependency_overrides[get_current_user] = lambda: User(email="test@kof")
    # Sin "with": no arranca el hilo de inicio (Postgres, worker del motor).
    yield TestClient(app), cola
    app.dependency_overrides.clear()


def _cuerpo(**extra):
    base = {"Titulo": "Cambio de formato", "Modalidad": "Frío", "Fecha": "2026-11-10",
            "Inicio": "08:00", "Fin": "10:00", "Maquinas": ["Llenadora"]}
    return {**base, **extra}


def test_ejecucion_en_paradas(cliente):
    c, cola = cliente

    # Crear sin Ejecucion -> "propio".
    r = c.post("/paradas", json=_cuerpo())
    assert r.status_code == 200, r.text
    propia = r.json()
    assert propia["ejecucion"] == "propio"

    # Crear y editar con "contratista".
    r = c.post("/paradas", json=_cuerpo(Ejecucion="contratista"))
    assert r.status_code == 200, r.text
    contratada = r.json()
    assert contratada["ejecucion"] == "contratista"
    r = c.put(f"/paradas/{propia['id_parada']}", json={"Ejecucion": "contratista"})
    assert r.status_code == 200 and r.json()["ejecucion"] == "contratista"
    # Editar sin Ejecucion conserva el valor actual.
    r = c.put(f"/paradas/{propia['id_parada']}", json={"Titulo": "Renombrada"})
    assert r.json()["ejecucion"] == "contratista" and r.json()["titulo"] == "Renombrada"

    # GET lista y detalle incluyen el campo.
    lista = c.get("/paradas", params={"desde": "2026-11-09", "hasta": "2026-11-15"}).json()
    assert {p["id_parada"]: p["ejecucion"] for p in lista} == {
        propia["id_parada"]: "contratista", contratada["id_parada"]: "contratista"}
    assert c.get(f"/paradas/{contratada['id_parada']}").json()["ejecucion"] == "contratista"

    # Valores inválidos -> 422 con mensaje claro.
    r = c.post("/paradas", json=_cuerpo(Ejecucion="tercero"))
    assert r.status_code == 422 and "propio" in r.text and "contratista" in r.text
    r = c.put(f"/paradas/{propia['id_parada']}", json={"Ejecucion": "otro"})
    assert r.status_code == 422 and "Ejecucion debe ser" in r.text
    r = c.put(f"/paradas/{propia['id_parada']}", json={"Fecha": "10/11/2026"})
    assert r.status_code == 422 and "AAAA-MM-DD" in r.text

    # Cambiar Fecha a otra semana encola la anterior y la nueva, despertando la cola una vez.
    cola["semanas"].clear()
    cola["despertares"] = 0
    r = c.put(f"/paradas/{contratada['id_parada']}", json={"Fecha": "2026-11-18"})
    assert r.status_code == 200 and r.json()["fecha"] == "2026-11-18"
    assert sorted(cola["semanas"]) == ["2026-W46", "2026-W47"]
    assert cola["despertares"] == 1

    # Reprogramar y cumplir-y-reprogramar conservan Ejecucion.
    r = c.post(f"/paradas/{contratada['id_parada']}/reprogramar",
               json={"Fecha": "2026-11-19", "Inicio": "09:00", "Fin": "11:00"})
    assert r.status_code == 200 and r.json()["ejecucion"] == "contratista"
    r = c.post(f"/paradas/{contratada['id_parada']}/cumplir-y-reprogramar",
               json={"Fecha": "2026-11-25", "Inicio": "09:00", "Fin": "11:00"})
    assert r.status_code == 200, r.text
    assert r.json()["parada_nueva"]["ejecucion"] == "contratista"

    # paradas.json del motor no lleva el campo.
    with SessionLocal() as db:
        paradas_json = motor_service._build_paradas_json(db)
    assert paradas_json["paradas"]
    for p in paradas_json["paradas"]:
        assert "Ejecucion" not in p and "ejecucion" not in p
