# KOF-Smart Maintenance AI · Motor de planes de mantenimiento

Motor en Python que alimenta tres pantallas de la app: **Historia clínica**, **Cumplimiento** y
**Planes de mantenimiento**. Toma el reporte diario de paros y tres exportaciones de SAP de la
Línea 4 PET (Coca-Cola FEMSA, Planta Barranquilla) y genera archivos JSON listos para el frontend.

Regla central: **Python calcula, la app solo muestra.** Todos los números, prioridades, alertas y
asignaciones a paradas salen de aquí. El frontend no calcula nada.

## Estructura

```
motor_planes/
├── correr_motor.py          ← ÚNICO punto de entrada: corre todas las fases en orden
├── README.md                ← este archivo
├── CONTRATO_DATOS.md        ← qué archivo lee cada pantalla y qué escribe la app (campo por campo)
├── requirements.txt
├── motor/                   ← el cálculo (no generan gráficas)
│   ├── kof_utils.py             utilitario de las fases 1-11 (carga del reporte diario)
│   ├── kof_planes_utils.py      carga y limpieza de SAP, rutas, clases de orden, reglas de texto
│   ├── kof_motor_utils.py       reglas compartidas del motor (grilla de frecuencias, niveles IPM)
│   ├── fase12_preparacion_sap.py
│   ├── fase13_catalogo_fusion.py
│   ├── fase14_historia_clinica.py
│   ├── fase15_cumplimiento.py
│   ├── fase16_alternativa_a.py
│   ├── fase17_alternativa_b.py
│   ├── fase18_fusion.py
│   ├── fase19_alertas.py
│   ├── fase20_programador.py
│   ├── fase21_fallas_mayores_acr.py
│   ├── fase22_repuestos_criticos.py
│   ├── fase23_alertas_y_antecedentes.py
│   └── fase24_validacion.py
├── graficas/                ← gráficas PNG para el informe (opcionales, no las usa la app)
├── ejemplos/                ← ejemplos de lo que escribe la app: paradas.json y decisiones.json
└── datos/
    ├── entrada/             ← aquí van los Excel y los archivos que escribe la app
    └── salida/              ← aquí quedan los resultados (CSV intermedios y JSON para la app)
```

## Qué hace cada fase

| Fase | Archivo | Qué hace | Pantalla que alimenta | Salida principal |
| --- | --- | --- | --- | --- |
| 12 | `fase12_preparacion_sap.py` | Limpia y cruza avisos, órdenes y plan preventivo | Base común | `avisos_limpios.csv`, `ot_limpias.csv`, `plan_pm_limpio.csv` |
| 13 | `fase13_catalogo_fusion.py` | Catálogo máquina → sistema → componente y unión con el reporte diario | Base común y menús del formulario | `catalogo.json` |
| 14 | `fase14_historia_clinica.py` | Indicadores por máquina, sistema y componente (MTTR, MTBF, Weibull, tendencia) | Historia clínica | `historia_clinica.json` |
| 15 | `fase15_cumplimiento.py` | Cumplimiento del preventivo, mezcla de costos y estado de cada plan | Cumplimiento | `cumplimiento.json` |
| 16 | `fase16_alternativa_a.py` | Prioridad y acción desde el reporte diario | Planes (selector) | `recomendaciones_A.csv` |
| 17 | `fase17_alternativa_b.py` | Prioridad y brechas del plan desde SAP | Planes (selector) | `recomendaciones_B.csv` |
| 18 | `fase18_fusion.py` | Fusión A + B: IPM por componente, acción final, canal y clase de orden | Planes | `plan_recomendado.json` |
| 19 | `fase19_alertas.py` | Alertas tempranas (ciclo, paros en aumento, síntomas, estacionalidad, cumplimiento, brecha) | Planes (carrusel) | `alertas.json` |
| 20 | `fase20_programador.py` | Acomoda las tareas en las paradas de la semana; alerta de oportunidad | Planes (calendario y tablas) | `plan_semanal.json` |
| 21 | `fase21_fallas_mayores_acr.py` | Lee el consolidado de fallas mayores y los ACR (causa raíz y acciones) | Base de las fases 22 a 24 | `fallas_mayores.csv`, `acr_eventos.csv`, `acr_acciones.csv` |
| 22 | `fase22_repuestos_criticos.py` | Componentes cuyo repuesto ya alargó o causó fallas graves | Historia clínica y alertas | `repuestos_criticos.csv` |
| 23 | `fase23_alertas_y_antecedentes.py` | Alertas nuevas (repuesto, racha, post-mantenimiento, acciones ACR) y antecedentes por componente | Planes (carrusel) e Historia clínica | `alertas.json`, `historia_clinica_acr.json` |
| 24 | `fase24_validacion.py` | Valida el ranking del motor contra fallas mayores y ACR | Documento final (no la app) | `validacion.json`, `validacion.xlsx` |

Cada fase lee lo que dejó la anterior, por eso **el orden importa**. Las fases 21 a 24 se suman al
final sin cambiar las anteriores: leen sus resultados y solo AGREGAN alertas a `alertas.json`. Cada archivo tiene al inicio
una explicación de qué hace, qué lee, qué entrega y qué reglas usa.

## Cómo correrlo

Instalar dependencias (una vez):

```bash
pip install -r requirements.txt
```

Poner en `datos/entrada/` el reporte diario y los tres Excel de SAP; además, el consolidado de fallas
mayores y la carpeta `acr/` con los Excel de los ACR (el nombre exacto no importa, se buscan por
patrón; ver CONTRATO_DATOS.md). Luego:

```bash
python correr_motor.py                                   # todo, semana siguiente a los datos
python correr_motor.py --semana 2026-W46 --fecha-ref 2026-11-08
python correr_motor.py --desde 20 --semana 2026-W46      # solo reprogramar la semana
python correr_motor.py --graficas                        # además, las gráficas PNG
```

Desde el backend (por ejemplo, FastAPI):

```python
from correr_motor import correr
resumen = correr(semana="2026-W46", fecha_ref="2026-11-08")   # devuelve ok, fases, archivos generados
```

## Cuándo correr qué

| Evento en la app | Qué correr | Tiempo aproximado |
| --- | --- | --- |
| Se sube un archivo nuevo (diario, avisos, órdenes o plan) | `correr(...)` completo | ~15 s |
| Cambio de semana en el calendario o nueva fecha de referencia | `correr(desde=19, semana=...)` | ~1 s |
| El usuario programa, edita o borra una parada | `correr(desde=20, semana=...)` | ~1 s |
| El usuario envía una tarea a una parada o la pospone | `correr(desde=20, semana=...)` | ~1 s |
| Llega un ACR nuevo o un consolidado nuevo | `correr(desde=21, ...)` | ~10 s |

En la app, `fecha_ref` debe ser la fecha de hoy y `semana` la semana que el usuario está viendo.

## Parámetros que se pueden ajustar

| Dónde | Parámetro | Valor actual | Qué controla |
| --- | --- | --- | --- |
| `kof_planes_utils.py` | `INICIO_SAP` | 2024-01-01 | Desde cuándo se usan avisos y órdenes |
| `kof_planes_utils.py` | `TIPOS_ORDEN` | Tabla de la planta | Clases de orden ZF + ámbito + tipo |
| `fase15_cumplimiento.py` | `GRACIA_DIAS` | 14 | Días para cerrar una orden antes de contarla incumplida |
| `fase16_alternativa_a.py` | `PESOS_A` | 50/25/15/10 | Pesos del puntaje A |
| `fase17_alternativa_b.py` | `PESOS_B` | 40/35/25 | Pesos del puntaje B |
| `fase19_alertas.py` | `MAX_ALERTAS_VISIBLES` | 5 | Alertas destacadas por semana |
| `fase20_programador.py` | `TECNICOS_POR_PARADA` | 2 | Capacidad: técnico de turno + tecnólogo |
| `fase20_programador.py` | `TAREAS_EN_CALIENTE` | Inspecciones | Qué tareas se permiten en paradas en caliente (por confirmar) |
| `fase20_programador.py` | `PALABRAS_ALTO_RIESGO` | alturas, amoníaco… | Tareas que van con contratista |

## Decisiones de diseño (resumen)

Están explicadas al inicio de `motor/kof_planes_utils.py`. Las más importantes:

- Los tiempos de paro y el MTTR salen **solo** del reporte diario; las fechas de cierre de SAP no son confiables.
- Los registros del reporte diario en horas consecutivas se unen en un solo evento.
- MTTR y MTBF se muestran con **mediana** como valor principal.
- La emergencia (clase 4) cuenta como correctiva; mejoras (6) y proyectos (8) no cuentan como fallas.
- Toda alerta y recomendación trae su evidencia y su nivel de confianza; con pocos datos se dice.

## ACR: extracción automática y versión revisada

Los ACR tienen formatos distintos. La Fase 21 los lee automáticamente y deja `acr_revision.xlsx` en la
salida. Ya se incluye `datos/entrada/acr_revisado.xlsx`, revisado a mano con los 19 ACR actuales
(componente, duración y causa raíz). Cuando llegue un ACR nuevo, basta con ponerlo en `datos/entrada/acr/`:
el motor lo lee automáticamente y lo marca como pendiente de revisión.

## Pendiente

- Fase 25: asistente con modelo de lenguaje local (solo redacta a partir de estos resultados).
- Decidir si se adopta el "IPM con consecuencia" (Fase 23) en lugar del IPM actual.
- Exportación de inventario de SAP, si se consigue, para que la alerta de repuesto diga cuántos hay.
