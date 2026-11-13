"""
KOF-Smart Maintenance AI — Fase 14: HISTORIA CLÍNICA por máquina, sistema y componente
(código puro, sin gráficas)                                  Alimenta: página "Historia clínica"

PREGUNTA QUE RESPONDE: "¿Cómo se ha comportado cada máquina, cada sistema y cada componente?"

Requiere: kof_planes_utils.py, kof_utils.py y las salidas de las Fases 12 y 13
  base_diaria_componentes.csv, avisos_clasificados.csv, catalogo_componentes.csv,
  ot_limpias.csv, equivalencias_borrador.xlsx (o equivalencias_validadas.xlsx si ya existe)

QUÉ SE CALCULA Y DE DÓNDE SALE (decisión D1: los tiempos solo salen del reporte diario)
  Del reporte diario:
    - Registros y EVENTOS. El reporte trae un registro por hora; un paro que dura dos horas
      aparece en dos filas. Por eso, registros del mismo componente en horas consecutivas
      se unen en un solo EVENTO. Los minutos no cambian; cambia el conteo de fallas.
    - MTTR (minutos por evento): media, mediana, RECORTADO (sin el 10 % más corto ni el 10 %
      más largo) y percentil 90. Si la media es mucho mayor que la mediana, hay pocos eventos
      muy largos que inflan el promedio. Paros ATÍPICOS = más largos que Q3 + 1,5 x RIC del
      mismo componente (se cuentan, no se eliminan).
    - MTBF entre eventos: media y mediana, en días operativos y en HORAS de operación (24 h por
      día operado). Se cuentan solo los días en que la línea operó (posicion_operativa).
      Con menos de 5 eventos no se calcula ("Datos insuficientes", igual que en la pestaña).
    - Tendencia: minutos de los últimos 6 meses frente a los 6 anteriores.
    - Forma de la falla (Weibull, beta) con al menos 10 eventos:
        beta > 1.2  Desgaste: la falla se vuelve más probable con el uso -> preventivo por tiempo.
        0.8 a 1.2   Aleatoria: falla sin patrón de edad -> inspección o monitoreo de condición.
        beta < 0.8  En racha: después de una falla es probable otra pronto (el arreglo no queda
                    bien a la primera, ajustes incompletos o repuesto defectuoso) -> revisar la
                    calidad de la reparación y del montaje, no acortar el preventivo.
      Los eventos del mismo turno cuentan como uno para este ajuste (tiempo entre fallas = 0).
  De SAP:
    - Avisos de falla, días medianos entre avisos, costo real de las órdenes correctivas,
      síntomas más reportados y tarea preventiva vigente (desde la tabla de equivalencias).

Salidas:
  hc_componentes.csv        una fila por componente con todos sus indicadores
  hc_sistemas.csv           una fila por máquina + sistema
  hc_maquinas.csv           una fila por máquina
  hc_series_mensuales.csv   minutos, eventos y avisos por mes y componente (para gráficas)
  historia_clinica.json     todo lo anterior anidado máquina -> sistema -> componente (para la web)
"""
import json
import os

import numpy as np
import pandas as pd
from scipy import stats
from kof_planes_utils import MAQ4, ARCHIVO_EQUIV_VALIDADAS
from kof_utils import posicion_operativa

pd.set_option("display.width", 200)
pd.set_option("display.max_columns", None)

MIN_EVENTOS_WEIBULL = 10      # mínimo de eventos para ajustar Weibull por componente
MIN_EVENTOS_MTBF = 5          # con menos de 5 fallas no se calcula MTBF (criterio de la pestaña)
RECORTE_MTTR = 0.10           # MTTR recortado: se quita el 10 % más corto y el 10 % más largo
MESES_TENDENCIA = 6
UMBRAL_EQUIV = 0.34           # parecido mínimo para mostrar una tarea del borrador como "vigente"

# ---------------------------------------------------------------------------
# 0. Entradas
# ---------------------------------------------------------------------------
diario = pd.read_csv("base_diaria_componentes.csv", parse_dates=["Fecha"])
avisos = pd.read_csv("avisos_clasificados.csv", parse_dates=["Fecha_aviso"])
catalogo = pd.read_csv("catalogo_componentes.csv")
ot = pd.read_csv("ot_limpias.csv", parse_dates=["Fecha_programada"])

if os.path.exists(ARCHIVO_EQUIV_VALIDADAS):
    equiv = pd.read_excel(ARCHIVO_EQUIV_VALIDADAS)
    col_val = [c for c in equiv.columns if c.startswith("Valida")][0]
    equiv = equiv[equiv[col_val].astype(str).str.strip().str.lower().isin(["sí", "si"])]
    equiv["Estado_equivalencia"] = "Validada"
else:
    equiv = pd.read_excel("equivalencias_borrador.xlsx")
    equiv = equiv[(equiv["Rango"] == 1) & (equiv["Similitud"] >= UMBRAL_EQUIV)]
    equiv["Estado_equivalencia"] = "Propuesta automática"
tarea_vigente = equiv.drop_duplicates("Id_componente").set_index("Id_componente")

fecha_corte = diario["Fecha"].max()
print(f"Reporte diario: {diario['Fecha'].min():%d-%m-%Y} a {fecha_corte:%d-%m-%Y} | "
      f"Equivalencias: {equiv['Estado_equivalencia'].iat[0] if len(equiv) else 'ninguna'}")

# ---------------------------------------------------------------------------
# 1. Eventos: unir registros del mismo componente en horas consecutivas
# ---------------------------------------------------------------------------
diario["Pos_operativa"], _ = posicion_operativa(diario)
diario["Momento"] = diario["Fecha"] + pd.to_timedelta(diario["Hora"], unit="h")
diario = diario.sort_values(["Id_componente", "Momento"])
horas_desde_anterior = diario.groupby("Id_componente")["Momento"].diff().dt.total_seconds() / 3600
diario["Nuevo_evento"] = horas_desde_anterior.isna() | (horas_desde_anterior > 1)
diario["Evento"] = diario.groupby("Id_componente")["Nuevo_evento"].cumsum()

eventos = (diario.groupby(["Id_componente", "Evento"])
                 .agg(Maquina=("Maquina", "first"), Inicio=("Momento", "min"),
                      Pos=("Pos_operativa", "min"), Minutos=("Minutos de paro", "sum"),
                      Turno=("Turno", "first"))
                 .reset_index())
print(f"Registros diarios: {len(diario):,} -> eventos: {len(eventos):,} "
      f"({1 - len(eventos)/len(diario):.0%} de los registros eran continuación de un paro)")


# ---------------------------------------------------------------------------
# 2. Indicadores por componente desde el reporte diario
# ---------------------------------------------------------------------------
def forma_weibull(tbf):
    """Ajusta Weibull a los tiempos entre eventos (días operativos). Devuelve beta, eta y lectura."""
    tbf = tbf[tbf > 0]
    if len(tbf) < MIN_EVENTOS_WEIBULL:
        return np.nan, np.nan, "Pocos datos"
    beta, _, eta = stats.weibull_min.fit(tbf, floc=0)
    if beta > 1.2:
        lectura = "Desgaste (preventivo por tiempo)"
    elif beta >= 0.8:
        lectura = "Aleatoria (inspección o condición)"
    else:
        lectura = "En racha (revisar calidad de la reparación)"
    return beta, eta, lectura


def tendencia(ev):
    fin = fecha_corte
    medio = fin - pd.DateOffset(months=MESES_TENDENCIA)
    ini = medio - pd.DateOffset(months=MESES_TENDENCIA)
    reciente = ev.loc[ev["Inicio"] > medio, "Minutos"].sum()
    anterior = ev.loc[(ev["Inicio"] > ini) & (ev["Inicio"] <= medio), "Minutos"].sum()
    n = ((ev["Inicio"] > ini)).sum()
    if n < 5:
        return "Pocos datos", reciente, anterior
    if anterior == 0:
        return "Empeora", reciente, anterior
    razon = reciente / anterior
    return ("Empeora" if razon > 1.2 else "Mejora" if razon < 0.8 else "Estable"), reciente, anterior


def limite_atipico(minutos):
    """Paro atípico = más largo que Q3 + 1,5 x rango intercuartílico de ESE componente."""
    q1, q3 = minutos.quantile([0.25, 0.75])
    return q3 + 1.5 * (q3 - q1)


filas = []
for idc, ev in eventos.sort_values("Inicio").groupby("Id_componente"):
    tbf = ev["Pos"].diff().dropna().values
    beta, eta, lectura = forma_weibull(tbf)
    tend, rec, ant = tendencia(ev)
    filas.append({
        "Id_componente": idc,
        "Eventos": len(ev),
        "Minutos_paro": ev["Minutos"].sum(),
        "MTTR_media_min": ev["Minutos"].mean(),
        "MTTR_mediana_min": ev["Minutos"].median(),
        "MTTR_p90_min": ev["Minutos"].quantile(0.9),
        "MTTR_recortado_min": stats.trim_mean(ev["Minutos"], RECORTE_MTTR),
        "Eventos_atipicos": int((ev["Minutos"] > limite_atipico(ev["Minutos"])).sum()) if len(ev) >= MIN_EVENTOS_MTBF else 0,
        "Datos_suficientes": len(ev) >= MIN_EVENTOS_MTBF,
        "MTBF_media_dias": tbf.mean() if len(tbf) and len(ev) >= MIN_EVENTOS_MTBF else np.nan,
        "MTBF_mediana_dias": np.median(tbf) if len(tbf) and len(ev) >= MIN_EVENTOS_MTBF else np.nan,
        "Ultimo_evento": ev["Inicio"].max().normalize(),
        "Tendencia": tend, "Minutos_ult_6m": rec, "Minutos_6m_previos": ant,
        "Weibull_beta": beta, "Weibull_eta_dias": eta, "Forma_falla": lectura,
        "Turno_mas_frecuente": ev["Turno"].mode().iat[0],
    })
ind_diario = pd.DataFrame(filas)
# MTBF en HORAS de operación, como lo muestra la pestaña (1 día operativo = 24 h de operación)
ind_diario["MTBF_media_h"] = ind_diario["MTBF_media_dias"] * 24
ind_diario["MTBF_mediana_h"] = ind_diario["MTBF_mediana_dias"] * 24

# ---------------------------------------------------------------------------
# 3. Indicadores por componente desde SAP
# ---------------------------------------------------------------------------
fallas = avisos[(avisos["Tipo_aviso"] == "Falla") & avisos["Id_componente"].notna()].sort_values("Fecha_aviso")


def top_sintomas(s, n=3):
    c = s.dropna().value_counts().head(n)
    return "; ".join(f"{k} ({v})" for k, v in c.items())


ind_sap = (fallas.groupby("Id_componente")
                 .agg(Avisos=("Aviso", "size"),
                      Dias_entre_avisos_mediana=("Fecha_aviso", lambda f: f.diff().dt.days.median()),
                      Ultimo_aviso=("Fecha_aviso", "max"),
                      Costo_correctivo=("Costo_real_orden", "sum"),
                      Sintomas=("Sintoma", top_sintomas),
                      Modo_principal=("Modo_falla", lambda m: m.mode().iat[0] if m.notna().any() else np.nan))
                 .reset_index())

# ---------------------------------------------------------------------------
# 4. Ficha de componente = catálogo + diario + SAP + tarea vigente
# ---------------------------------------------------------------------------
hc = (catalogo[["Id_componente", "Maquina", "Sistema", "Componente", "Fuente"]]
        .merge(ind_diario, on="Id_componente", how="left")
        .merge(ind_sap, on="Id_componente", how="left"))
for c in ["Eventos", "Minutos_paro", "Avisos", "Costo_correctivo", "Eventos_atipicos"]:
    hc[c] = hc[c].fillna(0)
hc["Datos_suficientes"] = hc["Datos_suficientes"].fillna(False).astype(bool)
hc["Sistema"] = hc["Sistema"].fillna("Sin sistema")
hc["Tendencia"] = hc["Tendencia"].fillna("Sin registros diarios")
hc["Tarea_vigente"] = hc["Id_componente"].map(tarea_vigente["Tarea"])
hc["Tipo_tarea_vigente"] = hc["Id_componente"].map(tarea_vigente["Tipo_tarea"])
hc["Frecuencia_tarea_sem"] = hc["Id_componente"].map(tarea_vigente["Frecuencia_sem"])
hc["Estado_equivalencia"] = hc["Id_componente"].map(tarea_vigente["Estado_equivalencia"]) \
                              .fillna("Sin tarea identificada")
hc["Confianza"] = np.select([hc["Eventos"] >= 10, hc["Eventos"] >= 5], ["Alta", "Media"], "Baja")
hc["Maquina"] = pd.Categorical(hc["Maquina"], MAQ4, ordered=True)
hc = hc.sort_values(["Maquina", "Minutos_paro"], ascending=[True, False]).reset_index(drop=True)

# Participación dentro de la máquina (para saber cuánto pesa cada componente)
hc["Pct_minutos_maquina"] = 100 * hc["Minutos_paro"] / hc.groupby("Maquina", observed=True)["Minutos_paro"].transform("sum")

# ---------------------------------------------------------------------------
# 5. Fichas de sistema y de máquina
# ---------------------------------------------------------------------------
ev_cat = eventos.merge(catalogo[["Id_componente", "Sistema"]], on="Id_componente", how="left")
ev_cat["Sistema"] = ev_cat["Sistema"].fillna("Sin sistema")


def agregar(ev_grupo):
    return pd.Series({"Eventos": len(ev_grupo), "Minutos_paro": ev_grupo["Minutos"].sum(),
                      "MTTR_media_min": ev_grupo["Minutos"].mean(),
                      "MTTR_mediana_min": ev_grupo["Minutos"].median()})


hs = ev_cat.groupby(["Maquina", "Sistema"]).apply(agregar, include_groups=False).reset_index()
sap_sis = hc.groupby(["Maquina", "Sistema"], observed=True)[["Avisos", "Costo_correctivo"]].sum().reset_index()
sap_sis["Maquina"] = sap_sis["Maquina"].astype(str)
hs = hs.merge(sap_sis, on=["Maquina", "Sistema"], how="outer").fillna({"Eventos": 0, "Minutos_paro": 0})
comp_top = hc.sort_values("Minutos_paro", ascending=False).drop_duplicates(["Maquina", "Sistema"])
comp_top = comp_top.assign(Maquina=comp_top["Maquina"].astype(str)).set_index(["Maquina", "Sistema"])["Componente"]
hs["Componente_principal"] = [comp_top.get((m, s), np.nan) for m, s in zip(hs["Maquina"], hs["Sistema"])]
hs = hs.sort_values(["Maquina", "Minutos_paro"], ascending=[True, False])

hm = eventos.groupby("Maquina").apply(agregar, include_groups=False).reindex(MAQ4)
hm["Avisos_falla"] = fallas.groupby("Maquina").size().reindex(MAQ4)
costos = (ot[ot["En_alcance"]].pivot_table(index="Maquina", columns="Tipo_orden", values="Costo_real",
                                            aggfunc="sum", fill_value=0).reindex(MAQ4))
for tipo in ["Preventiva", "Predictiva", "Correctiva"]:
    hm[f"Costo_{tipo.lower()}"] = costos[tipo] if tipo in costos else 0
hm["Componentes"] = hc.groupby("Maquina", observed=True).size()
hm["Componente_principal"] = hc.drop_duplicates("Maquina").set_index("Maquina")["Componente"]
hm = hm.reset_index()

# ---------------------------------------------------------------------------
# 6. Serie mensual por componente (para la gráfica de evolución en la web)
# ---------------------------------------------------------------------------
eventos["Mes"] = eventos["Inicio"].dt.to_period("M").astype(str)
fallas = fallas.assign(Mes=fallas["Fecha_aviso"].dt.to_period("M").astype(str))
serie = (eventos.groupby(["Id_componente", "Mes"]).agg(Eventos=("Evento", "size"), Minutos=("Minutos", "sum"))
         .join(fallas.groupby(["Id_componente", "Mes"]).size().rename("Avisos"), how="outer")
         .fillna(0).reset_index())

# ---------------------------------------------------------------------------
# 7. Resumen en pantalla
# ---------------------------------------------------------------------------
print("\n--- Ficha por máquina ---")
print(hm[["Maquina", "Eventos", "Minutos_paro", "MTTR_media_min", "MTTR_mediana_min", "Avisos_falla",
          "Costo_correctiva", "Componente_principal"]].round(1).to_string(index=False))

cols = ["Componente", "Eventos", "Minutos_paro", "MTTR_media_min", "MTTR_mediana_min", "MTTR_recortado_min",
        "Eventos_atipicos", "MTBF_mediana_h", "Avisos", "Tendencia", "Forma_falla"]
for m in MAQ4:
    print(f"\n--- {m}: top 5 componentes por minutos de paro ---")
    print(hc[hc["Maquina"] == m][cols].head(5).round(1).to_string(index=False))

sesgo = hc[(hc["Eventos"] >= 10) & (hc["MTTR_media_min"] > 1.5 * hc["MTTR_mediana_min"])]
print(f"\nComponentes donde la media del MTTR supera en 50 % a la mediana (pocos paros muy largos): {len(sesgo)}")
print(sesgo[["Maquina", "Componente", "Eventos", "MTTR_media_min", "MTTR_mediana_min", "MTTR_p90_min"]]
        .round(1).to_string(index=False))

print("\n--- Forma de la falla (componentes con 10 o más eventos) ---")
print(pd.crosstab(hc["Maquina"], hc["Forma_falla"]).drop(columns=["Pocos datos"], errors="ignore"))
print("\n--- Tendencia de los últimos 6 meses ---")
print(pd.crosstab(hc["Maquina"], hc["Tendencia"]))

# ---------------------------------------------------------------------------
# 8. Guardar (CSV para análisis + JSON anidado para la web)
# ---------------------------------------------------------------------------
hc.to_csv("hc_componentes.csv", index=False)
hs.to_csv("hc_sistemas.csv", index=False)
hm.to_csv("hc_maquinas.csv", index=False)
serie.to_csv("hc_series_mensuales.csv", index=False)


def limpio(d):
    """Convierte un registro a tipos que JSON entiende (NaN -> null, fechas -> texto)."""
    out = {}
    for k, v in d.items():
        if not isinstance(v, (list, dict, str)) and pd.isna(v):
            out[k] = None
        elif isinstance(v, (pd.Timestamp,)):
            out[k] = v.strftime("%Y-%m-%d")
        elif isinstance(v, (float, np.floating)):
            out[k] = None if np.isnan(v) else round(float(v), 2)
        elif isinstance(v, (np.integer,)):
            out[k] = int(v)
        else:
            out[k] = None if (v is None or (isinstance(v, float) and np.isnan(v))) else v
    return out


serie_por_comp = {k: g.drop(columns="Id_componente").to_dict("records") for k, g in serie.groupby("Id_componente")}
salida = {"generado_con_datos_hasta": fecha_corte.strftime("%Y-%m-%d"), "maquinas": []}
for _, m in hm.iterrows():
    nodo_m = limpio(m.to_dict())
    nodo_m["sistemas"] = []
    for _, s in hs[hs["Maquina"] == m["Maquina"]].iterrows():
        nodo_s = limpio(s.drop(labels="Maquina").to_dict())
        comps = hc[(hc["Maquina"] == m["Maquina"]) & (hc["Sistema"] == s["Sistema"])]
        nodo_s["componentes"] = []
        for _, c in comps.iterrows():
            nodo_c = limpio(c.drop(labels=["Maquina", "Sistema"]).to_dict())
            nodo_c["serie_mensual"] = [limpio(r) for r in serie_por_comp.get(c["Id_componente"], [])]
            nodo_s["componentes"].append(nodo_c)
        nodo_m["sistemas"].append(nodo_s)
    salida["maquinas"].append(nodo_m)
with open("historia_clinica.json", "w", encoding="utf-8") as f:
    json.dump(salida, f, ensure_ascii=False, indent=1)

print("\nGuardado: hc_componentes.csv, hc_sistemas.csv, hc_maquinas.csv, hc_series_mensuales.csv, "
      "historia_clinica.json")
