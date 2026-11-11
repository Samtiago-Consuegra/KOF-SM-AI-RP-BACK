"""
KOF-Smart Maintenance AI — Fase 13: Catálogo de componentes, equivalencias con el plan
y fusión con el reporte diario de fallas (código puro, sin gráficas)

PREGUNTA QUE RESPONDE: "¿Qué componentes tiene cada máquina, cómo se llaman en cada fuente
y qué tarea del plan preventivo los cubre?"

Requiere:
  kof_planes_utils.py y las salidas de la Fase 12 (avisos_limpios.csv, plan_pm_limpio.csv)
  Opcional pero recomendado: kof_utils.py y el Excel diario de fallas (el mismo de las fases 1-11).
  Si no están, la fase corre solo con SAP y lo avisa.

Pasos:
  1. Componentes del reporte diario (Subclave de Paro) separados en componente + modo (D6).
  2. Catálogo único por máquina: componentes de SAP y del diario, unidos por nombre.
  3. Recuperación del componente en avisos de falla SIN causa codificada, comparando su
     texto con los componentes conocidos de esa máquina. Queda marcado como "Sugerido".
  4. Borrador de equivalencias componente <-> tarea del plan, para VALIDAR con mantenimiento.

Salidas:
  catalogo_componentes.csv     una fila por máquina + componente, con fuentes y volumen
  catalogo.json                máquina -> sistema -> componente y clases de orden (menús de la app)
  avisos_clasificados.csv      avisos con componente final y su origen (SAP / Sugerido / Sin clasificar)
  base_diaria_componentes.csv  reporte diario con componente y modo (si hay base diaria)
  equivalencias_borrador.xlsx  hasta 3 tareas candidatas por componente, con columna para validar

Cómo funciona la comparación de textos (sin inteligencia artificial, 100 % reproducible):
  Cada texto se reduce a "raíces" (palabra sin plural, primeras 6 letras) de sus palabras útiles, después de aplicar un
  pequeño diccionario de sinónimos de planta. Dos textos se parecen según la proporción de raíces
  que comparten (índice de Jaccard: 0 = nada en común, 1 = idénticos).
"""
import os
import re

import numpy as np
import pandas as pd
from kof_planes_utils import (MAQ4, norm, separar_causa, unificar_componentes, PALABRAS_VACIAS,
                              reparar_truncado, LARGO_TRUNCADO)

pd.set_option("display.width", 200)
pd.set_option("display.max_columns", None)

UMBRAL_SUGERENCIA = 0.34      # parecido mínimo para sugerir un componente a un aviso sin causa
UMBRAL_EQUIVALENCIA = 0.20    # parecido mínimo para proponer una tarea del plan como candidata
from kof_planes_utils import ARCHIVO_EQUIV_VALIDADAS as ARCHIVO_VALIDADO   # cuando mantenimiento lo devuelva

# Sinónimos de planta: se reemplazan ANTES de comparar. Editable.
SINONIMOS = {
    r"\bbaquetas?\b": "barra envolvente",
    r"\bcorreas?\b": "banda",
    r"\bfotocel\w*": "sensor",
    r"\bchumaceras?\b": "rodamiento",
    r"\brod\b": "rodamiento",
    r"\bmotoreductor\w*|\bmotorreductor\w*": "motorreductor",
    r"\bvalv\b|\bvalvs\b": "valvula",
    r"\bcad\b": "cadena",
    r"\btransm\b": "transmision",
    r"\bsensores\b": "sensor",
    r"\bpelicula\b|\bfilm\b|\bpelex\b": "pelicula",
}


def raices_texto(texto):
    t = norm(texto)
    for patron, reemplazo in SINONIMOS.items():
        t = re.sub(patron, reemplazo, t)
    palabras = re.findall(r"[a-z]{3,}", t)
    return {raiz(p) for p in palabras if p not in PALABRAS_VACIAS}


def raiz(palabra):
    """Raíz simple: quita plural y 'e' final, y toma 6 letras.
    'peines' y 'peine' -> 'pein'; 'transportes' -> 'transp'; 'transmision' -> 'transm'."""
    w = palabra
    if len(w) > 4 and w.endswith("s"):
        w = w[:-1]
    if len(w) > 4 and w.endswith("e"):
        w = w[:-1]
    return w[:6]


def jaccard(a, b):
    return len(a & b) / len(a | b) if a and b else 0.0


# ---------------------------------------------------------------------------
# 0. Entradas
# ---------------------------------------------------------------------------
avisos = pd.read_csv("avisos_limpios.csv", parse_dates=["Fecha_aviso"])
plan = pd.read_csv("plan_pm_limpio.csv")
a4 = avisos[avisos["En_alcance"]].copy()

# Reporte diario: misma función de las fases anteriores (una sola versión del sistema)
diario = None
from kof_planes_utils import ARCHIVO_DIARIO
from kof_utils import cargar_base
if os.path.exists(ARCHIVO_DIARIO):
    diario = cargar_base(ARCHIVO_DIARIO)
    print(f"Reporte diario cargado: {len(diario):,} registros ({os.path.basename(ARCHIVO_DIARIO)})")
else:
    print("AVISO: no se encontró el reporte diario (un Excel cuyo nombre contenga 'BD', 'Reporte' y 'SAP'). "
          "La fase corre solo con SAP.")

# ---------------------------------------------------------------------------
# 1. Componente y modo en el reporte diario (misma regla D6 que en SAP)
# ---------------------------------------------------------------------------
if diario is not None:
    # El reporte diario corta la subclave en 30 caracteres: se completa con los nombres
    # completos de la misma máquina antes de separar componente y modo.
    diario["Subclave_original"] = diario["Subclave de Paro"]
    for m in MAQ4:
        en_m = diario["Maquina"] == m
        completos = set(a4.loc[a4["Maquina"] == m, "Causa_SAP"].dropna()) | \
                    {s for s in diario.loc[en_m, "Subclave de Paro"].dropna().unique()
                     if len(s) < LARGO_TRUNCADO}
        diario.loc[en_m, "Subclave de Paro"] = diario.loc[en_m, "Subclave de Paro"].map(
            lambda s: reparar_truncado(s, completos))
    reparados = diario["Subclave de Paro"].ne(diario["Subclave_original"]).sum()
    print(f"Subclaves cortadas a 30 caracteres y completadas: {reparados:,} registros")
    partes = diario["Subclave de Paro"].map(separar_causa)
    diario["Componente"] = unificar_componentes(partes.str[0])
    diario["Modo_falla"] = partes.str[1]
    diario = diario.rename(columns={"Clave 1 de Paro": "Sistema_diario"})
    resumen_diario = (diario.groupby(["Maquina", "Componente"])
                            .agg(registros_diario=("Minutos de paro", "size"),
                                 minutos_diario=("Minutos de paro", "sum"),
                                 sistema_diario=("Sistema_diario", lambda s: s.mode().iat[0]
                                                 if s.notna().any() else np.nan))
                            .reset_index())
else:
    resumen_diario = pd.DataFrame(columns=["Maquina", "Componente", "registros_diario",
                                           "minutos_diario", "sistema_diario"])

# ---------------------------------------------------------------------------
# 2. Catálogo único por máquina
# ---------------------------------------------------------------------------
fallas = a4[a4["Tipo_aviso"] == "Falla"]
resumen_sap = (fallas.dropna(subset=["Componente"])
                     .groupby(["Maquina", "Componente"])
                     .agg(avisos=("Aviso", "size"),
                          sistema_sap=("Sistema", lambda s: s.mode().iat[0] if s.notna().any() else np.nan),
                          costo_real=("Costo_real_orden", "sum"))
                     .reset_index())

# Unión por nombre normalizado; si no coincide exacto, se busca el más parecido en la otra fuente
resumen_sap["clave"] = resumen_sap["Componente"].map(norm)
resumen_diario["clave"] = resumen_diario["Componente"].map(norm)
if len(resumen_diario):
    for m in MAQ4:
        sap_m = resumen_sap[resumen_sap["Maquina"] == m]
        dia_m = resumen_diario[resumen_diario["Maquina"] == m]
        claves_dia = set(dia_m["clave"])
        for i, fila in sap_m.iterrows():
            if fila["clave"] in claves_dia:
                continue
            r = raices_texto(fila["Componente"])
            puntajes = dia_m.assign(p=dia_m["Componente"].map(lambda c: jaccard(r, raices_texto(c))))
            mejor = puntajes.sort_values("p", ascending=False).head(1)
            if len(mejor) and mejor["p"].iat[0] >= 0.5:
                resumen_sap.at[i, "clave"] = mejor["clave"].iat[0]

catalogo = resumen_sap.merge(resumen_diario.rename(columns={"Componente": "Componente_diario"}),
                             on=["Maquina", "clave"], how="outer")
catalogo["Componente"] = catalogo["Componente"].fillna(catalogo["Componente_diario"])
catalogo["Sistema"] = catalogo["sistema_sap"].fillna(catalogo["sistema_diario"])
catalogo["Fuente"] = np.select(
    [catalogo["avisos"].notna() & catalogo["registros_diario"].notna(), catalogo["avisos"].notna()],
    ["Ambas", "Solo SAP"], default="Solo diario")
for c in ["avisos", "registros_diario", "minutos_diario", "costo_real"]:
    catalogo[c] = pd.to_numeric(catalogo[c], errors="coerce").fillna(0)
catalogo["Maquina"] = pd.Categorical(catalogo["Maquina"], MAQ4, ordered=True)
catalogo = catalogo.sort_values(["Maquina", "minutos_diario", "avisos"], ascending=[True, False, False])
catalogo["Id_componente"] = (catalogo["Maquina"].astype(str).str[:3].str.upper() + "-"
                             + catalogo.groupby("Maquina", observed=True).cumcount().add(1)
                                       .astype(str).str.zfill(3))
# Mapa (máquina, nombre normalizado) -> código del catálogo. Se registran el nombre del catálogo
# y el nombre con que se unió el diario, para que AMBAS fuentes encuentren su código.
mapa_id = {}
for _, f in catalogo.iterrows():
    for clave in {norm(f["Componente"]), f["clave"]}:
        mapa_id[(str(f["Maquina"]), clave)] = (f["Id_componente"], f["Componente"])
catalogo = catalogo[["Id_componente", "Maquina", "Sistema", "Componente", "Fuente", "avisos",
                     "costo_real", "registros_diario", "minutos_diario"]]

print("\n--- Catálogo: componentes por máquina y fuente ---")
print(pd.crosstab(catalogo["Maquina"], catalogo["Fuente"], margins=True, margins_name="Total"))

# ---------------------------------------------------------------------------
# 3. Recuperar el componente de avisos de falla sin causa codificada
# ---------------------------------------------------------------------------
cat_raices = {(m, c): raices_texto(c) for m, c in zip(catalogo["Maquina"].astype(str), catalogo["Componente"])}
cat_sistema = dict(zip(zip(catalogo["Maquina"].astype(str), catalogo["Componente"]), catalogo["Sistema"]))

a4["Componente_final"] = a4["Componente"]
a4["Origen_componente"] = np.where(a4["Componente"].notna(), "SAP", "")
a4["Similitud"] = np.nan
sin_causa = (a4["Tipo_aviso"] == "Falla") & a4["Componente"].isna()

for i, fila in a4[sin_causa].iterrows():
    r = raices_texto(fila["Texto"])
    candidatos = []
    for (m, comp), rc in cat_raices.items():
        if m != fila["Maquina"]:
            continue
        p = jaccard(r, rc)
        # Si el aviso trae sistema y coincide con el del componente, se refuerza la similitud
        if pd.notna(fila["Sistema"]) and norm(fila["Sistema"]) == norm(cat_sistema.get((m, comp))):
            p += 0.10
        candidatos.append((min(p, 1.0), comp))
    candidatos.sort(reverse=True)
    if candidatos and candidatos[0][0] >= UMBRAL_SUGERENCIA and \
            (len(candidatos) == 1 or candidatos[0][0] > candidatos[1][0]):
        a4.at[i, "Componente_final"] = candidatos[0][1]
        a4.at[i, "Origen_componente"] = "Sugerido por texto"
        a4.at[i, "Similitud"] = round(candidatos[0][0], 2)
    else:
        a4.at[i, "Origen_componente"] = "Sin clasificar"

a4.loc[a4["Tipo_aviso"] != "Falla", "Origen_componente"] = "No aplica (no es falla)"
print("\n--- Avisos: origen del componente ---")
print(pd.crosstab(a4["Maquina"], a4["Origen_componente"]).reindex(MAQ4))
print("\nEjemplos de componentes sugeridos (revisar):")
print(a4[a4["Origen_componente"] == "Sugerido por texto"][["Maquina", "Texto", "Componente_final", "Similitud"]]
        .head(12).to_string(index=False))

# ---------------------------------------------------------------------------
# 4. Borrador de equivalencias componente <-> tarea del plan preventivo
# ---------------------------------------------------------------------------
tareas = plan[plan["En_alcance"] & plan["Es_tarea"]].copy()
tareas["raices"] = tareas["Tarea"].map(raices_texto)
filas = []
for _, comp in catalogo.iterrows():
    m = str(comp["Maquina"])
    r = raices_texto(comp["Componente"])
    t_m = tareas[tareas["Maquina"] == m].copy()
    t_m["Similitud"] = t_m["raices"].map(lambda x: jaccard(r, x))
    top = t_m[t_m["Similitud"] >= UMBRAL_EQUIVALENCIA].sort_values("Similitud", ascending=False).head(3)
    if top.empty:
        filas.append({**comp[["Id_componente", "Maquina", "Sistema", "Componente", "avisos",
                              "minutos_diario"]].to_dict(),
                      "Rango": np.nan, "Id_tarea": np.nan, "Tarea": "SIN TAREA CANDIDATA",
                      "Tipo_tarea": np.nan, "Frecuencia_sem": np.nan, "Duracion_h": np.nan,
                      "Similitud": 0.0})
    for rango, (_, t) in enumerate(top.iterrows(), start=1):
        filas.append({**comp[["Id_componente", "Maquina", "Sistema", "Componente", "avisos",
                              "minutos_diario"]].to_dict(),
                      "Rango": rango, "Id_tarea": t["Id_tarea"], "Tarea": t["Tarea"],
                      "Tipo_tarea": t["Tipo_tarea"], "Frecuencia_sem": t["Frecuencia_sem"],
                      "Duracion_h": t["Duracion_h"], "Similitud": round(t["Similitud"], 2)})
equiv = pd.DataFrame(filas)
equiv["Valida_mantenimiento (Sí / No / Otra tarea)"] = ""
equiv["Observación"] = ""

con_tarea = equiv.groupby("Id_componente")["Tarea"].first().ne("SIN TAREA CANDIDATA")
print(f"\nComponentes con al menos una tarea candidata: {con_tarea.sum()} de {len(con_tarea)}")
top_sin = catalogo[catalogo["Id_componente"].isin(con_tarea[~con_tarea].index)] \
            .sort_values("avisos", ascending=False).head(10)
print("Componentes con más avisos y SIN tarea candidata (posible brecha del plan):")
print(top_sin[["Maquina", "Componente", "avisos", "minutos_diario"]].to_string(index=False))

# ---------------------------------------------------------------------------
# 5. Código de catálogo en cada registro (lo usan las fases 14 en adelante)
# ---------------------------------------------------------------------------
def poner_codigo(df, col_componente):
    pares = [mapa_id.get((m, norm(c)), (np.nan, np.nan)) if pd.notna(c) else (np.nan, np.nan)
             for m, c in zip(df["Maquina"].astype(str), df[col_componente])]
    df["Id_componente"] = [p[0] for p in pares]
    df["Componente_catalogo"] = [p[1] for p in pares]
    return df

a4 = poner_codigo(a4, "Componente_final")
if diario is not None:
    diario = poner_codigo(diario, "Componente")
    print(f"\nRegistros diarios con código de catálogo: {diario['Id_componente'].notna().mean():.1%}")
print(f"Avisos de falla con código de catálogo: "
      f"{a4.loc[a4['Tipo_aviso'] == 'Falla', 'Id_componente'].notna().mean():.1%}")

# ---------------------------------------------------------------------------
# 6. Guardar
# ---------------------------------------------------------------------------
catalogo.to_csv("catalogo_componentes.csv", index=False)

# Catálogo para la app: llena los menús Máquina -> Sistema -> Componente del formulario
# "Programar parada", y trae la tabla de clases de orden.
import json
from kof_planes_utils import tabla_clases_orden
cat_json = {"maquinas": [], "clases_orden": tabla_clases_orden()}
for m in MAQ4:
    cm = catalogo[catalogo["Maquina"].astype(str) == m]
    sistemas = []
    for s, cs in cm.groupby(cm["Sistema"].fillna("Sin sistema"), sort=True):
        sistemas.append({"Sistema": s, "Componentes": [{"Id_componente": r.Id_componente, "Componente": r.Componente}
                                                         for r in cs.sort_values("Componente").itertuples()]})
    cat_json["maquinas"].append({"Maquina": m, "Sistemas": sistemas})
with open("catalogo.json", "w", encoding="utf-8") as fh:
    json.dump(cat_json, fh, ensure_ascii=False, indent=1)
a4.to_csv("avisos_clasificados.csv", index=False)
if diario is not None:
    diario.to_csv("base_diaria_componentes.csv", index=False)
with pd.ExcelWriter("equivalencias_borrador.xlsx") as xls:
    equiv.to_excel(xls, sheet_name="Equivalencias", index=False)
    pd.DataFrame({"Instrucción": [
        "Para cada componente se proponen hasta 3 tareas del plan preventivo que parecen cubrirlo.",
        "En la columna 'Valida_mantenimiento' escribir Sí, No, u 'Otra tarea: <nombre de la tarea>'.",
        "Si un componente no tiene tarea candidata, indicar si debería tenerla.",
        "Guardar el archivo como equivalencias_validadas.xlsx; las fases siguientes lo usarán.",
    ]}).to_excel(xls, sheet_name="Instrucciones", index=False)
print("\nGuardado: catalogo_componentes.csv, catalogo.json, avisos_clasificados.csv, equivalencias_borrador.xlsx"
      + (", base_diaria_componentes.csv" if diario is not None else ""))
if os.path.exists(ARCHIVO_VALIDADO):
    print(f"Se encontró {ARCHIVO_VALIDADO}: las fases siguientes usarán la versión validada.")
