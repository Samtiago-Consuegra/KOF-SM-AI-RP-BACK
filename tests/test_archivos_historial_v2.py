"""/motor/archivos sin cargar data, y el historial mezclando motor + reporte diario."""
from __future__ import annotations

import datetime as dt
import os
import tempfile

_TMPDIR = tempfile.mkdtemp(prefix="kof-historial-v2-")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(_TMPDIR, "test.db").replace("\\", "/")

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import event, inspect  # noqa: E402

import app.main as main_module  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.deps import get_current_user  # noqa: E402
from app.models import DataLoad, MotorArchivo, User  # noqa: E402


def _dia(d: int, h: int = 8) -> dt.datetime:
    return dt.datetime(2026, 11, d, h, 0)


def test_archivos_historial_v2() -> None:
    main_module._startup_worker = lambda: None
    Base.metadata.create_all(bind=engine, tables=[User.__table__, MotorArchivo.__table__, DataLoad.__table__])

    with SessionLocal() as db:
        ana = User(email="ana@kof.com", full_name="Ana Gómez", hashed_password="x")
        db.add_all([ana, User(email="luis@kof.com", full_name="Luis Pérez", hashed_password="x")])
        motor = [
            ("avisos", "HISTORICO_AVISOS_oct.xlsx", "script:carga_inicial", _dia(1)),
            ("avisos", "HISTORICO_AVISOS_nov.xlsx", "ana@kof.com", _dia(5)),
            ("ordenes", "Ordenes_nov.xlsx", "luis@kof.com", _dia(3)),
            ("plan", "PM_SAP.xlsx", "externo@otra.com", _dia(7)),
            ("acr", "01-11-2026_Llenadora.xlsx", "ana@kof.com", _dia(2)),
            ("acr", "04-11-2026_Empacadora.xlsx", "ana@kof.com", _dia(9)),
        ]
        for i, (tipo, nombre, por, subido) in enumerate(motor, start=1):
            db.add(MotorArchivo(tipo=tipo, nombre_archivo=nombre, data=b"x" * (100 * i),
                                subido_en=subido, subido_por=por))
        for d in (4, 6, 8, 10):
            db.add(DataLoad(file_name=f"BD_Reporte_SAP_{d:02d}-11.xlsx", uploaded_at=_dia(d, 9),
                            records_found=50, records_inserted=40, records_duplicated=10,
                            records_discarded=0, date_from=dt.date(2026, 11, d - 1),
                            date_to=dt.date(2026, 11, d), uploaded_by="luis@kof.com", file_size=2048 * d))
        db.commit()
        db.refresh(ana)

    main_module.app.dependency_overrides[get_current_user] = lambda: ana
    cargadas: list[MotorArchivo] = []

    def _al_cargar(target, context):
        cargadas.append(target)

    event.listen(MotorArchivo, "load", _al_cargar)
    try:
        client = TestClient(main_module.app)

        # 1) /motor/archivos no carga la columna data en ninguna instancia.
        r = client.get("/motor/archivos")
        assert r.status_code == 200, r.text
        assert len(cargadas) == 5  # avisos, ordenes y plan vigentes + 2 ACR
        for inst in cargadas:
            assert "data" in inspect(inst).unloaded
        cuerpo = r.json()
        assert cuerpo["vigentes"]["avisos"]["subido_por_nombre"] == "Ana Gómez"
        assert cuerpo["vigentes"]["plan"]["subido_por_nombre"] == "externo@otra.com"
        assert {a["subido_por_nombre"] for a in cuerpo["acr"]} == {"Ana Gómez"}
        assert "data" not in cuerpo["vigentes"]["avisos"]

        # 2) Historial sin tipo: 6 del motor + 4 reportes, 8 por página, orden descendente.
        p1 = client.get("/motor/archivos/historial").json()
        assert p1["por_pagina"] == 8 and p1["total"] == 10 and len(p1["items"]) == 8
        p2 = client.get("/motor/archivos/historial", params={"pagina": 2}).json()
        assert len(p2["items"]) == 2
        todos = p1["items"] + p2["items"]
        fechas = [i["subido_en"] for i in todos]
        assert fechas == sorted(fechas, reverse=True)
        assert {i["tipo"] for i in p1["items"]} >= {"reporte_diario", "acr", "avisos"}
        assert len({(i["tipo"], i["id"]) for i in todos}) == 10

        # 3) conteo_por_tipo incluye reporte_diario.
        assert p1["conteo_por_tipo"]["reporte_diario"] == 4
        assert p1["conteo_por_tipo"]["avisos"] == 2 and p1["conteo_por_tipo"]["acr"] == 2

        # 4) subido_por_nombre: usuario real y carga por script.
        por_nombre = {i["nombre_archivo"]: i for i in todos}
        assert por_nombre["HISTORICO_AVISOS_nov.xlsx"]["subido_por_nombre"] == "Ana Gómez"
        assert por_nombre["HISTORICO_AVISOS_oct.xlsx"]["subido_por_nombre"] == "Carga inicial (script)"
        assert por_nombre["BD_Reporte_SAP_10-11.xlsx"]["subido_por_nombre"] == "Luis Pérez"

        # 5) Solo reporte diario: forma de la fila.
        rd = client.get("/motor/archivos/historial", params={"tipo": "reporte_diario"}).json()
        assert rd["total"] == 4 and all(i["tipo"] == "reporte_diario" for i in rd["items"])
        fila = rd["items"][0]
        assert fila["nombre_archivo"] == "BD_Reporte_SAP_10-11.xlsx"
        assert fila["vigente"] is None and fila["tamano_bytes"] == 20480
        assert fila["registros_insertados"] == 40 and fila["registros_duplicados"] == 10
        assert fila["fecha_desde"] == "2026-11-09" and fila["fecha_hasta"] == "2026-11-10"

        # Un tipo del motor no trae reportes.
        solo_avisos = client.get("/motor/archivos/historial", params={"tipo": "avisos"}).json()
        assert solo_avisos["total"] == 2 and {i["tipo"] for i in solo_avisos["items"]} == {"avisos"}
    finally:
        event.remove(MotorArchivo, "load", _al_cargar)
        main_module.app.dependency_overrides.clear()
