"""
correr_motor.py — Corre el motor de KOF-Smart Maintenance AI de principio a fin.

Es el ÚNICO punto de entrada que necesita el backend: cuando se suben archivos nuevos, cuando el
usuario programa una parada o cuando cambia de semana, se llama a este programa y se regeneran los
archivos que lee la app.

USO
  Desde la terminal (o desde el backend con subprocess):
      python correr_motor.py
      python correr_motor.py --entrada datos/entrada --salida datos/salida
      python correr_motor.py --semana 2026-W46 --fecha-ref 2026-11-08
      python correr_motor.py --desde 20          (solo reprograma la semana: tras cambiar una parada)
      python correr_motor.py --graficas          (además genera las gráficas PNG, para el informe)

  Desde Python (por ejemplo, FastAPI):
      from correr_motor import correr
      resumen = correr(semana="2026-W46", fecha_ref="2026-11-08")

QUÉ HACE
  1. Busca los archivos de entrada en --entrada (reporte diario y los tres de SAP).
  2. Corre las fases en orden dentro de --salida. Cada fase lee lo que dejó la anterior.
  3. Si una fase falla, se detiene, informa cuál fue y no deja la app con datos a medias.
  4. Devuelve un resumen con el tiempo de cada fase y los archivos generados.

CUÁNDO CORRER QUÉ
  - Archivos nuevos (diario, avisos, órdenes o plan)  -> desde la fase 12 (todo).
  - El usuario programa, edita o borra una parada,
    o envía / pospone una tarea                         -> desde la fase 20.
  - Cambio de semana o de fecha de referencia           -> desde la fase 19.
  - Llega un ACR nuevo o un consolidado nuevo           -> desde la fase 21.
  Las fases 21 a 24 siempre corren después de la 20 (usan su plan semanal y sus alertas).
"""
import argparse
import os
import runpy
import sys
import time

RAIZ = os.path.dirname(os.path.abspath(__file__))
CARPETA_MOTOR = os.path.join(RAIZ, "motor")
CARPETA_GRAFICAS = os.path.join(RAIZ, "graficas")

FASES = [
    (12, "fase12_preparacion_sap.py", "Base común: limpieza y cruce de SAP"),
    (13, "fase13_catalogo_fusion.py", "Base común: catálogo y unión con el reporte diario"),
    (14, "fase14_historia_clinica.py", "Historia clínica"),
    (15, "fase15_cumplimiento.py", "Cumplimiento"),
    (16, "fase16_alternativa_a.py", "Mantenimiento: Alternativa A (reporte diario)"),
    (17, "fase17_alternativa_b.py", "Mantenimiento: Alternativa B (SAP)"),
    (18, "fase18_fusion.py", "Mantenimiento: fusión e IPM por componente"),
    (19, "fase19_alertas.py", "Mantenimiento: alertas"),
    (20, "fase20_programador.py", "Mantenimiento: programador semanal"),
    (21, "fase21_fallas_mayores_acr.py", "Fallas mayores y ACR"),
    (22, "fase22_repuestos_criticos.py", "Repuestos críticos"),
    (23, "fase23_alertas_y_antecedentes.py", "Alertas nuevas y antecedentes para la historia clínica"),
    (24, "fase24_validacion.py", "Validación del motor (para el documento)"),
]
GRAFICAS = {12: "fase12_graficas.py", 13: "fase13_graficas.py", 14: "fase14_graficas.py",
            15: "fase15_graficas.py", 18: "fase18_graficas.py", 20: "fase20_graficas.py",
            21: "fase21_graficas.py", 24: "fase24_graficas.py"}

# Archivos que lee la app (ver CONTRATO_DATOS.md)
SALIDAS_APP = ["catalogo.json", "historia_clinica.json", "historia_clinica_acr.json", "cumplimiento.json",
               "plan_recomendado.json", "alertas.json", "plan_semanal.json"]


def correr(entrada="datos/entrada", salida="datos/salida", semana=None, fecha_ref=None, desde=12,
           graficas=False, silencioso=False):
    entrada = os.path.abspath(os.path.join(RAIZ, entrada) if not os.path.isabs(entrada) else entrada)
    salida = os.path.abspath(os.path.join(RAIZ, salida) if not os.path.isabs(salida) else salida)
    os.makedirs(salida, exist_ok=True)

    # Parámetros que leen las fases (kof_planes_utils los toma de estas variables)
    os.environ["KOF_ENTRADA"] = entrada
    os.environ["KOF_SEMANA"] = semana or ""
    os.environ["KOF_FECHA_REFERENCIA"] = fecha_ref or ""
    if graficas:
        os.environ.setdefault("MPLBACKEND", "Agg")
    for carpeta in (CARPETA_MOTOR, CARPETA_GRAFICAS):
        if carpeta not in sys.path:
            sys.path.insert(0, carpeta)
    for modulo in ("kof_planes_utils", "kof_motor_utils", "kof_utils"):
        sys.modules.pop(modulo, None)          # recarga con los parámetros nuevos

    dir_original = os.getcwd()
    os.chdir(salida)
    resumen = {"entrada": entrada, "salida": salida, "fases": [], "ok": True}
    try:
        for numero, archivo, nombre in FASES:
            if numero < desde:
                continue
            inicio = time.time()
            if not silencioso:
                print(f"\n{'=' * 80}\nFase {numero}: {nombre}\n{'=' * 80}")
            try:
                runpy.run_path(os.path.join(CARPETA_MOTOR, archivo), run_name="__main__")
                if graficas and numero in GRAFICAS:
                    runpy.run_path(os.path.join(CARPETA_GRAFICAS, GRAFICAS[numero]), run_name="__main__")
            except Exception as e:  # se detiene en la primera falla
                resumen["ok"] = False
                resumen["fases"].append({"fase": numero, "nombre": nombre, "ok": False, "error": repr(e)})
                print(f"\nERROR en la fase {numero} ({archivo}): {e!r}")
                break
            resumen["fases"].append({"fase": numero, "nombre": nombre, "ok": True,
                                     "segundos": round(time.time() - inicio, 1)})
    finally:
        os.chdir(dir_original)
    resumen["archivos_app"] = {f: os.path.exists(os.path.join(salida, f)) for f in SALIDAS_APP}
    if not silencioso:
        print(f"\n{'=' * 80}\nResumen: {'OK' if resumen['ok'] else 'CON ERRORES'}")
        for f in resumen["fases"]:
            print(f"  Fase {f['fase']}: {'OK' if f['ok'] else 'ERROR'} {f.get('segundos', '')} s  {f['nombre']}")
        print("Archivos para la app:", ", ".join(f for f, ok in resumen["archivos_app"].items() if ok))
    return resumen


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Corre el motor de planes de KOF-Smart Maintenance AI")
    p.add_argument("--entrada", default="datos/entrada", help="Carpeta con el reporte diario y los archivos SAP")
    p.add_argument("--salida", default="datos/salida", help="Carpeta donde se escriben los resultados")
    p.add_argument("--semana", default=None, help="Semana a programar, formato AAAA-Wnn (por defecto, la siguiente)")
    p.add_argument("--fecha-ref", default=None, help="Fecha de referencia AAAA-MM-DD (en la app: hoy)")
    p.add_argument("--desde", type=int, default=12, help="Fase desde la que se corre (12 a 24)")
    p.add_argument("--graficas", action="store_true", help="Genera también las gráficas PNG")
    a = p.parse_args()
    r = correr(a.entrada, a.salida, a.semana, a.fecha_ref, a.desde, a.graficas)
    sys.exit(0 if r["ok"] else 1)
