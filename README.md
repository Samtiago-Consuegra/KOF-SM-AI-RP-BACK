# KOF-SMART Maintenance AI — Backend (Línea 4 BAQ)

API en FastAPI + PostgreSQL (Supabase) para el frontend
[KOF-SM-AI-RP-FRONT](https://github.com/Samtiago-Consuegra/KOF-SM-AI-RP-FRONT).
Reutiliza el backend demo [CocaColaRosaBack](https://github.com/Samtiago-Consuegra/CocaColaRosaBack)
(subida de Excel SAP + EDA) e integra el motor de predicción `kof_engine` **sin modificarlo**.

## Estructura

```
app/
  main.py               arranque: CORS, creación de tablas, RLS, rutas
  config.py             DATABASE_URL y CORS_ORIGINS desde variables de entorno
  db.py                 motor SQLAlchemy y sesión por petición
  models.py             tablas: failure_events, data_loads, model_artifacts, prediction_runs
  constants.py          máquinas críticas (tomadas de kof_engine.config)
  processing.py         lectura y limpieza del Excel SAP            (reutilizado del demo)
  eda.py                cálculos del EDA con filtros                 (reutilizado del demo)
  prediction_service.py puente BD ↔ kof_engine, en segundo plano    (nuevo)
  routers/
    upload.py           POST /upload, GET /uploads
    eda.py              GET /eda/summary, /eda/machine/{m}, /machines
    predicciones.py     GET /predicciones, /estado, /modelo · POST /ejecutar
kof_engine/             motor de predicción, copiado tal cual del zip kof_motor
requirements.txt        versiones fijadas (el modelo guardado depende de scikit-learn)
render.yaml             despliegue en Render
```

Del zip `kof_motor` se tomó **solo** el paquete `kof_engine/` (los 8 módulos: todos los usa
`generar_resultados`). No se incluyeron `probar.py`, `tests/`, `datos_prueba/`, `kof_modelos.joblib`
ni `resultados.json`: el modelo se entrena dentro del propio backend y se guarda en la base de datos.

## Recorrido en orden de ejecución

**1. Arranque** (`uvicorn app.main:app`) → `config.py` lee las variables de entorno → `db.py` crea el
pool de conexiones → `main.py` registra CORS y, en `startup`, crea las tablas que falten
(`create_all`), activa RLS en ellas y marca como *error* cualquier entrenamiento que haya quedado a
medias por un reinicio.

**2. Subida de un Excel** (`POST /upload`) → `processing.load_and_filter` lee la hoja *BD SAP* (o la
primera), valida las 9 columnas que exige el motor, filtra `P.EQ.LINEA`, limpia el nombre de la
máquina y descarta filas con turno o intervalo inválidos → a cada fila se le calcula `row_key`
(huella + nº de aparición) → se inserta en bloque con `ON CONFLICT DO NOTHING`, así que subir un
periodo repetido no duplica nada → si entraron filas nuevas, se crea un `prediction_run` y se lanza
`run_engine` en segundo plano. La respuesta vuelve de inmediato con `prediction_run_id`.

**3. Motor en segundo plano** (`prediction_service.run_engine`) → lee **todo** el histórico de la BD y
lo reconstruye con las columnas SAP (`events_to_sap_dataframe`) → `kof_engine.entrenar` (Random
Forest de Llenadora y Empacadora) → guarda el modelo comprimido en `model_artifacts` →
`kof_engine.generar_resultados` (IPM, carta p, pronósticos, tablero) + `historico_ipm` → arma la
`matriz` simplificada para la tabla del frontend → guarda todo como JSONB en `prediction_runs`
con `status = ok` (o `error` + mensaje).

**4. Lecturas** → `GET /eda/summary` calcula el EDA al vuelo desde `failure_events`;
`GET /predicciones` devuelve el último resultado `ok` ya calculado (responde en milisegundos).

## Endpoints

| Método | Ruta | Para qué |
|---|---|---|
| GET | `/health` | Comprobación de vida (Render la usa) |
| POST | `/upload` | Sube Excel/CSV SAP (campo `file`). Lanza el reentrenamiento si hay datos nuevos |
| GET | `/uploads?limit=50` | Historial de archivos subidos |
| GET | `/machines` | Máquinas presentes en los datos, críticas primero |
| GET | `/eda/summary` | EDA filtrado. Parámetros abajo |
| GET | `/eda/machine/{maquina}` | EDA de una máquina (`period=todo` por defecto) |
| GET | `/predicciones?include=...` | Último resultado del motor. `include` = `meta,ipm,regimen,pronostico,tablero,matriz,historico_ipm` |
| GET | `/predicciones/estado?run_id=N` | `en_proceso` / `ok` / `error` de una ejecución (para hacer *polling* tras subir) |
| POST | `/predicciones/ejecutar?reentrenar=true` | Relanza el motor a mano. `false` reutiliza el último modelo (~2 s) |
| GET | `/predicciones/modelo` | AUC fuera de muestra y cortes del semáforo del último modelo |

Documentación interactiva: `http://localhost:8000/docs`.

**Parámetros de `/eda/summary`:** `machines` = `criticas` · `todas` · `Llenadora,Empacadora` —
`period` = `dia` · `semana` · `mes` · `todo` — `date` = último día del periodo (por defecto, el
último día con datos, porque el Excel va atrasado respecto a hoy) — `start`/`end` explícitos tienen
prioridad sobre `period`.

Devuelve `kpis` (registros, minutos, horas, variación vs. periodo anterior, MTBF medio de las
críticas), `by_day` (minutos y paros por día, incluidos los días en 0) y las secciones del demo:
`machines`, `top_failures`, `monthly_trend`, `shifts`, `avg_duration`, `scatter`, `mtbf`.

**`matriz` de `/predicciones`** (una fila por máquina, pensada para la tabla del frontend):
`machine`, `risk` (`critico|alto|medio|bajo`, nivel del IPM), `ipm`, `ranking`, `probability`
(% o `null` en Paletizadora), `horizon`, `signal` (Verde/Amarillo/Rojo, solo Llenadora y
Empacadora), `expected_failures_week` (Paletizadora), `mean_residual_days_op` (Envolvedora),
`regime`, `top_cause`, `trend` (IPM de las últimas 12 semanas, formato `{k, v}` del `Sparkline`) y
`trend_pct`.

## 1. Crear la base en Supabase

1. En [supabase.com](https://supabase.com) → *New project*. Guarda la contraseña de la base; región
   cercana a donde esté Render (p. ej. *East US*).
2. Botón **Connect** del proyecto → copia la cadena **Session pooler**. La conexión *Direct* usa IPv6
   y Render no la alcanza; el *Session pooler* sí funciona por IPv4.
3. Reemplaza `[YOUR-PASSWORD]` y agrega `?sslmode=require` al final.

No hace falta crear tablas a mano: el backend las crea al arrancar y les activa RLS para que no
queden expuestas por la API REST pública de Supabase.

## 2. Correr en local

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows  (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt
copy .env.example .env            # y pega tu DATABASE_URL de Supabase
uvicorn app.main:app --reload     # http://localhost:8000/docs
```

Luego sube el Excel SAP completo (desde 2024-08-01) en `POST /upload` y consulta
`/predicciones/estado` hasta que diga `ok`.

## 3. Desplegar en Render

*New → Blueprint* → selecciona este repo (usa `render.yaml`) → llena `DATABASE_URL` y
`CORS_ORIGINS` (incluye la URL de Vercel del frontend) → *Apply*.

En el plan gratuito el servicio se duerme tras ~15 min sin tráfico y tarda ~50 s en despertar;
abre `/health` un minuto antes de una presentación. El entrenamiento también es más lento ahí
(CPU compartida): cuenta con alrededor de un minuto después de subir un Excel.

## Reglas heredadas del motor

- Pasar siempre el **histórico completo**: el backend ya lo hace leyendo toda la tabla.
- El tiempo de la Envolvedora es **tiempo medio residual**, no RUL (no hay datos de condición).
- Pesos y criticidad del IPM viven en `kof_engine/config.py`; cambiarlos no exige reentrenar
  (`POST /predicciones/ejecutar?reentrenar=false`).
