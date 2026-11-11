"""
KOF-Smart Maintenance AI — Fase 19: AVISOS INTELIGENTES (alertas tempranas)
(código puro)                                Alimenta: "Alertas tempranas" de la pestaña Mantenimiento

PREGUNTA QUE RESPONDE: "Esta semana, ¿qué está pasando que merezca atención antes de que falle?"
Funciona como las apps de seguimiento de salud: compara el historial de cada componente con su
propio patrón y avisa cuando algo se sale de lo normal. Cada alerta dice en qué se basa.

Requiere: salidas de las Fases 13 a 18 (base_diaria_componentes.csv, avisos_clasificados.csv,
          hc_componentes.csv, ot_limpias.csv, recomendaciones_fusion.csv)

TIPOS DE ALERTA Y SU REGLA (los umbrales son valores iniciales, ajustables con mantenimiento)
  1. Ciclo            El componente lleva sin avisos entre 0,8 y 1,5 veces su intervalo mediano
                      entre avisos (mínimo 3 avisos): "entra en su ventana habitual".
  2. Paros en aumento Minutos de paro de las últimas 4 semanas ≥ 1,5 veces su promedio de 4 semanas
                      del semestre anterior, y al menos 60 minutos.
  3. Síntomas         En las últimas 8 semanas hay 2 o más avisos con el mismo síntoma en un sistema,
                      y es al menos el doble de lo normal para 8 semanas.
  4. Estacionalidad   En las mismas 6 semanas del año pasado el componente tuvo 2 o más avisos.
  5. Cumplimiento     Una orden predictiva vence en las próximas 3 semanas, o la máquina acumula
                      3 o más preventivas sin ejecutar en las últimas 8 semanas.
  6. Brecha del plan  Componente de prioridad Crítica cuya tarea no lo cubre o es muy espaciada (Fase 17).
  (7. Oportunidad     Se genera en la Fase 20, porque depende del calendario de paradas.)

REGLAS DE HONESTIDAD
  - Nunca dice "va a fallar": dice "se ha repetido", "entra en su ventana", "el año pasado pasó".
  - Cada alerta trae su evidencia (cuántos datos) y un nivel de confianza.
  - Se muestran máximo 5 por semana (columna Mostrar), ordenadas por prioridad del componente,
    una por componente y máximo 2 del mismo tipo. Las demás quedan disponibles en "Ver todas".

FECHA DE REFERENCIA: en la app es HOY. En Colab se usa la fecha más reciente de los datos.

Salidas: alertas.csv y alertas.json
"""
import json

import numpy as np
import pandas as pd
from kof_planes_utils import MAQ4

pd.set_option("display.width", 220)
pd.set_option("display.max_colwidth", 120)

from kof_planes_utils import FECHA_REFERENCIA   # None = fecha más reciente de los datos; en la app: hoy
MAX_ALERTAS_VISIBLES = 5

diario = pd.read_csv("base_diaria_componentes.csv", parse_dates=["Fecha"])
avisos = pd.read_csv("avisos_clasificados.csv", parse_dates=["Fecha_aviso"])
hc = pd.read_csv("hc_componentes.csv")
ot = pd.read_csv("ot_limpias.csv", parse_dates=["Fecha_programada"])
fus = pd.read_csv("recomendaciones_fusion.csv")

ref = pd.Timestamp(FECHA_REFERENCIA) if FECHA_REFERENCIA else max(diario["Fecha"].max(), avisos["Fecha_aviso"].max())
ref_diario = min(ref, diario["Fecha"].max())          # el diario puede llegar hasta una fecha anterior
print(f"Fecha de referencia: {ref:%d-%m-%Y} (reporte diario hasta {diario['Fecha'].max():%d-%m-%Y})")

nivel = fus.set_index("Id_componente")["Prioridad"]
ipm = fus.set_index("Id_componente")["IPM"]
fallas = avisos[(avisos["Tipo_aviso"] == "Falla") & avisos["Id_componente"].notna()]
alertas = []
MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre",
         "octubre", "noviembre", "diciembre"]


def fecha_es(d):
    return f"{d.day} de {MESES[d.month - 1]}"


def agregar(tipo, fila_comp, mensaje, evidencia, n_datos, accion, enlace):
    idc = fila_comp.get("Id_componente") if isinstance(fila_comp, (dict, pd.Series)) else None
    alertas.append({
        "Tipo": tipo, "Maquina": fila_comp["Maquina"], "Componente": fila_comp.get("Componente", None),
        "Id_componente": idc, "Prioridad": nivel.get(idc, "Media") if idc else "Media",
        "IPM": ipm.get(idc, np.nan) if idc else np.nan, "Mensaje": mensaje, "Evidencia": evidencia,
        "Confianza": "Alta" if n_datos >= 10 else "Media" if n_datos >= 5 else "Baja",
        "Accion_sugerida": accion, "Enlace": enlace})


# ---------------------------------------------------------------------------
# 1. Ciclo: entra en su ventana habitual entre avisos
# ---------------------------------------------------------------------------
for _, c in hc[(hc["Avisos"] >= 3) & hc["Dias_entre_avisos_mediana"].notna()].iterrows():
    ult = fallas.loc[fallas["Id_componente"] == c["Id_componente"], "Fecha_aviso"].max()
    dias = (ref - ult).days
    razon = dias / c["Dias_entre_avisos_mediana"]
    if 0.8 <= razon <= 1.5:
        estado = "se acerca a" if razon < 1 else "ya pasó"
        agregar("Ciclo", c,
                f"{c['Componente']} ({c['Maquina']}) {estado} su intervalo habitual entre fallas: el último "
                f"aviso fue hace {dias} días y suele repetirse cada {c['Dias_entre_avisos_mediana']:.0f} días.",
                f"{int(c['Avisos'])} avisos 2024-2026; último el {ult:%d-%m-%Y}", c["Avisos"],
                "Revisarlo en la próxima parada", "plan")

# ---------------------------------------------------------------------------
# 2. Paros en aumento (reporte diario)
# ---------------------------------------------------------------------------
reciente = diario[diario["Fecha"] > ref_diario - pd.Timedelta(weeks=4)]
previo = diario[(diario["Fecha"] <= ref_diario - pd.Timedelta(weeks=4)) &
                (diario["Fecha"] > ref_diario - pd.Timedelta(weeks=30))]
m_rec = reciente.groupby("Id_componente")["Minutos de paro"].sum()
m_prev = previo.groupby("Id_componente")["Minutos de paro"].sum() / (26 / 4)   # promedio por 4 semanas
for idc, mins in m_rec.items():
    base = m_prev.get(idc, 0)
    if mins >= 60 and mins >= 1.5 * max(base, 1):
        c = hc[hc["Id_componente"] == idc].iloc[0]
        veces = (f"{mins / base:.1f} veces".replace(".", ",")) if base > 0 else "muy por encima de"
        agregar("Paros en aumento", c,
                f"Los paros por {c['Componente'].lower()} ({c['Maquina']}) están {veces} su nivel habitual: "
                f"{mins:,.0f} min en las últimas 4 semanas frente a {base:,.0f} min de promedio.",
                f"Reporte diario hasta {ref_diario:%d-%m-%Y}", int((reciente["Id_componente"] == idc).sum()),
                "Revisar el componente y su último ajuste", "componente")

# ---------------------------------------------------------------------------
# 3. Síntomas que se acumulan (avisos)
# ---------------------------------------------------------------------------
sint = avisos.dropna(subset=["Sintoma", "Sistema"])
sint_rec = sint[sint["Fecha_aviso"] > ref - pd.Timedelta(weeks=8)]
semanas_hist = max((ref - sint["Fecha_aviso"].min()).days / 7, 8)
for (maq, sis, s), g in sint_rec.groupby(["Maquina", "Sistema", "Sintoma"]):
    total = ((sint["Maquina"] == maq) & (sint["Sistema"] == sis) & (sint["Sintoma"] == s)).sum()
    normal = total / semanas_hist * 8
    if len(g) >= 2 and len(g) >= 2 * normal:
        comp = g["Componente_catalogo"].mode()
        fila = {"Maquina": maq, "Componente": comp.iat[0] if len(comp) else sis,
                "Id_componente": g["Id_componente"].mode().iat[0] if g["Id_componente"].notna().any() else None}
        agregar("Síntomas", fila,
                f"En las últimas 8 semanas van {len(g)} avisos de '{s.lower()}' en el {sis.lower()} de la "
                f"{maq}, cuando lo normal es {normal:.1f} en ese tiempo.",
                f"{total} avisos con ese síntoma desde 2024", total, "Inspeccionar el sistema antes de que escale",
                "componente")

# ---------------------------------------------------------------------------
# 4. Estacionalidad: el año pasado en esta época
# ---------------------------------------------------------------------------
ini, fin = ref - pd.DateOffset(years=1), ref - pd.DateOffset(years=1) + pd.Timedelta(weeks=6)
hace_un_ano = fallas[(fallas["Fecha_aviso"] >= ini) & (fallas["Fecha_aviso"] <= fin)]
for idc, g in hace_un_ano.groupby("Id_componente"):
    if len(g) >= 2:
        c = hc[hc["Id_componente"] == idc].iloc[0]
        agregar("Estacionalidad", c,
                f"El año pasado, entre el {fecha_es(ini)} y el {fecha_es(fin)}, {c['Componente'].lower()} de la "
                f"{c['Maquina']} tuvo {len(g)} avisos de falla. Conviene anticiparse esta temporada.",
                f"Avisos del {ini:%d-%m-%Y} al {fin:%d-%m-%Y}", len(g), "Programar una revisión preventiva", "plan")

# ---------------------------------------------------------------------------
# 5. Cumplimiento: predictivas por vencer y preventivas sin ejecutar
# ---------------------------------------------------------------------------
pred = ot[(ot["Tipo_orden"] == "Predictiva") & (ot["Estado"] == "Abierta") &
          (ot["Fecha_programada"].between(ref - pd.Timedelta(days=14), ref + pd.Timedelta(weeks=3)))]
for _, o in pred.iterrows():
    agregar("Cumplimiento", {"Maquina": o["Maquina"], "Componente": None},
            f"La orden predictiva '{o['Texto']}' de la {o['Maquina']} está programada para el "
            f"{o['Fecha_programada']:%d-%m-%Y}. Es la única medición de condición que tiene la línea: no dejarla pasar.",
            f"Orden {o['Orden']}", 10, "Ejecutar y comparar con la medición anterior", "plan")
noej = ot[(ot["Tipo_orden"] == "Preventiva") & (ot["Estado"] == "No ejecutada") &
          (ot["Fecha_programada"] > ref - pd.Timedelta(weeks=8))]
for maq, g in noej.groupby("Maquina"):
    if len(g) >= 3:
        agregar("Cumplimiento", {"Maquina": maq, "Componente": None},
                f"La {maq} acumula {len(g)} órdenes preventivas sin ejecutar en las últimas 8 semanas.",
                "; ".join(g["Texto"].head(3)), len(g), "Reprogramarlas en las próximas paradas", "calendario")

# ---------------------------------------------------------------------------
# 6. Brecha del plan en componentes críticos
# ---------------------------------------------------------------------------
for _, r in fus[(fus["Prioridad"] == "Crítica") & fus["Acciones"].str.contains("Crear|Acortar|Cumplir", na=False)].iterrows():
    agregar("Brecha del plan", r,
            f"{r['Componente']} ({r['Maquina']}) es prioridad crítica y su plan preventivo no está a la altura: "
            f"{r['Accion_principal'].lower()}.", r["Justificacion"][:200], r["Avisos"], r["Tarea_sugerida"], "plan")

# ---------------------------------------------------------------------------
# 7. Ordenar, limitar y guardar
# ---------------------------------------------------------------------------
al = pd.DataFrame(alertas)
orden_niv = {"Crítica": 0, "Alta": 1, "Media": 2, "Baja": 3, "Sin datos": 4}
peso_tipo = {"Ciclo": 0, "Paros en aumento": 1, "Síntomas": 1, "Cumplimiento": 2, "Estacionalidad": 3,
             "Brecha del plan": 4}
al["_n"] = al["Prioridad"].map(orden_niv)
al["_t"] = al["Tipo"].map(peso_tipo)
al = al.sort_values(["_n", "_t", "IPM"], ascending=[True, True, False]).drop(columns=["_n", "_t"])
# Vista principal: máximo 5, una por componente y máximo 2 del mismo tipo, para que la semana
# muestre situaciones distintas y no cinco alertas iguales. Las demás quedan en "Ver todas".
MAX_POR_TIPO = 2
al["Mostrar"] = False
vistos, por_tipo = set(), {}
for i, r in al.iterrows():
    clave = (r["Maquina"], r["Componente"], r["Tipo"] if r["Componente"] is None else "")
    if (clave not in vistos and por_tipo.get(r["Tipo"], 0) < MAX_POR_TIPO
            and al["Mostrar"].sum() < MAX_ALERTAS_VISIBLES):
        al.at[i, "Mostrar"] = True
        vistos.add(clave)
        por_tipo[r["Tipo"]] = por_tipo.get(r["Tipo"], 0) + 1
al.insert(0, "Id_alerta", [f"AL-{i + 1:03d}" for i in range(len(al))])
al["Fecha_referencia"] = ref.strftime("%Y-%m-%d")

print(f"\nAlertas generadas: {len(al)}")
print(al["Tipo"].value_counts())
print("\n--- Las 5 que se muestran en la pestaña ---")
for _, r in al[al["Mostrar"]].iterrows():
    print(f"\n[{r['Prioridad'].upper()} · {r['Tipo']} · {r['Maquina']}]  confianza {r['Confianza'].lower()}\n"
          f"  {r['Mensaje']}\n  Evidencia: {r['Evidencia']}\n  Acción: {r['Accion_sugerida']}")

al.to_csv("alertas.csv", index=False)
with open("alertas.json", "w", encoding="utf-8") as fh:
    json.dump({"fecha_referencia": ref.strftime("%Y-%m-%d"),
               "alertas": json.loads(al.to_json(orient="records", force_ascii=False))}, fh, ensure_ascii=False, indent=1)
print("\nGuardado: alertas.csv, alertas.json")
