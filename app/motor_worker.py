"""Ejecuta el motor de planes en un proceso aislado.

El motor cambia el directorio de trabajo, usa variables de entorno y recarga módulos, por eso
NO debe correr dentro del proceso de FastAPI. `app.motor_service` lanza este módulo con
subprocess.run y lee el JSON de la última línea de stdout.

Uso:
    python -m app.motor_worker --entrada <dir> --salida <dir> --semana 2026-W46 \
        --fecha-ref 2026-11-09 --fases 12,13,...,23
"""
from __future__ import annotations

import argparse
import json
import os
import runpy
import sys
import time
import traceback
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
CARPETA_MOTOR = RAIZ / "motor_planes" / "motor"

FASES = {
    12: "fase12_preparacion_sap.py",
    13: "fase13_catalogo_fusion.py",
    14: "fase14_historia_clinica.py",
    15: "fase15_cumplimiento.py",
    16: "fase16_alternativa_a.py",
    17: "fase17_alternativa_b.py",
    18: "fase18_fusion.py",
    19: "fase19_alertas.py",
    20: "fase20_programador.py",
    21: "fase21_fallas_mayores_acr.py",
    22: "fase22_repuestos_criticos.py",
    23: "fase23_alertas_y_antecedentes.py",
    # La fase 24 (validación) NO se ejecuta desde la app: solo sirve para el documento final.
}


def _parse_fases(value: str) -> list[int]:
    fases: list[int] = []
    for parte in value.replace(";", ",").split(","):
        parte = parte.strip()
        if not parte:
            continue
        numero = int(parte)
        if numero not in FASES:
            raise ValueError(f"Fase desconocida: {numero}")
        if numero not in fases:
            fases.append(numero)
    return sorted(fases)


def main() -> int:
    parser = argparse.ArgumentParser(description="Worker del motor de planes de KOF-Smart")
    parser.add_argument("--entrada", required=True)
    parser.add_argument("--salida", required=True)
    parser.add_argument("--semana", default="")
    parser.add_argument("--fecha-ref", default="")
    parser.add_argument("--fases", required=True, help="Lista separada por comas, p. ej. 19,20,23")
    args = parser.parse_args()

    entrada = os.path.abspath(args.entrada)
    salida = os.path.abspath(args.salida)
    os.makedirs(salida, exist_ok=True)

    try:
        fases = _parse_fases(args.fases)
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "fases": [], "error": f"Fases inválidas: {exc}"}, ensure_ascii=False))
        return 1

    # Igual que correr_motor.py: las fases leen estas variables al importar sus utilidades.
    os.environ["KOF_ENTRADA"] = entrada
    os.environ["KOF_SEMANA"] = args.semana or ""
    os.environ["KOF_FECHA_REFERENCIA"] = args.fecha_ref or ""
    if str(CARPETA_MOTOR) not in sys.path:
        sys.path.insert(0, str(CARPETA_MOTOR))
    for modulo in ("kof_planes_utils", "kof_motor_utils", "kof_utils"):
        sys.modules.pop(modulo, None)

    resumen: dict = {"ok": True, "fases": [], "error": None}
    dir_original = os.getcwd()
    try:
        os.chdir(salida)
        for numero in fases:
            archivo = CARPETA_MOTOR / FASES[numero]
            inicio = time.time()
            try:
                runpy.run_path(str(archivo), run_name="__main__")
            except Exception as exc:  # noqa: BLE001 - se detiene en la primera falla
                resumen["ok"] = False
                resumen["error"] = f"Fase {numero}: {exc!r}"
                resumen["fases"].append({"fase": numero, "ok": False, "segundos": round(time.time() - inicio, 1),
                                         "error": repr(exc)})
                traceback.print_exc()
                break
            resumen["fases"].append({"fase": numero, "ok": True,
                                     "segundos": round(time.time() - inicio, 1)})
    finally:
        os.chdir(dir_original)

    # La última línea de stdout es el contrato con motor_service.
    print(json.dumps(resumen, ensure_ascii=False))
    return 0 if resumen["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
