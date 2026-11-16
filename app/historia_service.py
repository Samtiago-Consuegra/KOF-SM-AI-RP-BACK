"""Historia clínica: construcción de la tabla historia_eventos y cálculos por periodo.

Dos responsabilidades:

1. construir_eventos(salida): convierte los CSV que deja el motor (fases 12 y 13) en filas de
   HistoriaEvento. Lo llama motor_service al guardar una corrida completa.
     - FALLA: reporte diario (base_diaria_componentes.csv). Los registros del mismo componente en
       horas consecutivas se unen en UN evento, igual que la Fase 14. Así el número de fallas de la
       página coincide con los "eventos" de la tarjeta de componentes.
     - PREVENTIVO / CORRECTIVO: órdenes SAP (ot_limpias.csv). El componente de una correctiva sale
       de su aviso; el de una preventiva, del plan (tabla de equivalencias).
     - AVISO: avisos SAP clasificados (avisos_clasificados.csv), con su componente de catálogo.
   El sistema de cada evento se toma del catálogo (el mismo que usa historia_clinica.json), para
   que los filtros Sistema y Componente de la página funcionen sobre todas las fuentes.

2. Cálculos que dependen del periodo elegido en la página (ficha, indicadores, paros por mes y
   resumen del PDF). La Fase 14 calcula sobre todo el histórico; estos cálculos repiten las mismas
   definiciones sobre el rango de fechas pedido. "Python calcula, la app solo muestra".

Definiciones (documentarlas en el informe):
  - Falla = evento del reporte diario (registros consecutivos unidos).
  - Días operados = días con al menos un registro en el reporte diario de las 4 máquinas
    (misma regla S2 de kof_utils.calendario y de posicion_operativa).
  - MTBF (h) = días operados del periodo x 24 / fallas.          (1 día operado = 24 h, Fase 14)
  - MTTR (min) = minutos de paro / fallas.
  - Disponibilidad (%) = 1 - minutos de paro / (días operados x 1440).
  - Cumplimiento del plan (%) = ejecutadas / órdenes preventivas y predictivas vencidas de las
    semanas del periodo (series semanales de la Fase 15). Es un dato de la máquina completa.
  - Falla predominante = forma de falla (Weibull, Fase 14) que acumula más minutos de paro entre
    los componentes de la máquina con datos suficientes.
"""
from __future__ import annotations

import logging
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .constants import CRITICAL_MACHINES
from .models import HistoriaEvento

log = logging.getLogger("kof.historia")

MAQUINAS = list(CRITICAL_MACHINES)
UMBRAL_EQUIV = 0.34          # mismo umbral de la Fase 14 para aceptar una equivalencia automática
MANTENIMIENTOS_POR_MES = 5   # máximo de preventivos listados por mes en la gráfica
TOP_FALLAS_PDF = 5
TAREAS_PDF = 10
MENSAJE_SIN_DATOS = "Aún no hay resultados del motor. Sube los archivos de SAP en Mantenimiento."

FORMAS = (  # prefijo de Forma_falla (Fase 14) -> etiqueta de la ficha
    ("Desgaste", "Por desgaste"),
    ("Aleatoria", "Aleatoria"),
    ("En racha", "En racha"),
)
POCOS_DATOS = "Pocos datos"


# =============================================================================================
# 1. Construcción de historia_eventos desde la carpeta de salida del motor
# =============================================================================================
def _leer_csv(salida: Path, nombre: str):
    import pandas as pd

    ruta = salida / nombre
    if not ruta.exists():
        return None
    try:
        return pd.read_csv(ruta)
    except Exception:  # noqa: BLE001
        log.exception("No se pudo leer %s", ruta)
        return None


def _vacio(valor) -> bool:
    import pandas as pd

    try:
        return valor is None or bool(pd.isna(valor))
    except (TypeError, ValueError):
        return False


def _texto(valor) -> str | None:
    if _vacio(valor):
        return None
    texto = str(valor).strip()
    return texto or None


def _codigo(valor) -> str | None:
    """Número de orden o aviso como texto: 4001234.0 -> '4001234'."""
    if _vacio(valor):
        return None
    try:
        numero = float(valor)
        if numero.is_integer():
            return str(int(numero))
    except (TypeError, ValueError):
        pass
    return _texto(valor)


def _fecha(valor) -> date | None:
    import pandas as pd

    if _vacio(valor):
        return None
    try:
        return pd.Timestamp(valor).date()
    except Exception:  # noqa: BLE001
        return None


def _verdadero(valor) -> bool:
    if isinstance(valor, bool):
        return valor
    return str(valor).strip().lower() in {"true", "1", "si", "sí", "yes"}


def _num(valor) -> float | None:
    if _vacio(valor):
        return None
    try:
        return float(valor)
    except (TypeError, ValueError):
        return None


def _catalogo(salida: Path) -> dict[str, dict]:
    """Id_componente -> {componente, sistema} según catalogo_componentes.csv (Fase 13)."""
    cat = _leer_csv(salida, "catalogo_componentes.csv")
    if cat is None:
        return {}
    out = {}
    for _, f in cat.iterrows():
        idc = _texto(f.get("Id_componente"))
        if idc:
            out[idc] = {"componente": _texto(f.get("Componente")),
                        "sistema": _texto(f.get("Sistema")) or "Sin sistema"}
    return out


def _componentes_por_plan(salida: Path, catalogo: dict) -> dict[str, dict]:
    """Plan SAP -> componente o sistema que cubre, desde la tabla de equivalencias de la Fase 13.

    Si el plan cubre un solo componente, se asigna ese componente; si cubre varios del mismo
    sistema, solo el sistema; si mezcla sistemas, nada (la orden queda a nivel de máquina).
    """
    import pandas as pd

    ruta = next((salida / n for n in ("equivalencias_validadas.xlsx", "equivalencias_borrador.xlsx")
                 if (salida / n).exists()), None)
    if ruta is None:
        return {}
    try:
        eq = pd.read_excel(ruta)
    except Exception:  # noqa: BLE001
        log.exception("No se pudo leer %s", ruta)
        return {}
    if "Rango" in eq.columns and "Similitud" in eq.columns:
        eq = eq[(eq["Rango"] == 1) & (eq["Similitud"] >= UMBRAL_EQUIV)]
    if "Id_tarea" not in eq.columns:
        return {}
    por_plan: dict[str, set] = defaultdict(set)
    for _, f in eq.iterrows():
        id_tarea, idc = _texto(f.get("Id_tarea")), _texto(f.get("Id_componente"))
        if id_tarea and idc:
            # Id_tarea = <Plan>-<Operacion>-<Suboperacion> (kof_planes_utils.cargar_plan)
            por_plan[id_tarea.rsplit("-", 2)[0]].add(idc)
    out = {}
    for plan, ids in por_plan.items():
        if len(ids) == 1:
            idc = next(iter(ids))
            info = catalogo.get(idc, {})
            out[plan] = {"id_componente": idc, "componente": info.get("componente"),
                         "sistema": info.get("sistema")}
        else:
            sistemas = {catalogo.get(i, {}).get("sistema") for i in ids}
            if len(sistemas) == 1 and None not in sistemas:
                out[plan] = {"id_componente": None, "componente": None, "sistema": sistemas.pop()}
    return out


def _fallas_diario(salida: Path, catalogo: dict) -> list[dict]:
    import pandas as pd

    d = _leer_csv(salida, "base_diaria_componentes.csv")
    if d is None or d.empty:
        return []
    d = d.copy()
    d["Fecha"] = pd.to_datetime(d["Fecha"], errors="coerce").dt.normalize()
    d = d[d["Fecha"].notna() & d["Maquina"].notna()]
    hora = pd.to_numeric(d["Hora"], errors="coerce").fillna(0) if "Hora" in d else 0
    d["Momento"] = d["Fecha"] + pd.to_timedelta(hora, unit="h")
    vacia = pd.Series(index=d.index, dtype=object)
    d["Comp_txt"] = d.get("Componente_catalogo", vacia).fillna(d.get("Componente", vacia))
    # Clave del componente: el código de catálogo; sin código, máquina + nombre del componente.
    sin_id = d["Id_componente"].isna() if "Id_componente" in d else pd.Series(True, index=d.index)
    d["Clave"] = d.get("Id_componente", vacia).astype(object)
    d.loc[sin_id, "Clave"] = d.loc[sin_id, "Maquina"].astype(str) + "|" + d.loc[sin_id, "Comp_txt"].astype(str)
    d = d.sort_values(["Clave", "Momento"])
    horas = d.groupby("Clave")["Momento"].diff().dt.total_seconds() / 3600
    d["Evento"] = (horas.isna() | (horas > 1)).groupby(d["Clave"]).cumsum()

    agregados = {"Maquina": ("Maquina", "first"), "Inicio": ("Momento", "min"),
                 "Minutos": ("Minutos de paro", lambda s: pd.to_numeric(s, errors="coerce").sum(min_count=1)),
                 "Turno": ("Turno", "first"), "Comp_txt": ("Comp_txt", "first")}
    if "Id_componente" in d:
        agregados["Id_componente"] = ("Id_componente", "first")
    if "Sistema_diario" in d:
        agregados["Sistema_diario"] = ("Sistema_diario", "first")
    ev = d.groupby(["Clave", "Evento"]).agg(**agregados).reset_index()

    eventos = []
    for _, f in ev.iterrows():
        idc = _texto(f.get("Id_componente"))
        info = catalogo.get(idc, {}) if idc else {}
        componente = info.get("componente") or _texto(f.get("Comp_txt"))
        minutos = _num(f.get("Minutos"))
        turno = _codigo(f.get("Turno"))
        texto_min = f"{minutos:g} min de paro" if minutos is not None else "minutos sin registrar"
        eventos.append({
            "maquina": _texto(f["Maquina"]), "fecha": f["Inicio"].date(), "tipo": "FALLA",
            "id_componente": idc, "componente": componente,
            "sistema": info.get("sistema") or _texto(f.get("Sistema_diario")),
            "titulo": f"Falla en {componente}" if componente else "Falla sin componente",
            "detalle": f"Turno {turno or '?'} · {texto_min} · Registrado en el Excel diario",
            "minutos_paro": minutos, "orden": None, "turno": turno,
            "estado": None, "clase": None, "plan": None,
        })
    return eventos


def _avisos(salida: Path, catalogo: dict) -> tuple[list[dict], dict[str, dict]]:
    """Eventos AVISO y, de paso, el componente de cada aviso (para las órdenes correctivas)."""
    av = _leer_csv(salida, "avisos_clasificados.csv")
    if av is None:
        av = _leer_csv(salida, "avisos_limpios.csv")
    if av is None:
        return [], {}
    eventos, por_aviso = [], {}
    for _, f in av.iterrows():
        if "En_alcance" in av.columns and not _verdadero(f.get("En_alcance")):
            continue
        maquina, fecha = _texto(f.get("Maquina")), _fecha(f.get("Fecha_aviso"))
        if not maquina or fecha is None:
            continue
        idc = _texto(f.get("Id_componente"))
        info = catalogo.get(idc, {}) if idc else {}
        componente = info.get("componente") or _texto(f.get("Componente_final")) or _texto(f.get("Componente"))
        sistema = info.get("sistema") or _texto(f.get("Sistema"))
        numero, orden = _codigo(f.get("Aviso")), _codigo(f.get("Orden"))
        tipo_aviso, sintoma, texto = _texto(f.get("Tipo_aviso")), _texto(f.get("Sintoma")), _texto(f.get("Texto"))
        if numero:
            por_aviso[numero] = {"id_componente": idc, "componente": componente, "sistema": sistema}
        partes = [f"Aviso {numero}" if numero else "Aviso", tipo_aviso, f"Síntoma: {sintoma}" if sintoma else None,
                  f"Vinculado a la orden {orden}" if orden else None]
        eventos.append({
            "maquina": maquina, "fecha": fecha, "tipo": "AVISO",
            "id_componente": idc, "componente": componente, "sistema": sistema,
            "titulo": texto or (f"Aviso de {tipo_aviso.lower()}" if tipo_aviso else "Aviso"),
            "detalle": " · ".join(p for p in partes if p),
            "minutos_paro": None, "orden": orden, "turno": None,
            "estado": None, "clase": tipo_aviso, "plan": None,
        })
    return eventos, por_aviso


def _ordenes(salida: Path, por_aviso: dict, por_plan: dict) -> list[dict]:
    ot = _leer_csv(salida, "ot_limpias.csv")
    if ot is None:
        return []
    eventos = []
    for _, f in ot.iterrows():
        if not _verdadero(f.get("En_alcance")):
            continue
        tipo_orden = (_texto(f.get("Tipo_orden")) or "").lower()
        if tipo_orden in ("preventiva", "predictiva"):
            tipo = "PREVENTIVO"
        elif tipo_orden in ("correctiva", "emergencia"):
            tipo = "CORRECTIVO"
        else:
            continue  # mejoras, proyectos y otras clases no son historia de fallas
        maquina, fecha = _texto(f.get("Maquina")), _fecha(f.get("Fecha_programada"))
        if not maquina or fecha is None:
            continue
        orden, plan = _codigo(f.get("Orden")), _codigo(f.get("Plan"))
        estado, texto = _texto(f.get("Estado")), _texto(f.get("Texto"))
        aviso = _codigo(f.get("Aviso"))
        comp = (por_plan.get(plan) if tipo == "PREVENTIVO" else por_aviso.get(aviso)) or {}
        componente = comp.get("componente")
        if tipo == "PREVENTIVO":
            titulo = " · ".join(p for p in (texto or "Preventivo", componente) if p)
            partes = [f"Orden {orden or '—'}", "Predictiva" if tipo_orden == "predictiva" else None,
                      f"Plan {plan}" if plan else None, f"Estado: {estado or '—'}"]
        else:
            titulo = f"Reparación de {componente}" if componente else (texto or "Orden correctiva")
            partes = [f"Orden {orden or '—'}", texto if componente else None,
                      f"Aviso {aviso}" if aviso else None, f"Estado: {estado or '—'}"]
        eventos.append({
            "maquina": maquina, "fecha": fecha, "tipo": tipo,
            "id_componente": comp.get("id_componente"), "componente": componente,
            "sistema": comp.get("sistema"),
            "titulo": titulo, "detalle": " · ".join(p for p in partes if p),
            "minutos_paro": None, "orden": orden, "turno": None,
            "estado": estado, "clase": _texto(f.get("Clase_detalle")), "plan": plan,
        })
    return eventos


def construir_eventos(salida: Path) -> list[dict]:
    """Todas las filas de historia_eventos a partir de la carpeta de salida de una corrida."""
    catalogo = _catalogo(salida)
    fallas = _fallas_diario(salida, catalogo)
    avisos, por_aviso = _avisos(salida, catalogo)
    ordenes = _ordenes(salida, por_aviso, _componentes_por_plan(salida, catalogo))
    log.info("Historia clínica: %d fallas, %d avisos, %d órdenes", len(fallas), len(avisos), len(ordenes))
    return fallas + avisos + ordenes


# =============================================================================================
# 2. Cálculos por periodo (se ejecutan en cada petición sobre historia_eventos)
# =============================================================================================
def _salida(db: Session, nombre: str) -> dict | None:
    from . import motor_service  # import tardío: motor_service importa este módulo

    return motor_service.get_salida(db, nombre)


def _hoy() -> date:
    from . import motor_service

    return motor_service.hoy_bogota()


def _en_rango(col, desde: date | None, hasta: date | None) -> list:
    filtros = []
    if desde:
        filtros.append(col >= desde)
    if hasta:
        filtros.append(col <= hasta)
    return filtros


def _fecha_iso(valor) -> date | None:
    try:
        return date.fromisoformat(str(valor)[:10]) if valor else None
    except ValueError:
        return None


def _entre(fecha: date | None, desde: date | None, hasta: date | None) -> bool:
    if fecha is None:
        return False
    return (desde is None or fecha >= desde) and (hasta is None or fecha <= hasta)


def _r(valor, dec: int = 1):
    return None if valor is None else round(float(valor), dec)


def hay_datos(db: Session) -> bool:
    return db.scalar(select(HistoriaEvento.id).limit(1)) is not None


def _fallas(db: Session, desde: date | None, hasta: date | None, maquina: str | None = None) -> list:
    filtros = [HistoriaEvento.tipo == "FALLA", *_en_rango(HistoriaEvento.fecha, desde, hasta)]
    if maquina:
        filtros.append(HistoriaEvento.maquina == maquina)
    return db.execute(select(HistoriaEvento.maquina, HistoriaEvento.fecha, HistoriaEvento.minutos_paro,
                             HistoriaEvento.componente, HistoriaEvento.id_componente,
                             HistoriaEvento.sistema).where(*filtros)).all()


def _filtrar_alcance(filas: list, sistema: str | None, componente: str | None) -> list:
    if componente:
        return [f for f in filas if f.id_componente == componente]
    if sistema:
        return [f for f in filas if f.sistema == sistema]
    return filas


def periodos(db: Session) -> dict | None:
    """Límites del filtro de periodo: {months, min, max, datos_hasta}."""
    minimo, maximo = db.execute(select(func.min(HistoriaEvento.fecha), func.max(HistoriaEvento.fecha))).one()
    if minimo is None:
        return None
    datos_hasta = db.scalar(select(func.max(HistoriaEvento.fecha)).where(HistoriaEvento.tipo == "FALLA"))
    maximo = min(maximo, _hoy())  # las órdenes programadas a futuro no amplían el calendario
    meses, y, m = [], minimo.year, minimo.month
    while (y, m) <= (maximo.year, maximo.month):
        meses.append(f"{y}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return {"months": meses, "min": minimo.isoformat(), "max": maximo.isoformat(),
            "datos_hasta": datos_hasta.isoformat() if datos_hasta else None}


def jerarquia(db: Session, maquina: str) -> list[dict] | None:
    """Sistemas y componentes de la máquina, tal como los organiza historia_clinica.json."""
    hc = _salida(db, "historia_clinica")
    if not hc:
        return None
    nodo = next((m for m in hc.get("maquinas") or [] if m.get("Maquina") == maquina), None)
    if nodo is None:
        return []
    sistemas = []
    for s in nodo.get("sistemas") or []:
        comps = sorted(({"id": c["Id_componente"], "nombre": c.get("Componente") or c["Id_componente"]}
                        for c in s.get("componentes") or [] if c.get("Id_componente")),
                       key=lambda c: c["nombre"])
        if comps:
            sistemas.append({"id": s.get("Sistema") or "Sin sistema", "nombre": s.get("Sistema") or "Sin sistema",
                             "componentes": comps})
    return sorted(sistemas, key=lambda s: s["nombre"])


def _cumplimiento(db: Session, maquina: str, desde: date | None, hasta: date | None) -> float | None:
    """% de órdenes preventivas y predictivas vencidas ejecutadas, en las semanas del periodo."""
    cump = _salida(db, "cumplimiento")
    if not cump:
        return None
    ordenes = ejecutadas = 0
    for fila in cump.get("semanal") or []:
        if fila.get("Maquina") != maquina:
            continue
        try:
            anio, semana = str(fila.get("Semana")).split("-W")
            lunes = date.fromisocalendar(int(anio), int(semana), 1)
        except (ValueError, TypeError):
            continue
        if _entre(lunes, desde, hasta):
            ordenes += fila.get("Ordenes") or 0
            ejecutadas += fila.get("Ejecutadas") or 0
    return 100 * ejecutadas / ordenes if ordenes else None


def _calcular(filas: list, dias_operados: int) -> dict:
    n = len(filas)
    minutos = sum(float(f.minutos_paro or 0) for f in filas)
    return {
        "fallas": n,
        "minutos_paro": _r(minutos),
        "mtbf_h": _r(dias_operados * 24 / n) if n and dias_operados else None,
        "mttr_min": _r(minutos / n) if n else None,
        "disponibilidad_pct": _r(max(0.0, 100 * (1 - minutos / (dias_operados * 1440)))) if dias_operados else None,
    }


def _promedio(valores: list) -> float | None:
    v = [x for x in valores if x is not None]
    return _r(sum(v) / len(v)) if v else None


def indicadores(db: Session, maquina: str, desde: date | None, hasta: date | None,
                sistema: str | None = None, componente: str | None = None) -> dict:
    """Seis indicadores del alcance pedido + promedio de las 4 máquinas en el mismo periodo."""
    todas = _fallas(db, desde, hasta)
    dias = len({f.fecha for f in todas})
    por_maquina = defaultdict(list)
    for f in todas:
        por_maquina[f.maquina].append(f)

    valores = _calcular(_filtrar_alcance(por_maquina[maquina], sistema, componente), dias)
    valores["cumplimiento_plan_pct"] = _r(_cumplimiento(db, maquina, desde, hasta))

    linea = []
    for m in MAQUINAS:
        fila = _calcular(por_maquina[m], dias)
        fila["cumplimiento_plan_pct"] = _cumplimiento(db, m, desde, hasta)
        linea.append(fila)
    promedio = {k: _promedio([fila[k] for fila in linea]) for k in valores}
    return {**valores, "dias_operados": dias,
            "alcance_cumplimiento": "maquina",  # el cumplimiento no se abre por sistema ni componente
            "promedio_linea": promedio}


def _forma_predominante(db: Session, maquina: str) -> dict:
    hc = _salida(db, "historia_clinica") or {}
    nodo = next((m for m in hc.get("maquinas") or [] if m.get("Maquina") == maquina), None)
    minutos, evaluados = Counter(), 0
    for s in (nodo or {}).get("sistemas") or []:
        for c in s.get("componentes") or []:
            forma = str(c.get("Forma_falla") or "")
            etiqueta = next((e for prefijo, e in FORMAS if forma.startswith(prefijo)), None)
            if etiqueta:
                evaluados += 1
                minutos[etiqueta] += float(c.get("Minutos_paro") or 0)
    if not minutos:
        return {"falla_predominante": POCOS_DATOS, "componentes_evaluados": 0}
    return {"falla_predominante": minutos.most_common(1)[0][0], "componentes_evaluados": evaluados}


def _pendientes(db: Session, maquina: str, desde: date | None, hasta: date | None) -> list[dict]:
    cump = _salida(db, "cumplimiento") or {}
    filas = [p for p in cump.get("pendientes") or []
             if p.get("Maquina") == maquina and _entre(_fecha_iso(p.get("Fecha_programada")), desde, hasta)]
    return sorted(filas, key=lambda p: str(p.get("Fecha_programada")), reverse=True)


def ficha(db: Session, maquina: str, desde: date | None, hasta: date | None) -> dict:
    todas = _fallas(db, desde, hasta)
    propias = [f for f in todas if f.maquina == maquina]

    conteo = Counter(f.componente or "Sin componente" for f in propias)
    principal = None
    if conteo:
        nombre, paros = conteo.most_common(1)[0]
        principal = {"nombre": nombre, "paros": paros}

    ranking = sorted(({"maquina": m, "fallas": sum(1 for f in todas if f.maquina == m)} for m in MAQUINAS),
                     key=lambda r: r["fallas"], reverse=True)
    puesto = next(i for i, r in enumerate(ranking, start=1) if r["maquina"] == maquina)

    cump = _salida(db, "cumplimiento") or {}
    planes = sum(1 for p in cump.get("planes") or [] if p.get("Maquina") == maquina)
    sin_ejecutar = sum(1 for p in _pendientes(db, maquina, desde, hasta) if p.get("Estado") == "No ejecutada")

    return {
        "maquina": maquina,
        **_forma_predominante(db, maquina),
        "componente_principal": principal,
        "planes_preventivos_vigentes": planes,
        "ordenes_sin_ejecutar": sin_ejecutar,
        "puesto_fallas": puesto,
        "es_la_mas_danada": puesto == 1 and ranking[0]["fallas"] > 0,
        "ranking_fallas": ranking,
    }


def paros_por_mes(db: Session, maquina: str, sistema: str | None = None,
                  componente: str | None = None, meses: int = 12) -> dict:
    """Últimos 12 meses con datos (no con respecto a hoy: el reporte diario va atrasado)."""
    fin = db.scalar(select(func.max(HistoriaEvento.fecha)).where(HistoriaEvento.tipo == "FALLA")) or _hoy()
    claves, y, m = [], fin.year, fin.month
    for _ in range(meses):
        claves.append(f"{y}-{m:02d}")
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    claves.reverse()
    inicio = date(int(claves[0][:4]), int(claves[0][5:]), 1)

    fallas = _filtrar_alcance(_fallas(db, inicio, fin, maquina), sistema, componente)
    filtros = [HistoriaEvento.maquina == maquina, HistoriaEvento.tipo == "PREVENTIVO",
               HistoriaEvento.estado == "Ejecutada", HistoriaEvento.fecha >= inicio, HistoriaEvento.fecha <= fin]
    if componente:
        filtros.append(HistoriaEvento.id_componente == componente)
    elif sistema:
        filtros.append(HistoriaEvento.sistema == sistema)
    preventivos = db.execute(select(HistoriaEvento.fecha, HistoriaEvento.orden, HistoriaEvento.titulo)
                             .where(*filtros).order_by(HistoriaEvento.fecha.desc())).all()

    paros, minutos, mant = Counter(), Counter(), defaultdict(list)
    for f in fallas:
        k = f.fecha.strftime("%Y-%m")
        paros[k] += 1
        minutos[k] += float(f.minutos_paro or 0)
    for p in preventivos:
        mant[p.fecha.strftime("%Y-%m")].append(p)

    serie = []
    for k in claves:
        lista = mant.get(k, [])
        serie.append({
            "mes": k, "paros": paros[k], "minutos_paro": _r(minutos[k]),
            "mantenimientos_total": len(lista),
            "mantenimientos": [{"orden": p.orden, "tarea": p.titulo, "fecha": p.fecha.isoformat()}
                               for p in lista[:MANTENIMIENTOS_POR_MES]],
        })
    return {"datos_hasta": fin.isoformat(), "meses": serie}


def resumen_pdf(db: Session, maquina: str, desde: date | None, hasta: date | None) -> dict:
    """Datos del PDF: máquina completa en el periodo (sistema/componente solo se documentan)."""
    todas = _fallas(db, desde, hasta)
    dias = len({f.fecha for f in todas})
    propias = [f for f in todas if f.maquina == maquina]
    ind = _calcular(propias, dias)

    top = defaultdict(lambda: {"veces": 0, "minutos": 0.0})
    for f in propias:
        t = top[f.componente or "Sin componente"]
        t["veces"] += 1
        t["minutos"] += float(f.minutos_paro or 0)
    top_fallas = sorted(({"componente": k, "veces": v["veces"], "minutos": _r(v["minutos"], 0)}
                         for k, v in top.items()), key=lambda t: t["veces"], reverse=True)[:TOP_FALLAS_PDF]

    avisos_falla = db.scalar(select(func.count(HistoriaEvento.id)).where(
        HistoriaEvento.maquina == maquina, HistoriaEvento.tipo == "AVISO", HistoriaEvento.clase == "Falla",
        *_en_rango(HistoriaEvento.fecha, desde, hasta))) or 0

    pendientes = _pendientes(db, maquina, desde, hasta)[:TAREAS_PDF]
    ordenes = [_codigo(p.get("Orden")) for p in pendientes]
    comp_de_orden = dict(db.execute(select(HistoriaEvento.orden, HistoriaEvento.componente).where(
        HistoriaEvento.orden.in_([o for o in ordenes if o]),
        HistoriaEvento.tipo.in_(("PREVENTIVO", "CORRECTIVO")))).all()) if any(ordenes) else {}
    tareas = [{"tarea": p.get("Texto") or "—", "componente": comp_de_orden.get(o) or "—",
               "orden": o or "—", "estado": p.get("Estado"), "fecha_programada": p.get("Fecha_programada")}
              for p, o in zip(pendientes, ordenes)]

    return {
        "maquina": maquina,
        "indicadores": {"paros": ind["fallas"], "minutos_paro": ind["minutos_paro"],
                        "mttr_min": ind["mttr_min"], "mtbf_h": ind["mtbf_h"]},
        "forma_falla": _forma_predominante(db, maquina)["falla_predominante"],
        "avisos_falla": int(avisos_falla),
        "top_fallas": top_fallas,
        "tareas_pendientes": tareas,
    }
