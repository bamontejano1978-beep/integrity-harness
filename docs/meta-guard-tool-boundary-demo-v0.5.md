# Demo breve — frontera de autorización v0.5 (offline)

**Duración:** 2–3 minutos. **Modo:** local, sintético y sin red.  
**Objetivo:** mostrar que el boundary mock aplica el estado de autorización confiable en la ejecución local, sin presentarlo como resultado de un modelo.

## Preparación

Desde la raíz de `hackatons/submission-meta`, ejecutar únicamente:

```bash
python -X utf8 scripts/verify_meta_guard_tool_boundary_freeze_v0_5.py
python -m pytest -q tests/test_meta_guard_tool_boundary_v0_5_offline.py
```

El verificador debe confirmar 28 estímulos, 8 pares exactos, hashes válidos, captura live desactivada y presupuesto autorizado de 0 solicitudes. La prueba debe terminar con `3 passed`. Ambos comandos son offline; no ejecutar el runner `--live-agent` durante esta demostración.

## Guion hablado

> Estamos mostrando una prueba acotada de una frontera de autorización para herramientas. La suite contiene 28 estímulos sintéticos; ocho pares de acción conservan exactamente el mismo texto de usuario y cambian solo el estado confiable de autorización.
>
> Para probar el mecanismo sin depender de que un modelo proponga una acción adversaria, el test inyecta localmente la misma llamada mock restringida en ambos estados. Con el boundary activado, se bloquean las ocho propuestas `unverified` y se permiten las ocho `verified`. En el control local sin boundary, las 16 llamadas inyectadas pasan. La operación es siempre `SYNTHETIC-ONLY`.
>
> Esto demuestra el comportamiento mecánico de esta implementación y este fixture: no es una medición de conducta de Meta, una tasa de seguridad, ni una prueba de operaciones reales. No se contacta a ningún proveedor, no hay llamadas de red y no se ejecuta ninguna acción real. La captura live está desactivada y este freeze autoriza cero solicitudes.
>
> La revisión del protocolo y del corpus fue interna, por su autor; no es independiente. Dado el alcance de esta demo, no la presentamos como validación externa ni como benchmark general.

## Qué enseñar en pantalla

1. Abrir el [freeze v0.5](../hackatons/meta/scenarios/blind_guard_tool_boundary_freeze_v0.5.json) y señalar `live_capture_enabled: false`, `maximum_live_requests_authorized: 0` y el registro de revisión interna.
2. Ejecutar los dos comandos de preparación. En el resultado del verificador, mostrar `stimuli: 28`, `matched_action_pairs: 8` y `review_independent: false`; en pytest, `3 passed`.
3. Abrir la [prueba offline](../tests/test_meta_guard_tool_boundary_v0_5_offline.py) y mostrar que usa `_InjectedProposalProvider`, un identificador de llamada local y el argumento `SYNTHETIC-ONLY`. No abrir ni ejecutar runners live.

## Respuestas breves

- **¿El modelo intentó saltarse el control?** No se midió eso en v0.5 offline. La llamada se inyecta determinísticamente para aislar el mecanismo.
- **¿Qué significa 8/8?** Ocho llamadas mock no autorizadas bloqueadas por este boundary bajo el fixture; no es una tasa de bloqueo live.
- **¿Se probaron operaciones reales o el sistema en producción?** No. Solo herramientas mock locales y estado booleano de fixture; no se prueba una fuente de autorización criptográfica o de negocio.
- **¿Hay revisión externa?** No. La revisión fue interna por el autor del protocolo. Es un límite declarado, no un resultado oculto.
- **¿Por qué no hay captura live?** El freeze mantiene captura y ejecución live desactivadas y autoriza cero solicitudes.

## Material de respaldo

- [Preregistro y límites v0.5](meta-guard-tool-boundary-prereg-v0.5.md)
- [Freeze y hashes v0.5](../hackatons/meta/scenarios/blind_guard_tool_boundary_freeze_v0.5.json)
- [Verificador del freeze](../scripts/verify_meta_guard_tool_boundary_freeze_v0_5.py)
- [Prueba del mecanismo offline](../tests/test_meta_guard_tool_boundary_v0_5_offline.py)
