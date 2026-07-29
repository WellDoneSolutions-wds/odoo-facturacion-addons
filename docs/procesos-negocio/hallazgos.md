# Hallazgos — defectos encontrados al levantar los procesos

Bugs y agujeros de control que aparecieron al mapear el código para el [catálogo](catalogo.md).
No son procesos: son **tickets**. Varios son precondición de los procesos (ver la Ola 0 del catálogo).

Cada uno lleva su **estado de verificación**:

- **✅ VERIFICADO** — abierto el archivo y leída la línea contra `l10n_pe_ne_biller` v19.0.1.6.0 (HEAD `cffb92d`).
- **🔶 REPORTADO** — lo halló un agente con cita concreta, pero **no está re-verificado a mano**. Confírmalo antes de actuar.

---

## H1 · Los dólares en efectivo son invisibles al arqueo ✅ VERIFICADO

`tools/caja_arqueo.py:29-32`, en `agrupar_ventas`:

```python
moneda = (v.get("moneda") or "PEN").upper()
if moneda != "PEN":
    count_usd += 1
    total_usd = _r2(total_usd + monto_total)
    continue          # <-- nunca entra a por_medio
```

El USD suma a un total informativo y **jamás entra a `por_medio`**, que es lo único que `calcular_arqueo` cruza contra el conteo físico. Traducción: **los dólares del cajón no se cuentan, no se esperan y no descuadran nunca.** Un cajero puede quedarse con el 100% del efectivo en dólares y la diferencia sale cero.

**Impacto:** cualquier control de arqueo que se construya encima (CN-04) es ciego a esa puerta. Relevante en negocios que cobran en dólares: servicios a empresas, turismo, repuestos importados.

---

## H2 · Toda venta emitida desde *Nuevo comprobante* se imputa 100% a Efectivo ✅ VERIFICADO

`tools/caja_arqueo.py:37-44`:

```python
if medios:
    for mp in medios: por_medio[medio] += _r2(mp.get("monto"))
elif forma == "Contado":
    # Contado sin medios detallados -> todo el total va a Efectivo (inferido).
    por_medio[EFECTIVO] += monto_total
    sin_medio += 1
```

Y en la SPA: `grep -c medios pages/POS.tsx` → **13**; `pages/Emitir.tsx` → **0**. **Emitir nunca envía medios.**

Por lo tanto toda venta hecha desde *Nuevo comprobante* (que es donde se configuran detracción, transferencia, anticipo) cae en el `elif` y va al esperado **en Efectivo por el 100% del total facturado**, aunque el cliente haya transferido o la detracción sólo deje cobrar el 88%.

**Impacto:** el "esperado en efectivo" está inflado casi todos los días, sin fraude y por diseño. **Ningún flujo de aprobación de descuadre debe implementarse antes de arreglar esto**, o el gate salta a diario sobre ventas legítimas, el dueño aprueba en automático y el control muere de fatiga de alarma — dejando algo peor que nada: un registro firmado que certifica un número falso.

---

## H3 · El tipo de cambio lo reescribe cualquier emisor, para cualquier fecha pasada ✅ VERIFICADO

`controllers/main.py:493-499` — `POST /ne/api/tipo-cambio` no comprueba **ningún** grupo:

```python
@http.route("/ne/api/tipo-cambio", **_POST)
def set_tipo_cambio(self, **kw):
    uid = self._identify()
    if not uid:
        return self._unauth()
    return self._run(lambda: self._company(uid).l10n_pe_ne_set_tipo_cambio(self._body()))
```

Y `models/res_company.py:155` acepta una `fecha` arbitraria, **retroactiva**:

```python
fecha = fields.Date.to_date(payload.get("fecha")) if payload.get("fecha") else fields.Date.context_today(self)
```

Está expuesto al cajero en el POS (`pages/POS.tsx`). El TC determina la conversión a soles del XML y del PLE: **es base imponible**.

**Impacto:** se ponen compuertas de aprobación en descuentos de S/5 mientras queda abierto el parámetro que multiplica todas las ventas en moneda extranjera, sin rastro (el modelo no hereda `mail.thread`).

---

## H4 · Las tres puertas abiertas del estado de la cotización ✅ VERIFICADO

Precondición dura de CN-01 y CN-05: sin esto el handoff a caja es decorativo.

**(a) Se puede cambiar el precio después de que el cliente pagó.** `models/l10n_pe_ne_cotizacion.py:244-266` — `l10n_pe_ne_update_cotizacion` reemplaza cabecera **y líneas** (`line_ids = [(5,0,0)] + …`) **sin mirar el estado**. Una cotización `convertida`, ya vinculada a un comprobante fiscal emitido, admite que le reescriban las líneas.

**(b) El estado se escribe crudo desde el payload.** Misma función, `:260-261`:

```python
if payload.get('estado'):
    vals['estado'] = payload['estado']
```

Una `convertida` vuelve a `borrador` con un `curl`.

**(c) `l10n_pe_ne_set_estado` (`:268-276`) no valida transiciones**, solo pertenencia al Selection:

```python
valid = dict(self._fields['estado'].selection)
if estado not in valid:
    raise UserError(_('Estado no válido.'))
self.estado = estado
```

**(d) `l10n_pe_ne_delete_cotizacion` (`:278-283`) hace `unlink()` sin guarda** — borra una cotización convertida con comprobante fiscal vinculado y se pierde el rastro cotización→comprobante.

**Arreglo (regla R7 del catálogo):** transiciones como métodos con nombre (`aceptar`, `rechazar`, `enviar`), nunca setters genéricos; guarda de estado en update y delete.

---

## H5 · Un parámetro de negocio vive en TypeScript ✅ VERIFICADO

`ne-express: pages/Caja.tsx:31` → `const AVISO_DIF = 10`, usado en `:305` para decidir si un descuadre se avisa.

Viola la regla arquitectónica del proyecto ("toda la lógica en Odoo; ne-express es solo un BFF"). La tolerancia de descuadre es **política del negocio y por RUC** → `res.company`, servida por `GET /ne/api/config`. (`ir.config_parameter` no sirve: es global a la BD, no por tenant.)

---

## H6 · El gate de anulación solo está en el controller 🔶 REPORTADO

`controllers/main.py:722-727` devuelve 403 sin `group_l10n_pe_ne_anulacion`, pero `l10n_pe_ne_quick_anular` (`models/account_move_biller.py:2189`) y `action_l10n_pe_send_baja` (`:5665`) **no re-chequean el grupo** → la vista backend de Odoo y los tests lo saltan.

Contrasta con el precedente admin, que **sí** valida en el modelo (`res_company.py:198,266`; `res_users.py:39,79`). Es el patrón correcto y ya está escrito: el modelo debe ser la autoridad, el controller solo da el 403 limpio.

Esfuerzo: XS. Es la única puerta cerrada del producto y tiene la llave puesta.

---

## H7 · Otros reportados, sin re-verificar 🔶 REPORTADO

| # | Hallazgo | Cita del agente |
|---|---|---|
| a | `list_price` se **escribe sin IGV** en `_l10n_pe_ne_quick_product` y se **lee con IGV** en el POS | bloquea CN-05 (tope de descuento) entero |
| b | `create_gasto` acepta monto ≤ 0 | `models/l10n_pe_ne_gasto.py` |
| c | `update_compra` destruye el detalle sin revertir stock | riesgo de kardex |
| d | El PLE deja pasar comprobantes en `error` / `en_proceso` | `models/` reportes; afecta CN-14 |
| e | `_serve_file` (`main.py:262`) sirve pdf/ticket/xml/cdr **sin contador ni log** | se puede reimprimir el ticket 50 veces sin huella |
| f | `_l10n_pe_tipo_operacion` (`:786-794`) **nunca devuelve `0104`** (Venta interna – Anticipos, cat. 51) | 🔴 **bloquea CN-02**: hoy el comprobante de anticipo saldría marcado como venta común |
| g | `l10n_pe_ne_anticipo_doc` es un `fields.Char` tecleado a mano; no valida que el comprobante exista, que sea del mismo cliente, ni que no se haya aplicado ya en otra factura | bloquea CN-02 con dos actores |
| h | `validez_dias` (`l10n_pe_ne_cotizacion.py:27`) existe, se serializa, se edita… y **no hace nada**: no hay estado `vencida`, ni cron, ni check en la conversión | el cliente llega el día 40 con precios de hace 40 días y el cajero no tiene cómo saberlo |

`f` y `g` son **prerrequisito fiscal de CN-02**; `h` es el arreglo más barato del catálogo (un estado, un cron, una guarda).
