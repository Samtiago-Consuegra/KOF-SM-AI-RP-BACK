"""Análisis exploratorio (EDA) de los paros guardados.

Reutilizado del backend demo. Añadido: filtro por máquinas y rango de fechas, indicadores (KPI)
con comparación contra el periodo anterior, minutos de paro por día y MTBF por periodo.

Todas las tablas se construyen con operaciones vectorizadas de pandas (sin `iterrows`) para que
el payload mantenga el mismo orden y los mismos tipos nativos de Python.
"""
from datetime import date, timedelta

import pandas as pd

from .constants import CRITICAL_MACHINES

PERIOD_DAYS = {"dia": 1, "semana": 7, "mes": 30}


def df_from_event_rows(rows) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["event_date"] = pd.to_datetime(df["event_date"])
    df["stop_minutes"] = pd.to_numeric(df["stop_minutes"], errors="coerce").fillna(0).astype(float)
    df["efficiency_points_lost"] = pd.to_numeric(df["efficiency_points_lost"], errors="coerce").fillna(0).astype(float)
    if "id" not in df.columns:
        df["id"] = range(1, len(df) + 1)
    return df


def resolve_range(df: pd.DataFrame, period: str, anchor: date | None, start: date | None, end: date | None):
    """Devuelve (inicio, fin) del periodo pedido.

    - start/end explícitos tienen prioridad.
    - period = dia | semana | mes: termina en `anchor` (por defecto, el último día con datos, porque el
      Excel suele ir unos días atrasado respecto a hoy).
    - period = todo: todo el histórico.
    """
    data_min, data_max = df["event_date"].min().date(), df["event_date"].max().date()
    if start or end:
        return start or data_min, end or data_max
    if period == "todo":
        return data_min, data_max
    days = PERIOD_DAYS.get(period, 7)
    last = anchor or data_max
    return last - timedelta(days=days - 1), last


def _filter(df: pd.DataFrame, start: date, end: date) -> pd.DataFrame:
    return df[(df["event_date"] >= pd.Timestamp(start)) & (df["event_date"] <= pd.Timestamp(end))]


def kpis(df: pd.DataFrame, full: pd.DataFrame, start: date, end: date) -> dict:
    """Indicadores del periodo + variación de registros frente al periodo anterior de igual duración."""
    days = (end - start).days + 1
    prev = _filter(full, start - timedelta(days=days), start - timedelta(days=1))
    count, prev_count = len(df), len(prev)
    minutes = float(df["stop_minutes"].sum()) if count else 0.0
    critical = [m for m in df["machine"].unique() if m in CRITICAL_MACHINES] if count else []
    mtbf_values = [(days * 24) / max(1, int((df["machine"] == m).sum())) for m in critical]
    return {
        "events_count": count,
        "stop_minutes": round(minutes, 2),
        "stop_hours": round(minutes / 60, 2),
        "machines_count": int(df["machine"].nunique()) if count else 0,
        "previous_events_count": prev_count,
        "delta_vs_previous_pct": round(100 * (count - prev_count) / prev_count, 1) if prev_count else None,
        "mtbf_critical_hours": round(sum(mtbf_values) / len(mtbf_values), 1) if mtbf_values else None,
        "critical_machines_count": len(critical),
    }


def by_day(df: pd.DataFrame, start: date, end: date) -> list:
    """Minutos y paros por día, incluyendo los días sin paros (con 0)."""
    days = pd.date_range(start, end, freq="D", name="event_date")
    grouped = df.groupby("event_date").agg(minutes=("stop_minutes", "sum"), stops=("id", "count"))
    grouped = grouped.reindex(days, fill_value=0).reset_index()
    grouped["date"] = grouped["event_date"].dt.strftime("%Y-%m-%d")
    grouped["minutes"] = grouped["minutes"].round(2).astype(float)
    grouped["stops"] = grouped["stops"].astype(int)
    return grouped[["date", "minutes", "stops"]].to_dict("records")


def by_day_machine(df: pd.DataFrame) -> list:
    """Paros y minutos por día y máquina. No rellena ceros: las gráficas de evolución
    arman la matriz día × máquina por su cuenta y así el payload se mantiene pequeño."""
    if df.empty:
        return []
    grouped = (
        df.groupby(["event_date", "machine"], dropna=False)
        .agg(count=("id", "count"), total_minutes=("stop_minutes", "sum"))
        .reset_index()
    )
    grouped["date"] = grouped["event_date"].dt.strftime("%Y-%m-%d")
    grouped["count"] = grouped["count"].astype(int)
    grouped["total_minutes"] = grouped["total_minutes"].round(2).astype(float)
    grouped["critical"] = grouped["machine"].isin(CRITICAL_MACHINES).astype(bool)
    return grouped[["date", "machine", "count", "total_minutes", "critical"]].to_dict("records")


def available_periods(dates) -> dict:
    """Meses con paros registrados y el rango real de la base.

    Alimenta el selector de mes y los límites del calendario del frontend: el histórico
    arranca en 2024, así que un rango fijo de "hoy hacia atrás" dejaría fuera casi todo.
    Acepta cualquier iterable de `date`; el router le pasa solo esa columna, sin cargar el resto.
    """
    days = [d.date() if hasattr(d, "date") else d for d in dates]
    if not days:
        return {"empty": True, "min": None, "max": None, "months": []}
    return {
        "empty": False,
        "min": min(days).isoformat(),
        "max": max(days).isoformat(),
        "months": sorted({f"{d.year:04d}-{d.month:02d}" for d in days}),
    }


def by_machine(df: pd.DataFrame) -> list:
    grouped = df.groupby("machine", dropna=False).agg(
        count=("id", "count"),
        total_minutes=("stop_minutes", "sum"),
        avg_minutes=("stop_minutes", "mean"),
    )
    grouped = grouped.sort_values("count", ascending=False).reset_index()
    grouped["count"] = grouped["count"].astype(int)
    grouped["total_minutes"] = grouped["total_minutes"].round(2).astype(float)
    grouped["avg_minutes"] = grouped["avg_minutes"].round(2).astype(float)
    grouped["critical"] = grouped["machine"].isin(CRITICAL_MACHINES).astype(bool)
    return grouped[["machine", "count", "total_minutes", "avg_minutes", "critical"]].to_dict("records")


def top_failures(df: pd.DataFrame, n: int = 10) -> list:
    grouped = (
        df.groupby(["machine", "failure_type"], dropna=False)
        .agg(count=("id", "count"), total_minutes=("stop_minutes", "sum"))
        .reset_index()
        .sort_values("count", ascending=False)
    )
    out = []
    for machine in sorted(df["machine"].unique(), key=lambda m: m in CRITICAL_MACHINES, reverse=True):
        top = grouped[grouped["machine"] == machine].head(n).copy()
        if top.empty:
            continue
        top["count"] = top["count"].astype(int)
        top["total_minutes"] = top["total_minutes"].round(2).astype(float)
        top["critical"] = top["machine"].isin(CRITICAL_MACHINES).astype(bool)
        out.extend(top[["machine", "failure_type", "count", "total_minutes", "critical"]].to_dict("records"))
    return out


def monthly_trend(df: pd.DataFrame) -> list:
    work = df.copy()
    work["month"] = work["event_date"].dt.strftime("%Y-%m")
    grouped = (
        work.groupby(["month", "machine"], dropna=False)
        .agg(count=("id", "count"), total_minutes=("stop_minutes", "sum"))
        .reset_index()
    )
    grouped["count"] = grouped["count"].astype(int)
    grouped["total_minutes"] = grouped["total_minutes"].round(2).astype(float)
    grouped["critical"] = grouped["machine"].isin(CRITICAL_MACHINES).astype(bool)
    return grouped[["month", "machine", "count", "total_minutes", "critical"]].to_dict("records")


def by_shift(df: pd.DataFrame) -> list:
    grouped = (
        df.groupby(["shift", "machine"], dropna=False)
        .agg(count=("id", "count"), total_minutes=("stop_minutes", "sum"))
        .reset_index()
    )
    grouped["shift"] = grouped["shift"].astype(str)
    grouped["count"] = grouped["count"].astype(int)
    grouped["total_minutes"] = grouped["total_minutes"].round(2).astype(float)
    grouped["critical"] = grouped["machine"].isin(CRITICAL_MACHINES).astype(bool)
    return grouped[["shift", "machine", "count", "total_minutes", "critical"]].to_dict("records")


def avg_duration_by_failure(df: pd.DataFrame) -> list:
    grouped = (
        df.groupby(["machine", "failure_type"], dropna=False)
        .agg(avg_minutes=("stop_minutes", "mean"), count=("id", "count"))
        .reset_index()
    )
    grouped["avg_minutes"] = grouped["avg_minutes"].round(2).astype(float)
    grouped["count"] = grouped["count"].astype(int)
    return grouped[["machine", "failure_type", "avg_minutes", "count"]].to_dict("records")


def scatter(df: pd.DataFrame) -> list:
    subset = df[["machine", "stop_minutes", "efficiency_points_lost"]]
    return (
        subset.assign(
            stop_minutes=subset["stop_minutes"].round(2).astype(float),
            efficiency_points_lost=subset["efficiency_points_lost"].round(4).astype(float),
            critical=subset["machine"].isin(CRITICAL_MACHINES).astype(bool),
        )[["machine", "stop_minutes", "efficiency_points_lost", "critical"]]
        .to_dict("records")
    )


def mtbf_by_machine(df: pd.DataFrame, start: date, end: date) -> list:
    """MTBF = horas del periodo / número de paros (misma definición que el KPI del dashboard)."""
    hours = ((end - start).days + 1) * 24
    counts = df.groupby("machine").size()
    result = [
        {
            "machine": m,
            "count": int(n),
            "mtbf_hours": round(hours / n, 1),
            "critical": m in CRITICAL_MACHINES,
        }
        for m, n in counts.items()
    ]
    return sorted(result, key=lambda x: (not x["critical"], x["machine"]))


def build_summary(full: pd.DataFrame, machines: list[str] | None, period: str = "semana",
                  anchor: date | None = None, start: date | None = None, end: date | None = None) -> dict:
    if full.empty:
        return {"empty": True, "message": "Aún no hay datos. Sube un Excel SAP en /upload."}

    data_range = {"min": full["event_date"].min().date().isoformat(),
                  "max": full["event_date"].max().date().isoformat()}
    if machines:
        full = full[full["machine"].isin(machines)]
        if full.empty:
            return {"empty": True, "data_range": data_range,
                    "message": f"No hay registros para las máquinas: {', '.join(machines)}"}

    start, end = resolve_range(full, period, anchor, start, end)
    df = _filter(full, start, end)
    result = {
        "empty": df.empty,
        "filters": {"machines": machines or "todas", "period": period,
                    "start": start.isoformat(), "end": end.isoformat()},
        "data_range": data_range,
        "kpis": kpis(df, full, start, end),
        "by_day": by_day(df, start, end),
        "by_day_machine": by_day_machine(df),
    }
    if df.empty:
        # El Excel suele ir atrasado respecto a hoy, así que el caso "no hay paros en el rango"
        # casi siempre significa "todavía no han llegado los datos de esas fechas".
        last = full["event_date"].max().date()
        return {**result, "machines": [], "top_failures": [], "monthly_trend": [], "shifts": [],
                "avg_duration": [], "scatter": [], "mtbf": [],
                "message": f"Sin paros entre {start.isoformat()} y {end.isoformat()}. "
                           f"El último registro es del {last.isoformat()}."}
    return {
        **result,
        "machines": by_machine(df),
        "top_failures": top_failures(df),
        "monthly_trend": monthly_trend(df),
        "shifts": by_shift(df),
        "avg_duration": avg_duration_by_failure(df),
        "scatter": scatter(df),
        "mtbf": mtbf_by_machine(df, start, end),
    }
