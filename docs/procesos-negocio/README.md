# Procesos de negocio con roles — Factorii / NE Express

Levantamiento de los **procesos de negocio multi-rol** del producto: qué son, quién hace cada paso, qué falta en el código y en qué orden implementarlos.

**Base:** addon `l10n_pe_ne_biller` **v19.0.1.6.0** (HEAD `cffb92d`) · SPA `@ne/web-bff` · Odoo 19 · 2026-07-16.

## Los documentos

**Decisiones tomadas** (leer primero — cierran preguntas y corrigen el catálogo/roles donde envejecieron):

| Documento | Qué decide |
|---|---|
| [decision-escala-libre.md](decision-escala-libre.md) | **Escala libre (1→N usuarios).** Cierra P1. Ningún proceso exige dos personas; solo se valida `has_group`, nunca identidad; gates `off/aviso/bloqueo` por RUC |
| [decision-alta-usuarios.md](decision-alta-usuarios.md) | **Alta de usuarios por el dueño del RUC.** Cierra P3. Addon `l10n_pe_ne_roles`, `sudo()` + whitelist, cero ACL. Incluye el pentest (7 vectores a cerrar) |
| [decision-integridad-datos.md](decision-integridad-datos.md) | **Integridad del dato antes que las compuertas.** El control real para el cliente con gente: conteo ciego, inmutabilidad, autoría, retiro con contraparte |
| [decision-friccion-pyme.md](decision-friccion-pyme.md) | **Fricción PyME: el dueño-solo contra su propio POS.** CN-01 y CN-02 clic a clic vs los 3 toques del POS; 12 reglas de degradación mínima y la línea roja SUNAT (emisión sí, ceremonia no) |

**Exploración** (material de base; donde contradiga a las decisiones de arriba, gana la decisión):

| Documento | Qué contiene |
|---|---|
| [catalogo.md](catalogo.md) | **Los 14 procesos** (CN-01…CN-14), reglas de construcción, habilitadores, patrón común y orden por olas. ⚠️ Sus estados `pendiente_aprobacion` y sus compuertas "Supervisor aprueba" están corregidos por las decisiones |
| [roles.md](roles.md) | El **modelo de roles** de Odoo 19: los 7 grupos, la matriz rol × acción, el XML/CSV, la migración. ⚠️ Sus comparaciones de identidad (`aprobador ≠ solicitante`) y el `0 = apagado` están corregidos por las decisiones |
| [hallazgos.md](hallazgos.md) | Los **bugs** encontrados al mapear. No son procesos, son tickets — y varios son precondición |
| [preguntas-abiertas.md](preguntas-abiertas.md) | **P2, P4, P5, P6** abiertas (P1 y P3 ya resueltas) — las que sólo el negocio puede responder |
| [critica.md](critica.md) | La crítica adversarial al catálogo de candidatos: qué falta, qué descarte fue un error, qué envejece mal |

## El resumen en cinco líneas

1. **El hueco no es técnico, es de roles.** De 90 rutas `/ne/api`, 6 comprueban grupo. Existe un solo rol funcional (`group_l10n_pe_ne_anulacion`) — y es la plantilla exacta a copiar.
2. **Los 14 procesos son la misma figura:** documento con estado + responsable (`user_id`, nullable = *en cola*) + handoff (un rol empuja al siguiente y sólo ese rol puede) + cola filtrada en servidor + auditoría + parámetro de política por RUC.
3. **La decisión de modelo:** un **mixin** (`l10n_pe_ne.flujo.mixin`), no un modelo genérico con `tipo`. Con una sola excepción: `l10n_pe_ne.pedido` (orden de trabajo / encargo / apartado).
4. **Empezar por CN-01** (mostrador: cotiza → caja → despacho): el 60% ya existe y valida el patrón. Antes, la Ola 0 de habilitadores.
5. **Nada de aprobaciones antes de arreglar sus entradas.** Hoy el arqueo cuenta mal (ver [H1](hallazgos.md) y [H2](hallazgos.md)): un gate encima de un número falso es peor que no tener gate.

## Cómo se produjo esto, y cuánto confiar

Lo generó un workflow multi-agente: 6 exploradores mapearon el código, 6 lentes distintas (retail, taller, segregación de funciones, dinero, SUNAT, huecos del código) propusieron **28 procesos candidatos**, un verificador escéptico por candidato refutó contra el código (**21 sobrevivieron, 5 descartados**, 2 se perdieron por errores de API), y tres agentes de síntesis produjeron el catálogo, los roles y la crítica.

**Limitaciones que debes conocer antes de usarlo:**

- **La mitad de la exploración se hizo contra una copia vieja del addon** (`code/fact/`, v19.0.1.3.0) porque este repo se clonó en `fact2` a mitad de la corrida. El agente del catálogo lo detectó y se re-verificó contra v19.0.1.6.0, pero **puede quedar evidencia caducada**. Ante una cita que no cuadre, gana el código.
- **Tres agentes murieron por errores de API**, entre ellos la lente de servicios/taller — justo la que profundizaba la mecánica de la cola de CN-02. Ese proceso tiene menos cobertura que el resto.
- **La crítica corrió en paralelo al catálogo**, así que critica la lista de candidatos, no el catálogo final. Su §1.1 ("los dos casos del usuario no están") **es falsa**: sí están, como CN-01 y CN-02.
- **Sólo los hallazgos marcados ✅ en [hallazgos.md](hallazgos.md) están verificados a mano.** El resto lleva cita pero no re-lectura.

## Decisión de implementación tomada

La lógica va en un **addon nuevo y separado** que depende de `l10n_pe_ne_biller`, dejando el facturador intacto. Sigue en pie la regla del proyecto: **toda la lógica en Odoo; `ne-express` es sólo un BFF** (la SPA pinta y llama; no decide).
