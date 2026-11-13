"""
KOF-Smart Maintenance AI — Fase 12: Preparación y cruce de los archivos SAP de mantenimiento
(código puro, sin gráficas)

PREGUNTA QUE RESPONDE: "¿Qué información confiable tenemos de avisos, órdenes y plan preventivo,
y cómo se conectan entre sí?"

Requiere (en la misma carpeta de Colab):
  kof_planes_utils.py
  HISTORICO_AVISOS_LINEA_4_BAQ__1_.xlsx      -> avisos de daño (qué se reportó)
  HISTORICO_OT_LINEA_4_BAQ__1_.xlsx          -> órdenes de trabajo (qué se hizo y cuánto costó)
  PM_SAP_LINEA_4_BAQ_17-03-2026__1_.xlsx     -> plan preventivo vigente (qué está programado)

Cómo se cruzan:
  Aviso  --(número de orden)-->  Orden de trabajo  --(código de plan)-->  Plan preventivo

Salidas:
  avisos_limpios.csv     un aviso por fila, con componente, modo de falla, tipo de aviso y costo de su orden
  ot_limpias.csv         una orden por fila, con tipo (preventiva/predictiva/correctiva) y estado
  plan_pm_limpio.csv     una operación del plan por fila, con frecuencia en semanas y tipo de tarea
  calidad_datos_sap.csv  controles de calidad para el informe

Decisiones de duración, fechas y tipos: ver el encabezado de kof_planes_utils.py (D1 a D6).
"""
import pandas as pd
from kof_planes_utils import cargar_avisos, cargar_ot, cargar_plan, MAQ4, ARCHIVO_AVISOS

pd.set_option("display.width", 200)
pd.set_option("display.max_columns", None)

# ---------------------------------------------------------------------------
# 1. Carga y limpieza (toda la lógica vive en kof_planes_utils)
# ---------------------------------------------------------------------------
avisos = cargar_avisos()
ot = cargar_ot()
plan = cargar_plan()
print(f"Avisos desde 2024: {len(avisos):,} | Órdenes desde 2024: {len(ot):,} | "
      f"Operaciones del plan: {len(plan):,}")

# ---------------------------------------------------------------------------
# 2. Cruce aviso -> orden: trae a cada aviso el costo real y el estado de su orden
# ---------------------------------------------------------------------------
# Un aviso tiene como máximo una orden. Se usa el costo REAL (lo que efectivamente se gastó).
cruce = ot[["Orden", "Tipo_orden", "Estado", "Costo_real", "Fecha_programada"]].rename(
    columns={"Tipo_orden": "Tipo_orden_aviso", "Estado": "Estado_orden",
             "Costo_real": "Costo_real_orden", "Fecha_programada": "Fecha_orden"})
avisos = avisos.merge(cruce, on="Orden", how="left")

con_orden = avisos["Orden"].notna()
encontrada = avisos["Costo_real_orden"].notna()
print(f"\nAvisos con número de orden: {con_orden.sum()} de {len(avisos)}"
      f" | de ellos, encontrados en el archivo de OT: {encontrada.sum()}")

# ---------------------------------------------------------------------------
# 3. Cruce orden -> plan preventivo: ¿las preventivas pertenecen a planes vigentes?
# ---------------------------------------------------------------------------
planes_vigentes = set(plan["Plan"].dropna())
prev = ot[ot["Tipo_orden"] == "Preventiva"]
planes_en_ot = set(prev["Plan"].dropna())
print(f"Planes del archivo PM: {len(planes_vigentes)} | presentes en órdenes preventivas: "
      f"{len(planes_vigentes & planes_en_ot)}")

# Máquina de cada orden: se toma del plan (preventivas) o del equipo del aviso (correctivas)
equipo_maquina = pd.concat([avisos[["Equipo", "Maquina"]], plan[["Equipo", "Maquina"]]]) \
                   .dropna().drop_duplicates("Equipo").set_index("Equipo")["Maquina"]
ot["Maquina"] = ot["Equipo"].map(equipo_maquina)
ot["En_alcance"] = ot["Maquina"].isin(MAQ4)

# ---------------------------------------------------------------------------
# 4. Resúmenes para entender los datos (solo las 4 máquinas del alcance)
# ---------------------------------------------------------------------------
a4 = avisos[avisos["En_alcance"]]
o4 = ot[ot["En_alcance"]]
p4 = plan[plan["En_alcance"] & plan["Es_tarea"]]

print("\n--- Avisos por máquina y tipo (D5: no todo aviso es una falla) ---")
print(pd.crosstab(a4["Maquina"], a4["Tipo_aviso"], margins=True, margins_name="Total")
        .reindex(MAQ4 + ["Total"]))

print("\n--- Modo de falla en los avisos de tipo Falla (D6) ---")
fallas = a4[a4["Tipo_aviso"] == "Falla"]
print(pd.crosstab(fallas["Maquina"], fallas["Modo_falla"].fillna("Sin causa codificada"))
        .reindex(MAQ4))

print("\n--- Órdenes por tipo (millones, costo real) ---")
resumen_ot = o4.pivot_table(index="Maquina", columns="Tipo_orden", values="Costo_real",
                            aggfunc="sum", fill_value=0).reindex(MAQ4) / 1e6
print(resumen_ot.round(1))

# Órdenes de costo muy alto: pueden ser proyectos o mejoras clasificadas como correctivas
# (p. ej. un "upgrade"). No se eliminan, pero se listan para validarlas con mantenimiento.
umbral = o4["Costo_real"].quantile(0.99)
caras = o4[o4["Costo_real"] >= umbral].sort_values("Costo_real", ascending=False)
print(f"\nÓrdenes con costo en el 1 % más alto (≥ {umbral/1e6:.1f} millones), para validar:")
print(caras[["Orden", "Maquina", "Tipo_orden", "Texto", "Fecha_programada", "Costo_real"]]
        .head(10).to_string(index=False))

print("\n--- Cumplimiento del preventivo y predictivo (órdenes ya programadas) ---")
pp = o4[o4["Tipo_orden"].isin(["Preventiva", "Predictiva"]) & (o4["Estado"] != "Abierta")]
cumpl = pp.groupby("Maquina").agg(ordenes=("Orden", "size"),
                                  no_ejecutadas=("Estado", lambda s: (s == "No ejecutada").sum()))
cumpl["cumplimiento_%"] = 100 * (1 - cumpl["no_ejecutadas"] / cumpl["ordenes"])
print(cumpl.reindex(MAQ4).round(1))

print("\n--- Plan preventivo vigente: tareas y horas por semana equivalente ---")
p4 = p4.assign(Horas_por_semana=p4["Duracion_h"] / p4["Frecuencia_sem"])
print(p4.groupby("Maquina").agg(tareas=("Id_tarea", "size"),
                                horas_por_semana=("Horas_por_semana", "sum"))
        .reindex(MAQ4).round(1))
print(pd.crosstab(p4["Maquina"], p4["Tipo_tarea"]).reindex(MAQ4))

# ---------------------------------------------------------------------------
# 5. Controles de calidad (para el informe)
# ---------------------------------------------------------------------------
calidad = pd.DataFrame([
    ("Avisos", "Total desde 2024", len(avisos)),
    ("Avisos", "De las 4 máquinas del alcance", len(a4)),
    ("Avisos", "Sin causa codificada (4 máquinas)", int(a4["Causa_SAP"].isna().sum())),
    ("Avisos", "Sin sistema (conjunto) (4 máquinas)", int(a4["Sistema"].isna().sum())),
    ("Avisos", "Con fecha de fin de avería (no se usa, D1)",
     int(pd.read_excel(ARCHIVO_AVISOS)["FinAvería"].notna().sum())),
    ("Avisos", "Con orden encontrada en el archivo de OT", int(encontrada.sum())),
    ("Órdenes", "Total desde 2024", len(ot)),
    ("Órdenes", "Correctivas de emergencia (clase 4)", int(ot["Clase_detalle"].str.startswith("Mantenimiento de emergencia").sum())),
    ("Órdenes", "Mejoras y proyectos (no cuentan como falla)", int((ot["Tipo_orden"] == "Mejora o proyecto").sum())),
    ("Órdenes", "Clase no reconocida", int((ot["Clase_detalle"] == "Clase no reconocida").sum())),
    ("Órdenes", "Sin máquina identificada", int(ot["Maquina"].isna().sum())),
    ("Plan", "Operaciones (filas)", len(plan)),
    ("Plan", "Tareas principales (Suboperacion = 0)", int(plan["Es_tarea"].sum())),
    ("Plan", "Tareas sin verbo claro (validar tipo)", int((plan["Es_tarea"] & plan["Tipo_tarea"].eq("Sin verbo (validar)")).sum())),
    ("Plan", "Tareas sin frecuencia", int(plan["Frecuencia_sem"].isna().sum())),
], columns=["Archivo", "Control", "Valor"])
print("\n--- Controles de calidad ---")
print(calidad.to_string(index=False))

# ---------------------------------------------------------------------------
# 6. Guardar
# ---------------------------------------------------------------------------
avisos.to_csv("avisos_limpios.csv", index=False)
ot.to_csv("ot_limpias.csv", index=False)
plan.to_csv("plan_pm_limpio.csv", index=False)
calidad.to_csv("calidad_datos_sap.csv", index=False)
print("\nGuardado: avisos_limpios.csv, ot_limpias.csv, plan_pm_limpio.csv, calidad_datos_sap.csv")
