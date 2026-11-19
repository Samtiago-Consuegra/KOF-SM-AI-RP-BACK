"""Endpoints de la pantalla Cumplimiento (/cumplimiento/...).

Las tarjetas de resumen y la gráfica salen de aquí; la tabla de seguimiento usa /paradas.
Todos los cálculos viven en cumplimiento_service.
"""
from __future__ import annotations

from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from .. import cumplimiento_service as cs
from ..db import get_db
from ..deps import get_current_user
from ..models import User

router = APIRouter(prefix="/cumplimiento", tags=["cumplimiento"])

Maquina = Literal["Llenadora", "Empacadora", "Paletizadora", "Envolvedora"]


def _validar(desde: date | None, hasta: date | None) -> None:
    if desde and hasta and desde > hasta:
        raise HTTPException(status_code=400, detail="La fecha 'desde' no puede ser posterior a 'hasta'")


@router.get("/resumen")
def resumen(
    desde: date | None = Query(None),
    hasta: date | None = Query(None),
    maquina: Maquina | None = Query(None, description="Vacío = todas las máquinas"),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """Tarjetas: cumplimiento del plan (%), ejecutadas de programadas, pospuestas y vencidas."""
    _validar(desde, hasta)
    return cs.resumen(db, desde, hasta, maquina)


@router.get("/grafica")
def grafica(
    desde: date | None = Query(None),
    hasta: date | None = Query(None),
    maquina: Maquina | None = Query(None),
    agrupar: Literal["maquina", "semana"] = Query("maquina", description="maquina = vista semanal · semana = vista mensual"),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """Barras de la gráfica: programadas, ejecutadas, vencidas, pospuestas y % por máquina (y semana).

    `por_tipo`: programadas del periodo y la máquina por clase (Preventiva, Correctiva, Predictiva).
    """
    _validar(desde, hasta)
    por_tipo: dict = {}
    filas = cs.por_maquina_y_semana(db, desde, hasta, maquina, agrupar, por_tipo=por_tipo)
    return {"filas": filas, "por_tipo": por_tipo}
