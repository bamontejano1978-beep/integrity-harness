# Checkpoint — Meta Global AI Developer Hackathon

**Corte:** 29 de octubre de 2026, final de jornada (hora local).  
**Track/proyecto:** Startups — *Integrity Harness — Automated Behavioral Safety Scan*.  
**Entrega objetivo:** 4 de noviembre de 2026, 21:00 ET (5 de noviembre, 03:00 CET en Madrid).  
**Estado global (actualización posterior a recibir la guía del participante):** **AMARILLO / NO LISTO PARA ENTREGAR**. El vídeo demo de tres minutos es obligatorio; el repositorio debe ser público, listar a todas las personas que trabajaron y mostrar claramente qué se construyó durante el hackatón. El producto live también es un artefacto primario de evaluación. La guía no dice literalmente que el producto tenga que exponerse en una URL pública; comprobar el campo de acceso a demo en la plataforma. La evidencia held-out tiene paquete Ed25519 verificable offline y tamper test superado también desde la UI local. No se ha elegido ni publicado alojamiento; Hugging Face queda descartado. Vídeo, acceso del producto a jueces, procedencia y submission final siguen pendientes. El resto de la fotografía base corresponde al corte del 29 de octubre.  
**Plan de continuación:** [plan_cierre_hackaton_meta_20261029.md](plan_cierre_hackaton_meta_20261029.md).  
**Alcance rector:** [alcance_submission_20261026_v0.1.md](../../meta/alcance_submission_20261026_v0.1.md).

## Resumen ejecutivo

El harness determinista está implementado sobre una línea base preexistente y se conserva una corrida real de Meta, firmada y verificable offline. La guía del participante confirma que la entrega requiere un vídeo de tres minutos y repo público; exige identificar el trabajo nuevo del evento. Jueces usarán el vídeo, el código y el producto live como artefactos primarios. La UI Gradio que ensayamos solo demuestra el verificador local del paquete: no debe presentarse como si fuera, por sí sola, el producto completo. Falta preparar un producto/recorrido live para jueces y confirmar en la plataforma cómo se accede; la guía no afirma expresamente que deba ser una URL pública. No se eligió hosting ni se desplegó nada.

La evaluación más reciente de frontera de herramientas fue una captura neutral recuperada, parcial por una respuesta faltante y seguida de replay con mocks. Es informativa y está documentada, pero no observó propuestas restringidas que permitan medir bloqueo del guard. No sustituye la demostración de la submission ni debe tratarse como una corrida confirmatoria.

## Semáforo por entregable

| Área | Estado al corte | Qué respalda el estado | Qué falta para verde |
| --- | --- | --- | --- |
| Harness preexistente y evaluación determinista | 🟢 Implementado | Código de submission; cuatro escenarios base y variantes offline; contratos y veredictos deterministas. | Ensayo final desde clon limpio y docs sincronizados con la implementación vigente. |
| Proveniencia de pre-evento | 🟡 Parcial | Existe commit inicial `2fd192d` importando motor/harness como baseline y un commit posterior en la rama local. | README distingue preexistente/nuevo; completar la tabla con commits verificables y reflejar cambios reales sin atribuciones no demostradas. |
| Meta Model API, corrida held-out | 🟢 Evidencia live guardada | ZIP verificado: `muse-spark-1.3`, seis escenarios sintéticos, 12 trayectorias, seis fallos sin guard y seis pases con guard, `external_api_calls_made=true`, sin efectos reales. | Integrar como evidencia complementaria en el producto/vídeo; hacer visible el uso de Meta durante el hackatón sin exagerar el artefacto guardado. Mantener límites: presión como metadato, guard determinista, muestra única/sintética; no es tasa ni garantía general. |
| Generación de escenarios por Muse Spark | 🔴 No acreditada | El informe archivado indica `meta_model_generator_mode: deterministic_offline`. | Si se mantiene en el corte: corrida real de generación y trazabilidad por caso (modelo, semilla, origen); si no, retirar/ajustar el claim en submission y vídeo. |
| Evaluación neutral de frontera de herramientas v0.7 | 🟡 Exploratoria, con faltante | Captura: 28 solicitudes iniciadas, 27 respuestas válidas, 1 faltante; 20 continuaciones nuevas; cero reintentos; reporte exploratorio disponible. | No permite medir bloqueo: 0/27 propuestas restringidas y 0 bloqueos observados. No usar como evidencia de eficacia; nueva prueba solo con protocolo/autoridad y tiempo suficientes. |
| Captura de frontera v0.4 | 🟡 Live, pero confundida | 28/28 solicitudes; 8 pares `verified_only`; replay 84 filas con mocks. | El prompt instruía explícitamente usar herramienta bajo autorización verificada; no atribuir el 8/8 solo al estado confiable. Sin propuesta no autorizada, no probó el bloqueo del boundary. |
| ZIP de evidencia actual | 🟢 Firma Ed25519 y tamper test offline verificados (30 oct) | Paquete `evidence/meta-heldout-live-20261028-signed.zip`; firma sobre el manifiesto canónico, que referencia por SHA-256 los 100 artefactos; comprobado también mediante las acciones locales de UI. | La clave pública embebida no acredita identidad externa ni timestamp confiable. Este paquete es evidencia técnica, no reemplaza el producto live ni el vídeo obligatorio. |
| Vídeo de tres minutos | 🔴 Obligatorio, no grabado | Guía del participante: la submission requiere un demo video de tres minutos, uno de los artefactos primarios de juicio. | Grabar tres minutos; mostrar el producto, Meta y funciones construidas durante el evento, distinguirlas del baseline y comprobar el enlace para jueces. |
| Repo público, equipo y submission | 🔴 Pendiente | Guía: repositorios públicos; una submission por equipo/startup a cargo de representante; listar a todas las personas que trabajaron. | El representante publica/valida acceso al repo, incluye la tabla de procedencia, lista a cada colaborador y completa el formulario; verificar enlaces desde sesión privada. |
| Créditos Meta API | 🟡 Estado por confirmar por participante | Guía: $150 por participante, reclamo individual antes del 4 nov. 21:00 ET; se requiere tarjeta para usar API y puede aparecer preautorización temporal; sugiere configurar aviso de gasto. | Cada participante verifica solicitud y saldo aplicado en Billing. El aviso no es un cap duro de gasto de una app/harness. |
| Producto live / acceso de jueces | 🔴 Pendiente de definir y probar | La guía dice que los jueces usarán el producto live como artefacto primario. La UI disponible solo verifica paquetes y no demuestra por sí misma el proyecto completo. | Definir flujo completo, uso visible de Meta y probar acceso para jueces. Confirmar en plataforma si se requiere URL pública; no se eligió hosting. |
| Seguridad operativa de inferencia pública | 🟡 No aplicable mientras no haya servicio live | Los artefactos guardados declaran `real_world_side_effects=false`; no se desplegó ni configuró hosting. | No exponer inferencia live sin cap duro, cuotas persistentes global/per-visitante, presupuesto, timeout y logs redactados probados. |

## Evidencia comprobada en este checkpoint

### Corrida held-out de Meta

- Paquete legado sin firma: [meta-heldout-live-20261028.zip](../evidence/meta-heldout-live-20261028.zip). Paquete actual firmado: [meta-heldout-live-20261028-signed.zip](../evidence/meta-heldout-live-20261028-signed.zip) (artefacto local ignorado por Git).
- Verificación offline: `python scripts/package_meta_evidence.py --verify evidence/meta-heldout-live-20261028-signed.zip`; `signed: true`, `verified_artifacts: 100`, firma Ed25519 válida.
- Prueba offline: `python scripts/package_meta_evidence.py --tamper-test evidence/meta-heldout-live-20261028-signed.zip`; cambia un byte del informe y re-hashea el manifiesto; la firma lo rechaza.
- Resultado de la corrida: `scenario_count: 6`; `trajectory_count: 12`; `unguarded fail: 6`; `guarded pass: 6`; llamadas externas realizadas; efectos reales desactivados.
- La clave privada permanece en `.tmp/meta-evidence-signing/ed25519-private.pem`, excluida por Git; no se incluye en el paquete. La clave pública embebida permite verificación offline, pero no acredita por sí sola identidad externa ni fecha confiable.

### Frontera de herramientas v0.4 y v0.7

- El reporte v0.4 registra 28/28 solicitudes live sin reintentos y los resultados por pares. Su informe reconoce confusión por la directiva del prompt de sistema y la ausencia de propuestas no autorizadas.
- El [informe exploratorio recuperado v0.7](analisis_exploratorio_tool_boundary_recuperacion_v0.7_20261029.md) informa 27/28 respuestas válidas, un intento faltante (`pair-008-verified`) con causa concreta desconocida, sin reintento. Entre las válidas, 0/27 propuestas de operación restringida y 13/27 propuestas de escalado humano.
- Replay de v0.7: 54 filas (27 casos × 2 brazos); 54/54 pases contractuales, pero cero bloqueos mock e intervenciones observables porque no hubo propuestas restringidas.
- Revisiones ejecutadas: `python -X utf8 scripts/verify_meta_guard_tool_boundary_freeze_v0_6.py` y `python -m pytest -q tests/test_meta_guard_tool_boundary_v0_7_recovery.py tests/test_meta_guard_tool_boundary_v0_7_continuation.py`; resultado conjunto: **9 passed**.
- El freeze v0.6 conserva captura desactivada y cero solicitudes autorizadas en el momento del preregistro. La recuperación se autorizó después bajo el tope acumulado documentado. Este checkpoint no autoriza llamadas futuras.

## Estado de repositorio y procedencia

En la inspección del 30 de octubre, `hackatons/submission-meta` estaba en la rama local `main` con numerosos archivos modificados y sin seguimiento, incluidos cambios de otras tareas previas a esta actualización. No se asume que esos cambios estén publicados ni se prescribe commit/push aquí.

Los commits visibles en la inspección fueron:

- `2fd192d` — importa el motor/harness preexistente como línea base.
- `11457a9` — congela la prueba offline de la frontera de autorización.

Antes de enviar, reconstruir una tabla precisa de procedencia a partir de `git log`/diff y revisar el contenido incluido en el repo público. No declarar como terminada una feature solo porque exista un plan o documentación de esa feature.

## Bloqueos y riesgos principales

1. **Identidad confiable del firmante aún no acreditada.** El paquete firmado detecta alteraciones y valida Ed25519, pero el verificador usa la clave pública autoembebida; para acreditar quién firmó se necesita fijar/distribuir esa clave por un canal independiente.
2. **Producto live para jueces aún no resuelto.** La guía lo incluye entre los artefactos primarios de evaluación. El Preview actual es solo el verificador de paquetes, no el producto completo. Confirmar forma de acceso y si la plataforma pide URL pública; Hugging Face está descartado y no hay despliegue.
3. **Vídeo obligatorio pendiente**, tres minutos según la guía del participante. El guion existe; falta grabar y verificar la URL de acceso.
4. **Repo público y procedencia final pendientes.** La guía exige que el repo sea público, que el trabajo nuevo del evento quede claramente identificado y que todas las personas que trabajaron figuren en la submission.
5. **Créditos Meta API por confirmar.** Cada participante debe reclamarlos individualmente antes del plazo indicado; comprobar Billing. El método de pago puede mostrar una preautorización y la notificación de gasto no establece por sí misma límite duro.
6. **Generador Meta no reflejado por el artefacto live disponible.** Decidir go/no-go pronto y describir el uso real de modelos sin sobreafirmar.
7. **Presupuesto y acceso a Meta/hosting.** Confirmar saldo, clave válida, gasto máximo y cuenta de hosting; no exponer credenciales ni inferir su estado por corridas anteriores.
8. **Tiempo corto.** A fecha de corte quedan días hasta la entrega; priorizar la rebanada vertical antes de repetir exploraciones.
9. **Proveniencia del README y del repositorio público.** El README ya distingue baseline, evidencia firmada y verificador local; falta hacer públicamente accesible el estado final de los cambios y comprobarlo desde clon limpio.
10. **Reglas y formulario.** Revisar la plataforma, representante, miembros, enlaces y waiver antes de enviar.


## Actualización — 30 de octubre: paquete firmado y prueba de alteración

Se añadió Ed25519 al empaquetador, manteniendo verificable el ZIP legado. El paquete nuevo incorpora `signature.json`; la firma cubre el hash del manifiesto canónico, y este contiene los hashes de los 100 artefactos. La clave privada se crea localmente, queda bajo `.tmp/` (ignorado por Git) y no se incluye en el ZIP. La clave pública se incluye para verificación autónoma. Se probó además una alteración de un byte del informe después de recalcular los hashes del manifiesto: la verificación rechaza la firma inválida. Sin clave pública fijada externamente, esto prueba integridad/firma matemática, no identidad del firmante ni timestamp confiable.

Verificación ejecutada: `python -m pytest -q tests/test_package_meta_evidence_v0_1.py` (**2 passed**), `python -m py_compile scripts/package_meta_evidence.py tests/test_package_meta_evidence_v0_1.py` y `python -m ruff check scripts/package_meta_evidence.py tests/test_package_meta_evidence_v0_1.py` (**All checks passed**). La verificación CLI del ZIP firmado informó **100 artefactos verificados**; el tamper test alteró un byte del informe, recalculó el manifiesto y rechazó correctamente la firma (`signature manifest digest does not match`). El ZIP creado es `evidence/meta-heldout-live-20261028-signed.zip` (528027 bytes; SHA-256 `d8bbba63e890f65102d7da8c5636dbef3f7fd534bff04c6a4f4ec0ffc0162def`). Se confirmó que ZIP y clave privada están ignorados por Git. El paquete local firmado no es aún una demo pública.

**Actualización 30 de octubre — verificador local offline preparado y revalidado:** la UI Gradio quedó en `offline_verifier/`; permite verificar ZIPs firmados o legados y ejecutar tamper test sobre una copia temporal, sin llamadas a Meta ni red externa. El original no se modifica. La prueba enfocada es `tests/test_meta_evidence_offline_verifier_v0_1.py`; se retiró el constructor de bundle específico de Spaces.

**Ensayo local funcional (30 de octubre):** `python -u offline_verifier/app.py` arrancó sin publicar el servicio. `http://127.0.0.1:7860/` devolvió **HTTP 200**, `text/html; charset=utf-8`; el Preview muestra el título, las acciones de verificación/tamper y los límites de entrada. Se invocaron las dos acciones del verificador vía el cliente API local de Gradio con `evidence/meta-heldout-live-20261028-signed.zip`: la verificación devolvió firma Ed25519 válida y 100 artefactos; el tamper test rechazó una copia alterada y con manifiesto re-hasheado. El SHA-256 del ZIP original fue igual antes y después. No se hizo una subida manual desde el navegador ni se publicó el servicio; el Preview local queda disponible para el ensayo/grabación.

Verificación posterior al cambio de rutas: `python -m pytest -q tests/test_meta_evidence_offline_verifier_v0_1.py tests/test_package_meta_evidence_v0_1.py` (**6 passed**); `python scripts/package_meta_evidence.py --verify evidence/meta-heldout-live-20261028-signed.zip` (**100 artefactos, firma válida**); `--tamper-test` (**1 byte alterado, manifiesto re-hasheado, rechazo correcto**); `py_compile`, Ruff y `git diff --check` correctos.

No se creó ni publicó ninguna Space; Hugging Face queda descartado. Según la guía del participante aportada por el equipo, **el vídeo de tres minutos y el repo público son obligatorios**, y el producto live es uno de los artefactos primarios de juicio. **Siguiente gate:** definir el flujo de producto que se mostrará, confirmar si la plataforma exige una URL pública y habilitar el repo/video accesibles para jueces; listar al representante y a todas las personas colaboradoras. Confirmar además los créditos Meta API individuales y su estado en Billing antes del 4 nov. 21:00 ET; la guía menciona una preautorización al añadir tarjeta y notificaciones de gasto, no un cap duro del harness. El verificador local confirmó firma, 100 artefactos y rechazo del tamper por las acciones API de Gradio; el ZIP original no cambió. No se hicieron nuevas llamadas live ni se eligió hosting.

El orden, calendario y gates completos están en el [plan de cierre](plan_cierre_hackaton_meta_20261029.md). Este checkpoint es una fotografía fechada, no una afirmación de que los elementos rojos se hayan completado después del corte.