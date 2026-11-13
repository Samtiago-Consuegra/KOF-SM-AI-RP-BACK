"""Endpoints del motor de planes y de la Historia clínica."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Literal

from fastapi import (APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile)
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import motor_service
from ..db import get_db
from ..deps import get_current_user
from ..models import DataLoad, DecisionTarea, HistoriaEvento, MotorArchivo, User

router = APIRouter(prefix="/motor", tags=["motor"])

MAX_ARCHIVO = 50 * 1024 * 1024
EXTENSIONES = (".xlsx", ".xls")
MENSAJE_SIN_DATOS = "Aún no hay resultados del motor. Sube los archivos de SAP en Mantenimiento."
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class DecisionIn(BaseModel):
    Id_tarea: str
    Decision: Literal["Enviada", "Pospuesta"]
    Id_parada: str | None = None
    semana: str | None = None


def _archivo_dict(archivo: MotorArchivo) -> dict:
    return {
        "id": archivo.id,
        "tipo": archivo.tipo,
        "nombre_archivo": archivo.nombre_archivo,
        "subido_en": archivo.subido_en.isoformat() if archivo.subido_en else None,
        "subido_por": archivo.subido_por,
    }


def _sin_datos() -> dict:
    return {"available": False, "message": MENSAJE_SIN_DATOS}


def _version_filtrada(contenido: dict, version: str) -> dict:
    if version == "todas":
        return contenido
    versiones = contenido.get("versiones") or {}
    return {**contenido, "versiones": {version: versiones.get(version)}}


@router.post("/archivos")
def subir_archivo(
    tipo: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    tipo = (tipo or "").strip().lower()
    if tipo not in motor_service.TIPOS_ARCHIVO:
        raise HTTPException(status_code=400, detail=f"Tipo inválido. Use: {', '.join(sorted(motor_service.TIPOS_ARCHIVO))}")

    nombre = Path(file.filename or "").name
    sufijo = Path(nombre).suffix.lower()
    if sufijo not in EXTENSIONES:
        raise HTTPException(status_code=400, detail="El archivo debe tener extensión .xlsx o .xls")
    if not nombre:
        raise HTTPException(status_code=400, detail="El archivo necesita un nombre")
    if tipo == "acr" and not motor_service.ACR_RE.match(nombre):
        raise HTTPException(status_code=400, detail="El nombre del ACR debe empezar por DD-MM-AAAA_ y terminar en .xlsx")

    contenido = file.file.read(MAX_ARCHIVO + 1)
    if len(contenido) > MAX_ARCHIVO:
        raise HTTPException(status_code=400, detail="Archivo demasiado grande (máx. 50 MB)")

    # Un ACR con el mismo nombre reemplaza al anterior.
    if tipo == "acr":
        anterior = db.scalars(select(MotorArchivo).where(
            MotorArchivo.tipo == "acr", MotorArchivo.nombre_archivo == nombre)).first()
        if anterior is not None:
            db.delete(anterior)
            db.flush()

    archivo = MotorArchivo(tipo=tipo, nombre_archivo=nombre, data=contenido, subido_por=user.email)
    db.add(archivo)
    db.commit()
    db.refresh(archivo)

    fases = motor_service.FASES_COMPLETA if tipo in motor_service.TIPOS_COMPLETA else motor_service.FASES_ACR
    run = motor_service.encolar(db, trigger="upload", fases=fases)
    return {"archivo": _archivo_dict(archivo), "run_id": run.id}


SCRIPT_NOMBRE = "Carga inicial (script)"


def _nombres(db: Session, correos) -> dict[str, str]:
    """email -> full_name en una sola consulta."""
    correos = {c for c in correos if c and not c.startswith("script:")}
    if not correos:
        return {}
    return {email: nombre for email, nombre in db.execute(
        select(User.email, User.full_name).where(User.email.in_(correos))) if nombre}


def _nombre_de(correo: str | None, nombres: dict[str, str]) -> str | None:
    if not correo:
        return None
    if correo.startswith("script:"):
        return SCRIPT_NOMBRE
    return nombres.get(correo, correo)


@router.get("/archivos")
def listar_archivos(db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    # data es diferida en el modelo: aquí solo viajan nombres y fechas.
    recientes: dict[str, MotorArchivo | None] = {}
    for tipo in sorted(motor_service.TIPOS_ARCHIVO - {"acr"}):
        recientes[tipo] = db.scalars(select(MotorArchivo).where(MotorArchivo.tipo == tipo)
                                     .order_by(MotorArchivo.subido_en.desc(), MotorArchivo.id.desc())
                                     .limit(1)).first()
    acr = db.scalars(select(MotorArchivo).where(MotorArchivo.tipo == "acr")
                     .order_by(MotorArchivo.subido_en.desc(), MotorArchivo.id.desc())).all()

    nombres = _nombres(db, [a.subido_por for a in [*recientes.values(), *acr] if a])

    def fila(a: MotorArchivo) -> dict:
        return {**_archivo_dict(a), "subido_por_nombre": _nombre_de(a.subido_por, nombres)}

    return {"vigentes": {t: fila(a) if a else None for t, a in recientes.items()},
            "acr": [fila(a) for a in acr]}


TipoArchivo = Literal["avisos", "ordenes", "plan", "consolidado", "acr", "acr_revisado", "equivalencias",
                      "reporte_diario"]
REPORTE_DIARIO = "reporte_diario"


def _iso(valor) -> str | None:
    return valor.isoformat() if valor else None


@router.get("/archivos/historial")
def historial_archivos(
    tipo: TipoArchivo | None = Query(None),
    desde: date | None = Query(None, description="AAAA-MM-DD sobre la fecha de carga"),
    hasta: date | None = Query(None, description="AAAA-MM-DD sobre la fecha de carga (incluido)"),
    buscar: str | None = Query(None),
    pagina: int = Query(1, ge=1),
    por_pagina: int = Query(8, ge=1, le=100),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    texto = buscar.strip().lower() if buscar and buscar.strip() else None
    ini = datetime.combine(desde, time.min) if desde else None
    fin = datetime.combine(hasta + timedelta(days=1), time.min) if hasta else None

    def filtros_de(col_fecha, col_nombre) -> list:
        f = []
        if ini is not None:
            f.append(col_fecha >= ini)
        if fin is not None:
            f.append(col_fecha < fin)
        if texto:
            f.append(func.lower(col_nombre).contains(texto))
        return f

    # Cada fuente aporta sus primeras pagina*por_pagina filas; se mezclan y se corta la página.
    tope = pagina * por_pagina
    usar_motor = tipo != REPORTE_DIARIO
    usar_reporte = tipo in (None, REPORTE_DIARIO)

    filas: list[tuple] = []  # (fecha, id, item)
    total = 0

    if usar_motor:
        fm = filtros_de(MotorArchivo.subido_en, MotorArchivo.nombre_archivo)
        if tipo:
            fm.append(MotorArchivo.tipo == tipo)
        total += int(db.scalar(select(func.count(MotorArchivo.id)).where(*fm)) or 0)
        vigentes = motor_service.ids_archivos_vigentes(db)
        for f in db.execute(
            select(MotorArchivo.id, MotorArchivo.tipo, MotorArchivo.nombre_archivo,
                   MotorArchivo.subido_en, MotorArchivo.subido_por,
                   func.length(MotorArchivo.data).label("tamano_bytes"))
            .where(*fm).order_by(MotorArchivo.subido_en.desc(), MotorArchivo.id.desc()).limit(tope)
        ):
            filas.append((f.subido_en, f.id, {
                "id": f.id, "tipo": f.tipo, "nombre_archivo": f.nombre_archivo,
                "subido_en": _iso(f.subido_en), "subido_por": f.subido_por,
                "tamano_bytes": int(f.tamano_bytes or 0), "vigente": f.id in vigentes,
            }))

    if usar_reporte:
        fr = filtros_de(DataLoad.uploaded_at, DataLoad.file_name)
        total += int(db.scalar(select(func.count(DataLoad.id)).where(*fr)) or 0)
        for d in db.execute(
            select(DataLoad.id, DataLoad.file_name, DataLoad.uploaded_at, DataLoad.uploaded_by,
                   DataLoad.file_size, DataLoad.records_inserted, DataLoad.records_duplicated,
                   DataLoad.date_from, DataLoad.date_to)
            .where(*fr).order_by(DataLoad.uploaded_at.desc(), DataLoad.id.desc()).limit(tope)
        ):
            filas.append((d.uploaded_at, d.id, {
                "id": d.id, "tipo": REPORTE_DIARIO, "nombre_archivo": d.file_name,
                "subido_en": _iso(d.uploaded_at), "subido_por": d.uploaded_by,
                "tamano_bytes": d.file_size, "vigente": None,
                "registros_insertados": d.records_inserted,
                "registros_duplicados": d.records_duplicated,
                "fecha_desde": _iso(d.date_from), "fecha_hasta": _iso(d.date_to),
            }))

    filas.sort(key=lambda t: (_orden_fecha(t[0]), t[1]), reverse=True)
    items = [item for _, _, item in filas[(pagina - 1) * por_pagina: tope]]

    nombres = _nombres(db, [i["subido_por"] for i in items])
    for item in items:
        item["subido_por_nombre"] = _nombre_de(item["subido_por"], nombres)

    conteo = {t: 0 for t in sorted(motor_service.TIPOS_ARCHIVO)}
    for t, n in db.execute(select(MotorArchivo.tipo, func.count(MotorArchivo.id)).group_by(MotorArchivo.tipo)):
        conteo[t] = int(n)
    conteo[REPORTE_DIARIO] = int(db.scalar(select(func.count(DataLoad.id))) or 0)

    return {
        "items": items,
        "total": total,
        "pagina": pagina,
        "por_pagina": por_pagina,
        "conteo_por_tipo": conteo,
    }


def _orden_fecha(valor) -> float:
    """Clave comparable aunque una fuente traiga fechas con zona y otra sin ella."""
    if valor is None:
        return float("-inf")
    if valor.tzinfo is None:
        valor = valor.replace(tzinfo=timezone.utc)
    return valor.timestamp()


@router.get("/estado")
def estado(db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    run = motor_service.latest_run(db)
    pendientes = motor_service.runs_pendientes(db)
    return {
        "run": motor_service.run_to_dict(run),
        "en_cola": pendientes > 0,
        "pendientes": pendientes,
    }


@router.post("/ejecutar")
def ejecutar(
    conjunto: Literal["completa", "acr", "semana", "paradas"] = Query("completa"),
    semana: str | None = Query(None, description="AAAA-Wnn; por defecto, la semana ISO actual"),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    try:
        semana = motor_service.normalizar_semana(semana)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    fases = motor_service.CONJUNTOS[conjunto]
    run = motor_service.encolar(db, trigger=f"manual:{conjunto}", fases=fases, semana=semana)
    return {"run_id": run.id, "conjunto": conjunto, "fases": fases, "semana": semana,
            "status": run.status}


@router.get("/catalogo")
def catalogo(db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    contenido = motor_service.get_salida(db, "catalogo")
    return contenido if contenido is not None else _sin_datos()


@router.get("/historia-clinica")
def historia_clinica(db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    contenido = motor_service.get_salida(db, "historia_clinica")
    return contenido if contenido is not None else _sin_datos()


@router.get("/historia-clinica-acr")
def historia_clinica_acr(db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    contenido = motor_service.get_salida(db, "historia_clinica_acr")
    return contenido if contenido is not None else _sin_datos()


@router.get("/cumplimiento-sap")
def cumplimiento_sap(db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    contenido = motor_service.get_salida(db, "cumplimiento")
    return contenido if contenido is not None else _sin_datos()


@router.get("/plan-recomendado")
def plan_recomendado(
    version: Literal["fusion", "A", "B", "todas"] = Query("fusion"),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    contenido = motor_service.get_salida(db, "plan_recomendado")
    if contenido is None:
        return _sin_datos()
    return _version_filtrada(contenido, version)


@router.get("/plan-semanal/excel")
def plan_semanal_excel(
    semana: str | None = Query(None),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    semana = _semana(semana)
    archivo = motor_service.get_salida_archivo(db, motor_service.SALIDA_EXCEL, semana)
    if archivo is None:
        if not motor_service.hay_run_ok(db):
            return _sin_datos()
        run = motor_service.encolar(db, trigger="semana", fases=motor_service.FASES_SEMANA, semana=semana)
        return Response(
            content=_json_202(run.id), status_code=202, media_type="application/json")
    return Response(
        content=archivo, media_type=XLSX_MIME,
        headers={"Content-Disposition": f'attachment; filename="plan_semanal_{semana}.xlsx"'})


@router.get("/plan-semanal")
def plan_semanal(
    semana: str | None = Query(None),
    version: Literal["fusion", "todas"] = Query("fusion"),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    semana = _semana(semana)
    contenido = motor_service.get_salida(db, "plan_semanal", semana)
    if contenido is None:
        if not motor_service.hay_run_ok(db):
            return _sin_datos()
        run = motor_service.encolar(db, trigger="semana", fases=motor_service.FASES_SEMANA, semana=semana)
        return Response(content=_json_202(run.id), status_code=202, media_type="application/json")
    return _version_filtrada(contenido, version)


@router.get("/alertas")
def alertas(
    semana: str | None = Query(None),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    semana = _semana(semana)
    contenido = motor_service.get_salida(db, "alertas", semana)
    if contenido is None:
        if not motor_service.hay_run_ok(db):
            return _sin_datos()
        run = motor_service.encolar(db, trigger="semana", fases=motor_service.FASES_SEMANA, semana=semana)
        return Response(content=_json_202(run.id), status_code=202, media_type="application/json")
    return contenido


@router.get("/historia/eventos")
def historia_eventos(
    maquina: str | None = Query(None),
    tipo: Literal["Todo", "FALLA", "PREVENTIVO", "CORRECTIVO", "AVISO"] = Query("Todo"),
    desde: date | None = Query(None),
    hasta: date | None = Query(None),
    id_componente: str | None = Query(None),
    pagina: int = Query(1, ge=1),
    por_pagina: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    filtros = []
    if maquina:
        filtros.append(HistoriaEvento.maquina == maquina)
    if tipo != "Todo":
        filtros.append(HistoriaEvento.tipo == tipo)
    if desde:
        filtros.append(HistoriaEvento.fecha >= desde)
    if hasta:
        filtros.append(HistoriaEvento.fecha <= hasta)
    if id_componente:
        filtros.append(HistoriaEvento.id_componente == id_componente)

    total = int(db.scalar(select(func.count(HistoriaEvento.id)).where(*filtros)) or 0)
    filas = db.scalars(
        select(HistoriaEvento).where(*filtros)
        .order_by(HistoriaEvento.fecha.desc(), HistoriaEvento.id.desc())
        .offset((pagina - 1) * por_pagina).limit(por_pagina)
    ).all()
    return {
        "total": total,
        "pagina": pagina,
        "por_pagina": por_pagina,
        "eventos": [
            {
                "id": e.id, "maquina": e.maquina, "fecha": e.fecha.isoformat(), "tipo": e.tipo,
                "id_componente": e.id_componente, "componente": e.componente, "sistema": e.sistema,
                "titulo": e.titulo, "detalle": e.detalle,
                "minutos_paro": float(e.minutos_paro) if e.minutos_paro is not None else None,
                "orden": e.orden,
            }
            for e in filas
        ],
    }


@router.post("/decisiones")
def guardar_decision(
    body: DecisionIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if body.Id_parada:
        from ..models import Parada

        if db.get(Parada, body.Id_parada) is None:
            raise HTTPException(status_code=404, detail="La parada indicada no existe")

    semana = _semana(body.semana, desde_parada=body.Id_parada, db=db)
    decision = db.get(DecisionTarea, body.Id_tarea)
    if decision is None:
        decision = DecisionTarea(id_tarea=body.Id_tarea)
        db.add(decision)
    decision.decision = body.Decision
    decision.id_parada = body.Id_parada
    decision.semana = semana
    decision.decidida_por = user.email
    db.commit()
    db.refresh(decision)

    run = motor_service.encolar(db, trigger="decision", fases=motor_service.FASES_PARADAS, semana=semana)
    return {
        "decision": {
            "id_tarea": decision.id_tarea, "decision": decision.decision,
            "id_parada": decision.id_parada,
            "decidida_por": decision.decidida_por,
            "decidida_en": decision.decidida_en.isoformat() if decision.decidida_en else None,
        },
        "run_id": run.id,
    }


@router.delete("/decisiones/{id_tarea}")
def borrar_decision(
    id_tarea: str,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    decision = db.get(DecisionTarea, id_tarea)
    if decision is None:
        raise HTTPException(status_code=404, detail="Decisión no encontrada")
    semana = decision.semana or motor_service.semana_actual()
    db.delete(decision)
    db.commit()
    run = motor_service.encolar(db, trigger="decision", fases=motor_service.FASES_PARADAS, semana=semana)
    return {"deleted": True, "id_tarea": id_tarea, "run_id": run.id}


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------
def _json_202(run_id: int) -> bytes:
    import json

    return json.dumps({"status": "calculando", "run_id": run_id}).encode("utf-8")


def _semana(valor: str | None, desde_parada: str | None = None, db: Session | None = None) -> str:
    if valor:
        try:
            return motor_service.normalizar_semana(valor)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if desde_parada and db is not None:
        from ..models import Parada

        parada = db.get(Parada, desde_parada)
        if parada is not None:
            return motor_service.semana_iso(parada.fecha)
    return motor_service.semana_actual()
