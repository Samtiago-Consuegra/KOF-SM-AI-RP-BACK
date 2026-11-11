"""
KOF-Smart Maintenance AI — Fase 15: CUMPLIMIENTO del mantenimiento planeado
(código puro, sin gráficas)                                  Alimenta: pestaña "Cumplimiento"

PREGUNTA QUE RESPONDE: "¿Se está ejecutando lo que se planea, y cuánto del mantenimiento
es planeado frente a correctivo?"

Requiere: kof_planes_utils.py y las salidas de la Fase 12 (ot_limpias.csv, plan_pm_limpio.csv)

INDICADORES
  1. Cumplimiento (%) = órdenes preventivas y predictivas ejecutadas / órdenes ya vencidas.
     Solo cuentan las órdenes cuya fecha programada pasó hace más de 14 días (GRACIA_DIAS):
     una orden de la semana pasada puede estar abierta solo porque no se ha cerrado en SAP.
  2. Pendientes vencidas (backlog): órdenes abiertas cuya fecha programada ya pasó.
  3. Mezcla del mantenimiento: % de órdenes y % del costo en preventivo, predictivo y
     correctivo. Una referencia común en la industria es que el planeado supere al correctivo;
     aquí se muestra el dato real sin imponer una meta.
  4. Regularidad de cada plan preventivo: intervalo real entre sus órdenes frente a su
     frecuencia nominal, y días desde su última orden. Muchos planes cuentan en horas de
     operación, así que se tolera hasta 50 % más de intervalo antes de marcarlo.
     (Se usan las fechas programadas; SAP no trae una fecha real de ejecución confiable, D1.)
  5. Serie semanal y trimestral del cumplimiento, porque la planta trabaja por semanas.

Salidas:
  cump_maquinas.csv     resumen por máquina
  cump_planes.csv       una fila por plan preventivo con su regularidad y cumplimiento
  cump_semanal.csv      cumplimiento por semana y máquina
  cump_trimestral.csv   cumplimiento por trimestre y máquina
  cump_pendientes.csv   órdenes no ejecutadas y abiertas vencidas (para revisar una a una)
  cumplimiento.json     todo lo anterior para la web
"""
import json

import numpy as np
import pandas as pd
from kof_planes_utils import MAQ4, INICIO_SAP

pd.set_option("display.width", 200)
pd.set_option("display.max_columns", None)

GRACIA_DIAS = 14   # días que se dan para cerrar una orden en SAP antes de contarla como incumplida

ot = pd.read_csv("ot_limpias.csv", parse_dates=["Fecha_programada", "Fecha_creacion"])
plan = pd.read_csv("plan_pm_limpio.csv")
ot = ot[ot["En_alcance"]].copy()

# El corte es la fecha más reciente en que se creó una orden: equivale a la fecha de exportación
corte = ot["Fecha_creacion"].max()
print(f"Corte de los datos: {corte:%d-%m-%Y} | se evalúan órdenes programadas hasta "
      f"{corte - pd.Timedelta(days=GRACIA_DIAS):%d-%m-%Y} ({GRACIA_DIAS} días de gracia para cerrarlas)")

# Una orden es evaluable cuando su fecha programada pasó hace más de GRACIA_DIAS: las órdenes
# recientes suelen estar abiertas solo porque aún no se cierran en SAP, no porque no se hicieran.
corte_eval = corte - pd.Timedelta(days=GRACIA_DIAS)
ot["Vencida"] = ot["Fecha_programada"] <= corte_eval
planeado = ot[ot["Tipo_orden"].isin(["Preventiva", "Predictiva"])].copy()
evaluables = planeado[planeado["Vencida"]].copy()
evaluables["Cumplida"] = evaluables["Estado"].eq("Ejecutada")
print(f"Órdenes planeadas (preventivas + predictivas): {len(planeado):,} | ya vencidas y evaluables: "
      f"{len(evaluables):,} | programadas a futuro: {(~planeado['Vencida']).sum():,}")

# ---------------------------------------------------------------------------
# 1. Resumen por máquina
# ---------------------------------------------------------------------------
res = evaluables.groupby("Maquina").agg(
    Ordenes_vencidas=("Orden", "size"),
    Ejecutadas=("Cumplida", "sum"),
    No_ejecutadas=("Estado", lambda s: (s == "No ejecutada").sum()),
    Abiertas_vencidas=("Estado", lambda s: (s == "Abierta").sum()))
res["Cumplimiento_pct"] = 100 * res["Ejecutadas"] / res["Ordenes_vencidas"]

# Cumplimiento de las últimas 13 semanas (un trimestre), para ver el estado actual
recientes = evaluables[evaluables["Fecha_programada"] > corte_eval - pd.Timedelta(weeks=13)]
res["Cumplimiento_13sem_pct"] = 100 * recientes.groupby("Maquina")["Cumplida"].mean()

# Mezcla: % de órdenes y % de costo por tipo (solo órdenes ya vencidas, todos los tipos)
mezcla_n = pd.crosstab(ot.loc[ot["Vencida"], "Maquina"], ot.loc[ot["Vencida"], "Tipo_orden"], normalize="index") * 100
mezcla_c = ot[ot["Vencida"]].pivot_table(index="Maquina", columns="Tipo_orden", values="Costo_real",
                                         aggfunc="sum", fill_value=0)
mezcla_c = mezcla_c.div(mezcla_c.sum(axis=1), axis=0) * 100
for tipo in ["Preventiva", "Predictiva", "Correctiva"]:
    res[f"Pct_ordenes_{tipo.lower()}"] = mezcla_n.get(tipo, 0)
    res[f"Pct_costo_{tipo.lower()}"] = mezcla_c.get(tipo, 0)
res["Ordenes_predictivas"] = ot[ot["Tipo_orden"] == "Predictiva"].groupby("Maquina").size()
res = res.reindex(MAQ4).fillna({"Ordenes_predictivas": 0}).reset_index()

print("\n--- Cumplimiento por máquina ---")
print(res[["Maquina", "Ordenes_vencidas", "Ejecutadas", "No_ejecutadas", "Abiertas_vencidas",
           "Cumplimiento_pct", "Cumplimiento_13sem_pct"]].round(1).to_string(index=False))
print("\n--- Mezcla del mantenimiento (% de órdenes | % del costo) ---")
print(res[["Maquina", "Pct_ordenes_preventiva", "Pct_ordenes_predictiva", "Pct_ordenes_correctiva",
           "Pct_costo_preventiva", "Pct_costo_correctiva", "Ordenes_predictivas"]].round(1).to_string(index=False))

# ---------------------------------------------------------------------------
# 2. Series semanal y trimestral
# ---------------------------------------------------------------------------
semanal = (evaluables.groupby(["Semana", "Maquina"])
                     .agg(Ordenes=("Orden", "size"), Ejecutadas=("Cumplida", "sum")).reset_index())
semanal["Cumplimiento_pct"] = 100 * semanal["Ejecutadas"] / semanal["Ordenes"]

evaluables["Trimestre"] = evaluables["Fecha_programada"].dt.to_period("Q").astype(str)
trimestral = (evaluables.groupby(["Trimestre", "Maquina"])
                        .agg(Ordenes=("Orden", "size"), Ejecutadas=("Cumplida", "sum")).reset_index())
trimestral["Cumplimiento_pct"] = 100 * trimestral["Ejecutadas"] / trimestral["Ordenes"]
print("\n--- Cumplimiento trimestral (%) ---")
print(trimestral.pivot(index="Trimestre", columns="Maquina", values="Cumplimiento_pct")
                .reindex(columns=MAQ4).round(0))

# ---------------------------------------------------------------------------
# 3. Regularidad de cada plan preventivo
# ---------------------------------------------------------------------------
tareas = plan[plan["En_alcance"] & plan["Es_tarea"]]
info_plan = tareas.groupby("Plan").agg(Maquina=("Maquina", "first"), Tareas=("Id_tarea", "size"),
                                       Frecuencia_sem=("Frecuencia_sem", "min"),
                                       Horas_por_orden=("Duracion_h", "sum"),
                                       Especialidad=("Especialidad", lambda s: s.mode().iat[0] if s.notna().any() else np.nan),
                                       Ejemplo_tarea=("Tarea", "first"))
ord_plan = evaluables.groupby("Plan").agg(Ordenes_generadas=("Orden", "size"),
                                          Ejecutadas=("Cumplida", "sum"),
                                          Primera_programada=("Fecha_programada", "min"),
                                          Ultima_programada=("Fecha_programada", "max"))
planes = info_plan.join(ord_plan, how="left").fillna({"Ordenes_generadas": 0, "Ejecutadas": 0})

# Intervalo real entre órdenes del mismo plan, frente a su frecuencia nominal.
# OJO: muchos planes SAP cuentan en HORAS DE OPERACIÓN (168 h = "1 semana"), así que en el
# calendario sus órdenes salen cada 8-9 días y no cada 7. Por eso no se exige el número exacto
# de órdenes: solo se marca un plan cuando su intervalo real supera en 50 % al nominal.
intervalo = (evaluables.sort_values("Fecha_programada").groupby("Plan")["Fecha_programada"]
                       .apply(lambda f: f.diff().dt.days.median()))
planes["Intervalo_nominal_dias"] = planes["Frecuencia_sem"] * 7
planes["Intervalo_real_dias"] = intervalo
planes["Razon_intervalo"] = planes["Intervalo_real_dias"] / planes["Intervalo_nominal_dias"]
planes["Dias_desde_ultima"] = (corte_eval - planes["Ultima_programada"]).dt.days
planes["Cumplimiento_pct"] = 100 * planes["Ejecutadas"] / planes["Ordenes_generadas"].replace(0, np.nan)
espaciado = (planes["Ordenes_generadas"] >= 2) & (planes["Razon_intervalo"] > 1.5)
sin_reciente = planes["Dias_desde_ultima"] > 1.5 * planes["Intervalo_nominal_dias"] + GRACIA_DIAS
planes["Estado_plan"] = np.select(
    [planes["Ordenes_generadas"] == 0, espaciado | sin_reciente, planes["Cumplimiento_pct"] < 90],
    ["Sin órdenes desde 2024", "Se saltan ciclos", "Órdenes sin ejecutar"], default="Al día")
planes = planes.reset_index().sort_values(["Maquina", "Estado_plan"])

print("\n--- Estado de los planes preventivos ---")
print(pd.crosstab(planes["Maquina"], planes["Estado_plan"]).reindex(MAQ4))
print("\nPlanes que requieren revisión:")
print(planes[planes["Estado_plan"] != "Al día"][["Maquina", "Plan", "Intervalo_nominal_dias", "Intervalo_real_dias",
      "Dias_desde_ultima", "Ordenes_generadas", "Cumplimiento_pct", "Estado_plan", "Ejemplo_tarea"]]
      .round(1).to_string(index=False))

# ---------------------------------------------------------------------------
# 4. Pendientes: no ejecutadas y abiertas vencidas
# ---------------------------------------------------------------------------
pendientes = evaluables[evaluables["Estado"].isin(["No ejecutada", "Abierta"])].copy()
pendientes["Dias_vencida"] = (corte - pendientes["Fecha_programada"]).dt.days
pendientes = pendientes.sort_values("Fecha_programada", ascending=False)[
    ["Orden", "Maquina", "Tipo_orden", "Estado", "Plan", "Texto", "Fecha_programada", "Semana", "Dias_vencida"]]
print(f"\nPendientes: {(pendientes['Estado'] == 'No ejecutada').sum()} no ejecutadas y "
      f"{(pendientes['Estado'] == 'Abierta').sum()} abiertas vencidas")

# ---------------------------------------------------------------------------
# 5. Guardar
# ---------------------------------------------------------------------------
res.to_csv("cump_maquinas.csv", index=False)
planes.to_csv("cump_planes.csv", index=False)
semanal.to_csv("cump_semanal.csv", index=False)
trimestral.to_csv("cump_trimestral.csv", index=False)
pendientes.to_csv("cump_pendientes.csv", index=False)


def registros(df):
    d = df.copy()
    for c in d.columns:
        if pd.api.types.is_datetime64_any_dtype(d[c]):
            d[c] = d[c].dt.strftime("%Y-%m-%d")
    d = d.astype(object).where(d.notna(), None)
    return [{k: (round(v, 2) if isinstance(v, float) else v) for k, v in r.items()} for r in d.to_dict("records")]


salida = {"datos_hasta": corte.strftime("%Y-%m-%d"),
          "maquinas": registros(res), "planes": registros(planes),
          "semanal": registros(semanal), "trimestral": registros(trimestral),
          "pendientes": registros(pendientes)}
with open("cumplimiento.json", "w", encoding="utf-8") as f:
    json.dump(salida, f, ensure_ascii=False, indent=1, default=str)
print("\nGuardado: cump_maquinas.csv, cump_planes.csv, cump_semanal.csv, cump_trimestral.csv, "
      "cump_pendientes.csv, cumplimiento.json")
