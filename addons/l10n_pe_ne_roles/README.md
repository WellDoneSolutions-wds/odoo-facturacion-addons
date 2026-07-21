# l10n_pe_ne_roles — Roles y flujos de trabajo de NE Express

Addon que implementa los **procesos de negocio con roles** de NE Express sobre el facturador
`l10n_pe_ne_biller`, al que **extiende** (`_inherit`/`super()`) sin reescribirlo. Toda la lógica
vive aquí; la SPA solo la pinta y la llama por `/ne/api`.

El diseño completo está en `../../docs/procesos-negocio/` (catálogo de 14 procesos, modelo de
roles y las decisiones de escala libre, alta de usuarios, integridad de datos y fricción PyME).

## Estado — Iteración 1 (cimientos)

Esta iteración entrega **solo el cimiento técnico**, sin cambio visible para el usuario:

- **`l10n_pe_ne.flujo.mixin`** (`models/l10n_pe_ne_flujo_mixin.py`) — el motor de transiciones
  que comparten todos los procesos: `estado` (lo declara cada modelo) + `user_id` (responsable,
  NULL = en cola) + `mail.thread` (auditoría) + el `_check_transicion` de **tres ejes** (¿existe
  la transición?, ¿el usuario tiene el grupo?, ¿la realidad lo permite?) que **jamás compara
  identidades de usuario** (regla de escala libre), más los folds (`_avanzar`, `_ruta`,
  `_avanzar_hasta`) y el cálculo de acciones para la SPA (`_acciones`).
- **H-5** (en el biller): `l10n_pe_ne_quick_anular` re-chequea el grupo de anulación en el
  modelo, no solo en el controller.
- **`test_escala_libre.py`** — el invariante ejecutable: ningún modelo de flujo puede atascar
  a un usuario con todos los roles, ni comparar identidades en una guarda.

Todavía **ningún modelo hereda el mixin** (los primeros serán la cotización en CN-01 y el pedido
en CN-02) y **no hay grupos ni ACL** (llegan en la iteración 3, con el alta de usuarios por el
dueño). El motor de gates de política por RUC (`off/aviso/bloqueo`) llega en la iteración 4 y se
engancha en el hook `_politica_de`, hoy un no-op.

## Verificación

El CI de este repo no corre la suite de Odoo (solo despliega). Para validar esta iteración:

```
odoo-bin -i l10n_pe_ne_roles -u l10n_pe_ne_biller --test-enable --stop-after-init -d <bd>
```

Debe instalar el addon nuevo, actualizar el biller (por H-5) y pasar `test_install` y
`test_escala_libre`.
