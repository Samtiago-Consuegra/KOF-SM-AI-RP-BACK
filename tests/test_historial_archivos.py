"""GET /motor/archivos/historial: filtros, paginación, conteos, vigente y sin cargar data."""
from __future__ import annotations

import datetime as dt
import os
import re
import tempfile

_TMPDIR = tempfile.mkdtemp(prefix="kof-historial-test-")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(_TMPDIR, "test.db").replace("\\", "/")

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import event  # noqa: E402

import app.main as main_module  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.deps import get_current_user  # noqa: E402
from app.models import MotorArchivo, User  # noqa: E402


def _dia(d: int) -> dt.datetime:
    return dt.datetime(2026, 11, d, 8, 0)


def test_historial_archivos() -> None:
    main_module._startup_worker = lambda: None
    Base.metadata.create_all(bind=engine, tables=[User.__table__, MotorArchivo.__table__])

    with SessionLocal() as db:
        usuario = User(email="planner@kof.com", full_name="Planner", hashed_password="x")
        db.add(usuario)
        cargas = [
            ("avisos", "HISTORICO_AVISOS_oct.xlsx", b"a" * 10, _dia(1)),
            ("avisos", "HISTORICO_AVISOS_nov.xlsx", b"a" * 20, _dia(5)),
            ("ordenes", "Ordenes_nov.xlsx", b"o" * 30, _dia(3)),
            ("acr", "01-11-2026_Llenadora.xlsx", b"r" * 40, _dia(2)),
            ("acr", "01-11-2026_Llenadora.xlsx", b"r" * 50, _dia(6)),
            ("acr", "04-11-2026_Empacadora.xlsx", b"r" * 60, _dia(4)),
        ]
        for tipo, nombre, data, subido in cargas:
            db.add(MotorArchivo(tipo=tipo, nombre_archivo=nombre, data=data,
                                subido_en=subido, subido_por="planner@kof.com"))
        db.commit()
        db.refresh(usuario)

    main_module.app.dependency_overrides[get_current_user] = lambda: usuario
    consultas: list[str] = []

    def _capturar(conn, cursor, statement, *args):
        consultas.append(statement)

    event.listen(engine, "before_cursor_execute", _capturar)
    try:
        client = TestClient(main_module.app)

        # Sin filtros: todo, ordenado por subido_en descendente.
        r = client.get("/motor/archivos/historial")
        assert r.status_code == 200, r.text
        cuerpo = r.json()
        assert cuerpo["total"] == 6 and cuerpo["pagina"] == 1 and cuerpo["por_pagina"] == 20
        fechas = [i["subido_en"] for i in cuerpo["items"]]
        assert fechas == sorted(fechas, reverse=True)
        conteo_esperado = {"avisos": 2, "ordenes": 1, "acr": 3, "plan": 0, "consolidado": 0,
                           "acr_revisado": 0, "equivalencias": 0}
        assert cuerpo["conteo_por_tipo"] == conteo_esperado
        for item in cuerpo["items"]:
            assert "data" not in item
            assert set(item) == {"id", "tipo", "nombre_archivo", "subido_en", "subido_por",
                                 "tamano_bytes", "vigente"}

        # Filtro por tipo: solo el aviso más reciente es vigente; conteo_por_tipo no cambia.
        r = client.get("/motor/archivos/historial", params={"tipo": "avisos"})
        avisos = r.json()
        assert avisos["total"] == 2
        assert [(i["nombre_archivo"], i["vigente"], i["tamano_bytes"]) for i in avisos["items"]] == [
            ("HISTORICO_AVISOS_nov.xlsx", True, 20), ("HISTORICO_AVISOS_oct.xlsx", False, 10)]
        assert avisos["conteo_por_tipo"] == conteo_esperado

        # ACR: de los dos con el mismo nombre solo el último es vigente; el otro nombre también.
        acr = client.get("/motor/archivos/historial", params={"tipo": "acr"}).json()["items"]
        assert [(i["tamano_bytes"], i["vigente"]) for i in acr] == [(50, True), (60, True), (40, False)]

        # Paginación.
        p2 = client.get("/motor/archivos/historial", params={"por_pagina": 2, "pagina": 2}).json()
        assert p2["total"] == 6 and p2["pagina"] == 2 and len(p2["items"]) == 2
        assert [i["id"] for i in p2["items"]] == [i["id"] for i in cuerpo["items"][2:4]]
        assert client.get("/motor/archivos/historial", params={"por_pagina": 101}).status_code == 422

        # Buscar (sin distinguir mayúsculas) y rango de fechas.
        assert client.get("/motor/archivos/historial", params={"buscar": "llenADORA"}).json()["total"] == 2
        rango = client.get("/motor/archivos/historial",
                           params={"desde": "2026-11-03", "hasta": "2026-11-05"}).json()
        assert rango["total"] == 3

        # Ninguna consulta pide la columna data salvo dentro de length(...).
        selects = [q for q in consultas if "motor_archivos" in q and q.lstrip().upper().startswith("SELECT")]
        assert selects
        for q in selects:
            assert not re.search(r"(?<!length\()motor_archivos\.data\b", q), q
    finally:
        event.remove(engine, "before_cursor_execute", _capturar)
        main_module.app.dependency_overrides.clear()
