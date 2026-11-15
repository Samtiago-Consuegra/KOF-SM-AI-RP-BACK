# KOF-SMART Maintenance AI — Backend (FastAPI + Supabase, desplegado en Render gratis)

## Rendimiento (obligatorio en todo cambio)
- Ningún endpoint debe cargar columnas pesadas que no devuelve (por ejemplo, MotorArchivo.data, que está diferida).
- Nada de consultas N+1: una consulta por lista, con joins o IN, nunca una consulta por fila.
- Reutiliza la caché en memoria existente (cache.py y la caché de motor_salidas); invalídala solo al escribir.
- Nada pesado en el hilo de la petición: el motor y kof_engine corren en segundo plano con la cola existente.
- Los cambios de esquema van como ALTER TABLE ... IF NOT EXISTS en el arranque en segundo plano, nunca bloqueando el arranque.
- Las respuestas grandes van paginadas o filtradas; GZip ya está activo.
- Antes de terminar, mide los endpoints que tocaste y reporta su tiempo de respuesta.

## Seguridad
- Todo endpoint nuevo exige Depends(get_current_user).
- Ningún Excel de SAP, ACR ni dato de la planta se versiona en el repo.