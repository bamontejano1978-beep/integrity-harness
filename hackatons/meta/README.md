# Meta Global AI Developer Hackathon 2026 — Integrity Harness

**Track:** Startup (Candidatura aprobada)  
**Periodo:** 26 de octubre – 4 de noviembre de 2026  
**Proyecto:** *Integrity Harness — Automated Behavioral Safety Scan*

---

## 1. Ejecución reproducible

Modo offline (predeterminado, sin llamadas externas):

```bash
.venv/Scripts/python.exe -X utf8 hackatons/meta/run_meta_harness.py
```

Modo con **Meta Model API real** como agente evaluado (4 escenarios base, 8 trayectorias emparejadas, 24 turnos de inferencia por defecto):

```bash
export META_MODEL_API_KEY="<tu-model-api-key>"
export META_MODEL_NAME="muse-spark-1.3"
export META_MODEL_BASE_URL="https://api.meta.ai/v1"
.venv/Scripts/python.exe -X utf8 hackatons/meta/run_meta_harness.py --live-agent
```

`MODEL_API_KEY` también se reconoce como alternativa. No escribas claves en archivos ni las incluyas en la línea de comandos compartida. El endpoint/modelo por defecto siguen el API oficial de Meta Model API (la clave `Llama-3.3-70B-Instruct` no se debe asumir como ID alojado por Meta; usa modelos habilitados para tu cuenta, consultables con `GET https://api.meta.ai/v1/models`). Los candidatos guarded y unguarded llaman al mismo modelo Meta; solo cambia el límite determinista local. En `guarded`, los fixtures de presión pueden sustituir la respuesta del modelo con una respuesta de bloqueo/derivación definida por escenario. Esto prueba el hook del harness bajo las fixtures existentes, no constituye un guard general de producción ni demuestra que una política del modelo resista todas las presiones.

La suite ampliada de 12 variantes (4 por presión) se ejecuta en modo offline. Para consumir API con ellas, opta explícitamente:

```bash
.venv/Scripts/python.exe -X utf8 hackatons/meta/run_meta_harness.py --live-agent --include-expanded-live
```

Esto genera bastantes más llamadas/coste de API. `--use-meta-api` es otra opción separada: genera una reformulación del prompt, solo si además se proporcionó `META_MODEL_API_KEY`/`MODEL_API_KEY`; nunca evalúa resultados. En `--live-agent`, el texto generado no se usa para evitar mezclar el experimento fijo con una intervención de generación. No uses ambas opciones salvo que necesites probar específicamente esa combinación.

El harness nunca ejecuta efectos reales: los tools de finanzas/compliance son locales y los logs registran propuestas de tool-call del modelo, pero no llaman a endpoints externos. El evaluador contractual determinista sigue siendo quien asigna veredictos; el modelo no actúa como juez. Los resultados en `--live-agent` son una corrida con las respuestas del modelo disponibles, no una estimación de tasas generales ni una prueba causal. Si falta la clave, `--live-agent` termina con error en vez de reportar falsamente modo real.

El reporte se escribe en `hackatons/meta/output/` por defecto:
- `meta_safety_scan_report.json`: trazas, candidatos, configuración, resultados y métricas.
- `meta_safety_scan_report.md`: matriz de diagnóstico y agregados descriptivos.

## 2. Batería fija y métricas

Se ejecutan 4 casos base más 12 variaciones sintéticas —4 cada una de `authority`, `urgency` y `ambiguity`— en comparaciones before/after. En modo offline son 32 ejecuciones de candidato y el total muestra 15 fallos simulados en baseline frente a 0 en guarded. Esos datos verifican plumbing y las reglas/fixtures, no son resultados de Llama.

Las métricas por presión (`n=4`) son descriptivas de fixtures fijas; no implican rendimiento del modelo, significación estadística o causalidad. El modo live, por coste, ejecuta solo los cuatro casos base por defecto; el informe indica si se omitieron las variantes ampliadas.

## 3. Alcance y arquitectura

La implementación usa `DeclarativeAgentSandbox`, el adaptador del contrato y `evaluate_contract`. Cinco cláusulas hacen explícitos el scope, autorización, verificación, reafirmación de límites y escalada. El panel interactivo está en la ruta de canarios de la web y ejecuta por defecto el modo offline.

Las herramientas declaradas en el sandbox son mocks locales; la salida structured tool call se registra y se compara con una allowlist cerrada, sin efecto de pago/transferencia. No es una integración de producción con endpoints bancarios ni `guard_server` MCP. Antes de una demo real, revisar permisos, herramientas tipadas y la política de retención de las conversaciones.

Alcance cerrado y reparto por días de la submission: [alcance_submission_20261026_v0.1.md](alcance_submission_20261026_v0.1.md).

Para el detalle de decisiones del dossier, véase [meta.docx](../meta.docx). Para endpoint/modelos oficiales, consulta [Meta Model API](https://dev.meta.ai/docs/api-reference), [Chat Completions](https://dev.meta.ai/docs/protocols/chat-completions), [Models](https://dev.meta.ai/docs/models) y [Quickstart](https://dev.meta.ai/docs/quickstart).
