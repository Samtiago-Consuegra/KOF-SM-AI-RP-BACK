"""Cumplimiento filtra paradas por rango en SQL sin cambiar los resultados."""
from datetime import date, datetime

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import cumplimiento_service as cs
from app import motor_service
from app.db import Base
from app.models import Parada

MAQ = ["Llenadora", "Empacadora", "Paletizadora", "Envolvedora"]
DESDE, HASTA = date(2026, 3, 1), date(2026, 3, 31)
AHORA = datetime(2026, 4, 10, 12, 0)


def _parada(pid, fecha, estado="Pendiente", completadas=(), **kw):
    return Parada(id_parada=pid, titulo=pid, modalidad="Frío", fecha=fecha, inicio="06:00", fin="10:00",
                  maquinas=["Toda la línea"], tareas_manuales=[], estado_base=estado,
                  tareas_estado={f"{pid}-{k}": {"completada": True} for k in completadas}, **kw)


def test_cumplimiento_rango(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}")
    Base.metadata.create_all(engine, tables=[Parada.__table__])
    db = sessionmaker(bind=engine)()

    marzo = datetime(2026, 3, 25, 10, 0)
    db.add_all([
        _parada("CUMPLIDA", date(2026, 3, 5), "Cumplida", completadas=(0, 1, 2, 3)),
        _parada("VENCIDA", date(2026, 3, 12), completadas=(0,)),
        _parada("RESTANTES", date(2026, 3, 19), "Cumplida", completadas=(0, 1), reprogramada_a="NUEVA"),
        _parada("NUEVA", date(2026, 4, 2), parada_original_id="RESTANTES",
                historial_reprogramaciones=[{"motivo": "tareas-restantes", "reprogramada_en": "2026-03-19"}]),
        # Reprogramada desde marzo a otro mes: sus pospuestas cuentan en marzo.
        _parada("DESDE_OTRO_MES", date(2026, 4, 20), actualizada_en=marzo,
                historial_reprogramaciones=[{"motivo": "vencida", "reprogramada_en": "2026-03-25",
                                             "tareas_pendientes_por_maquina": {"Llenadora": 1, "Empacadora": 1}}]),
        # Sin relación con marzo.
        _parada("ENERO", date(2026, 1, 10), "Cumplida", completadas=(0,)),
        _parada("FEBRERO_REPROG", date(2026, 2, 15), actualizada_en=datetime(2026, 1, 30, 9, 0),
                historial_reprogramaciones=[{"motivo": "vencida", "reprogramada_en": "2026-01-30"}]),
        _parada("JUNIO", date(2026, 6, 1)),
    ])
    db.commit()

    # Plan semanal falso: 4 tareas por parada, una por máquina. Cuenta las lecturas.
    paradas = db.scalars(select(Parada)).all()
    planes: dict[str, list] = {}
    for p in paradas:
        tareas = [{"Id_tarea": f"{p.id_parada}-{k}", "Maquina": m, "Estado": "Enviada a la parada"}
                  for k, m in enumerate(MAQ)]
        planes.setdefault(motor_service.semana_iso(p.fecha), []).append({"Id_parada": p.id_parada, "Tareas": tareas})
    lecturas: list[str] = []

    def get_salida(_db, nombre, semana=""):
        lecturas.append(semana)
        return {"versiones": {"fusion": {"paradas": planes.get(semana, [])}}}

    monkeypatch.setattr(motor_service, "get_salida", get_salida)

    def calcular():
        return (cs.resumen(db, DESDE, HASTA, ahora=AHORA),
                cs.resumen(db, DESDE, HASTA, "Llenadora", ahora=AHORA),
                cs.por_maquina_y_semana(db, DESDE, HASTA, ahora=AHORA),
                cs.por_maquina_y_semana(db, DESDE, HASTA, agrupar="semana", ahora=AHORA))

    despues = calcular()
    cargadas = {p.id_parada for p in cs._paradas_del_rango(db, DESDE, HASTA)}

    # "Antes": la lógica original recorría todas las paradas de la base.
    with monkeypatch.context() as m:
        m.setattr(cs, "_paradas_del_rango", lambda d, *_: d.scalars(select(Parada)).all())
        antes = calcular()

    assert despues == antes
    total = antes[0]
    assert total["ejecutadas"] and total["vencidas"] and total["pospuestas"]
    assert {"CUMPLIDA", "VENCIDA", "RESTANTES", "DESDE_OTRO_MES"} <= cargadas
    assert not cargadas & {"ENERO", "FEBRERO_REPROG", "JUNIO"}

    # Plan semanal: una lectura por semana en cada petición.
    lecturas.clear()
    cs.resumen(db, DESDE, HASTA, ahora=AHORA)
    assert len(lecturas) == len(set(lecturas))
