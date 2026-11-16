"""Endpoints de la página Historia clínica (/motor/historia/...).

Cada endpoint alimenta una sección de la página; todos los cálculos viven en historia_service.
Si el motor aún no ha corrido, responden {"available": false, "message": ...} (estado normal).
"""
from __future__ import annotations

from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import historia_service as hs
from ..db import get_db
from ..deps import get_current_user
from ..models import HistoriaEvento, User

router = APIRouter(prefix="/motor/historia", tags=["historia clínica"])

Maquina = Literal["Llenadora", "Empacadora", "Paletizadora", "Envolvedora"]


def _sin_datos() -> dict:
    return {"available": False, "message": hs.MENSAJE_SIN_DATOS}


def _validar_rango(desde: date | None, hasta: date | None) -> None:
    if desde and hasta and desde > hasta:
        raise HTTPException(status_code=400, detail="La fecha 'desde' no puede ser posterior a 'hasta'")


@router.get("/periodos")
def periodos(db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    """Límites del filtro de periodo de la página."""
    datos = hs.periodos(db)
    return {"available": True, **datos} if datos else _sin_datos()


@router.get("/jerarquia")
def jerarquia(maquina: Maquina, db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    """Sistemas y componentes de la máquina para los filtros en cascada."""
    sistemas = hs.jerarquia(db, maquina)
    if sistemas is None:
        return _sin_datos()
    return {"available": True, "maquina": maquina, "sistemas": sistemas}


@router.get("/ficha")
def ficha(
    maquina: Maquina,
    desde: date | None = Query(None),
    hasta: date | None = Query(None),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    _validar_rango(desde, hasta)
    if not hs.hay_datos(db):
        return _sin_datos()
    return {"available": True, **hs.ficha(db, maquina, desde, hasta)}


@router.get("/indicadores")
def indicadores(
    maquina: Maquina,
    desde: date | None = Query(None),
    hasta: date | None = Query(None),
    sistema: str | None = Query(None),
    componente: str | None = Query(None, description="Id_componente del catálogo"),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    _validar_rango(desde, hasta)
    if not hs.hay_datos(db):
        return _sin_datos()
    return {"available": True, **hs.indicadores(db, maquina, desde, hasta, sistema, componente)}


@router.get("/paros-por-mes")
def paros_por_mes(
    maquina: Maquina,
    sistema: str | None = Query(None),
    componente: str | None = Query(None),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    if not hs.hay_datos(db):
        return _sin_datos()
    return {"available": True, **hs.paros_por_mes(db, maquina, sistema, componente)}


@router.get("/resumen")
def resumen(
    maquina: Maquina,
    desde: date | None = Query(None),
    hasta: date | None = Query(None),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """Datos del PDF "Exportar historia"."""
    _validar_rango(desde, hasta)
    if not hs.hay_datos(db):
        return _sin_datos()
    return {"available": True, **hs.resumen_pdf(db, maquina, desde, hasta)}


@router.get("/eventos")
def eventos(
    maquina: str | None = Query(None),
    tipo: Literal["Todo", "FALLA", "PREVENTIVO", "CORRECTIVO", "AVISO"] = Query("Todo"),
    desde: date | None = Query(None),
    hasta: date | None = Query(None),
    sistema: str | None = Query(None),
    id_componente: str | None = Query(None),
    pagina: int = Query(1, ge=1),
    por_pagina: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """Línea de tiempo paginada, del evento más reciente al más antiguo."""
    _validar_rango(desde, hasta)
    filtros = []
    if maquina:
        filtros.append(HistoriaEvento.maquina == maquina)
    if tipo != "Todo":
        filtros.append(HistoriaEvento.tipo == tipo)
    if desde:
        filtros.append(HistoriaEvento.fecha >= desde)
    if hasta:
        filtros.append(HistoriaEvento.fecha <= hasta)
    if sistema:
        filtros.append(HistoriaEvento.sistema == sistema)
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
                "orden": e.orden, "turno": e.turno, "estado": e.estado, "clase": e.clase, "plan": e.plan,
            }
            for e in filas
        ],
    }
