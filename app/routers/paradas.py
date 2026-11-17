"""Paradas programadas y su seguimiento para la pantalla Cumplimiento."""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import cumplimiento_service as cs
from .. import motor_service
from ..db import get_db
from ..deps import get_current_user
from ..models import DecisionTarea, Parada, User

router = APIRouter(prefix="/paradas", tags=["paradas"])

HORA_RE = re.compile(r"^\d{2}:\d{2}$")
MODALIDADES = {"Frío", "Caliente"}


class TareaManualIn(BaseModel):
    Maquina: str
    Sistema: str | None = None
    Id_componente: str | None = None
    Componente: str | None = None
    Tipo_tarea: Literal["Preventiva", "Predictiva", "Correctiva"] = "Preventiva"
    Tarea: str
    Duracion_h: float | None = 0.5


class ParadaIn(BaseModel):
    Titulo: str = Field(min_length=1, max_length=255)
    Modalidad: Literal["Frío", "Caliente"]
    Fecha: date
    Inicio: str
    Fin: str
    Maquinas: list[str] = Field(default_factory=list)
    Tareas_manuales: list[TareaManualIn] = Field(default_factory=list)


class ParadaUpdate(BaseModel):
    Titulo: str | None = Field(default=None, min_length=1, max_length=255)
    Modalidad: Literal["Frío", "Caliente"] | None = None
    Fecha: date | None = None
    Inicio: str | None = None
    Fin: str | None = None
    Maquinas: list[str] | None = None
    Tareas_manuales: list[TareaManualIn] | None = None


class SeguimientoIn(BaseModel):
    tareas_estado: dict[str, dict] | None = None
    observaciones: str | None = None
    estado_base: Literal["Pendiente", "En proceso", "Cumplida"] | None = None
    fin_real: str | None = None


class CumplirIn(BaseModel):
    fin_real: str | None = None
    observaciones: str | None = None
    tareas_estado: dict[str, dict] | None = None


class CumplirReprogramarIn(BaseModel):
    Fecha: date
    Inicio: str
    Fin: str
    Titulo: str | None = None
    Modalidad: Literal["Frío", "Caliente"] | None = None
    observaciones: str | None = None
    tareas_estado: dict[str, dict] | None = None


class ReprogramarIn(BaseModel):
    Fecha: date
    Inicio: str
    Fin: str
    motivo: str | None = None


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------
def _ahora_hora() -> str:
    return datetime.now(motor_service.BOGOTA).strftime("%H:%M")


def _validar_horas(inicio: str, fin: str) -> None:
    if not HORA_RE.match(inicio or "") or not HORA_RE.match(fin or ""):
        raise HTTPException(status_code=400, detail="Inicio y Fin deben tener formato HH:MM")


def _nuevo_id(db: Session, fecha: date) -> str:
    prefijo = f"PAR-{fecha.isoformat()}-"
    existentes = db.scalars(select(Parada.id_parada).where(
        Parada.id_parada.like(f"{prefijo}%"))).all()
    numeros = []
    for valor in existentes:
        try:
            numeros.append(int(valor.rsplit("-", 1)[1]))
        except (ValueError, IndexError):
            continue
    return f"{prefijo}{max(numeros, default=0) + 1:02d}"


def _parada_dict(parada: Parada, tareas: list[dict] | None = None) -> dict:
    datos = {
        "id_parada": parada.id_parada,
        "titulo": parada.titulo,
        "modalidad": parada.modalidad,
        "fecha": parada.fecha.isoformat(),
        "inicio": parada.inicio,
        "fin": parada.fin,
        "maquinas": parada.maquinas or [],
        "tareas_manuales": parada.tareas_manuales or [],
        "creada_por": parada.creada_por,
        "creada_en": parada.creada_en.isoformat() if parada.creada_en else None,
        "actualizada_en": parada.actualizada_en.isoformat() if parada.actualizada_en else None,
        "estado_base": parada.estado_base,
        "fin_real": parada.fin_real,
        "tareas_estado": parada.tareas_estado or {},
        "observaciones": parada.observaciones,
        "historial_reprogramaciones": parada.historial_reprogramaciones or [],
        "parada_original_id": parada.parada_original_id,
        "reprogramada_a": parada.reprogramada_a,
        "reprogramada_a_fecha": parada.reprogramada_a_fecha.isoformat() if parada.reprogramada_a_fecha else None,
        "cumplida_parcial": bool(parada.cumplida_parcial),
        "avance_al_cumplir": parada.avance_al_cumplir,
    }
    inicio, fin, fin_real = cs.momentos(parada)
    datos["estado"] = cs.estado_efectivo(parada)          # Pendiente | En proceso | Vencida | Cumplida
    datos["inicio_iso"] = inicio.isoformat()
    datos["fin_iso"] = fin.isoformat()
    datos["fin_real_iso"] = fin_real.isoformat() if fin_real else None
    if tareas is not None:
        datos["tareas"] = tareas
    return datos


# Movidas a cumplimiento_service (también las usa el cálculo del cumplimiento).
_tareas_parada = cs.tareas_parada


def _avance(tareas: list[dict], estado: dict) -> int:
    if not tareas:
        return 100
    completas = sum(1 for t in tareas if (estado.get(str(t.get("Id_tarea"))) or {}).get("completada"))
    return round(100 * completas / len(tareas))


def _fusionar_estado(actual: dict | None, nuevo: dict | None) -> dict:
    resultado = dict(actual or {})
    for id_tarea, valor in (nuevo or {}).items():
        previo = dict(resultado.get(id_tarea) or {})
        previo["completada"] = bool(valor.get("completada", previo.get("completada", False)))
        if "comentario" in valor:
            previo["comentario"] = valor.get("comentario")
        resultado[id_tarea] = previo
    return resultado


def _encolar_semanas(db: Session, semanas: list[str], trigger: str) -> list[int]:
    ids = []
    for semana in sorted({s for s in semanas if s}):
        run = motor_service.encolar(db, trigger=trigger, fases=motor_service.FASES_PARADAS, semana=semana)
        ids.append(run.id)
    return ids


def _normalizar_estado(valor: str | None) -> str | None:
    if not valor:
        return None
    mapa = {"pendiente": "Pendiente", "en_proceso": "En proceso", "en proceso": "En proceso",
            "en ejecución": "En proceso", "en ejecucion": "En proceso", "cumplida": "Cumplida",
            "vencida": "Vencida"}
    return mapa.get(valor.strip().lower(), valor.strip())


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------
@router.get("")
def listar_paradas(
    desde: date | None = Query(None),
    hasta: date | None = Query(None),
    maquina: str | None = Query(None),
    estado: str | None = Query(None, description="Estado efectivo: Pendiente, En proceso, Vencida o Cumplida"),
    incluir_tareas: bool = Query(False, description="Agrega las tareas de cada parada (tabla de Cumplimiento)"),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    consulta = select(Parada)
    if desde:
        consulta = consulta.where(Parada.fecha >= desde)
    if hasta:
        consulta = consulta.where(Parada.fecha <= hasta)
    paradas = db.scalars(consulta.order_by(Parada.fecha, Parada.inicio)).all()
    if maquina:
        paradas = [p for p in paradas if maquina in (p.maquinas or []) or "Toda la línea" in (p.maquinas or [])]
    # El estado se filtra sobre el estado efectivo: "Vencida" no se guarda, se calcula con la hora actual.
    estado_buscado = _normalizar_estado(estado)
    if estado_buscado:
        ahora = cs.ahora_bogota()
        paradas = [p for p in paradas if cs.estado_efectivo(p, ahora) == estado_buscado]
    return [_parada_dict(p, _tareas_parada(db, p) if incluir_tareas else None) for p in paradas]


@router.get("/{id_parada}")
def obtener_parada(id_parada: str, db: Session = Depends(get_db),
                   _: User = Depends(get_current_user)):
    parada = db.get(Parada, id_parada)
    if parada is None:
        raise HTTPException(status_code=404, detail="Parada no encontrada")
    return _parada_dict(parada, _tareas_parada(db, parada))


@router.post("")
def crear_parada(
    body: ParadaIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _validar_horas(body.Inicio, body.Fin)
    if not body.Maquinas:
        raise HTTPException(status_code=400, detail="Indica al menos una máquina")
    if body.Modalidad not in MODALIDADES:
        raise HTTPException(status_code=400, detail="Modalidad debe ser Frío o Caliente")

    parada = Parada(
        id_parada=_nuevo_id(db, body.Fecha),
        titulo=body.Titulo,
        modalidad=body.Modalidad,
        fecha=body.Fecha,
        inicio=body.Inicio,
        fin=body.Fin,
        maquinas=body.Maquinas,
        tareas_manuales=[t.model_dump() for t in body.Tareas_manuales],
        creada_por=user.email,
        estado_base="Pendiente",
        tareas_estado={},
        historial_reprogramaciones=[],
        cumplida_parcial=False,
    )
    db.add(parada)
    db.commit()
    db.refresh(parada)
    run_ids = _encolar_semanas(db, [motor_service.semana_iso(parada.fecha)], "parada")
    return {**_parada_dict(parada, _tareas_parada(db, parada)), "run_ids": run_ids}


@router.put("/{id_parada}")
def editar_parada(
    id_parada: str,
    body: ParadaUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    parada = db.get(Parada, id_parada)
    if parada is None:
        raise HTTPException(status_code=404, detail="Parada no encontrada")

    datos = body.model_dump(exclude_unset=True)
    inicio = datos.get("Inicio", parada.inicio)
    fin = datos.get("Fin", parada.fin)
    _validar_horas(inicio, fin)
    if "Maquinas" in datos and not datos["Maquinas"]:
        raise HTTPException(status_code=400, detail="Indica al menos una máquina")

    semana_anterior = motor_service.semana_iso(parada.fecha)
    for campo in ("Titulo", "Modalidad", "Fecha", "Inicio", "Fin", "Maquinas"):
        if campo in datos and datos[campo] is not None:
            setattr(parada, campo, datos[campo])
    if "Tareas_manuales" in datos and datos["Tareas_manuales"] is not None:
        parada.tareas_manuales = datos["Tareas_manuales"]
    db.commit()
    db.refresh(parada)

    semana_nueva = motor_service.semana_iso(parada.fecha)
    run_ids = _encolar_semanas(db, [semana_anterior, semana_nueva], "parada")
    return {**_parada_dict(parada, _tareas_parada(db, parada)), "run_ids": run_ids}


@router.delete("/{id_parada}")
def borrar_parada(id_parada: str, db: Session = Depends(get_db),
                  _: User = Depends(get_current_user)):
    parada = db.get(Parada, id_parada)
    if parada is None:
        raise HTTPException(status_code=404, detail="Parada no encontrada")
    semana = motor_service.semana_iso(parada.fecha)
    db.query(DecisionTarea).filter(DecisionTarea.id_parada == id_parada).update(
        {DecisionTarea.id_parada: None})
    db.delete(parada)
    db.commit()
    run_ids = _encolar_semanas(db, [semana], "parada")
    return {"deleted": True, "id_parada": id_parada, "run_ids": run_ids}


# ---------------------------------------------------------------------------
# Seguimiento (Cumplimiento)
# ---------------------------------------------------------------------------
@router.post("/{id_parada}/seguimiento")
def seguimiento(
    id_parada: str,
    body: SeguimientoIn,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    parada = db.get(Parada, id_parada)
    if parada is None:
        raise HTTPException(status_code=404, detail="Parada no encontrada")

    if cs.estado_efectivo(parada) == "Vencida":
        raise HTTPException(status_code=400, detail="Las paradas vencidas solo se pueden reprogramar")
    if body.fin_real is not None and body.fin_real and not HORA_RE.match(body.fin_real):
        raise HTTPException(status_code=400, detail="fin_real debe tener formato HH:MM")
    parada.tareas_estado = _fusionar_estado(parada.tareas_estado, body.tareas_estado)
    if body.observaciones is not None:
        parada.observaciones = body.observaciones
    if body.estado_base is not None:
        parada.estado_base = body.estado_base
    if body.fin_real is not None:
        parada.fin_real = body.fin_real or None
    db.commit()
    db.refresh(parada)
    run_ids = _encolar_semanas(db, [motor_service.semana_iso(parada.fecha)], "seguimiento")
    return {**_parada_dict(parada, _tareas_parada(db, parada)), "run_ids": run_ids}


@router.post("/{id_parada}/cumplir")
def cumplir(
    id_parada: str,
    body: CumplirIn,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    parada = db.get(Parada, id_parada)
    if parada is None:
        raise HTTPException(status_code=404, detail="Parada no encontrada")

    if body.fin_real and not HORA_RE.match(body.fin_real):
        raise HTTPException(status_code=400, detail="fin_real debe tener formato HH:MM")
    tareas = _tareas_parada(db, parada)
    estado = _fusionar_estado(parada.tareas_estado, body.tareas_estado)
    if body.tareas_estado is None:
        # Sin detalle de tareas: cerrar la parada equivale a ejecutarlas todas (comportamiento anterior).
        for tarea in tareas:
            id_tarea = str(tarea.get("Id_tarea"))
            estado[id_tarea] = {**(estado.get(id_tarea) or {}), "completada": True}
    # Con detalle: se respeta lo marcado; si quedan tareas sin hacer, queda "cumplida parcial".
    parada.tareas_estado = estado
    avance = _avance(tareas, estado)
    if body.observaciones is not None:
        parada.observaciones = body.observaciones
    parada.estado_base = "Cumplida"
    parada.fin_real = body.fin_real or _ahora_hora()
    parada.cumplida_parcial = avance < 100
    parada.avance_al_cumplir = avance
    db.commit()
    db.refresh(parada)
    run_ids = _encolar_semanas(db, [motor_service.semana_iso(parada.fecha)], "cumplir")
    return {**_parada_dict(parada, _tareas_parada(db, parada)), "run_ids": run_ids}


@router.post("/{id_parada}/cumplir-y-reprogramar")
def cumplir_y_reprogramar(
    id_parada: str,
    body: CumplirReprogramarIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    parada = db.get(Parada, id_parada)
    if parada is None:
        raise HTTPException(status_code=404, detail="Parada no encontrada")
    _validar_horas(body.Inicio, body.Fin)

    tareas = _tareas_parada(db, parada)
    estado = _fusionar_estado(parada.tareas_estado, body.tareas_estado)
    pendientes = []
    for tarea in tareas:
        id_tarea = str(tarea.get("Id_tarea"))
        if (estado.get(id_tarea) or {}).get("completada"):
            continue
        pendientes.append({
            "Id_tarea": id_tarea,
            "Maquina": tarea.get("Maquina"),
            "Sistema": tarea.get("Sistema"),
            "Id_componente": tarea.get("Id_componente"),
            "Componente": tarea.get("Componente"),
            "Tipo_tarea": tarea.get("Tipo_tarea") or "Preventiva",
            "Tarea": tarea.get("Tarea"),
            "Duracion_h": float(tarea.get("Duracion_h") or 0.5),
        })

    semana_anterior = motor_service.semana_iso(parada.fecha)
    avance = _avance(tareas, estado)
    parada.tareas_estado = estado
    parada.estado_base = "Cumplida"
    parada.fin_real = _ahora_hora()
    # Las restantes pasan a la parada nueva: esta queda cumplida con lo que se hizo.
    parada.cumplida_parcial = False
    parada.avance_al_cumplir = avance
    if body.observaciones is not None:
        parada.observaciones = body.observaciones

    if not pendientes:  # nada que reprogramar: es un cierre normal
        db.commit()
        db.refresh(parada)
        run_ids = _encolar_semanas(db, [semana_anterior], "cumplir")
        return {"parada_original": _parada_dict(parada, _tareas_parada(db, parada)),
                "parada_nueva": None, "run_ids": run_ids}

    historial = {
        "fecha_anterior": parada.fecha.isoformat(), "inicio_anterior": parada.inicio, "fin_anterior": parada.fin,
        "fecha_nueva": body.Fecha.isoformat(), "inicio_nuevo": body.Inicio, "fin_nuevo": body.Fin,
        "motivo": "tareas-restantes", "avance": avance, "tareas_pendientes": len(pendientes),
        "reprogramada_en": datetime.now(motor_service.BOGOTA).isoformat(), "reprogramada_por": user.email,
    }
    parada.historial_reprogramaciones = list(parada.historial_reprogramaciones or []) + [historial]

    nueva = Parada(
        id_parada=_nuevo_id(db, body.Fecha),
        titulo=body.Titulo or f"{parada.titulo} (reprogramada)",
        modalidad=body.Modalidad or parada.modalidad,
        fecha=body.Fecha,
        inicio=body.Inicio,
        fin=body.Fin,
        maquinas=parada.maquinas or [],
        tareas_manuales=pendientes,
        creada_por=user.email,
        estado_base="Pendiente",
        tareas_estado={},
        historial_reprogramaciones=[historial],
        parada_original_id=parada.id_parada,
        cumplida_parcial=False,
    )
    db.add(nueva)
    db.flush()
    parada.reprogramada_a = nueva.id_parada
    parada.reprogramada_a_fecha = nueva.fecha
    db.commit()
    db.refresh(parada)
    db.refresh(nueva)

    run_ids = _encolar_semanas(
        db, [semana_anterior, motor_service.semana_iso(nueva.fecha)], "reprogramar")
    return {
        "parada_original": _parada_dict(parada, _tareas_parada(db, parada)),
        "parada_nueva": _parada_dict(nueva, _tareas_parada(db, nueva)),
        "run_ids": run_ids,
    }


@router.post("/{id_parada}/reprogramar")
def reprogramar(
    id_parada: str,
    body: ReprogramarIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    parada = db.get(Parada, id_parada)
    if parada is None:
        raise HTTPException(status_code=404, detail="Parada no encontrada")
    _validar_horas(body.Inicio, body.Fin)

    anterior = {"fecha": parada.fecha.isoformat(), "inicio": parada.inicio, "fin": parada.fin}
    semana_anterior = motor_service.semana_iso(parada.fecha)
    tareas = _tareas_parada(db, parada)
    parada.historial_reprogramaciones = list(parada.historial_reprogramaciones or []) + [{
        "fecha_anterior": anterior["fecha"],
        "inicio_anterior": anterior["inicio"],
        "fin_anterior": anterior["fin"],
        "fecha_nueva": body.Fecha.isoformat(),
        "inicio_nuevo": body.Inicio,
        "fin_nuevo": body.Fin,
        "motivo": body.motivo,
        "avance": cs.avance(tareas),
        "tareas_pendientes": sum(1 for t in tareas if not t.get("Completada")),
        "tareas_pendientes_por_maquina": cs.pendientes_por_maquina(tareas),  # cuentan como pospuestas
        "reprogramada_en": datetime.now(motor_service.BOGOTA).isoformat(),
        "reprogramada_por": user.email,
    }]
    parada.fecha = body.Fecha
    parada.inicio = body.Inicio
    parada.fin = body.Fin
    # La parada vuelve a quedar abierta en su nueva fecha.
    if parada.estado_base != "Cumplida":
        parada.estado_base = "Pendiente"
        parada.fin_real = None
    db.commit()
    db.refresh(parada)

    semana_nueva = motor_service.semana_iso(parada.fecha)
    run_ids = _encolar_semanas(db, [semana_anterior, semana_nueva], "reprogramar")
    return {**_parada_dict(parada, _tareas_parada(db, parada)), "run_ids": run_ids}
