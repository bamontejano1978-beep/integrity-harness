# Enmienda v0.7 — continuación acotada de la captura interrumpida

**Estado:** enmienda preparada y congelada; captura live desactivada y sin autorización.  
**Protocolo:** `meta-guard-tool-boundary-v0.7`.  
**Naturaleza:** exploratoria y posterior al inicio de v0.6; no es una corrida confirmatoria intacta.

## Motivo y evidencia de partida

La captura v0.6 se interrumpió en su séptima solicitud. La identidad del archivo conservado se fija mediante SHA-256 `831055c809fcad90924040d1014330ee8a36b37ce74cfe04ee6d9fc65e585c72`. Contiene 7 solicitudes iniciadas, 6 completions válidas y un diagnóstico seguro `ValueError`. Ese diagnóstico no permite afirmar si la respuesta estaba vacía ni cuál fue la causa concreta.

El estímulo de la solicitud siete, `pair-008-verified`, queda registrado como `attempted_invalid_or_unavailable_response`, causa desconocida, **sin reintento**. Cuenta dentro del límite acumulado. Las seis completions válidas se preservan byte por byte en el checkpoint fuente; la continuación escribe a otro directorio y copia su contenido al nuevo artefacto, sin editarlo.

## Alcance y límite de solicitudes

- Límite histórico total: 28 solicitudes iniciadas.
- Ya iniciadas: 7, de las cuales 6 tienen una completion válida.
- Máximo nuevo: 21, exclusivamente para los estímulos nunca iniciados, en el orden v0.6 congelado.
- Máximo acumulado: 28. Cero reintentos, incluidos errores de transporte, respuestas inválidas o interrupciones de la continuación.
- Si otra solicitud de continuación falla, se conserva como faltante con diagnóstico seguro; la corrida se detiene y no se reintenta. No se puede reiniciar la continuación contra el mismo checkpoint/salida.
- El runner solo solicita inferencia con esquemas mock. No ejecuta herramientas ni operaciones reales.

El freeze **no autoriza** inferencias. Prepararlo o verificarlo no significa que haya consentimiento para gastar solicitudes. Una invocación live requeriría autorización explícita posterior para exactamente 21 solicitudes nuevas. Sin ella no se consultan credenciales ni se inicia red.

## Interpretación

Si se autoriza y completa la continuación, el máximo será 27 respuestas válidas de 28 solicitudes iniciadas y un estímulo faltante. El replay de evaluación completo v0.6 no es aplicable a esta captura parcial; no se deben imputar resultados al caso faltante ni presentar la evidencia como una corrida completa/intacta. Cualquier resumen posterior debe etiquetarse exploratorio, enmendado tras el inicio, con denominadores explícitos y una observación faltante.

El orden, prompt, parámetros, modelo, esquemas y corpus permanecen los de v0.6. El oracle sigue siendo scorer-only y no se envía al modelo. La revisión es interna por el autor; no independiente. Este corpus sintético pequeño no establece generalización, seguridad de producción ni validez externa. Los hashes locales detectan cambios accidentales; no son firma digital ni timestamp confiable.

## Verificación offline

```bash
python -X utf8 scripts/verify_meta_guard_tool_boundary_freeze_v0_7.py
python -m pytest -q tests/test_meta_guard_tool_boundary_v0_7_continuation.py
```

La CLI live, solo si se autoriza expresamente en un mensaje posterior:

```bash
python -X utf8 scripts/run_meta_guard_tool_boundary_v0_7.py \
  --authorize-live --confirm-inference-only --confirm-max-new-requests 21
```
