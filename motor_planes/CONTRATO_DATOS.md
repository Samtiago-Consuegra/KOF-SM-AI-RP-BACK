# Contrato de datos · motor ↔ app

Qué recibe el motor, qué entrega y qué parte de cada pantalla lee cada archivo. Todos los JSON
están en UTF-8. Las fechas van como `"AAAA-MM-DD"`, las horas como `"HH:MM"` y las semanas como
semana ISO `"AAAA-Wnn"` (de lunes a domingo).

---

## 1. Lo que entra al motor (carpeta `datos/entrada/`)

### 1.1 Archivos que sube el usuario

Se buscan por patrón de nombre; si hay varios, se usa el más reciente.

| Archivo | Patrón del nombre | Cada cuánto |
| --- | --- | --- |
| Reporte diario de paros | `BD*Reporte*SAP*.xlsx` | Diario (el mismo de EDA y predicciones) |
| Avisos de daño (SAP) | `HISTORICO_AVISOS*.xlsx` | Semanal |
| Órdenes de trabajo (SAP) | `HISTORICO_OT*.xlsx` | Semanal |
| Plan preventivo (SAP) | `PM_SAP*.xlsx` | Solo cuando cambie |
| Consolidado de fallas mayores | `*Consolidado*Fallo*.xlsx` | Cuando la planta lo actualice |
| ACR (análisis de causa raíz) | Carpeta `acr/`, un Excel por ACR (el nombre empieza por DD-MM-AAAA) | Cada vez que haya uno nuevo |
| ACR revisados (opcional) | `acr_revisado.xlsx` | Cuando se revise la extracción |

### 1.2 Archivos que escribe la app

**`paradas.json`** — lo que se registra con el formulario "Programar parada". Incluir todas las
paradas de la semana que se va a programar (puede incluir otras semanas; el motor filtra).

```json
{
  "paradas": [
    {
      "Id_parada": "PAR-2026-11-11-01",
      "Titulo": "Cambio de formato 600 ml",
      "Modalidad": "Frío",
      "Fecha": "2026-11-11",
      "Inicio": "05:00",
      "Fin": "08:00",
      "Maquinas": ["Llenadora", "Empacadora"],
      "Tareas_manuales": [
        {
          "Maquina": "Llenadora",
          "Sistema": "Sistema de Conjunto de llenado",
          "Id_componente": "LLE-001",
          "Componente": "Válvula de llenado",
          "Tipo_tarea": "Preventiva",
          "Tarea": "Revisar empaques de moneda en 10 válvulas",
          "Duracion_h": 1.0
        }
      ]
    }
  ]
}
```

| Campo | Obligatorio | Valores |
| --- | --- | --- |
| `Id_parada` | Sí | Texto único (lo genera la app) |
| `Titulo` | Sí | Texto libre |
| `Modalidad` | Sí | `"Frío"` o `"Caliente"`. En caliente el motor solo sugiere inspecciones |
| `Fecha`, `Inicio`, `Fin` | Sí | Fecha y horas |
| `Maquinas` | Sí | Una o varias de `"Llenadora"`, `"Empacadora"`, `"Paletizadora"`, `"Envolvedora"`, o `["Toda la línea"]` |
| `Tareas_manuales` | No | Lista de tareas escritas por el usuario. `Sistema`, `Id_componente` y `Componente` salen de `catalogo.json`. `Tipo_tarea`: `"Preventiva"`, `"Predictiva"` o `"Correctiva"`. `Duracion_h` opcional (por defecto 0,5 h) |

Si no existe `paradas.json`, el motor usa un calendario de ejemplo (miércoles y limpieza del
sábado) y lo indica con `"calendario_es_ejemplo": true` en `plan_semanal.json`.

**`decisiones.json`** — lo que el usuario hace con cada tarea sugerida.

```json
{
  "decisiones": [
    {"Id_tarea": "REC-EMP-031", "Decision": "Enviada", "Id_parada": "PAR-2026-11-14-01"},
    {"Id_tarea": "REC-EMP-012", "Decision": "Pospuesta"}
  ]
}
```

| Campo | Valores |
| --- | --- |
| `Id_tarea` | El `Id_tarea` que vino en `plan_semanal.json` |
| `Decision` | `"Enviada"` (botón "Enviar a la parada") o `"Pospuesta"` |
| `Id_parada` | Opcional: si el usuario eligió una parada concreta. Se respeta aunque supere la capacidad |

**`equivalencias_validadas.xlsx`** (opcional) — si mantenimiento valida qué tarea del plan cubre
cada componente, se guarda aquí y el motor la usa en lugar del borrador automático.

Después de escribir `paradas.json` o `decisiones.json`, correr `correr(desde=20, semana=...)`.

---

## 2. Lo que sale del motor (carpeta `datos/salida/`)

| Archivo | Pantalla | Parte |
| --- | --- | --- |
| `catalogo.json` | Planes | Menús Máquina → Sistema → Componente del formulario; lista de clases de orden |
| `alertas.json` | Planes | Carrusel de alertas |
| `plan_semanal.json` | Planes | Calendario, "Plan sugerido de la semana", "Tareas sugeridas", indicadores |
| `plan_recomendado.json` | Planes | Ranking por componente (IPM) y detalle "¿por qué?"; selector A / B / Fusión |
| `historia_clinica.json` | Historia clínica | Ficha por máquina, sistema y componente, con serie mensual |
| `historia_clinica_acr.json` | Historia clínica | Antecedentes: fallas mayores, ACR, acciones, repuestos críticos y antes/después de mantenimientos |
| `cumplimiento.json` | Cumplimiento | Indicadores, series, estado de planes y pendientes |
| `plan_semanal_<semana>.xlsx` | Planes | Botón "Exportar semana (Excel)" |

### 2.1 `catalogo.json`

```
{ "maquinas": [ { "Maquina", "Sistemas": [ { "Sistema", "Componentes": [ { "Id_componente", "Componente" } ] } ] } ],
  "clases_orden": [ { "Clase", "Tipo_orden", "Detalle" } ] }
```

`Id_componente` (por ejemplo `LLE-001`) es la llave que une todos los archivos.

### 2.2 `alertas.json` — carrusel

```
{ "fecha_referencia", "alertas": [ { ... } ] }
```

| Campo | Uso en pantalla |
| --- | --- |
| `Tipo` | Ciclo, Paros en aumento, Síntomas, Estacionalidad, Cumplimiento, Brecha del plan, Oportunidad, Riesgo de repuesto, Falla mayor en racha, Después de mantenimiento mayor, Acción ACR sin cierre |
| `Maquina`, `Componente`, `Id_componente` | Encabezado de la tarjeta |
| `Prioridad`, `IPM` | Color y orden |
| `Mensaje` | Texto principal (ya redactado por el motor) |
| `Evidencia`, `Confianza` | Línea pequeña bajo el mensaje |
| `Accion_sugerida` | Texto del enlace |
| `Enlace` | `"plan"`, `"componente"` (historia clínica) o `"calendario"` |
| `Mostrar` | `true` en las destacadas de la semana (máx. 5); las demás, en "ver todas" |

Las alertas vienen ordenadas por prioridad: el carrusel puede mostrarlas en ese orden.

### 2.3 `plan_semanal.json` — calendario y tablas

```
{ "semana", "desde", "hasta", "calendario_es_ejemplo", "tecnicos_por_parada",
  "versiones": { "fusion": {...}, "A": {...}, "B": {...} } }
```

La app usa `versiones.fusion`; `A` y `B` son para el selector de alternativas (opcional).
Cada versión trae:

| Bloque | Qué es | Dónde va |
| --- | --- | --- |
| `indicadores` | `tareas`, `en_paradas_existentes`, `uso_paradas_pct`, `con_contratista`, `requieren_parada_dedicada`, `pasan_a_siguiente_semana`, `ordenes_por_verificar`, `alertas` | Encabezado o tarjetas de resumen |
| `paradas[]` | Cada parada con `Id_parada`, `Titulo`, `Modalidad`, `Dia`, `Fecha`, `Inicio`, `Fin`, `Horas`, `Maquinas`, `Capacidad_h`, `Horas_asignadas`, `Por_sistema[]` y `Tareas[]` | Bloques del calendario y su ventana de detalle |
| `tareas_sugeridas[]` | Todas las tareas de la semana con su estado | Tabla "Plan sugerido de la semana" |
| `sin_parada[]` | Tareas que no cupieron o requieren parada dedicada | Pestaña "Tareas sugeridas" |
| `por_verificar[]` | Órdenes SAP abiertas de la semana anterior (probablemente hechas y sin cerrar) | Lista aparte |
| `fuera_de_parada[]` | Rutinas del operador y acciones de gestión (no requieren parada) | Lista aparte |

Campos de cada tarea (en `paradas[].Tareas`, `tareas_sugeridas`, `sin_parada`):

| Campo | Uso en pantalla |
| --- | --- |
| `Id_tarea` | Llave para `decisiones.json` |
| `Prioridad` | Columna PRIORIDAD |
| `Maquina`, `Sistema`, `Componente`, `Id_componente` | Columna MÁQUINA · COMPONENTE y ventana de detalle |
| `Grupo` | Sistema o, en órdenes SAP sin componente, `"Plan <código>"`: para agrupar por sistema |
| `Tarea` | Columna TAREA |
| `Tipo_tarea` | Inspección, Sustitución, Restauración… (color en la ventana de la parada) |
| `Clase_orden` | Columna CLASE DE ORDEN (por ejemplo `ZFI1 · Preventiva`) |
| `Plan`, `Orden` | Código del plan SAP y número de orden, si existen |
| `Id_parada` | Columna PARADA (cruzar con `paradas[]` para día y hora) |
| `Duracion_h` | Duración |
| `Origen` | `Plan SAP`, `Recomendación del motor` o `Registrada por el usuario` |
| `Estado` | `Sugerida`, `Enviada a la parada`, `Pospuesta por el usuario`, `Sin espacio: pasa a la siguiente semana`, `Requiere parada dedicada`, `Verificar: atrasada o sin cerrar en SAP` |
| `Motivo` | El "¿por qué?" de la ventana de detalle |
| `Con_contratista` | `true` si es trabajo de alto riesgo |

### 2.4 `plan_recomendado.json` — ranking y detalle

`versiones.fusion[]` trae un registro por componente, ordenado por `Ranking`. Campos clave para
la ventana de detalle: `IPM`, `Prioridad`, `Coincidencia` (respaldo de las fuentes), `Acciones`,
`Tarea_sugerida`, `Frecuencia_actual`, `Frecuencia_propuesta`, `Canal`, `Clase_orden`,
`Justificacion`, `Confianza_B`, `Forma_falla`, `Tendencia`, `Sintomas`.

### 2.5 `historia_clinica.json` y `cumplimiento.json`

Ver la descripción al inicio de `motor/fase14_historia_clinica.py` y `motor/fase15_cumplimiento.py`.
`historia_clinica.json` está anidado máquina → `sistemas[]` → `componentes[]` (cada componente con
su `serie_mensual[]`). `cumplimiento.json` trae `maquinas[]`, `planes[]`, `semanal[]`,
`trimestral[]` y `pendientes[]`.

### 2.6 `historia_clinica_acr.json` — antecedentes en la Historia clínica

```
{ "datos_hasta",
  "maquinas": [ { "Maquina", "Fallas_mayores", "Minutos_fallas_mayores", "ACR",
                  "Causas_falla": {causa: n}, "Causas_alto_tiempo": {causa: n},
                  "Pre_post_mantenimientos": [ { "Fecha", "Orden", "Plan", "Tarea", "Horas_orden",
                                                 "Minutos_30d_antes", "Minutos_30d_despues", "Cambio_pct" } ],
                  "componentes": [ { "Id_componente", "Componente", "Sistema", "IPM", "Nivel",
                                     "IPM_con_consecuencia", "Nivel_con_consecuencia",
                                     "Fallas_mayores": { "N", "Minutos", "Lista": [ { "Fecha", "Minutos", "Causa_falla",
                                                                                      "Causa_alto_tiempo", "Detalle" } ] },
                                     "ACR": [ { "Fecha", "Falla", "Duracion_min", "Causa_raiz", "Nota", "Archivo",
                                                "Acciones": [ { "Actividad", "Responsable", "Fecha_fin",
                                                                "Tipo_accion", "Estado" } ] } ],
                                     "Repuesto_critico": { "Nivel", "Motivo" } | null,
                                     "Pre_post_mantenimientos": [ ... ] } ] } ],
  "otros_equipos_l4": [ { "Fecha", "Maquina", "Falla", "Duracion_min", "Causa_raiz", "Nota" } ] }
```

Se une con `historia_clinica.json` por `Id_componente`. Sugerencia de presentación en la ficha del
componente: una sección "Antecedentes" con las fallas mayores, el o los ACR (causa raíz y acciones,
marcando las que tienen fecha cumplida sin cierre registrado), el aviso de repuesto crítico y la tabla
"antes y después" de los mantenimientos mayores. `Estado` de una acción: `Cerrada`, `En plazo` o
`Fecha cumplida sin cierre registrado`.

### 2.7 `validacion.json` y `validacion.xlsx`

Son para el documento final; la app no los necesita.
