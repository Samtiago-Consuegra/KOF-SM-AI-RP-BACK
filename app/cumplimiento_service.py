"""Cumplimiento del plan: tareas de cada parada, estado efectivo y conteos por periodo.

Definiciones (documentarlas en el informe):
  - Programadas = tareas de las paradas cuya fecha cae en el periodo, menos las que se pasaron
    a otra parada al cerrarla ("Cumplir y reprogramar restantes"): esas cuentan como pospuestas y
    vuelven a contar como programadas en la parada nueva.
  - Ejecutadas = tareas programadas marcadas como completadas en el seguimiento.
  - Cumplimiento del plan (%) = ejecutadas / programadas.
  - Vencidas = tareas sin ejecutar de paradas cuyo fin programado ya pasó y no se cerraron.
  - Pospuestas = tareas sin ejecutar que se movieron a otra fecha dentro del periodo, ya sea al
    reprogramar una parada vencida o al cerrar una parada con tareas restantes.
  - Estado efectivo de una parada: Cumplida si se cerró; Vencida si su fin programado ya pasó sin
    cerrarse; si no, Pendiente o En proceso según el seguimiento.
"""
from __future__ import annotations

import unicodedata
from collections import defaultdict
from datetime import date, datetime, timedelta

from sqlalchemy import String, and_, cast, or_, select
from sqlalchemy.orm import Session, defer

from . import motor_service
from .constants import CRITICAL_MACHINES
from .models import Parada

MAQUINAS = list(CRITICAL_MACHINES)
CAMPOS = ("programadas", "ejecutadas", "vencidas", "pospuestas")
TIPOS = ("Preventiva", "Correctiva", "Predictiva")
_TIPO_NORMAL = {"preventiva": "Preventiva", "correctiva": "Correctiva",
                "predictiva": "Predictiva", "emergencia": "Correctiva"}


# ---------------------------------------------------------------------------------------------
# Tareas de una parada (movido desde routers/paradas.py sin cambios de lógica)
# ---------------------------------------------------------------------------------------------
def tarea_manual(parada: Parada, indice: int, tarea: dict) -> dict:
    id_tarea = tarea.get("Id_tarea") or f"MAN-{parada.id_parada}-{indice + 1}"
    return {
        "Id_tarea": id_tarea,
        "Maquina": tarea.get("Maquina"),
        "Sistema": tarea.get("Sistema"),
        "Id_componente": tarea.get("Id_componente"),
        "Componente": tarea.get("Componente"),
        "Tarea": tarea.get("Tarea"),
        "Tipo_tarea": tarea.get("Tipo_tarea") or "Preventiva",
        "Duracion_h": float(tarea.get("Duracion_h") or 0.5),
        "Origen": "Registrada por el usuario",
        "Estado": "Enviada a la parada",
    }


def con_seguimiento(tarea: dict, seguimiento: dict | None) -> dict:
    seguimiento = seguimiento or {}
    return {**tarea, "Completada": bool(seguimiento.get("completada")),
            "Comentario": seguimiento.get("comentario")}


def tareas_parada(db: Session, parada: Parada, planes: dict | None = None) -> list[dict]:
    """Tareas enviadas a la parada desde el plan semanal del motor + tareas manuales.

    `planes` (opcional) memoriza el plan semanal por semana durante una petición.
    """
    semana = motor_service.semana_iso(parada.fecha)
    if planes is None:
        contenido = motor_service.get_salida(db, "plan_semanal", semana)
    else:
        if semana not in planes:
            planes[semana] = motor_service.get_salida(db, "plan_semanal", semana)
        contenido = planes[semana]
    estado = parada.tareas_estado or {}
    tareas: list[dict] = []
    vistos: set[str] = set()

    if contenido:
        version = ((contenido.get("versiones") or {}).get("fusion") or {})
        for item in version.get("paradas") or []:
            if item.get("Id_parada") != parada.id_parada:
                continue
            for tarea in item.get("Tareas") or []:
                id_tarea = str(tarea.get("Id_tarea"))
                if not id_tarea or id_tarea in vistos:
                    continue
                # Una parada solo contiene lo que el usuario decidió.
                if tarea.get("Estado") != "Enviada a la parada":
                    continue
                vistos.add(id_tarea)
                tareas.append(con_seguimiento(tarea, estado.get(id_tarea)))

    for indice, tarea in enumerate(parada.tareas_manuales or []):
        manual = tarea_manual(parada, indice, tarea)
        id_tarea = manual["Id_tarea"]
        if id_tarea in vistos:
            for existente in tareas:
                if existente.get("Id_tarea") == id_tarea:
                    existente.update(con_seguimiento(existente, estado.get(id_tarea)))
            continue
        vistos.add(id_tarea)
        tareas.append(con_seguimiento(manual, estado.get(id_tarea)))
    return tareas


# ---------------------------------------------------------------------------------------------
# Fechas y estado efectivo
# ---------------------------------------------------------------------------------------------
def ahora_bogota() -> datetime:
    """Hora local de la planta, sin zona (se compara con fecha + HH:MM de la parada)."""
    return datetime.now(motor_service.BOGOTA).replace(tzinfo=None)


def _hora(valor: str | None) -> tuple[int, int]:
    try:
        h, m = str(valor).split(":")[:2]
        return int(h), int(m)
    except (ValueError, AttributeError):
        return 0, 0


def momentos(parada: Parada) -> tuple[datetime, datetime, datetime | None]:
    """Inicio, fin programado y fin real. Si una hora es menor que la de inicio, es del día siguiente."""
    inicio = datetime.combine(parada.fecha, datetime.min.time()).replace(
        hour=_hora(parada.inicio)[0], minute=_hora(parada.inicio)[1])
    fin = inicio.replace(hour=_hora(parada.fin)[0], minute=_hora(parada.fin)[1])
    if fin <= inicio:
        fin += timedelta(days=1)
    fin_real = None
    if parada.fin_real:
        fin_real = inicio.replace(hour=_hora(parada.fin_real)[0], minute=_hora(parada.fin_real)[1])
        if fin_real < inicio:
            fin_real += timedelta(days=1)
    return inicio, fin, fin_real


def estado_efectivo(parada: Parada, ahora: datetime | None = None) -> str:
    base = parada.estado_base or "Pendiente"
    if base == "Cumplida":
        return "Cumplida"
    _, fin, _ = momentos(parada)
    if fin < (ahora or ahora_bogota()):
        return "Vencida"
    return base


# ---------------------------------------------------------------------------------------------
# Conteos
# ---------------------------------------------------------------------------------------------
def _de_la_maquina(tarea: dict, maquina: str | None) -> bool:
    return maquina is None or tarea.get("Maquina") == maquina


def _maquinas_de(tareas: list[dict], parada: Parada) -> list[str]:
    desde_tareas = {t.get("Maquina") for t in tareas if t.get("Maquina") in MAQUINAS}
    if desde_tareas:
        return sorted(desde_tareas)
    explicitas = [m for m in parada.maquinas or [] if m in MAQUINAS]
    return MAQUINAS if "Toda la línea" in (parada.maquinas or []) else explicitas


def _entre(dia: date, desde: date | None, hasta: date | None) -> bool:
    return (desde is None or dia >= desde) and (hasta is None or dia <= hasta)


def _fecha_iso(valor) -> date | None:
    try:
        return date.fromisoformat(str(valor)[:10]) if valor else None
    except ValueError:
        return None


def _tipo_normal(valor) -> str | None:
    texto = unicodedata.normalize("NFKD", str(valor or "")).encode("ascii", "ignore").decode()
    return _TIPO_NORMAL.get(texto.strip().lower())


def tipo_de_tarea(tarea: dict) -> str:
    """Clase de la orden ("ZFI1 · Preventiva") → Tipo_tarea manual → Preventiva (planes del motor)."""
    clase = str(tarea.get("Clase_orden") or "")
    if "·" in clase:
        tipo = _tipo_normal(clase.split("·", 1)[1])
        if tipo:
            return tipo
    return _tipo_normal(tarea.get("Tipo_tarea")) or "Preventiva"


def _vacio() -> dict:
    return {c: 0 for c in CAMPOS}


def _sumar(destino: dict, origen: dict) -> None:
    for c in CAMPOS:
        destino[c] += origen[c]


def _pct(fila: dict) -> float | None:
    return round(100 * fila["ejecutadas"] / fila["programadas"], 1) if fila["programadas"] else None


def _paradas_del_rango(db: Session, desde: date | None, hasta: date | None) -> list[Parada]:
    """Paradas que pueden aportar al periodo, en una sola consulta.

    Las del rango por fecha, más las que tienen historial de reprogramaciones y se escribieron
    después de `desde` (la fecha del movimiento nunca es posterior a actualizada_en; un día de
    margen cubre UTC vs. Bogotá). Es un superconjunto: _registros aplica el filtro exacto.
    """
    consulta = select(Parada).options(defer(Parada.observaciones))
    if desde is None and hasta is None:
        return db.scalars(consulta).all()
    en_rango = and_(*([Parada.fecha >= desde] if desde else []), *([Parada.fecha <= hasta] if hasta else []))
    con_historial = cast(Parada.historial_reprogramaciones, String).notin_(["[]", "null"])
    if desde is not None:
        con_historial = and_(con_historial, Parada.actualizada_en >= desde - timedelta(days=1))
    return db.scalars(consulta.where(or_(en_rango, con_historial))).all()


def _registros(db: Session, desde: date | None, hasta: date | None, ahora: datetime):
    """Recorre las paradas y genera (fecha_del_conteo, maquina, conteos) para el periodo.

    Las pospuestas por "Reprogramar" se ubican en la fecha en que se reprogramó (la parada ya
    cambió de fecha); todo lo demás, en la fecha de la parada.
    """
    planes: dict = {}  # plan semanal por semana, una lectura por petición
    for parada in _paradas_del_rango(db, desde, hasta):
        tareas = tareas_parada(db, parada, planes)
        estado = estado_efectivo(parada, ahora)
        movida_a_otra = bool(parada.reprogramada_a)  # "Cumplir y reprogramar restantes"

        if _entre(parada.fecha, desde, hasta):
            for maquina in _maquinas_de(tareas, parada):
                fila = _vacio()
                tipos = fila["_tipos"] = dict.fromkeys(TIPOS, 0)  # fuera de CAMPOS: no se suma en filas
                for t in tareas:
                    if not _de_la_maquina(t, maquina):
                        continue
                    if t.get("Completada"):
                        fila["programadas"] += 1
                        fila["ejecutadas"] += 1
                        tipos[tipo_de_tarea(t)] += 1
                    elif movida_a_otra:
                        fila["pospuestas"] += 1
                    else:
                        fila["programadas"] += 1
                        tipos[tipo_de_tarea(t)] += 1
                        if estado == "Vencida":
                            fila["vencidas"] += 1
                yield parada.fecha, maquina, fila

        for entrada in parada.historial_reprogramaciones or []:
            if entrada.get("motivo") == "tareas-restantes":
                continue  # ya contadas arriba, en la parada original
            dia = _fecha_iso(entrada.get("reprogramada_en"))
            if dia is None or not _entre(dia, desde, hasta):
                continue
            por_maquina = entrada.get("tareas_pendientes_por_maquina") or {}
            for maquina in _maquinas_de(tareas, parada):
                fila = _vacio()
                fila["pospuestas"] = int(por_maquina.get(maquina, 0)) if por_maquina else sum(
                    1 for t in tareas if _de_la_maquina(t, maquina) and not t.get("Completada"))
                if fila["pospuestas"]:
                    yield dia, maquina, fila


def resumen(db: Session, desde: date | None, hasta: date | None, maquina: str | None = None,
            ahora: datetime | None = None) -> dict:
    total = _vacio()
    for _, m, fila in _registros(db, desde, hasta, ahora or ahora_bogota()):
        if maquina is None or m == maquina:
            _sumar(total, fila)
    return {**total, "cumplimiento_pct": _pct(total)}


def por_maquina_y_semana(db: Session, desde: date | None, hasta: date | None, maquina: str | None = None,
                         agrupar: str = "maquina", ahora: datetime | None = None,
                         por_tipo: dict | None = None) -> list[dict]:
    """Filas para la gráfica: una por máquina (agrupar='maquina') o por semana y máquina ('semana').

    Si se pasa `por_tipo`, acumula ahí las programadas por tipo (mismo recorrido, sin consultas extra).
    """
    grupos: dict[tuple, dict] = defaultdict(_vacio)
    maquinas = [maquina] if maquina else MAQUINAS
    if por_tipo is not None:
        for tipo in TIPOS:
            por_tipo.setdefault(tipo, 0)
    for dia, m, fila in _registros(db, desde, hasta, ahora or ahora_bogota()):
        if m not in maquinas:
            continue
        if por_tipo is not None:
            for tipo, n in fila.get("_tipos", {}).items():
                por_tipo[tipo] += n
        clave = (motor_service.semana_iso(dia) if agrupar == "semana" else None, m)
        _sumar(grupos[clave], fila)

    semanas = sorted({k[0] for k in grupos}) if agrupar == "semana" else [None]
    filas = []
    for semana in semanas:
        for m in maquinas:  # todas las máquinas aparecen, aunque no tengan tareas
            fila = grupos.get((semana, m), _vacio())
            filas.append({
                "semana": semana,
                "etiqueta": f"Sem {int(semana.split('-W')[1])}" if semana else None,
                "maquina": m, **fila, "cumplimiento_pct": _pct(fila),
            })
    return filas


def pendientes_por_maquina(tareas: list[dict]) -> dict[str, int]:
    """Cuántas tareas sin ejecutar tiene cada máquina (se guarda en el historial al reprogramar)."""
    conteo: dict[str, int] = defaultdict(int)
    for t in tareas:
        if not t.get("Completada"):
            conteo[t.get("Maquina") or "Sin máquina"] += 1
    return dict(conteo)


def avance(tareas: list[dict]) -> int:
    if not tareas:
        return 100
    return round(100 * sum(1 for t in tareas if t.get("Completada")) / len(tareas))
