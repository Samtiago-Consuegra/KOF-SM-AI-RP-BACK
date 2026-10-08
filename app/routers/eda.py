from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..constants import CRITICAL_MACHINES
from ..db import get_db
from ..eda import available_periods, build_summary, df_from_event_rows
from ..models import FailureEvent

router = APIRouter(tags=["eda"])

Period = Literal["dia", "semana", "mes", "todo"]


def _fetch_rows(db: Session, machines: list[str] | None = None):
    q = select(
        FailureEvent.id, FailureEvent.machine, FailureEvent.subsystem, FailureEvent.failure_type,
        FailureEvent.shift, FailureEvent.interval, FailureEvent.event_date, FailureEvent.stop_minutes,
        FailureEvent.efficiency_points_lost,
    )
    if machines:
        q = q.where(FailureEvent.machine.in_(machines))
    return [dict(r._mapping) for r in db.execute(q)]


def _parse_machines(machines: str | None) -> list[str] | None:
    """'criticas' | 'todas' | 'Llenadora,Empacadora'."""
    if not machines or machines == "todas":
        return None
    if machines == "criticas":
        return CRITICAL_MACHINES
    return [m.strip() for m in machines.split(",") if m.strip()]


@router.get("/eda/periodos")
def eda_periodos(db: Session = Depends(get_db)):
    """Meses que tienen paros registrados + límites del calendario.

    Consulta ligera: solo la columna de fechas, sin el resto del evento.
    """
    dates = db.execute(select(FailureEvent.event_date).distinct()).scalars().all()
    return available_periods(dates)


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
    df = df_from_event_rows(_fetch_rows(db))
    return build_summary(df, selected, period=period, anchor=date_, start=start, end=end)


@router.get("/eda/machine/{machine}")
def eda_machine(machine: str, period: Period = "todo", db: Session = Depends(get_db)):
    df = df_from_event_rows(_fetch_rows(db, [machine]))
    return {"machine": machine, "critical": machine in CRITICAL_MACHINES,
            **build_summary(df, [machine], period=period)}


@router.get("/machines")
def list_machines(db: Session = Depends(get_db)):
    """Máquinas presentes en los datos cargados (las críticas primero)."""
    rows = db.execute(
        select(FailureEvent.machine, func.count(FailureEvent.id),
               func.min(FailureEvent.event_date), func.max(FailureEvent.event_date))
        .group_by(FailureEvent.machine)
    ).all()
    out = [{"machine": m, "critical": m in CRITICAL_MACHINES, "has_prediction": m in CRITICAL_MACHINES,
            "events_count": n, "first_date": a.isoformat(), "last_date": b.isoformat()}
           for m, n, a, b in rows]
    return sorted(out, key=lambda x: (not x["critical"], x["machine"]))
