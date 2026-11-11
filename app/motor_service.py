"""Servicio del motor de planes de mantenimiento.

Responsabilidades:
  - Armar la carpeta temporal de entrada/salida de cada corrida.
  - Ejecutar el motor SIEMPRE en un subproceso (app.motor_worker) y leer su resumen.
  - Fusionar peticiones en una sola corrida (unión de fases y semanas).
  - Guardar las salidas de forma transaccional, reconstruir HistoriaEvento y crear snapshots.
  - Servir las salidas con una caché en memoria invalidada al guardar.

Nada de este módulo modifica motor_planes/motor/ ni kof_engine/.
"""
from __future__ import annotations

import io
import json
import logging
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
from collections import OrderedDict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .db import SessionLocal
from .models import (DecisionTarea, HistoriaEvento, MotorArchivo, MotorRun, MotorSalida,
                     MotorSnapshot, Parada)
from .prediction_service import _engine_lock

log = logging.getLogger("kof.motor")

RAIZ = Path(__file__).resolve().parent.parent
MOTOR_WORKER = ["-m", "app.motor_worker"]
BOGOTA = timezone(timedelta(hours=-5))

FASES_COMPLETA = [12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23]
FASES_ACR = [21, 22, 23]
FASES_SEMANA = [19, 20, 23]
FASES_PARADAS = [20, 23]

CONJUNTOS = {
    "completa": FASES_COMPLETA,
    "acr": FASES_ACR,
    "semana": FASES_SEMANA,
    "paradas": FASES_PARADAS,
}

# Salidas JSON del motor. alertas y plan_semanal se guardan por semana; el resto con semana "".
SALIDAS_SEMANA = {"alertas", "plan_semanal"}
SALIDAS_JSON = {
    "catalogo.json": "catalogo",
    "historia_clinica.json": "historia_clinica",
    "historia_clinica_acr.json": "historia_clinica_acr",
    "cumplimiento.json": "cumplimiento",
    "plan_recomendado.json": "plan_recomendado",
    "alertas.json": "alertas",
    "plan_semanal.json": "plan_semanal",
}
SALIDA_EXCEL = "plan_semanal_xlsx"

TIPOS_ARCHIVO = {
    "avisos", "ordenes", "plan", "consolidado", "acr", "acr_revisado", "equivalencias",
}
TIPOS_COMPLETA = {"avisos", "ordenes", "plan", "equivalencias"}
TIPOS_ACR = {"consolidado", "acr", "acr_revisado"}

SEMANA_RE = re.compile(r"^\d{4}-W\d{2}$")
ACR_RE = re.compile(r"^\d{2}-\d{2}-\d{4}_.+\.xlsx?$", re.IGNORECASE)

CACHE_MAX = 32
_CACHE_LOCK = threading.RLock()
_SALIDA_CACHE: "OrderedDict[tuple, dict]" = OrderedDict()

# ---------------------------------------------------------------------------
# Cola: un hilo daemon, peticiones fusionadas
# ---------------------------------------------------------------------------
_LOCK = threading.RLock()
_COND = threading.Condition(_LOCK)
_PENDING: dict | None = None
_WORKER_STARTED = False


# ---------------------------------------------------------------------------
# Fechas y semanas (America/Bogota = UTC-5, sin horario de verano)
# ---------------------------------------------------------------------------
def hoy_bogota() -> date:
    return datetime.now(BOGOTA).date()


def fecha_ref_hoy() -> str:
    return hoy_bogota().isoformat()


def semana_iso(valor: date | datetime) -> str:
    if isinstance(valor, datetime):
        valor = valor.date()
    iso = valor.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def semana_actual() -> str:
    return semana_iso(hoy_bogota())


def normalizar_semana(semana: str | None) -> str:
    """Valida 'AAAA-Wnn' y, si viene vacía, usa la semana ISO actual."""
    if not semana:
        return semana_actual()
    semana = semana.strip().upper()
    if not SEMANA_RE.match(semana):
        raise ValueError("La semana debe tener el formato AAAA-Wnn")
    try:
        date.fromisocalendar(int(semana[:4]), int(semana[-2:]), 1)
    except ValueError as exc:
        raise ValueError(f"Semana ISO inválida: {semana}") from exc
    return semana


def normalizar_fecha_ref(fecha: str | None) -> str:
    if not fecha:
        return fecha_ref_hoy()
    return date.fromisoformat(fecha).isoformat()


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------
def run_to_dict(run: MotorRun | None) -> dict | None:
    if run is None:
        return None
    return {
        "id": run.id,
        "status": run.status,
        "trigger": run.trigger,
        "fases": run.fases or [],
        "semana": run.semana,
        "fecha_ref": run.fecha_ref.isoformat() if run.fecha_ref else None,
        "creado_en": run.creado_en.isoformat() if run.creado_en else None,
        "iniciado_en": run.iniciado_en.isoformat() if run.iniciado_en else None,
        "terminado_en": run.terminado_en.isoformat() if run.terminado_en else None,
        "error": run.error,
        "resumen": run.resumen,
    }


def encolar(db: Session, trigger: str, fases: list[int], semana: str | None = None,
            fecha_ref: str | None = None, iniciar: bool = True) -> MotorRun:
    """Crea un MotorRun en_cola y lo suma al lote pendiente del worker."""
    semana = normalizar_semana(semana)
    fecha_ref = normalizar_fecha_ref(fecha_ref)
    fases = sorted({int(f) for f in fases})

    run = MotorRun(status="en_cola", trigger=trigger, fases=fases, semana=semana,
                   fecha_ref=date.fromisoformat(fecha_ref))
    db.add(run)
    db.commit()
    db.refresh(run)

    _acumular(run.id, fases, semana)
    if iniciar:
        start_worker()
    return run


def _acumular(run_id: int, fases: list[int], semana: str) -> None:
    global _PENDING
    with _COND:
        _PENDING = _fusionar(_PENDING, fases, semana)
        _PENDING["runs"].append({"id": run_id, "fases": sorted(set(fases)), "semana": semana})
        _COND.notify()


def _fusionar(pendiente: dict | None, fases: list[int], semana: str) -> dict:
    """Fusión pura de una petición con el lote pendiente (expuesta para el test)."""
    if pendiente is None:
        pendiente = {"fases": set(), "semanas": set(), "runs": []}
    pendiente.setdefault("fases", set()).update(fases)
    pendiente.setdefault("semanas", set()).add(semana)
    pendiente.setdefault("runs", [])
    return pendiente


def start_worker() -> None:
    global _WORKER_STARTED
    with _LOCK:
        if _WORKER_STARTED:
            return
        _WORKER_STARTED = True
        hilo = threading.Thread(target=_worker_loop, name="kof-motor", daemon=True)
        hilo.start()


def _worker_loop() -> None:
    global _PENDING
    while True:
        with _COND:
            while _PENDING is None:
                _COND.wait()
            lote = _PENDING
            _PENDING = None
        try:
            _procesar_lote(lote)
        except Exception:  # noqa: BLE001 - el hilo no debe morir por una corrida fallida
            log.exception("Fallo inesperado al procesar el lote del motor")


def mark_interrupted_runs() -> int:
    """en_proceso quedaron a medias: se marcan error. Los en_cola se reencolan al arrancar."""
    with SessionLocal() as db:
        runs = db.scalars(select(MotorRun).where(MotorRun.status == "en_proceso")).all()
        for run in runs:
            run.status = "error"
            run.error = "El servidor se reinició mientras la corrida estaba en proceso"
            run.terminado_en = datetime.now(timezone.utc)
        db.commit()
        return len(runs)


def requeue_pending_runs() -> int:
    """Carga en la cola los runs que quedaron en_cola (p. ej. creados por el script local)."""
    with SessionLocal() as db:
        runs = db.scalars(select(MotorRun).where(MotorRun.status == "en_cola")
                          .order_by(MotorRun.id)).all()
        for run in runs:
            _acumular(run.id, list(run.fases or []), run.semana or semana_actual())
        return len(runs)


def _procesar_lote(lote: dict) -> None:
    run_ids = [r["id"] for r in lote["runs"]]
    semanas = sorted(lote["semanas"])
    fases = sorted(lote["fases"])
    if not run_ids:
        return

    # Un solo entrenamiento/corrida a la vez en la instancia (512 MB en Render).
    with _engine_lock:
        tmp = Path(tempfile.mkdtemp(prefix="kof-motor-"))
        entrada = tmp / "entrada"
        salida = tmp / "salida"
        capturas = tmp / "capturas"
        entrada.mkdir(parents=True, exist_ok=True)
        salida.mkdir(parents=True, exist_ok=True)
        capturas.mkdir(parents=True, exist_ok=True)
        db = SessionLocal()
        try:
            runs = db.scalars(select(MotorRun).where(MotorRun.id.in_(run_ids))).all()
            ahora = datetime.now(timezone.utc)
            for run in runs:
                run.status = "en_proceso"
                run.iniciado_en = ahora
                run.error = None
            db.commit()

            resumen_fases: list[dict] = []
            with _entrada_armada(db, entrada, salida, fases) as fases_efectivas:
                fases = fases_efectivas
                for indice, semana in enumerate(semanas):
                    fases_semana = fases
                    if indice > 0 and 12 in fases and len(fases) > 1:
                        # La fase 12 es independiente de la semana: se corre una vez por lote.
                        fases_semana = [f for f in fases if f != 12]
                    resumen = _run_subprocess(entrada, salida, semana, fecha_ref_hoy(), fases_semana)
                    resumen_fases.append({"semana": semana, **resumen})
                    if not resumen.get("ok"):
                        raise RuntimeError(resumen.get("error") or f"El motor falló en {semana}")
                    _capturar_semana(salida, capturas / semana, semana, fases_semana)

            _guardar_salidas(db, run_ids[-1], salida, capturas, semanas, fases)
            fin = datetime.now(timezone.utc)
            for run in runs:
                run.status = "ok"
                run.terminado_en = fin
                run.error = None
                run.fases = fases
                run.resumen = {
                    **(run.resumen or {}),
                    "ok": True,
                    "semanas": semanas,
                    "fases": resumen_fases,
                    "archivos": sorted(p.name for p in salida.iterdir() if p.is_file()),
                }
            db.commit()
            invalidate_cache()
            log.info("Motor OK: runs=%s semanas=%s fases=%s", run_ids, semanas, fases)
        except Exception as exc:  # noqa: BLE001
            log.exception("Motor con error: %s", exc)
            db.rollback()
            try:
                runs = db.scalars(select(MotorRun).where(MotorRun.id.in_(run_ids))).all()
                fin = datetime.now(timezone.utc)
                for run in runs:
                    run.status = "error"
                    run.terminado_en = fin
                    run.error = str(exc)[:4000]
                db.commit()
            except Exception:  # noqa: BLE001
                db.rollback()
                log.exception("No se pudo marcar el run del motor como error")
        finally:
            db.close()
            shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Subproceso
# ---------------------------------------------------------------------------
def _run_subprocess(entrada: Path, salida: Path, semana: str, fecha_ref: str,
                    fases: list[int]) -> dict:
    """Lanza app.motor_worker y devuelve el JSON de su última línea."""
    cmd = [
        sys.executable, *MOTOR_WORKER,
        "--entrada", str(entrada),
        "--salida", str(salida),
        "--semana", semana,
        "--fecha-ref", fecha_ref,
        "--fases", ",".join(str(f) for f in fases),
    ]
    try:
        proceso = subprocess.run(cmd, cwd=str(RAIZ), capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("El motor superó el límite de 600 segundos") from exc

    resumen: dict | None = None
    for linea in reversed((proceso.stdout or "").splitlines()):
        linea = linea.strip()
        if not linea:
            continue
        try:
            candidato = json.loads(linea)
        except json.JSONDecodeError:
            continue
        if isinstance(candidato, dict) and "ok" in candidato:
            resumen = candidato
            break

    if resumen is None:
        detalle = (proceso.stderr or proceso.stdout or "").strip()[-2000:]
        raise RuntimeError(f"El motor no devolvió un resumen válido. {detalle}")
    if proceso.returncode != 0 or not resumen.get("ok"):
        raise RuntimeError(resumen.get("error") or (proceso.stderr or "")[-2000:] or "Error del motor")
    return resumen


# ---------------------------------------------------------------------------
# Armado de entradas
# ---------------------------------------------------------------------------
def _entrada_armada(db: Session, entrada: Path, salida: Path, fases: list[int]) -> "_EntradaArmada":
    return _EntradaArmada(db, entrada, salida, fases)


class _EntradaArmada:
    def __init__(self, db: Session, entrada: Path, salida: Path, fases: list[int]):
        self.db = db
        self.entrada = entrada
        self.salida = salida
        self.fases = list(fases)

    def __enter__(self) -> list[int]:
        self._copiar_archivos()
        _build_excel_diario(self.db, self.entrada / "BD_Reporte_SAP_desde_bd.xlsx")
        (self.entrada / "paradas.json").write_text(
            json.dumps(_build_paradas_json(self.db), ensure_ascii=False, indent=1), encoding="utf-8")
        (self.entrada / "decisiones.json").write_text(
            json.dumps(_build_decisiones_json(self.db), ensure_ascii=False, indent=1), encoding="utf-8")

        if 12 not in self.fases and not _restaurar_snapshot(self.db, self.salida):
            log.info("No hay snapshot para reutilizar: la corrida %s pasa a COMPLETA", self.fases)
            self.fases = list(FASES_COMPLETA)
        return list(self.fases)

    def __exit__(self, *args):
        return False

    def _copiar_archivos(self) -> None:
        recientes: dict[str, MotorArchivo] = {}
        for tipo in TIPOS_ARCHIVO - {"acr"}:
            archivo = self.db.scalars(
                select(MotorArchivo).where(MotorArchivo.tipo == tipo)
                .order_by(MotorArchivo.subido_en.desc(), MotorArchivo.id.desc()).limit(1)
            ).first()
            if archivo is not None:
                recientes[tipo] = archivo
        for archivo in recientes.values():
            destino = self.entrada / _nombre_canonico(archivo)
            destino.write_bytes(archivo.data)

        carpeta_acr = self.entrada / "acr"
        carpeta_acr.mkdir(exist_ok=True)
        acrs = self.db.scalars(
            select(MotorArchivo).where(MotorArchivo.tipo == "acr").order_by(MotorArchivo.id)
        ).all()
        for archivo in acrs:
            nombre = Path(archivo.nombre_archivo).name
            if not ACR_RE.match(nombre):
                log.warning("ACR ignorado por nombre inválido: %s", nombre)
                continue
            (carpeta_acr / nombre).write_bytes(archivo.data)


def _nombre_canonico(archivo: MotorArchivo) -> str:
    sufijo = Path(archivo.nombre_archivo).suffix.lower() or ".xlsx"
    if sufijo not in (".xlsx", ".xls"):
        sufijo = ".xlsx"
    base = {
        "avisos": "HISTORICO_AVISOS",
        "ordenes": "HISTORICO_OT",
        "plan": "PM_SAP",
        "consolidado": "Consolidado_Base_Reportes_de_Fallo",
        "acr_revisado": "acr_revisado",
        "equivalencias": "equivalencias_validadas",
    }.get(archivo.tipo, archivo.tipo)
    if archivo.tipo in ("acr_revisado", "equivalencias"):
        return base + sufijo
    return f"{base}_{archivo.id}{sufijo}"


def _build_excel_diario(db: Session, destino: Path) -> Path:
    """Excel diario con la hoja y columnas exactas que espera la fase 12 del motor."""
    import pandas as pd
    from .models import FailureEvent

    filas = db.execute(select(
        FailureEvent.event_date, FailureEvent.shift, FailureEvent.interval,
        FailureEvent.machine, FailureEvent.failure_type, FailureEvent.subsystem,
        FailureEvent.stop_minutes, FailureEvent.crew,
    ).order_by(FailureEvent.event_date, FailureEvent.shift, FailureEvent.interval)).all()

    columnas = ["Fecha Contable", "Turno", "Intervalo", "Tipo de Paro", "Clave de Paro",
                "Subclave de Paro", "Clave 1 de Paro", "Minutos de paro", "Tripulación"]
    df = pd.DataFrame(filas, columns=["Fecha Contable", "Turno", "Intervalo", "Clave de Paro",
                                      "Subclave de Paro", "Clave 1 de Paro", "Minutos de paro",
                                      "Tripulación"])
    df["Tipo de Paro"] = "P.EQ.LINEA"
    df["Minutos de paro"] = pd.to_numeric(df["Minutos de paro"], errors="coerce").astype(float)
    df = df[columnas]

    with pd.ExcelWriter(destino, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="BD SAP", index=False)
    return destino


def _build_paradas_json(db: Session) -> dict:
    paradas = db.scalars(select(Parada).order_by(Parada.fecha, Parada.inicio)).all()
    return {
        "paradas": [
            {
                "Id_parada": p.id_parada,
                "Titulo": p.titulo,
                "Modalidad": p.modalidad,
                "Fecha": p.fecha.isoformat(),
                "Inicio": p.inicio,
                "Fin": p.fin,
                "Maquinas": p.maquinas or [],
                "Tareas_manuales": p.tareas_manuales or [],
            }
            for p in paradas
        ]
    }


def _build_decisiones_json(db: Session) -> dict:
    decisiones = db.scalars(select(DecisionTarea).order_by(DecisionTarea.id_tarea)).all()
    filas = []
    for d in decisiones:
        fila = {"Id_tarea": d.id_tarea, "Decision": d.decision}
        if d.id_parada:
            fila["Id_parada"] = d.id_parada
        filas.append(fila)
    return {"decisiones": filas}


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------
def _crear_snapshot(db: Session, run_id: int, salida: Path) -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for ruta in sorted(salida.rglob("*")):
            if ruta.is_file():
                tar.add(ruta, arcname=str(ruta.relative_to(salida)))
    db.add(MotorSnapshot(run_id=run_id, data=buffer.getvalue()))
    db.flush()

    ids = db.scalars(select(MotorSnapshot.id).order_by(MotorSnapshot.id.desc())).all()
    for viejo in ids[2:]:
        db.execute(delete(MotorSnapshot).where(MotorSnapshot.id == viejo))


def _restaurar_snapshot(db: Session, salida: Path) -> bool:
    snap = db.scalars(select(MotorSnapshot).order_by(MotorSnapshot.id.desc()).limit(1)).first()
    if snap is None:
        return False
    try:
        with tarfile.open(fileobj=io.BytesIO(snap.data), mode="r:gz") as tar:
            for miembro in tar.getmembers():
                destino = (salida / miembro.name).resolve()
                if not str(destino).startswith(str(salida.resolve())):
                    raise RuntimeError("Snapshot con ruta insegura")
                if not (miembro.isfile() or miembro.isdir()):
                    continue
            tar.extractall(salida, filter="data")
        return True
    except Exception:  # noqa: BLE001
        log.exception("No se pudo restaurar el último snapshot del motor")
        return False


# ---------------------------------------------------------------------------
# Guardado de salidas e HistoriaEvento
# ---------------------------------------------------------------------------
def _upsert_salida(db: Session, nombre: str, semana: str, run_id: int,
                   contenido: dict | None = None, archivo: bytes | None = None) -> None:
    salida = db.scalars(select(MotorSalida).where(
        MotorSalida.nombre == nombre, MotorSalida.semana == semana).limit(1)).first()
    if salida is None:
        salida = MotorSalida(nombre=nombre, semana=semana, run_id=run_id)
        db.add(salida)
    salida.run_id = run_id
    if contenido is not None:
        salida.contenido = contenido
    if archivo is not None:
        salida.archivo = archivo


def _capturar_semana(salida: Path, destino: Path, semana: str, fases: list[int]) -> None:
    """Copia las salidas semanales que genera esta corrida, antes de correr la siguiente semana.

    Solo se copia lo que la corrida realmente produce: en ACR y PARADAS no se ejecuta la fase 20,
    así que no hay plan semanal nuevo que guardar. Sin esto una corrida ACR reutilizaría el plan
    restaurado del snapshot y lo reescribiría para la semana pedida.
    """
    nombres: list[str] = []
    if 20 in fases:
        nombres.extend(["plan_semanal.json", f"plan_semanal_{semana}.xlsx"])
    if {19, 20, 23} & set(fases):
        nombres.append("alertas.json")
    if not nombres:
        return
    destino.mkdir(parents=True, exist_ok=True)
    for nombre in nombres:
        ruta = salida / nombre
        if ruta.exists():
            shutil.copy2(ruta, destino / nombre)


def _guardar_salidas(db: Session, run_id: int, salida: Path, capturas: Path,
                     semanas: list[str], fases: list[int]) -> None:
    semanas_plan = [s for s in semanas if s]
    for archivo, nombre in SALIDAS_JSON.items():
        if nombre in SALIDAS_SEMANA:
            for semana in semanas_plan:
                ruta = capturas / semana / archivo
                if not ruta.exists():
                    continue
                try:
                    contenido = json.loads(ruta.read_text(encoding="utf-8"))
                except Exception:  # noqa: BLE001
                    log.exception("JSON inválido en la salida del motor: %s", ruta)
                    continue
                _upsert_salida(db, nombre, semana, run_id, contenido=contenido)
        else:
            ruta = salida / archivo
            if not ruta.exists():
                continue
            try:
                contenido = json.loads(ruta.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                log.exception("JSON inválido en la salida del motor: %s", ruta)
                continue
            _upsert_salida(db, nombre, "", run_id, contenido=contenido)

    for carpeta in capturas.iterdir():
        if not carpeta.is_dir():
            continue
        for ruta in sorted(carpeta.glob("plan_semanal_*.xlsx")):
            match = re.search(r"plan_semanal_(.+)\.xlsx$", ruta.name)
            if match:
                _upsert_salida(db, SALIDA_EXCEL, match.group(1), run_id, archivo=ruta.read_bytes())

    if 13 in fases:
        _reemplazar_historia(db, salida)

    if 12 in fases:
        _crear_snapshot(db, run_id, salida)

    db.flush()


def _verdadero(valor) -> bool:
    if isinstance(valor, bool):
        return valor
    return str(valor).strip().lower() in {"true", "1", "si", "sí", "yes"}


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


def _fecha(valor) -> date | None:
    if valor is None:
        return None
    try:
        import pandas as pd

        if pd.isna(valor):
            return None
        return pd.Timestamp(valor).date()
    except Exception:  # noqa: BLE001
        return None


def _texto(valor) -> str | None:
    try:
        import pandas as pd

        if valor is None or pd.isna(valor):
            return None
    except Exception:  # noqa: BLE001
        if valor is None:
            return None
    texto = str(valor).strip()
    return texto or None


def _eventos_desde_salida(salida: Path) -> list[dict]:
    """Convierte los CSV del motor en filas de HistoriaEvento (contrato del usuario)."""
    eventos: list[dict] = []

    diario = _leer_csv(salida, "base_diaria_componentes.csv")
    if diario is not None:
        for _, fila in diario.iterrows():
            maquina = _texto(fila.get("Maquina"))
            fecha = _fecha(fila.get("Fecha"))
            if not maquina or fecha is None:
                continue
            componente = _texto(fila.get("Componente_catalogo")) or _texto(fila.get("Componente"))
            minutos = fila.get("Minutos de paro")
            try:
                minutos = float(minutos)
                detalle_min = f"{minutos:g}"
            except (TypeError, ValueError):
                minutos = None
                detalle_min = "?"
            turno = _texto(fila.get("Turno")) or "?"
            eventos.append({
                "maquina": maquina, "fecha": fecha, "tipo": "FALLA",
                "id_componente": _texto(fila.get("Id_componente")),
                "componente": componente,
                "sistema": _texto(fila.get("Sistema_diario")),
                "titulo": componente or "Falla",
                "detalle": f"Turno {turno} · {detalle_min} min de paro",
                "minutos_paro": minutos, "orden": None,
            })

    ot = _leer_csv(salida, "ot_limpias.csv")
    if ot is not None:
        for _, fila in ot.iterrows():
            if not _verdadero(fila.get("En_alcance")):
                continue
            tipo_orden = (_texto(fila.get("Tipo_orden")) or "").lower()
            if tipo_orden in ("preventiva", "predictiva"):
                tipo = "PREVENTIVO"
            elif tipo_orden in ("correctiva", "emergencia"):
                tipo = "CORRECTIVO"
            else:
                continue
            maquina = _texto(fila.get("Maquina"))
            fecha = _fecha(fila.get("Fecha_programada"))
            if not maquina or fecha is None:
                continue
            texto = _texto(fila.get("Texto"))
            orden = _texto(fila.get("Orden"))
            plan = _texto(fila.get("Plan"))
            estado = _texto(fila.get("Estado"))
            eventos.append({
                "maquina": maquina, "fecha": fecha, "tipo": tipo,
                "id_componente": None, "componente": None, "sistema": None,
                "titulo": texto or f"{tipo_orden.title()} {orden or ''}".strip(),
                "detalle": (f"Programada: {fecha.isoformat()} · Plan: {plan or '—'} · "
                            f"Orden: {orden or '—'} · {texto or '—'} · Estado: {estado or '—'}"),
                "minutos_paro": None, "orden": orden,
            })

    avisos = _leer_csv(salida, "avisos_limpios.csv")
    if avisos is not None:
        for _, fila in avisos.iterrows():
            if not _verdadero(fila.get("En_alcance")):
                continue
            maquina = _texto(fila.get("Maquina"))
            fecha = _fecha(fila.get("Fecha_aviso"))
            if not maquina or fecha is None:
                continue
            texto = _texto(fila.get("Texto"))
            tipo_aviso = _texto(fila.get("Tipo_aviso"))
            sintoma = _texto(fila.get("Sintoma"))
            eventos.append({
                "maquina": maquina, "fecha": fecha, "tipo": "AVISO",
                "id_componente": None, "componente": _texto(fila.get("Componente")),
                "sistema": _texto(fila.get("Sistema")),
                "titulo": texto or tipo_aviso or "Aviso",
                "detalle": (f"Tipo: {tipo_aviso or '—'} · Síntoma: {sintoma or '—'}"
                            f" · {texto or ''}").strip(),
                "minutos_paro": None, "orden": _texto(fila.get("Orden")),
            })

    return eventos


def _reemplazar_historia(db: Session, salida: Path) -> None:
    eventos = _eventos_desde_salida(salida)
    db.execute(delete(HistoriaEvento))
    if eventos:
        db.bulk_insert_mappings(HistoriaEvento, eventos)


# ---------------------------------------------------------------------------
# Lectura con caché
# ---------------------------------------------------------------------------
def invalidate_cache() -> None:
    with _CACHE_LOCK:
        _SALIDA_CACHE.clear()


def get_salida(db: Session, nombre: str, semana: str = "") -> dict | None:
    fila = db.execute(select(MotorSalida.run_id, MotorSalida.contenido).where(
        MotorSalida.nombre == nombre, MotorSalida.semana == semana).limit(1)).first()
    if fila is None:
        return None
    run_id, contenido = fila
    clave = (nombre, semana)
    with _CACHE_LOCK:
        cacheado = _SALIDA_CACHE.get(clave)
        if cacheado is not None and cacheado["run_id"] == run_id:
            _SALIDA_CACHE.move_to_end(clave)
            return cacheado["contenido"]
    with _CACHE_LOCK:
        _SALIDA_CACHE[clave] = {"run_id": run_id, "contenido": contenido}
        _SALIDA_CACHE.move_to_end(clave)
        while len(_SALIDA_CACHE) > CACHE_MAX:
            _SALIDA_CACHE.popitem(last=False)
    return contenido


def get_salida_archivo(db: Session, nombre: str, semana: str = "") -> bytes | None:
    return db.scalars(select(MotorSalida.archivo).where(
        MotorSalida.nombre == nombre, MotorSalida.semana == semana).limit(1)).first()


def latest_run(db: Session) -> MotorRun | None:
    return db.scalars(select(MotorRun).order_by(MotorRun.id.desc()).limit(1)).first()


def hay_run_ok(db: Session) -> bool:
    return db.scalars(select(MotorRun.id).where(MotorRun.status == "ok").limit(1)).first() is not None


def runs_pendientes(db: Session) -> int:
    return int(db.scalar(select(func.count(MotorRun.id)).where(
        MotorRun.status.in_(("en_cola", "en_proceso")))) or 0)
