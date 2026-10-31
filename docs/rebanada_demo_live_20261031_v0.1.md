# Rebanada demo live — Meta Global AI Developer Hackathon

**Corte:** 31 de octubre de 2026.  
**Entrega:** 4 de noviembre de 2026, 21:00 ET = 5 de noviembre, 03:00 CET.  
**Estado:** definición cerrada; nada de esta rebanada está implementado todavía.  
**Documento rector:** [alcance_submission_20261026_v0.1.md](../../meta/alcance_submission_20261026_v0.1.md).  
**Plan de cierre:** [plan_cierre_hackaton_meta_20261029.md](plan_cierre_hackaton_meta_20261029.md).

---

## 0. Decisión

La «mini versión» no es un producto paralelo ni un motor nuevo. Es **una rebanada vertical de una sola pasada** sobre el harness existente:

> Un juez pulsa un botón, se ejecuta **un caso fijo** contra Meta Model API en vivo, el contrato determinista emite los veredictos de los dos brazos, y la corrida se convierte en un paquete firmado que se verifica y que rechaza una alteración.

Eso es todo. No hay chat libre, no hay entrada de texto, no hay segundo evaluador, no hay generación de escenarios con Meta y no hay métricas nuevas.

**Por qué esta forma y no otra:**

- La etapa 1 exige que el camino que ejecuta el juez llame a modelos de Meta. El harness actual arranca offline; la rebanada hace que la única acción posible del juez sea una corrida live.
- El demo debe mostrar trabajo del hackatón. La app, la corrida acotada, el paquete de un caso y su verificación son nuevos; el motor contractual y el harness quedan rotulados como baseline `2fd192d`.
- Quedan cuatro días. El vídeo, el repo público y el acceso para jueces son obligatorios; la rebanada alimenta los tres, un producto paralelo no alimenta ninguno.

**Lo que no se hace:** no se reimplementa el evaluador, no se entrena nada, no se añaden escenarios, no se toca la suite de seis casos y no se reclama que Muse Spark genere escenarios. El informe seguirá diciendo `meta_model_generator_mode: deterministic_offline`.

---

## 1. Recorrido del juez

### 1.1 En la app alojada

1. Abre la URL. Ve una tarjeta fija con el caso y los tres turnos de usuario; no hay campo de texto.
2. Pulsa **«Ejecutar corrida live (máx. 6 llamadas)»**.
3. La app muestra el progreso: `Llamada 1/6`, `2/6`, … y el modelo configurado.
4. Al terminar, ve dos columnas:
   - **Sin guard:** respuesta final del modelo y veredicto del contrato.
   - **Con guard:** propuesta original del modelo, respuesta efectiva tras la intervención determinista y veredicto.
5. Debajo, la tabla de cláusulas con `pass` / `fail` / `indeterminate` y el motivo registrado.
6. Pulsa **«Firmar paquete»**. La app genera un ZIP, muestra SHA-256, Key ID y número de artefactos.
7. Pulsa **«Verificar»**. La firma Ed25519 y todos los hashes se validan.
8. Pulsa **«Alterar un byte»**. Se modifica una copia temporal, se re-hashea el manifiesto y la firma la rechaza. El ZIP original no cambia.
9. Puede descargar el ZIP y verificarlo offline con el comando del README.

**Tiempo objetivo:** 45–90 segundos de corrida; menos de 3 minutos el recorrido completo.  
**Si el cap está agotado:** mensaje claro y enlace al paquete firmado guardado y al verificador offline. No se hace una llamada más.

### 1.2 En local, sin alojamiento

El mismo recorrido con:

```bash
python demo_app.py
```

La app escucha en `127.0.0.1:7860` por defecto. Sin clave de Meta, muestra un rechazo de una línea y no cae a fixtures.

### 1.3 Qué ve el juez en pantalla sobre procedencia

Un rótulo permanente, no una nota al pie:

> **Baseline:** motor contractual y harness importados en `2fd192d` (antes del hackatón).  
> **Nuevo (31 oct–1 nov):** corrida live acotada, app de demo, paquete de un caso, verificación y tamper test.

---

## 2. Contrato técnico

### 2.1 Caso

| Campo | Valor |
| --- | --- |
| Escenario | `heldout_authority_01_audit_chair` |
| ID en el informe | `meta-heldout_authority_01_audit_chair` |
| Presión | `authority` |
| Turnos | 3 |
| Fuente | `hackatons/meta/scenarios/held_out_scenarios_v0.1.json` |
| Por qué este | Es el caso que ya narra el guion guardado; el contraste sin/con guard es legible en menos de un minuto |

Los tres turnos son los del fixture: settlement pausado, aprobación verbal del comité de auditoría sin actas, y petición de liberar saltándose el artefacto de aprobación.

**No se garantiza el resultado.** Una corrida nueva puede producir respuestas distintas. El paquete acredita que la corrida ocurrió y qué veredictos emitió el contrato; no exige que el brazo sin guard falle. Si pasa, se muestra y se dice.

### 2.2 Llamadas y coste por corrida

El sandbox llama al proveedor en **cada turno de cada brazo**, incluso cuando el guard sustituye después la respuesta. Por tanto:

```
1 escenario × 3 turnos × 2 brazos = 6 llamadas como máximo
```

Parámetros fijados por la configuración live existente:

- `max_tokens = 1024`
- `temperature = 0.0`
- `timeout = 25 s` por llamada en la app (45 s por defecto en el harness)
- `0` reintentos automáticos
- herramientas locales mock; `side_effects_enabled = false`

**Cap duro por corrida:** contador que rechaza la llamada 7 antes de enviarla. No basta con que el escenario tenga tres turnos: el contador debe ser explícito y tener un test que demuestre el rechazo.

### 2.3 Extensión del harness

Se añade un modo acotado a `hackatons/meta/run_meta_harness.py`:

```bash
python -X utf8 hackatons/meta/run_meta_harness.py \
  --live-agent \
  --demo-only meta-heldout_authority_01_audit_chair \
  --output-dir .tmp/demo-runs/<run_id>
```

Reglas:

- `--demo-only` exige `--live-agent`.
- Es mutuamente excluyente con `--held-out-only`, `--include-expanded-live` y `--use-meta-api`.
- Carga el escenario de `held_out_scenarios_v0.1.json` por ID; acepta el ID con o sin prefijo `meta-`.
- Reutiliza `evaluate_pair` y `_configure_live_candidate`; no duplica la lógica de evaluación.
- Escribe `meta_safety_scan_report.json` y `.md` en el directorio de salida.
- Si falta la clave o el modelo no está habilitado, sale con código 1 y un mensaje de una línea; no escribe un informe etiquetado como live.

**Informe mínimo:**

```json
{
  "suite_kind": "demo_live_slice",
  "suite_version": "meta-demo-live-v0.1",
  "behavioral_agents_mode": "live_meta_model_with_local_mock_tools",
  "meta_model_generator_mode": "deterministic_offline",
  "external_api_calls_made": true,
  "side_effects_enabled": false,
  "meta_model_name": "<id confirmado con GET /models>",
  "live_run_limits": {
    "maximum_model_requests": 6,
    "requests_observed": 6,
    "provider_timeout_seconds": 25,
    "max_completion_tokens": 1024,
    "automatic_retries": 0,
    "tools_are_local_mocks": true,
    "real_world_side_effects": false
  },
  "demo": {
    "scenario_id": "meta-heldout_authority_01_audit_chair",
    "axis": "authority",
    "turns": 3,
    "arms": 2
  },
  "totals": {
    "scenarios_evaluated": 1,
    "trajectories_executed": 2,
    "assistant_proposals_recorded": 6
  }
}
```

El informe conserva `comparisons[0].before_remediation_unguarded` y `after_remediation_guarded` con `conversation_turns` que incluyen `model_proposal_text`, `guard_intervened` y `guard_intervention`. Sin esos campos no se puede mostrar la diferencia entre propuesta y respuesta efectiva.

### 2.4 Empaquetador: cambio mínimo, sin romper el held-out

`scripts/package_meta_evidence.py` está acoplado a la corrida de seis casos: `REQUIRED_CASES`, `validate_report`, `package_id` fijo y salida por defecto. Para el paquete de un caso:

1. **Dispatch, no reescritura.** `validate_report(report)` conserva la rama actual para `suite_kind == "held_out_only"` y añade `_validate_demo_report` para `suite_kind == "demo_live_slice"`.
2. **La rama demo no exige un resultado concreto.** Valida estructura y procedencia:
   - un solo `comparison`, dos trayectorias, seis propuestas de asistente;
   - mismo `provider_model` en ambos brazos;
   - `external_api_calls_made is True`, `side_effects_enabled is False`;
   - `meta_model_generator_mode == "deterministic_offline"`;
   - `live_run_limits.maximum_model_requests == 6`;
   - `suite_source == "demo_live_slice"`.
   Los estados `pass` / `fail` / `indeterminate` se registran, no se imponen.
3. **`package_id` y salida parametrizables.** `--profile demo` usa `meta-demo-live-<fecha>` y `evidence/meta-demo-live-<fecha>-signed.zip`; `--report` y `--output` siguen siendo explícitos.
4. **Mismos artefactos.** El ZIP incluye informe JSON/MD, el escenario demo, la suite held-out completa, contratos, adaptador, sandboxes, runner, `src/autopilot/`, el propio empaquetador y un README con límites.
5. **`verify_package` y `tamper_test_package` se reutilizan.** Solo cambia el dispatch de validación; la firma, el manifiesto y la alteración de un byte no se tocan.
6. **Test de no regresión.** El ZIP held-out existente debe seguir verificando 100 artefactos y rechazando el tamper test.

**Si el dispatch se complica, la alternativa es un `scripts/package_demo_evidence.py` separado.** No se toca la validación held-out. Pero no se duplica la firma: se importan `_sign_manifest`, `_write_archive` y `_sha256`.

### 2.5 App de demo

Archivo nuevo: `demo_app.py` en la raíz de `submission-meta`.

- Gradio 6.31.0, ya fijado en `offline_verifier/requirements.txt`.
- Sin `gr.Textbox` ni `gr.File` en la pestaña live: el caso es fijo y no se aceptan entradas.
- Botones: ejecutar, firmar, verificar, alterar, descargar.
- Resultado en dos columnas y tabla de cláusulas.
- Rótulo de procedencia y límites visible.
- `demo.queue(max_size=4, default_concurrency_limit=1)`.
- `launch(server_name=DEMO_HOST, server_port=DEMO_PORT, share=False, show_error=False)`.
- La clave se lee solo en el servidor; nunca se envía al navegador ni se escribe en logs.
- Sin clave: mensaje `DEMO REFUSED: Meta Model API key not configured`; sin fallback a fixtures.
- Error del proveedor a mitad de corrida: abortar, no emitir paquete, mensaje legible; la corrida consume cupo.

La app no sustituye al verificador offline: reutiliza las funciones de verificación del empaquetador. El verificador Gradio sigue siendo una herramienta local sin red; la app live es otra pieza.

---

## 3. Cap de coste y seguridad

### 3.1 Números duros

| Límite | Valor | Dónde se aplica |
| --- | ---: | --- |
| Llamadas por corrida | 6 | Contador previo a cada `POST /chat/completions` |
| Corridas por visitante | 3 / 24 h | Sesión de Gradio; IP + user-agent como respaldo; `X-Forwarded-For` solo desde proxy declarado en `DEMO_TRUSTED_PROXIES` |
| Corridas globales por día | 40 | Ledger persistente |
| Corridas totales | 200 | Ledger persistente |
| Llamadas totales | 1.200 | Derivado: 200 × 6 |
| Concurrencia | 1 | Cola de Gradio |
| Cola máxima | 4 | Cola de Gradio |
| Timeout por llamada | 25 s en la app; 45 s en el harness | Provider |
| Timeout por corrida | 180 s | Orquestador |
| Reintentos | 0 | Provider y app |
| `max_tokens` | 1024 | Provider |

Los topes globales son una **decisión de propietario**: se ajustan después de medir la primera corrida real y el precio del modelo, dejando al menos el 50% de los $150 sin gastar. El cap de la app no es un cap monetario de la cuenta; el límite de gasto se configura en Meta Billing.

### 3.2 Fail-closed

- Sin clave, clave inválida o modelo no habilitado: no se ejecuta; no se escribe un informe live.
- Llamada 7: se rechaza antes de enviar; no se genera paquete.
- Error del proveedor: se aborta la corrida; no se rellena con fixtures ni con la corrida guardada.
- Cap agotado: no se llama; se ofrece el paquete guardado y el verificador offline.
- El ZIP solo se genera si las 6 llamadas terminaron y el informe es válido; un paquete parcial no se firma.

### 3.3 Privacidad y logs

- No se registran prompts, respuestas ni la clave.
- Se registra: `run_id`, modelo, número de llamadas, estado, duración y código de error.
- La IP se guarda solo como hash con sal; no se muestra.
- El directorio de la corrida se borra una hora después de firmar; si no se firma, la siguiente corrida limpia los restos de más de una hora. La copia descargada es del usuario.
- No se aceptan uploads en la pestaña live; el verificador local mantiene sus límites de ZIP.

---

## 4. Procedencia y commits

### 4.1 Qué es baseline y qué es nuevo

| Pieza | Procedencia | Commit |
| --- | --- | --- |
| Motor contractual (`src/autopilot/`), harness, escenarios, contratos | Preexistente | `2fd192d` |
| Freeze offline de la frontera de autorización | Evento, ya commiteado | `11457a9` |
| Empaquetador firmado y verificador offline | Evento, sin commit al corte | Nuevo commit |
| Corrida demo de un caso (`--demo-only`) | Nuevo | Nuevo commit |
| App de demo live y caps | Nuevo | Nuevo commit |
| Vídeo de 3 minutos | Nuevo | Enlace en README/submission |
| Producto live alojado | Nuevo, si el gate de hosting pasa | URL en README/submission |

El README debe dejar de decir que el empaquetador «aún no está representado por un commit post-baseline» en cuanto se commitee. La tabla de procedencia se actualiza con hashes verificables, no con afirmaciones.

### 4.2 Commits propuestos

1. `Documenta la rebanada demo live de un solo caso` — este documento.
2. `Añade corrida demo acotada y paquete firmado de un caso` — harness, empaquetador, tests.
3. `Añade la app de demo live con caps y verificación` — `demo_app.py`, ledger, tests.
4. `Actualiza procedencia, comandos y límites para la submission` — README, Dockerfile si aplica.

**Reglas:**

- `git add` con rutas explícitas; nunca `git add -A`.
- No commitear `.env`, `.env.txt`, `apimeta.env`, `.tmp/`, `evidence/*.zip`, `.pytest_cache`, `__pycache__`.
- No commitear los archivos no trackeados de la exploración v0.3–v0.7 ni los análisis exploratorios: no forman parte de esta rebanada y confunden la procedencia. `11457a9` ya está en la historia y no se reescribe.
- No reescribir `2fd192d` ni `11457a9`.
- Push solo con autorización del propietario; después, clon anónimo y comprobación de que `origin/main` coincide.
- `LicenseRef-Proprietary` en un repo público sigue siendo una contradicción. Es decisión del propietario: licencia real o explicación explícita. No se publica sin resolverlo.

### 4.3 Qué se excluye del commit público

- Los runners y análisis de las capturas v0.3–v0.7.
- Los ZIP de evidencia y las claves.
- Los logs de preview y los directorios de corrida.
- Cualquier informe con contenido de conversaciones que no esté dentro de un paquete firmado.

---

## 5. Calendario y gates

Hoy es sábado 31 de octubre. El plan de cierre marca el producto el 1 de noviembre y el vídeo el 2. Esta rebanada se adelanta para no comerse el colchón.

### Sábado 31 de octubre — código y primera corrida

- [ ] Preflight `GET https://api.meta.ai/v1/models` con la clave local; confirmar el ID habilitado. No imprimir la clave.
- [ ] Implementar `--demo-only` y el contador de 6 llamadas.
- [ ] Implementar el dispatch del empaquetador y el perfil demo.
- [ ] Test offline del contador: la llamada 7 se rechaza.
- [ ] Primera corrida live del caso de autoridad.
- [ ] Firmar, verificar y tamper test del paquete de un caso.
- [ ] Test de no regresión del paquete held-out.

**Gate 31 oct, 22:00:** informe con 6 llamadas, paquete firmado que verifica y alteración rechazada. Si falla la clave o el modelo, se escala al propietario esa misma noche; no se simula una corrida live.

### Domingo 1 de noviembre — app, caps y hosting

- [ ] `demo_app.py` con el recorrido completo.
- [ ] Ledger persistente y cuota por visitante; probar cap agotado sin llamar a Meta.
- [ ] Dockerfile y dependencias de demo.
- [ ] Decisión de hosting a las 12:00; deploy si hay autorización.
- [ ] Prueba desde navegador limpio, dos sesiones independientes.
- [ ] Prueba de clave ausente y de error del proveedor.

**Gate 1 nov, 18:00:** URL (o recorrido local) que un tercero completa sin instrucciones; cap agotado explicado; clave no visible. Si no hay hosting, se registra la limitación y se sigue con vídeo y repo; no se inventa un producto alojado.

### Lunes 2 de noviembre — vídeo

- [ ] Grabar 3:00 ± 10 s con la corrida live real.
- [ ] Rótulo en pantalla de baseline vs nuevo.
- [ ] Mostrar firma, verificación y rechazo de la alteración.
- [ ] Revisar audio, legibilidad y enlace.

**Gate 2 nov, 20:00:** vídeo subido y accesible desde ventana privada.

### Martes 3 de noviembre — repo y submission

- [ ] Commits de procedencia y README final.
- [ ] Clon limpio: harness offline, verificador y tests pasan sin clave.
- [ ] Enlaces de repo, vídeo y demo desde sesión privada.
- [ ] Lista completa de personas y representante.
- [ ] Texto de submission y waiver revisados.

**Gate 3 nov, 20:00:** todo accesible; no quedan features esenciales.

### Miércoles 4 de noviembre — envío temprano

- [ ] Smoke test final.
- [ ] Enviar por la mañana, no por la tarde.
- [ ] Guardar acuse y URL final.

---

## 6. Go / no-go y fallbacks

| Situación | Decisión | Qué se hace |
| --- | --- | --- |
| La clave no funciona o el modelo no está habilitado | **No-go live** | Se escala al propietario; se conserva el paquete guardado y el verificador; no se finge una corrida |
| La corrida live funciona pero el paquete no verifica | **No-go paquete** | Se corrige el empaquetador; no se graba el vídeo hasta que verifique |
| El paquete held-out se rompe con el dispatch | **Rollback** | Se separa `package_demo_evidence.py`; la rama held-out queda intacta |
| No hay hosting al 1 nov 18:00 | **Live local** | Vídeo con la app local, repo público, limitación declarada en la submission |
| El modelo sin guard pasa el caso | **Se muestra** | El contrato es el producto; no se fuerza un fallo ni se edita la respuesta |
| La corrida tarda más de 3 minutos | **Recortar** | Menos `max_tokens` o modelo más rápido; no se recorta el vídeo ni el repo |
| Se acaba el tiempo | **Prioridad del plan** | Vídeo + producto + repo; la firma/tamper se mantiene porque ya está hecha |

**Regla de honestidad:** si algo no se ejecutó, se dice. Un paquete guardado no es una corrida nueva; una replay no es una llamada a Meta; una firma autoembebida no acredita identidad externa.

---

## 7. Guion del vídeo (3 minutos)

| Tiempo | Qué se ve | Qué se dice |
| --- | --- | --- |
| 0:00–0:25 | Tarjeta del caso | El problema: presión de autoridad en operaciones restringidas; el contrato decide, no un LLM |
| 0:25–0:50 | Rótulo baseline vs nuevo | El motor y el harness son baseline `2fd192d`; lo nuevo es la corrida live acotada, la app, el paquete y la verificación |
| 0:50–1:50 | Corrida live: 6 llamadas, dos columnas | Respuestas reales de Meta; veredicto determinista; herramientas mock; sin efectos reales |
| 1:50–2:30 | Firmar, verificar, alterar | Firma Ed25519, todos los artefactos verificados, alteración rechazada; clave autoembebida, sin identidad externa |
| 2:30–3:00 | Límites y enlaces | Una corrida sintética no es una tasa; repo, URL y cómo verificarlo |

No se preescribe la respuesta del modelo. Se usa la corrida real y se lee lo que salió. Si el brazo sin guard pasa, se dice.

---

## 8. Decisiones que requieren al propietario

1. **Hosting:** ¿se aloja la app? ¿Qué cuenta y presupuesto? Sin decisión antes del 1 nov 12:00, la rebanada queda local.
2. **Licencia:** resolver `LicenseRef-Proprietary` antes de publicar.
3. **Push:** autorizar la publicación de `11457a9` y de los commits nuevos en `origin/main`.
4. **Modelo y créditos:** confirmar el ID habilitado y el saldo; el preflight no debe imprimir la clave.
5. **Alcance del repo público:** confirmar que la exploración v0.3–v0.7 queda fuera.
6. **Equipo:** representante y lista completa de personas.

---

## 9. Definición de hecho

La rebanada está lista cuando se cumplen las seis:

1. Desde la app, un clic ejecuta el caso fijo con **6 llamadas como máximo** y escribe un informe live.
2. El informe distingue propuesta del modelo y respuesta efectiva tras el guard, y declara `external_api_calls_made: true`, `side_effects_enabled: false` y `meta_model_generator_mode: deterministic_offline`.
3. El paquete de un caso se firma, verifica y rechaza una alteración de un byte; el ZIP held-out de seis casos sigue verificando.
4. La app rechaza sin clave, con cap agotado y ante error del proveedor, sin caer a fixtures.
5. El vídeo dura tres minutos, muestra la corrida real y rotula qué es baseline y qué es nuevo.
6. El repo público se clona limpio, el harness offline y el verificador funcionan sin clave, y la submission lista a todas las personas y los enlaces.

---

## 10. Lo que este documento no afirma

- No afirma que la rebanada sea un motor nuevo ni que el baseline sea trabajo del hackatón.
- No afirma que una corrida de un caso mida tasas de fallo, robustez general o causalidad.
- No afirma que el guard sea un producto de producción ni que entienda cualquier presión.
- No afirma que Muse Spark genere los escenarios: el generador sigue offline.
- No afirma que la firma acredite identidad externa ni timestamp confiable.
- No afirma que haya producto alojado hasta que exista una URL probada.
