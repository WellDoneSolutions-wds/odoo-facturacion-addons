# Preguntas abiertas — hay que responderlas antes de implementar

Cuatro preguntas abiertas (P2, P4, P5, P6) que el catálogo **asume** y que sólo el negocio puede contestar. Cada una cambia el diseño, no el detalle. **P1 y P3 ya están resueltas** (ver abajo). Están ordenadas por cuánto mueven.

---

## P1 · ¿Cuántas personas hay en el local? — ✅ RESUELTA

> **Respuesta del usuario (2026-07-17):** no hay una empresa en particular; el producto es SaaS multi-tenant y debe funcionar de **1 usuario con todos los roles** a **N usuarios segregados**.

**Consecuencia:** ninguna compuerta puede exigir dos personas. La segregación de funciones deja de ser el control principal (con 1 usuario es matemáticamente imposible: `aprobador ≠ solicitante` con `|U|=1` es `False`). La primitiva por defecto es **registro + evidencia + revisión asíncrona** + gates opcionales `off/aviso/bloqueo` (default apagado). El rediseño está en **[decision-escala-libre.md](decision-escala-libre.md)** y el barrido de las compuertas, y el control que sí protege al cliente con gente está en **[decision-integridad-datos.md](decision-integridad-datos.md)**.

*(Nota: la vieja redacción de P1 asumía que había un escenario único y que "≥2 personas por turno" era la premisa; eso quedó refutado por la respuesta del usuario.)*

---

## P2 · ¿Un local o varios? ¿Un RUC o varios por dueño?

**Por qué importa:** el addon asume **un solo local por RUC** en dos sitios estructurales:

- `models/account_move_biller.py:3494-3495` y `:3650` → `stock.warehouse.search([("company_id","=",…)], limit=1)`
- `models/l10n_pe_ne_caja.py:41-48` → índice único: **una sesión de caja abierta por compañía**

En retail peruano el eje de segregación suele ser **el punto de venta** (la sucursal de Gamarra no ve lo de Grau), no el rol. Si hay dos locales, el kardex y la caja son irreparables con este catálogo y el enfoque entero está mal.

**Cambia:** el eje del modelo de datos.

---

## P3 · ¿Quién da de alta al cajero y le asigna el rol? — ✅ RESUELTA

> **Decisión del usuario (2026-07-17):** lo hace el **dueño del RUC** (la sugerencia recomendada).

Se implementa como el habilitador **H-4** en el addon nuevo `l10n_pe_ne_roles`: grupo marcador `group_l10n_pe_ne_duenio` + métodos `.sudo()` con **whitelist** de grupos otorgables + scope por compañía por **inclusión**, y **cero filas de ACL** sobre `res.users`. El análisis que estaba aquí (por qué grupo + ACL es inseguro) era correcto y ahora es la razón del diseño, no una pregunta. Ver **[decision-alta-usuarios.md](decision-alta-usuarios.md)** — incluye el resultado del pentest (los 4 objetivos duros quedan cerrados; 7 vectores a arreglar antes de merge).

**Preguntas menores que esto deja abiertas:** ¿hay tope de usuarios por RUC / se cobra por usuario? ¿un dueño puede tener más de un RUC?

---

## P4 · Caso 2 — el adelanto: ¿se factura al recibirlo, o se da un recibo interno y un solo comprobante al final?

**Por qué importa:** es el núcleo fiscal de CN-02.

- **Si se factura:** hay que emitir con `tipoOperacion` **`0104`** (Venta interna – Anticipos, cat. 51) — que hoy `_l10n_pe_tipo_operacion` (`:786-794`) **nunca devuelve** — y regularizar con descuento global cód. 04, convirtiendo `l10n_pe_ne_anticipo_doc` (hoy un `Char` tecleado a mano) en Many2one con constraints de unicidad y partner.
- **Si es recibo interno:** no se toca nada fiscal y CN-02 cuesta un tercio (aunque SUNAT diga otra cosa: recibir plata a cuenta obliga a emitir).

**Cambia:** el núcleo fiscal del Caso 2.

---

## P5 · Caso 1 — ¿el despacho entrega siempre en el acto, o el cliente puede recoger después / otro día?

**Por qué importa:** decide si "despacho" es una pantalla o un modelo.

- **En el acto:** es un paso de UI + un par de campos. Barato.
- **Diferido:** necesitas modelo de pedido + reserva. Y hay que decidir qué pasa con el kardex: **la emisión ya descuenta el stock directo al cliente sin `stock.picking`** (`models/account_move_biller.py:3494-3510`), así que el producto sale contablemente mientras el bulto sigue en el mostrador.

**Cambia:** si el Caso 1 es una pantalla o un modelo nuevo.

---

## P6 · La cotización: ¿el precio y la validez de 15 días son vinculantes?

**Por qué importa:** es la condición de entrada **y** la de salida del Caso 1.

`validez_dias` existe (`models/l10n_pe_ne_cotizacion.py:27`, default 15) y **no hace nada**. Si el cliente llega a caja el día 40: ¿se respeta el precio, se recotiza, o el sistema lo bloquea? En una librería son S/2 de margen; en una ferretería con fierro y cemento es la venta entera a pérdida.

Y una vez que pagó, **¿alguien puede seguir editando esa cotización?** Hoy sí (ver [hallazgos H4](hallazgos.md)).

**Cambia:** la condición de entrada y la de salida del Caso 1.
