"""Caché en memoria del EDA.

Objetivo: no volver a descargar ni tipar todo `failure_events` en cada petición. El DataFrame
completo se indexa por una "versión de datos" barata: (count(id), max(id)). Si la versión no cambia,
los resúmenes, periodos y máquinas se sirven desde memoria.

`invalidate()` limpia todo; se llama tras insertar filas en /upload. Aunque un escritor externo no
llame a `invalidate()`, la comprobación de versión detecta el cambio y reconstruye la caché.
"""
from __future__ import annotations

import threading
from collections import OrderedDict
from datetime import date
from typing import Any

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .constants import CRITICAL_MACHINES
from .eda import available_periods, build_summary, df_from_event_rows
from .models import FailureEvent

SUMMARY_CACHE_MAX = 64

_LOCK = threading.RLock()

_state: dict[str, Any] = {"version": None, "df": None}
_summary_cache: "OrderedDict[tuple, dict]" = OrderedDict()
_periods_cache: dict[str, Any] = {"version": None, "value": None}
_machines_cache: dict[str, Any] = {"version": None, "value": None}


def data_version(db: Session) -> tuple[int, int | None]:
    """Versión de los datos con una única consulta ligera (sin traer las filas)."""
    count, max_id = db.execute(
        select(func.count(FailureEvent.id), func.max(FailureEvent.id))
    ).one()
    return int(count or 0), max_id


def _fetch_rows(db: Session) -> list[dict]:
    q = select(
        FailureEvent.id, FailureEvent.machine, FailureEvent.subsystem, FailureEvent.failure_type,
        FailureEvent.shift, FailureEvent.interval, FailureEvent.event_date, FailureEvent.stop_minutes,
        FailureEvent.efficiency_points_lost,
    )
    return [dict(r._mapping) for r in db.execute(q)]


def _reset_derived() -> None:
    _summary_cache.clear()
    _periods_cache["version"] = None
    _periods_cache["value"] = None
    _machines_cache["version"] = None
    _machines_cache["value"] = None


def get_dataframe(db: Session) -> tuple[tuple, pd.DataFrame]:
    """DataFrame completo de `failure_events`, reconstruido solo cuando cambia la versión."""
    with _LOCK:
        version = data_version(db)
        if _state["version"] == version and _state["df"] is not None:
            return version, _state["df"]

        df = df_from_event_rows(_fetch_rows(db))
        _state["version"] = version
        _state["df"] = df
        _reset_derived()
        return version, df


def get_summary(db: Session, machines: list[str] | None, period: str = "semana",
                anchor: date | None = None, start: date | None = None, end: date | None = None) -> dict:
    """`build_summary` sobre el DataFrame cacheado, con LRU por combinación de filtros."""
    version, df = get_dataframe(db)
    key = (
        version,
        tuple(machines) if machines else None,
        period,
        anchor.isoformat() if anchor else None,
        start.isoformat() if start else None,
        end.isoformat() if end else None,
    )
    with _LOCK:
        cached = _summary_cache.get(key)
        if cached is not None:
            _summary_cache.move_to_end(key)
            return cached

    value = build_summary(df, machines, period=period, anchor=anchor, start=start, end=end)

    with _LOCK:
        if _state["version"] == version:
            _summary_cache[key] = value
            _summary_cache.move_to_end(key)
            while len(_summary_cache) > SUMMARY_CACHE_MAX:
                _summary_cache.popitem(last=False)
    return value


def get_machine_summary(db: Session, machine: str, period: str = "todo") -> dict:
    """Resumen de una máquina. Se filtra el DataFrame antes de `build_summary` para conservar
    exactamente el `data_range` que devolvía la consulta con `WHERE machine = ...`."""
    version, df = get_dataframe(db)
    key = (version, "__machine__", machine, period)
    with _LOCK:
        cached = _summary_cache.get(key)
        if cached is not None:
            _summary_cache.move_to_end(key)
            return cached

    subset = df if df.empty else df[df["machine"] == machine]
    value = build_summary(subset, [machine], period=period)

    with _LOCK:
        if _state["version"] == version:
            _summary_cache[key] = value
            _summary_cache.move_to_end(key)
            while len(_summary_cache) > SUMMARY_CACHE_MAX:
                _summary_cache.popitem(last=False)
    return value


def get_periods(db: Session) -> dict:
    version, df = get_dataframe(db)
    with _LOCK:
        if _periods_cache["version"] == version:
            return _periods_cache["value"]

    dates = [] if df.empty else df["event_date"].tolist()
    value = available_periods(dates)

    with _LOCK:
        if _state["version"] == version:
            _periods_cache["version"] = version
            _periods_cache["value"] = value
    return value


def get_machines(db: Session) -> list[dict]:
    version, df = get_dataframe(db)
    with _LOCK:
        if _machines_cache["version"] == version:
            return _machines_cache["value"]

    if df.empty:
        value: list[dict] = []
    else:
        grouped = df.groupby("machine", dropna=False).agg(
            count=("id", "count"),
            first=("event_date", "min"),
            last=("event_date", "max"),
        )
        value = [
            {
                "machine": m,
                "critical": m in CRITICAL_MACHINES,
                "has_prediction": m in CRITICAL_MACHINES,
                "events_count": int(n),
                "first_date": a.date().isoformat() if hasattr(a, "date") else str(a),
                "last_date": b.date().isoformat() if hasattr(b, "date") else str(b),
            }
            for m, n, a, b in grouped.itertuples(name=None)
        ]
        value.sort(key=lambda x: (not x["critical"], x["machine"]))

    with _LOCK:
        if _state["version"] == version:
            _machines_cache["version"] = version
            _machines_cache["value"] = value
    return value


def invalidate() -> None:
    """Limpia el DataFrame y todos los resultados derivados."""
    with _LOCK:
        _state["version"] = None
        _state["df"] = None
        _reset_derived()
