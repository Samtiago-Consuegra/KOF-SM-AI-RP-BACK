from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from .. import cache
from ..constants import CRITICAL_MACHINES
from ..db import get_db

router = APIRouter(tags=["eda"])

Period = Literal["dia", "semana", "mes", "todo"]


def _parse_machines(machines: str | None) -> list[str] | None:
    """'criticas' | 'todas' | 'Llenadora,Empacadora'."""
    if not machines or machines == "todas":
        return None
    if machines == "criticas":
        return CRITICAL_MACHINES
    return [m.strip() for m in machines.split(",") if m.strip()]


@router.get("/eda/periodos")
def eda_periodos(db: Session = Depends(get_db)):
    """Meses que tienen paros registrados + límites del calendario."""
    return cache.get_periods(db)


@router.get("/eda/summary")
def eda_summary(
    machines: str | None = Query(None, description="criticas | todas | lista separada por comas"),
    period: Period = Query("semana", description="dia | semana | mes | todo"),
    date_: date | None = Query(None, alias="date", description="Último día del periodo (por defecto, el último con datos)"),
    start: date | None = Query(None, description="Inicio explícito (YYYY-MM-DD); tiene prioridad sobre period"),
    end: date | None = Query(None, description="Fin explícito (YYYY-MM-DD)"),
    db: Session = Depends(get_db),
):
    selected = _parse_machines(machines)
    return cache.get_summary(db, selected, period=period, anchor=date_, start=start, end=end)


@router.get("/eda/machine/{machine}")
def eda_machine(machine: str, period: Period = "todo", db: Session = Depends(get_db)):
    result = cache.get_machine_summary(db, machine, period=period)
    return {"machine": machine, "critical": machine in CRITICAL_MACHINES, **result}


@router.get("/machines")
def list_machines(db: Session = Depends(get_db)):
    """Máquinas presentes en los datos cargados (las críticas primero)."""
    return cache.get_machines(db)
