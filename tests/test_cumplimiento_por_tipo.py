"""por_tipo en GET /cumplimiento/grafica: clasificación y coincidencia con programadas."""
from datetime import date, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import cumplimiento_service as cs
from app import motor_service
from app.models import Parada
from app.routers.cumplimiento import grafica


def test_por_tipo_clasifica_y_cuadra_con_programadas(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'cumplimiento.db'}")
    Parada.__table__.create(engine)
    db = sessionmaker(bind=engine)()

    fecha = date(2026, 11, 10)
    db.add(Parada(
        id_parada="P-1", titulo="Parada de prueba", modalidad="Programada", creada_por="test", fecha=fecha, inicio="06:00", fin="10:00", estado_base="Pendiente",
        maquinas=["Llenadora"], historial_reprogramaciones=[], tareas_estado={},
        tareas_manuales=[
            {"Id_tarea": "MAN-1", "Maquina": "Llenadora", "Tarea": "Cambio de sello", "Tipo_tarea": "Correctiva"},
            {"Id_tarea": "MAN-2", "Maquina": "Llenadora", "Tarea": "Fuga", "Tipo_tarea": "EMERGENCIA"},
        ],
    ))
    db.commit()

    plan = {"versiones": {"fusion": {"paradas": [{"Id_parada": "P-1", "Tareas": [
        {"Id_tarea": "T-1", "Maquina": "Llenadora", "Tipo_tarea": "Sustitución",
         "Clase_orden": "ZFI1 · Preventiva", "Estado": "Enviada a la parada"},
        {"Id_tarea": "T-2", "Maquina": "Llenadora", "Tipo_tarea": "Inspección",
         "Estado": "Enviada a la parada"},
        {"Id_tarea": "T-3", "Maquina": "Llenadora", "Tipo_tarea": "Limpieza",
         "Clase_orden": "ZFI2 · PREDICTIVA", "Estado": "Enviada a la parada"},
    ]}]}}}
    monkeypatch.setattr(motor_service, "get_salida", lambda *_: plan)
    monkeypatch.setattr(cs, "ahora_bogota", lambda: datetime(2026, 11, 1))

    respuesta = grafica(desde=fecha, hasta=fecha, maquina=None, agrupar="maquina", db=db, _=None)

    assert respuesta["por_tipo"] == {"Preventiva": 2, "Correctiva": 2, "Predictiva": 1}
    programadas = sum(f["programadas"] for f in respuesta["filas"])
    assert sum(respuesta["por_tipo"].values()) == programadas == 5
    assert all("_tipos" not in f for f in respuesta["filas"])  # formato de filas intacto
