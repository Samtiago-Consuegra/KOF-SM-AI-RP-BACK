"""Carga inicial de los Excel del motor de planes en MotorArchivo.

Uso (en local, con DATABASE_URL apuntando a Supabase):
    python scripts/cargar_entradas_motor.py --carpeta motor_planes/datos/entrada
    python scripts/cargar_entradas_motor.py --carpeta motor_planes/datos/entrada --encolar

Lee una carpeta con la estructura de motor_planes/datos/entrada:
  HISTORICO_AVISOS*.xlsx, HISTORICO_OT*.xlsx, PM_SAP*.xlsx, *Consolidado*Fallo*.xlsx,
  acr_revisado.xlsx, equivalencias_validadas.xlsx y la subcarpeta acr/.

El Excel diario (BD*Reporte*SAP*) se ignora: el backend lo genera desde failure_events.
Con --encolar crea un MotorRun COMPLETA en_cola para que el servidor lo tome al arrancar.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from sqlalchemy import select  # noqa: E402

from app import motor_service  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import MotorArchivo  # noqa: E402

EXTENSIONES = {".xlsx", ".xls"}


def _tipo(nombre: str) -> str | None:
    n = nombre.lower()
    if n.startswith("bd") and "reporte" in n and "sap" in n:
        return None  # Excel diario: lo genera el backend desde failure_events
    if "historico_avisos" in n:
        return "avisos"
    if "historico_ot" in n:
        return "ordenes"
    if "pm_sap" in n:
        return "plan"
    if "consolidado" in n and "fallo" in n:
        return "consolidado"
    if n in {"acr_revisado.xlsx", "acr_revisado.xls"}:
        return "acr_revisado"
    if "equivalencias_validadas" in n:
        return "equivalencias"
    return None


def _guardar(db, ruta: Path, tipo: str, autor: str) -> None:
    if tipo == "acr":
        anterior = db.scalars(select(MotorArchivo).where(
            MotorArchivo.tipo == "acr", MotorArchivo.nombre_archivo == ruta.name)).first()
        if anterior is not None:
            db.delete(anterior)
            db.flush()
    archivo = MotorArchivo(tipo=tipo, nombre_archivo=ruta.name, data=ruta.read_bytes(), subido_por=autor)
    db.add(archivo)


def main() -> int:
    parser = argparse.ArgumentParser(description="Carga los Excel de entrada del motor de planes")
    parser.add_argument("--carpeta", required=True, help="Carpeta datos/entrada con los Excel de SAP")
    parser.add_argument("--encolar", action="store_true", help="Crear un MotorRun COMPLETA en cola")
    args = parser.parse_args()

    carpeta = Path(args.carpeta)
    if not carpeta.is_dir():
        print(f"No existe la carpeta: {carpeta}")
        return 2

    autor = "script:cargar_entradas_motor"
    contadores: dict[str, int] = {}

    with SessionLocal() as db:
        for ruta in sorted(carpeta.iterdir()):
            if not ruta.is_file() or ruta.suffix.lower() not in EXTENSIONES:
                continue
            tipo = _tipo(ruta.name)
            if tipo is None:
                continue
            _guardar(db, ruta, tipo, autor)
            contadores[tipo] = contadores.get(tipo, 0) + 1

        acr_dir = carpeta / "acr"
        if acr_dir.is_dir():
            for ruta in sorted(acr_dir.iterdir()):
                if not ruta.is_file() or ruta.suffix.lower() not in EXTENSIONES:
                    continue
                if not motor_service.ACR_RE.match(ruta.name):
                    print(f"ACR ignorado (nombre inválido): {ruta.name}")
                    continue
                _guardar(db, ruta, "acr", autor)
                contadores["acr"] = contadores.get("acr", 0) + 1

        db.commit()

        run_id = None
        if args.encolar:
            run = motor_service.encolar(db, trigger="script", fases=motor_service.FASES_COMPLETA,
                                        iniciar=False)
            run_id = run.id

    if contadores:
        print("Archivos cargados:")
        for tipo, cantidad in sorted(contadores.items()):
            print(f"  {tipo}: {cantidad}")
    else:
        print("No se encontraron archivos reconocibles en la carpeta.")
    if run_id is not None:
        print(f"MotorRun COMPLETA en cola: {run_id} (el servidor lo tomará al arrancar).")
    else:
        print("No se encoló ninguna corrida (usa --encolar para hacerlo).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
