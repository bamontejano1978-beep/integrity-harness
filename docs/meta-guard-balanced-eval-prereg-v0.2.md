# Enmienda preregistrada v0.2 — comparación interna del guard textual

**Estado:** preparada y congelada para revisión; **no ejecutar hasta autorización expresa para esta nueva versión**.  
**Suite/corpus:** `meta-guard-balanced-screen-v0.1` (los mismos 12 estímulos congelados; no se cambian textos ni etiquetas).  
**Protocolo:** `meta-guard-balanced-screen-v0.2`.  
**Fecha:** 2026-10-28.

## Motivo de la enmienda

La primera ejecución autorizada bajo el protocolo v0.1 se detuvo durante el primer intento del brazo `generic_guard_off/case-001`: el proveedor produjo `AgentExecutionError`; el checkpoint registró una solicitud iniciada y ninguna respuesta completa. El código de estado HTTP no se conservó, por lo que la causa concreta queda **desconocida**. No se reanudó ni se repetirá bajo v0.1. Una llamada iniciada puede haber generado coste; debe considerarse potencialmente facturable.

Esta nueva versión conserva sin cambios el protocolo, estímulos, oracle, checkpoint y carpeta de v0.1. Corrige el presupuesto de salida y el diagnóstico persistido para que un próximo error se pueda categorizar sin conservar secretos o respuestas del proveedor.

## Parámetros y límites v0.2

- Mismo modelo y endpoint indicados en el manifiesto de la corrida; configuración idéntica en ambos brazos.
- `temperature=0.0`, `reasoning_effort=low`, `max_completion_tokens=1024` por solicitud. Se alinea con la configuración live previa del harness (1.024); **no se afirma que sea la causa del error v0.1**.
- Los resultados nuevos se guardarían en `.tmp/meta-guard-balanced-live-v0_2-20261028/`, separados del checkpoint v0.1.
- Secuencia fija: 12 casos en `generic_guard_off`, después los mismos 12 en `generic_guard_on`.
- Un turno por caso y brazo; máximo 24 solicitudes nuevas; sin reintentos, backoff, recuperación selectiva ni reanudación parcial.
- El intento v0.1 fallido es adicional: máximo acumulado entre ambas versiones hasta 25 solicitudes iniciadas, de las que una ya fue iniciada y 24 quedarían para una corrida v0.2 completa.
- Herramientas únicamente mock locales, `verified_authorization=false` y cero efectos externos. Si falla una solicitud, la corrida v0.2 se detiene y no se reejecuta con los mismos datos/versión.

## Diagnóstico seguro de errores

Guardar únicamente una categoría allowlisted: `http_error` con el entero `http_status`; `url_error` con el nombre de clase de `reason`; o `provider_or_harness_error` con la clase de excepción superior. No guardar `str(exception)`, cuerpos JSON/HTTP, cabeceras, URL con query, clave, prompt de solicitud ni texto de respuesta en el campo de diagnóstico. El error v0.1 no conserva cuerpo ni código, por lo que su causa permanece desconocida. El checkpoint sí puede contener las respuestas de casos completados, como requiere la evaluación, pero permanece local e ignorado por Git. No crear un checkpoint nuevo si la validación previa de hash o del límite falla.

## Congelación y procedencia

El manifiesto separado `hackatons/meta/scenarios/blind_guard_eval_freeze_v0.2.json` vincula por SHA-256 esta enmienda, los estímulos y oracle v0.1, el runner v0.2 y el verificador. El corpus/oracle originales se comparten intencionalmente y siguen protegidos por su hash original. El manifiesto v0.1 no se edita. El nuevo verificador es offline. Estos hashes internos detectan cambios accidentales; no son firma digital ni timestamp confiable.

## Decisión actual

Esta enmienda solo corrige protocolo/código. **No se ha vuelto a llamar al proveedor ni se autoriza aquí una segunda corrida.** Para ejecutar v0.2 hará falta autorización específica, aceptando el coste potencial de hasta 24 solicitudes nuevas más la solicitud ya intentada en v0.1.
