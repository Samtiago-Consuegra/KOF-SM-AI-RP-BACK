"""Cobertura de datos: rango de fechas y total de cada fuente, para que el front avise de filtros vacíos.

Solo agregados (MIN/MAX/COUNT) sobre columnas indexadas; la respuesta completa se cachea en memoria
y se invalida al escribir (subidas, salidas del motor y paradas).
"""
from __future__ import annotations

import threading
from datetime import date, datetime

from sqlalchemy import case, event, func, select
from sqlalchemy.orm import Session

from .models import (DataLoad, FailureEvent, HistoriaEvento, MotorArchivo, MotorSalida, OrdenSap, Parada,
                     PredictionRun)
from .motor_service import BOGOTA

_LOCK = threading.Lock()
_CACHE: dict | None = None


def invalidate() -> None:
    global _CACHE
    with _LOCK:
        _CACHE = None


def _iso(valor) -> str | None:
    if valor is None:
        return None
    if isinstance(valor, str):
        return valor[:10]
    if isinstance(valor, datetime):
        if valor.tzinfo is not None:
            valor = valor.astimezone(BOGOTA)
        return valor.date().isoformat()
    if isinstance(valor, date):
        return valor.isoformat()
    return str(valor)[:10]


def _rango(desde, hasta, total) -> dict:
    return {"desde": _iso(desde), "hasta": _iso(hasta), "total": int(total or 0)}


def _agregado(db: Session, columna) -> tuple:
    return db.execute(select(func.min(columna), func.max(columna), func.count(columna))).one()


def _calcular(db: Session) -> dict:
    reporte = _rango(*_agregado(db, FailureEvent.event_date))

    h = HistoriaEvento
    falla = case((h.tipo == "FALLA", h.fecha))
    aviso = case((h.tipo == "AVISO", h.fecha))
    hist = db.execute(select(
        func.min(h.fecha), func.max(h.fecha), func.count(h.fecha),
        func.min(falla), func.max(falla), func.count(falla),
        func.min(aviso), func.max(aviso), func.count(aviso),
    )).one()

    # Fechas de carga: se combinan en Python ya convertidas a Bogotá.
    archivos_min, archivos_max, archivos_total = None, None, 0
    for desde, hasta, total in (_agregado(db, MotorArchivo.subido_en), _agregado(db, DataLoad.uploaded_at)):
        archivos_total += total or 0
        for valor in (desde, hasta):
            iso = _iso(valor)
            if iso:
                archivos_min = iso if archivos_min is None else min(archivos_min, iso)
                archivos_max = iso if archivos_max is None else max(archivos_max, iso)

    datos_hasta = db.scalar(select(MotorSalida.contenido["datos_hasta"].as_string()).where(
        MotorSalida.nombre == "cumplimiento", MotorSalida.semana == "").limit(1))

    corte = db.scalar(select(PredictionRun.data_until).where(
        PredictionRun.status == "ok", PredictionRun.data_until.is_not(None))
        .order_by(PredictionRun.id.desc()).limit(1))
    corte = _iso(corte) or reporte["hasta"]

    return {
        "reporte_diario": reporte,
        "fallas_historia": _rango(hist[3], hist[4], hist[5]),
        "eventos_historia": _rango(hist[0], hist[1], hist[2]),
        "ordenes_sap": _rango(*_agregado(db, OrdenSap.fecha_programada)),
        "avisos": _rango(hist[6], hist[7], hist[8]),
        "paradas": _rango(*_agregado(db, Parada.fecha)),
        "archivos": {"desde": archivos_min, "hasta": archivos_max, "total": archivos_total},
        "cumplimiento_sap": {"desde": None, "hasta": _iso(datos_hasta), "total": 1 if datos_hasta else 0},
        "predicciones": {"desde": None, "hasta": corte, "total": 1 if corte else 0},
    }


def get_cobertura(db: Session) -> dict:
    global _CACHE
    with _LOCK:
        if _CACHE is not None:
            return _CACHE
    respuesta = {**_calcular(db), "generado_en": datetime.now(BOGOTA).isoformat(timespec="seconds")}
    with _LOCK:
        _CACHE = respuesta
    return respuesta


# Paradas se crean/editan/borran por ORM en routers/paradas.py: se invalida al confirmar el commit.
@event.listens_for(Session, "after_flush")
def _marcar_paradas(session: Session, _ctx) -> None:
    if any(isinstance(o, Parada) for o in (*session.new, *session.dirty, *session.deleted)):
        session.info["cobertura_sucia"] = True


@event.listens_for(Session, "after_commit")
def _invalidar_tras_commit(session: Session) -> None:
    if session.info.pop("cobertura_sucia", False):
        invalidate()
