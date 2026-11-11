"""
KOF-Smart Maintenance AI — Fase 17: ALTERNATIVA B — plan desde los archivos SAP
(avisos, órdenes de trabajo y plan preventivo)      Alimenta: pestaña "Mantenimiento" (vía fusión)

PREGUNTA QUE RESPONDE: "Comparando lo que falla con lo que está planeado y lo que realmente se
ejecuta, ¿dónde está la brecha del plan preventivo?"

Requiere: kof_motor_utils.py y las salidas de las Fases 12 a 15
  (hc_componentes.csv, plan_pm_limpio.csv, cump_planes.csv,
   equivalencias_borrador.xlsx o equivalencias_validadas.xlsx)

1. BRECHAS. Un componente puede tener varias; la más grave define la acción principal.
   Gravedad  Brecha                      Señal en los datos
   1.0       Sin cobertura               3+ avisos de falla y ninguna tarea del plan lo cubre
   1.0       Frecuencia insuficiente     falla (mediana entre avisos) antes de lo que dice su tarea
   0.9       Plan no se cumple           su plan se salta ciclos o tiene órdenes sin ejecutar (Fase 15)
   0.7       Tarea débil                 la tarea solo "revisa" pero el componente se DAÑA (3+ avisos)
   0.2       Posible sobre-mantenimiento tarea mensual o más frecuente, sin avisos y casi sin paros
   0.0       Sin brecha                  mantener

   Frecuencia propuesta (cuando hay que crear o acortar): la MITAD del intervalo mediano entre
   avisos, redondeada hacia abajo a la grilla. Revisar a la mitad del intervalo típico da al menos
   una oportunidad de detectar el problema antes de la siguiente falla.

2. PUNTAJE B (0 a 100): 40 % costo correctivo + 35 % avisos + 25 % gravedad de la brecha.

Las tareas que dependen de una equivalencia automática (no validada) quedan marcadas "por validar".

Salida: recomendaciones_B.csv  (mismas columnas que A y la fusión: COLUMNAS_RECOMENDACION)
"""
import os

import numpy as np
import pandas as pd
from kof_motor_utils import (a_grilla, nombre_frecuencia, rango_pct, niveles, miles, COLUMNAS_RECOMENDACION)
from kof_planes_utils import MAQ4, ARCHIVO_EQUIV_VALIDADAS

pd.set_option("display.width", 200)
pd.set_option("display.max_columns", None)

PESOS_B = {"costo": 0.40, "avisos": 0.35, "brecha": 0.25}
UMBRAL_EQUIV = 0.34

hc = pd.read_csv("hc_componentes.csv")
plan = pd.read_csv("plan_pm_limpio.csv")
cump = pd.read_csv("cump_planes.csv")

# Tarea vigente de cada componente, con su plan (para saber si ese plan se cumple)
if os.path.exists(ARCHIVO_EQUIV_VALIDADAS):
    eq = pd.read_excel(ARCHIVO_EQUIV_VALIDADAS)
    col_val = [c for c in eq.columns if c.startswith("Valida")][0]
    eq = eq[eq[col_val].astype(str).str.strip().str.lower().isin(["sí", "si"])].assign(Validada=True)
else:
    eq = pd.read_excel("equivalencias_borrador.xlsx")
    eq = eq[(eq["Rango"] == 1) & (eq["Similitud"] >= UMBRAL_EQUIV)].assign(Validada=False)
eq = eq.drop_duplicates("Id_componente").merge(
    plan[["Id_tarea", "Plan", "Especialidad"]], on="Id_tarea", how="left") \
    .merge(cump[["Plan", "Estado_plan", "Intervalo_real_dias"]], on="Plan", how="left")
eq = eq.set_index("Id_componente")

b = hc[(hc["Avisos"] > 0) | hc["Id_componente"].isin(eq.index)].copy()
b["Tiene_tarea"] = b["Id_componente"].isin(eq.index)
b["Plan"] = b["Id_componente"].map(eq["Plan"])
b["Estado_plan"] = b["Id_componente"].map(eq["Estado_plan"])
b["Intervalo_real_plan_dias"] = b["Id_componente"].map(eq["Intervalo_real_dias"])
b["Equivalencia_validada"] = b["Id_componente"].map(eq["Validada"]).fillna(False)
b["Frecuencia_actual_sem"] = b["Frecuencia_tarea_sem"]

minutos_bajos = hc["Minutos_paro"].quantile(0.25)


# ---------------------------------------------------------------------------
# 1. Brechas
# ---------------------------------------------------------------------------
def tipo_por_modo(modo):
    return {"Daño o desgaste": "Sustitución o restauración", "Desajuste": "Inspección y ajuste",
            "Suciedad": "Limpieza"}.get(modo, "Inspección")


def brechas_b(f):
    """Devuelve una lista de (gravedad, nombre, acción, tipo, frecuencia propuesta, texto)."""
    out = []
    n = f["Avisos"]
    rec_dias = f["Dias_entre_avisos_mediana"]
    frec_prop = a_grilla(rec_dias / 7 / 2) if pd.notna(rec_dias) else np.nan
    costo = f" Costo correctivo: {miles(f['Costo_correctivo'])}." if f["Costo_correctivo"] > 0 else ""
    if n >= 3 and not f["Tiene_tarea"]:
        out.append((1.0, "Sin cobertura", "Crear tarea preventiva", tipo_por_modo(f["Modo_principal"]), frec_prop,
                    f"{int(n)} avisos de falla (uno cada {rec_dias:.0f} días en mediana) y ninguna tarea del "
                    f"plan lo cubre.{costo}"))
    if f["Tiene_tarea"] and n >= 3 and pd.notna(rec_dias) and rec_dias < f["Frecuencia_actual_sem"] * 7:
        out.append((1.0, "Frecuencia insuficiente", "Acortar la frecuencia o agregar una inspección intermedia",
                    "Inspección" if f["Tipo_tarea_vigente"] != "Inspección" else f["Tipo_tarea_vigente"], frec_prop,
                    f"Falla cada {rec_dias:.0f} días (mediana de {int(n)} avisos), pero su tarea "
                    f"'{f['Tarea_vigente']}' es cada {f['Frecuencia_actual_sem'] * 7:.0f} días.{costo}"))
    if f["Tiene_tarea"] and n >= 1 and f["Estado_plan"] in ("Se saltan ciclos", "Órdenes sin ejecutar"):
        real = (f" En la práctica sus órdenes salen cada {f['Intervalo_real_plan_dias']:.0f} días."
                if pd.notna(f["Intervalo_real_plan_dias"]) else "")
        out.append((0.9, "Plan no se cumple", "Cumplir la frecuencia del plan vigente", f["Tipo_tarea_vigente"],
                    f["Frecuencia_actual_sem"],
                    f"Su plan {f['Plan']} está marcado '{f['Estado_plan']}'.{real}"))
    if f["Tiene_tarea"] and n >= 3 and f["Tipo_tarea_vigente"] == "Inspección" and f["Modo_principal"] == "Daño o desgaste":
        out.append((0.7, "Tarea débil", "Cambiar la tarea de revisar a sustituir o restaurar",
                    "Sustitución o restauración", f["Frecuencia_actual_sem"],
                    f"Su tarea solo revisa, pero {int(n)} avisos reportan daño o desgaste."))
    if f["Tiene_tarea"] and n == 0 and f["Frecuencia_actual_sem"] <= 4 and f["Minutos_paro"] <= minutos_bajos:
        out.append((0.2, "Posible sobre-mantenimiento", "Evaluar espaciar la tarea", f["Tipo_tarea_vigente"],
                    np.nan, f"Tarea cada {f['Frecuencia_actual_sem']:.0f} semanas sin avisos y casi sin paros."))
    if not out:
        out.append((0.0, "Sin brecha", "Mantener plan vigente",
                    f["Tipo_tarea_vigente"] if f["Tiene_tarea"] else "Seguimiento", np.nan,
                    f"{int(n)} avisos; no se detecta brecha con el plan."))
    return sorted(out, key=lambda x: -x[0])


lista = b.apply(brechas_b, axis=1)
b["Gravedad"] = lista.map(lambda l: l[0][0])
b["Brecha"] = lista.map(lambda l: l[0][1])
b["Accion"] = lista.map(lambda l: l[0][2])
b["Tipo_tarea_propuesta"] = lista.map(lambda l: l[0][3])
b["Frecuencia_propuesta_sem"] = lista.map(lambda l: l[0][4])
b["Justificacion"] = lista.map(lambda l: " ".join(x[5] for x in l))
b["Otras_brechas"] = lista.map(lambda l: "; ".join(x[1] for x in l[1:]))
b["Frecuencia_propuesta"] = b["Frecuencia_propuesta_sem"].map(nombre_frecuencia)

# ---------------------------------------------------------------------------
# 2. Puntaje B y confianza
# ---------------------------------------------------------------------------
b["Puntaje"] = (PESOS_B["costo"] * rango_pct(b["Costo_correctivo"]) + PESOS_B["avisos"] * rango_pct(b["Avisos"])
                + PESOS_B["brecha"] * b["Gravedad"] * 100)
b["Prioridad"] = niveles(b["Puntaje"])
b["Confianza"] = np.select([b["Avisos"] >= 10, b["Avisos"] >= 5], ["Alta", "Media"], "Baja")
b.loc[b["Tiene_tarea"] & ~b["Equivalencia_validada"], "Confianza"] += " (tarea por validar)"

recB = b[COLUMNAS_RECOMENDACION + ["Brecha", "Otras_brechas", "Avisos", "Costo_correctivo", "Plan",
                                   "Tarea_vigente", "Estado_plan"]] \
         .sort_values("Puntaje", ascending=False).reset_index(drop=True)

# ---------------------------------------------------------------------------
# 3. Resumen
# ---------------------------------------------------------------------------
print("--- Alternativa B: brecha principal por máquina ---")
print(pd.crosstab(recB["Maquina"], recB["Brecha"]).reindex(MAQ4))
print("\n--- Top 12 de la Alternativa B ---")
print(recB[["Maquina", "Componente", "Puntaje", "Prioridad", "Brecha", "Frecuencia_actual_sem",
            "Frecuencia_propuesta"]].head(12).round(1).to_string(index=False))
v = recB[recB["Componente"] == "Válvula de llenado"]
if len(v):
    print("\nEjemplo, válvula de llenado:\n " + v["Justificacion"].iat[0])

recB.to_csv("recomendaciones_B.csv", index=False)
print("\nGuardado: recomendaciones_B.csv")
