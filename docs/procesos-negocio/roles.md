# Modelo de roles y permisos — `l10n_pe_ne_biller` (Odoo 19)

> ⚠️ **PARCIALMENTE SUPERSEDIDO por las decisiones del 2026-07-17.** Donde este documento:
> - compara **identidades de usuario** (`aprobador_id ≠ solicitante_id`, `usuario_revision_id ∉ {cajero_id, usuario_cierre_id}`), o hace depender una regla de `search_count()` de usuarios en runtime → **eliminado**: es un deadlock con 1 usuario. Ver **[decision-escala-libre.md](decision-escala-libre.md)**.
> - usa la convención **`0 = apagado`** para una tolerancia/umbral → se separa en **dos campos ortogonales** `_modo` (`off/aviso/bloqueo`) y `_umbral` (0 = tolerancia cero cuando está encendido).
> - dice que el **supervisor** da de alta usuarios o asigna roles, o que "Dueño del RUC no es un grupo" → lo hace el **dueño**, y **sí** hay un grupo marcador `group_l10n_pe_ne_duenio` (con cero ACL); el scope es por **inclusión**, no intersección. Ver **[decision-alta-usuarios.md](decision-alta-usuarios.md)**.
>
> El resto (los 7 grupos, la matriz rol × acción, la migración, el bloqueante R2 sobre `account.move`) sigue vigente.

> **Antes de nada: el brief apunta al repo equivocado.** Verifiqué los dos árboles. El canónico es
> **`addons/l10n_pe_ne_biller`** — `__manifest__.py` v**19.0.1.6.0**, commit `cffb92d` de hoy, con `migrations/`, `stock`, `product_expiry`, guías y **90 rutas**.
> `code/fact/...` es v**19.0.1.3.0**, commit del 10-jul: una copia congelada. **Todo el mapa de "roles" del contexto se leyó sobre `fact/` y por eso está desactualizado en su hallazgo central.** Corrijo abajo. El resto de este documento se apoya solo en `fact2` + Odoo 19 vanilla.

---

## 0. Corrección: el patrón que hay que generalizar YA EXISTE y está probado

El contexto afirma repetidamente que `puedeAnular` es "un permiso fantasma que falla ABIERTO" y que "no existe ni una sola comprobación de grupo funcional". **Es falso contra el código canónico.** En la v19.0.1.4.0 alguien ya resolvió exactamente este problema, una vez, entero:

| Pieza | Dónde (canónico) | Qué hace |
|---|---|---|
| Grupo funcional | `security/l10n_pe_ne_security.xml:33-37` | `group_l10n_pe_ne_anulacion`, `implied_ids = [group_l10n_pe_ne_emisor]` |
| Helper de permiso | `controllers/main.py:128-133` | `_puede_anular(uid)` → `has_group('...group_l10n_pe_ne_anulacion')` |
| Gate HTTP | `controllers/main.py:717-727` | `/ne/api/anular` → **403** "No tienes permiso para anular comprobantes." |
| Contrato al cliente | `controllers/main.py:330` y `:353` | `login` **y** `whoami` devuelven `"puedeAnular": self._puede_anular(uid)` |
| Migración | `migrations/19.0.1.4.0/post-anulacion-grupo.py` | mete a **todos** los emisores previos (`all_user_ids`) en el grupo nuevo |
| Bootstrap | `hooks.py` | `post_init_hook` mete a `base.user_admin` en el grupo (install-only) |
| Tests | `tests/test_admin_group.py:17-44` | verifica admin con ambos grupos y "cajero" con emisor y **sin** anulación |

**Esto no es un hueco: es la plantilla.** El diseño que sigue **no inventa un mecanismo nuevo** — replica este exacto, ocho veces más. Y el comentario de esa migración es la doctrina de migración de todo este documento, literal:

> *"el upgrade no debe quitarle a nadie una capacidad que ya tenía en silencio. Restringir quién anula es una decisión del admin, no un efecto colateral del upgrade."*

Lo que sí falta, y es real (verificado):

1. **El modelo no es la autoridad.** `l10n_pe_ne_quick_anular` (`account_move_biller.py:2189-2199`) **no tiene `has_group`**. El gate vive solo en el controller. `res_company.py:198,266` y `res_users.py:39,79` sí re-chequean en el modelo — el precedente correcto existe, pero la anulación no lo sigue. Grep de `has_group` en todo el addon: **17 hits**, y en `models/` **solo** hay `base.group_system`.
2. **Los otros 88 endpoints** (de 90) no comprueban ningún grupo funcional.
3. **`l10n_pe_ne_list_tenants` (`res_company.py:270`) tiene un bug latente que este diseño hace estallar:** busca `('group_ids','in',grp.id)` y en Odoo 19 `res.users.group_ids` es **solo los grupos explícitos** (`res_users.py:257`, *"Groups explicitly assigned"*); la implicación vive en `all_group_ids` (`:258-259`, computed = `group_ids.all_implied_ids`). Hoy ya falla: un usuario al que le asignes **solo** `group_l10n_pe_ne_anulacion` es invisible en la pantalla de Emisores. Con roles nuevos, la mitad del personal desaparece del panel. **Se arregla en el mismo PR** (`('all_group_ids','in',grp.id)`).

---

## 1. Los dos hechos de Odoo 19 que fijan el diseño (verificados, no citados de oídas)

### 1.1 `account.move` NO se puede segregar por visibilidad. Nunca. Punto.

```
ir_rule.py:160-173   →  reglas CON groups se combinan con OR entre sí,
                        y el OR resultante se AND-ea con las globales.
account_security.xml:232-237  →  account_move_see_all  domain [(1,'=',1)]  groups=group_account_invoice
account_security.xml:263-271  →  account_move_rule_group_readonly [(1,'=',1)] groups=group_account_readonly (solo read)
account_security.xml:55-71    →  group_account_user → {group_account_basic → group_account_invoice, group_account_readonly}
security/l10n_pe_ne_security.xml:20  →  group_l10n_pe_ne_emisor implied_ids = [account.group_account_user]
```

Todo emisor carga **dos** reglas `TRUE` sobre `account.move`. Cualquier `ir.rule` restrictiva que yo añada se OR-ea contra `TRUE` y **muere**.

**Decisión de diseño (explícita y asumida): no peleo con esto.**

- **La visibilidad de comprobantes es del RUC, no del rol.** Todos los roles operativos ven todos los comprobantes de su empresa. El aislamiento real lo da `account_move_comp_rule` (`account_security.xml:128-132`), que es **GLOBAL** (`[('company_id','in',company_ids)]`, sin `groups`) → se AND-ea siempre, pase lo que pase. Ese es el aislamiento que importa y **no se toca**.
- **La segregación sobre comprobantes es por ACCIÓN, no por vista:** `has_group` en Python del addon. Que el despachador vea la boleta de ayer no es un riesgo de negocio; que la anule sí.
- **Las alternativas son peores:** (a) sobrescribir el dominio de `account.account_move_see_all` desde el addon rompe el resto de `account`, que asume esa regla; (b) no implicar `group_account_invoice` obliga a redeclarar decenas de ACL nativas y a pelear con los `has_group` internos del módulo `account`. Coste desproporcionado para una PyME de 3 personas.
- **Los modelos PROPIOS sí se segregan con `ir.rule`** (cotización, caja, pedido, gasto): ahí no hay ninguna regla `TRUE` nativa que anule nada. **Ahí es donde vive la segregación por vista.**

> ⚠️ **Trampa de `ir.rule` que hay que tener presente todo el rato:** si un usuario **no tiene ninguna regla de grupo** sobre un modelo, `group_domains` queda vacío y **solo aplican las globales** → **ve TODO**. La ausencia de regla es *permitir*, no *negar*. Por eso abajo cada rol aparece **explícitamente** en una regla, aunque sea la de "ve todas": la intención tiene que estar en el código, no ser un accidente.

### 1.2 Implicación transitiva = la palanca de migración

`all_implied_ids` es recursivo (`res_groups.py:71-72`) y es lo que consumen tanto `ir.rule` (`ir_rule.py:158`, `env.user.all_group_ids`) como los ACL (`res_users.py:1149`, `all_group_ids.model_access`). Los ACL son **UNIÓN: el más permisivo gana.**

De ahí sale el truco central de la migración: **intercalo `group_l10n_pe_ne_base` entre `emisor` y `account.group_account_user`.** La cadena transitiva queda idéntica → **ningún usuario provisionado pierde nada**.

Dos notas menores confirmadas:
- `api_key_duration`: `_check_expiration_date` (`res_users.py:1577-1587`) **se salta con sudo** (`env.is_system()`), y `/ne/api/login` mintea con `.with_user(uid).sudo()` (`main.py:315-320`). Los grupos nuevos **no** necesitan `api_key_duration`; el TTL real son 12h (`_TTL_HOURS_DEFAULT`).
- Odoo 19 usa `res.groups.privilege` (`privilege_id`), **no** `category_id`, con constraint `UNIQUE(privilege_id, name)` (`res_groups.py:36,39`). La exclusividad (`disjoint_ids`) solo aplica a grupos de tipo de usuario (`res_groups.py:289-295`) → **los roles funcionales se acumulan libremente**, que es exactamente lo que el punto 6 necesita.

---

## 2. El conjunto mínimo de roles

**Principio:** un grupo = **una capacidad**, no un cargo. El cargo ("Cajero de la librería") es un **conjunto** de grupos, porque en una PyME una persona lleva varios sombreros y los sombreros cambian por vertical (librería ≠ taller ≠ farmacia). Nombro cada grupo por el rol dominante para que sea legible, pero **el test de admisión de cada uno es un handoff real de la lista de procesos válidos**: existe si y solo si hay un "quien hace X no debe poder hacer Y".

```
                       base.group_user
                              ↑
                  account.group_account_user
                              ↑
                   group_l10n_pe_ne_base        ← técnico, NO asignable solo
                              ↑
   ┌──────────┬──────────┬────┴─────┬───────────┬────────────┬──────────────┐
 ventas     emisor      caja      despacho    taller      supervisor
              ↑
          anulacion  (implica emisor — YA ES ASÍ, se conserva intacto)

 contador ──→ account.group_account_readonly ──→ base.group_user   (FUERA de la escalera)
 base.group_system  (nativo, admin de plataforma — sin cambios)
```

| # | XML id | Nombre (ES) | Existe hoy | La segregación que habilita (por qué existe) |
|---|---|---|---|---|
| 0 | `group_l10n_pe_ne_base` | **Operador NE Express** | ✖ nuevo | Técnico. Piso común: leer catálogo, clientes, config, comprobantes. Absorbe el `implied_ids = account.group_account_user` que hoy cuelga de `emisor`. **No se asigna nunca directo.** |
| 1 | `group_l10n_pe_ne_ventas` | **Vendedor / Cotizador** | ✖ nuevo | **Caso 1, primer eslabón.** Cotiza y no cobra: quien pone el precio no recibe el dinero. Sin esto no hay handoff cotización→caja. |
| 2 | `group_l10n_pe_ne_emisor` | **Emisor NE Express** | ✔ **existe** | Firma y envía a SUNAT. Se conserva **con su nombre y su semántica**: es lo que hace que la SPA y los tenants actuales no se enteren del cambio. |
| 3 | `group_l10n_pe_ne_caja` | **Cajero** | ✖ nuevo | **Caso 1, segundo eslabón.** Custodia del cajón: abre, cobra, mueve, cuenta y cierra. Ortogonal a `emisor` a propósito: el back-office que factura al crédito no toca el efectivo, y el cajero del mostrador acumula ambos (§6). |
| 4 | `group_l10n_pe_ne_despacho` | **Despachador / Almacén** | ✖ nuevo | **Caso 1, tercer eslabón.** Custodia de mercadería física (entrada y salida): entrega solo contra pago, recepciona del proveedor, confirma destino de devoluciones. Que el que cobra sea el que entrega anula el control. |
| 5 | `group_l10n_pe_ne_taller` | **Operario / Taller** | ✖ nuevo | **Caso 2.** Atiende la cola y avanza el trabajo. **No entrega y no cobra**: si el técnico pudiera marcar "entregado", saldría mercadería sin el saldo pagado. Ese es el control, y por eso no se fusiona con `despacho`. |
| 6 | `group_l10n_pe_ne_anulacion` | **Anulación de comprobantes** | ✔ **existe** | Revierte dinero e impacta SUNAT de forma irreversible. **No se toca** (implica `emisor`, tiene migración, tests y gate). |
| 7 | `group_l10n_pe_ne_supervisor` | **Supervisor / Dueño** | ✖ nuevo | **Aprueba lo que otro hizo**: descuadre de caja, descuento fuera de política, egreso sobre tope, línea de crédito, devolución. El invariante `aprobador_id ≠ solicitante` es lo que le da sentido; sin un grupo distinto no hay a quién escalar. También: borra maestros y cierra periodo. |
| 8 | `group_l10n_pe_ne_contador` | **Contador externo** | ✖ nuevo | **Actor real que hoy usa la credencial del dueño.** Lee y descarga libros; no emite, no anula, no borra. Único rol donde el read-only importa de verdad, porque es un tercero fuera de la empresa. |
| — | `base.group_system` | **Admin de plataforma** | ✔ nativo | Provisión de tenants. Sin cambios. |

**Roles que deliberadamente NO creo** (y por qué, para que nadie los reponga sin pelear):

- ~~Cobrador de calle~~ → es `ventas` + un campo `cobrador_id`. La segregación que importa (cobra ≠ reconcilia) ya la da `caja`. Añadir el grupo cuando exista cartera real, no antes.
- ~~Comprador~~ → es `despacho` (recepción) + `supervisor` (aprueba). Un tercer grupo para "teclear la factura del proveedor" no separa nada que `emisor` no separe ya.
- ~~Supervisor de caja / de descuento / de reparto~~ → **un solo `supervisor`**. En una tienda peruana el que aprueba es el dueño, siempre. Si un cliente grande necesita partirlo, se añade un hermano nuevo **sin tocar nada de lo demás** — ese es el punto del diseño.
- ~~Rol "Dueño del RUC" que da de alta usuarios~~ → **no es un grupo, es imposible como grupo.** `base/security/base_security.xml` define `res_users_rule` como **global** con `['|',('share','=',False),('company_ids','in',company_ids)]`: todo usuario interno ve a todos los internos, sin filtro de compañía. Y quien puede escribir `group_ids` en `res.users` se auto-otorga `base.group_system` en un request. Por eso el alta/asignación de roles es **un método `sudo()` del addon con whitelist + scope de compañía** (§3.4), no un permiso.

### 2.1 El XML

```xml
<?xml version="1.0" encoding="utf-8"?>
<odoo>
  <data>
    <!-- Odoo 19: los grupos se agrupan por res.groups.privilege (privilege_id),
         NO por category_id. Constraint UNIQUE(privilege_id, name). -->
    <record id="privilege_ne_express" model="res.groups.privilege">
      <field name="name">NE Express</field>
      <field name="sequence">10</field>
      <field name="placeholder">Sin acceso</field>
    </record>

    <!--
      BASE. Absorbe el implied_ids que hoy cuelga de group_l10n_pe_ne_emisor.
      NO se asigna directo: es el piso común de los roles operativos.

      Por qué sigue implicando account.group_account_user y no algo más fino:
      todos los roles necesitan leer account.move/journal/partner/product, y el
      CRUD de account.move está soldado a account.group_account_invoice
      (account/security/ir.model.access.csv:38). Desconectarlo obliga a
      redeclarar media contabilidad. Consecuencia asumida y documentada: la
      visibilidad de account.move NO se segrega por rol (ver §1.1); el gate de
      comprobantes es has_group en Python del addon.
    -->
    <record id="group_l10n_pe_ne_base" model="res.groups">
      <field name="name">Operador NE Express (base)</field>
      <field name="privilege_id" ref="privilege_ne_express"/>
      <field name="implied_ids" eval="[(4, ref('account.group_account_user'))]"/>
      <field name="api_key_duration">365</field>
      <field name="comment">Piso común de los roles NE Express: lectura de catálogo,
        clientes, configuración y comprobantes de su RUC. No se asigna directamente;
        lo implican todos los roles operativos.</field>
    </record>

    <!-- EMISOR: se re-fundamenta sobre base. (6,0,[...]) REEMPLAZA el implied.
         La cadena transitiva queda idéntica (base → account.group_account_user):
         ningún emisor provisionado pierde una sola capacidad. -->
    <record id="group_l10n_pe_ne_emisor" model="res.groups">
      <field name="name">Emisor NE Express</field>
      <field name="privilege_id" ref="privilege_ne_express"/>
      <field name="implied_ids" eval="[(6, 0, [ref('group_l10n_pe_ne_base')])]"/>
      <field name="api_key_duration">365</field>
      <field name="comment">Emite comprobantes electrónicos y guías de remisión a SUNAT.
        Para anular hace falta además "Anulación de comprobantes".</field>
    </record>

    <record id="group_l10n_pe_ne_ventas" model="res.groups">
      <field name="name">Vendedor / Cotizador NE Express</field>
      <field name="privilege_id" ref="privilege_ne_express"/>
      <field name="implied_ids" eval="[(4, ref('group_l10n_pe_ne_base'))]"/>
      <field name="comment">Cotiza y gestiona clientes. NO emite ni cobra: el handoff
        cotización → caja es el control (quien fija el precio no recibe el dinero).</field>
    </record>

    <record id="group_l10n_pe_ne_caja" model="res.groups">
      <field name="name">Cajero NE Express</field>
      <field name="privilege_id" ref="privilege_ne_express"/>
      <field name="implied_ids" eval="[(4, ref('group_l10n_pe_ne_base'))]"/>
      <field name="comment">Abre/cierra caja, registra ingresos y retiros y declara el
        conteo. Ortogonal a Emisor: para cobrar en mostrador se asignan los dos.</field>
    </record>

    <record id="group_l10n_pe_ne_despacho" model="res.groups">
      <field name="name">Despachador / Almacén NE Express</field>
      <field name="privilege_id" ref="privilege_ne_express"/>
      <field name="implied_ids" eval="[(4, ref('group_l10n_pe_ne_base'))]"/>
      <field name="comment">Custodia física: entrega al cliente (solo contra pago),
        recepciona del proveedor y confirma destino de devoluciones.</field>
    </record>

    <record id="group_l10n_pe_ne_taller" model="res.groups">
      <field name="name">Operario / Taller NE Express</field>
      <field name="privilege_id" ref="privilege_ne_express"/>
      <field name="implied_ids" eval="[(4, ref('group_l10n_pe_ne_base'))]"/>
      <field name="comment">Toma órdenes de la cola y avanza el trabajo. NO entrega
        ni cobra: liberar mercadería sin saldo pagado es justo lo que se evita.</field>
    </record>

    <record id="group_l10n_pe_ne_supervisor" model="res.groups">
      <field name="name">Supervisor / Dueño NE Express</field>
      <field name="privilege_id" ref="privilege_ne_express"/>
      <field name="implied_ids" eval="[(4, ref('group_l10n_pe_ne_base'))]"/>
      <field name="comment">Aprueba lo que otro hizo: descuadre de caja, descuento fuera
        de política, egreso sobre tope, crédito, devolución. Ve todo su RUC. NO implica
        los roles operativos: un supervisor puro aprueba pero no vende.</field>
    </record>

    <!-- ANULACIÓN: intacto. Ya implica emisor y ya tiene migración, gate y tests. -->
    <record id="group_l10n_pe_ne_anulacion" model="res.groups">
      <field name="name">Anulación de comprobantes NE Express</field>
      <field name="privilege_id" ref="privilege_ne_express"/>
      <field name="implied_ids" eval="[(4, ref('group_l10n_pe_ne_emisor'))]"/>
    </record>

    <!--
      CONTADOR: FUERA de la escalera. Único rol que NO implica group_l10n_pe_ne_base.
      Implica account.group_account_readonly, que verifiqué que concede SOLO ACLs de
      lectura (29 filas en account/security/ir.model.access.csv, todas 1,0,0,0,
      incluida access_account_move_readonly). Así NO recibe group_account_invoice y
      por tanto NO tiene ACL de escritura sobre account.move: aunque encontrara un
      endpoint, el ORM lo para. Ve los moves de su RUC por account_move_comp_rule,
      que es GLOBAL.
    -->
    <record id="group_l10n_pe_ne_contador" model="res.groups">
      <field name="name">Contador externo NE Express</field>
      <field name="privilege_id" ref="privilege_ne_express"/>
      <field name="implied_ids" eval="[(4, ref('account.group_account_readonly'))]"/>
      <field name="comment">Solo lectura + libros electrónicos (PLE/RVIE) y cierre de
        periodo. No emite, no anula, no borra. Existe para que el contador deje de usar
        la credencial del dueño.</field>
    </record>
  </data>
</odoo>
```

---

## 3. Ficha por rol: qué VE, qué PUEDE, qué NO debe poder NUNCA

Las 9 `ir.rule` globales de compañía que ya existen (`security/l10n_pe_ne_security.xml:30-125`, gasto/cotización+línea/caja+movimiento/lote+fila/guía+línea) **se conservan sin tocar**: son `global=True` → se AND-ean con todo lo de abajo. **El aislamiento por RUC sobrevive a cualquier error de este diseño.** Eso es deliberado.

### 3.1 Vendedor / Cotizador — `group_l10n_pe_ne_ventas`

| | |
|---|---|
| **VE** | `l10n_pe_ne.cotizacion`: **las suyas** (`user_id = user.id` — campo nuevo, ver §3.7) + las huérfanas. `l10n_pe_ne.pedido`: los suyos. Clientes, productos, comprobantes: todo el RUC (impuesto por §1.1; además necesita ver si su cotización se facturó). |
| **PUEDE** | `GET/POST/PUT/DELETE /ne/api/cotizaciones` (borrador propio), `POST .../enviar|aceptar|rechazar` ★, `GET .../detalle|pdf`, `POST /ne/api/clientes` + `lookup` + `datos`, `GET /ne/api/productos`, `GET /ne/api/comprobantes`. |
| **NUNCA** | Emitir (`/ne/api/emitir` → 403). Cobrar ni tocar la caja. Anular. Entregar. Aprobar su propio descuento. **Editar una cotización `aprobada`/`convertida`** (congelada en el modelo: el precio que el cajero cobra no se reescribe después). Borrar una cotización que no sea `borrador` suya. Cambiar precio de lista del catálogo. |

### 3.2 Emisor — `group_l10n_pe_ne_emisor` (existe)

| | |
|---|---|
| **VE** | Todo el RUC en comprobantes (§1.1). Cotizaciones: todas (necesita convertirlas). |
| **PUEDE** | `POST /ne/api/emitir` (01/03/07/08/20/40), `/ne/api/guias/*`, `/ne/api/lotes/*`, `POST /ne/api/comprobantes/<id>/reenviar|email`, todas las descargas, `POST /ne/api/cotizaciones/<id>/convertir` ★. Crear cliente/producto al vuelo durante la emisión. |
| **NUNCA** | Anular (ya es así hoy: 403 en `main.py:722-727`). Aprobar descuentos ni descuadres. Cerrar periodo. Borrar clientes/productos. Emitir con `formaPago=Credito` sin aprobación vigente ★. **Emitir con fecha de periodo cerrado** (lo para `_check_fiscal_lock_dates` nativo al postear). |

### 3.3 Cajero — `group_l10n_pe_ne_caja`

| | |
|---|---|
| **VE** | `l10n_pe_ne.caja.sesion`: **solo las suyas** (`usuario_apertura_id` / `usuario_cierre_id`). Cotizaciones: todas (el cliente llega con la suya). Comprobantes: todo el RUC. |
| **PUEDE** | `GET /ne/api/caja`, `POST /ne/api/caja/abrir|movimientos|cerrar`, `GET /ne/api/caja/historial` (filtrado por la regla), `GET /ne/api/caja/<id>/arqueo`, `POST /ne/api/cotizaciones/<id>/cobrar` ★, `POST /ne/api/comprobantes/<id>/abono` ★. |
| **NUNCA** | **Aprobar su propio descuadre** (invariante duro: `usuario_revision_id ∉ {cajero_id, usuario_cierre_id}`). Editar precios de una cotización aprobada. Anular. Entregar mercadería. Ver las sesiones de otro cajero. Retirar por encima del efectivo disponible (ya lo impide `l10n_pe_ne_caja.py:236-247`). |

### 3.4 Despachador / Almacén — `group_l10n_pe_ne_despacho`

| | |
|---|---|
| **VE** | `l10n_pe_ne.pedido`: **la cola entera** de su RUC (a diferencia del vendedor). Existencias, lotes, vencimientos. |
| **PUEDE** | `GET /ne/api/cola` ★, `POST /ne/api/pedidos/<id>/entregar` ★ (**el modelo verifica `payment_state='paid'` — no es un permiso, es una guarda**), `POST /ne/api/recepciones/*` ★, `GET /ne/api/inventario/vencimientos` ★, `POST /ne/api/ajustes` ★ (propone, no aplica sin aprobar), `PUT /ne/api/productos/<id>` limitado a `list_price` (field-level, §3.9). |
| **NUNCA** | Emitir. Cobrar. **Aplicar un ajuste de inventario sin autorización** (bajar mercadería a merma es sacar plata). Entregar un pedido con saldo pendiente. Anular. |

### 3.5 Operario / Taller — `group_l10n_pe_ne_taller`

| | |
|---|---|
| **VE** | `l10n_pe_ne.pedido` con `tipo='orden_trabajo'`: la cola **completa** (sin dueño = disponible) + las asignadas a él. Nada de dinero: **no ve `l10n_pe_ne.caja.*`** (sin fila ACL → el ORM lo para antes que cualquier regla). |
| **PUEDE** | `GET /ne/api/cola?tipo=orden_trabajo` ★, `POST /ne/api/pedidos/<id>/tomar|avanzar|terminar` ★. |
| **NUNCA** | **Entregar** (ese es el control del Caso 2: el técnico termina, el mostrador entrega contra saldo). Cobrar. Emitir. Ver ni tocar caja. Cambiar precios. |

### 3.6 Supervisor / Dueño — `group_l10n_pe_ne_supervisor`

| | |
|---|---|
| **VE** | **Todo** su RUC: todas las cotizaciones, todas las sesiones de caja, toda la cola, todos los gastos. (Regla `[(1,'=',1)]` con `groups=supervisor` → se OR-ea hacia arriba.) |
| **PUEDE** | `POST /ne/api/caja/<id>/aprobar-cierre` ★, `POST /ne/api/cotizaciones/<id>/aprobar|rechazar-aprobacion` ★, `POST /ne/api/gastos/<id>/aprobar|rechazar` ★, `POST /ne/api/ajustes/<id>/aprobar` ★, `POST /ne/api/devoluciones/<id>/autorizar` ★, `POST /ne/api/credito/<id>/aprobar` ★, `DELETE /ne/api/clientes|productos/<id>`, `PUT /ne/api/negocio`, `PUT /ne/api/admin/users/<id>/roles` ★ (**acotado a su RUC**). |
| **NUNCA** | **Aprobar algo que él mismo solicitó** cuando exista otro usuario con el grupo (`@api.constrains`, no un `if` en el controller). Otorgarse `base.group_system` ni tocar usuarios de otro RUC (por eso el método es `sudo()` con whitelist, no un ACL). Saltarse las guardas fiscales: `_l10n_pe_check_baja` **no es un permiso** y no se aprueba. |

### 3.7 Contador externo — `group_l10n_pe_ne_contador`

| | |
|---|---|
| **VE** | Comprobantes y compras de su RUC (por `account_move_comp_rule`, global). Cajas y gastos: todos, en lectura. |
| **PUEDE** | `GET /ne/api/reportes/ple-ventas|ple-compras|ple-inventario|rvie-reemplazo|export|dashboard|ventas`, `GET /ne/api/comprobantes` + descargas, `GET /ne/api/compras`, `POST /ne/api/periodos/<YYYYMM>/cerrar` ★. |
| **NUNCA** | Emitir, anular, cobrar, crear/borrar nada. **Ni siquiera con un endpoint mal escrito**: no tiene `group_account_invoice` → **no tiene ACL de escritura sobre `account.move`**; el ORM lo detiene aunque el controller falle. Es el único rol con defensa a nivel de ORM, y es a propósito: es un tercero. |

### 3.8 Las `ir.rule` (solo modelos propios — ahí sí funcionan)

```xml
<!-- COTIZACIÓN: escalera. El vendedor ve lo suyo; el resto ve todo.
     Se OR-ean entre sí y se AND-ean con rule_l10n_pe_ne_cotizacion_company
     (global, ya existente) → el aislamiento por RUC nunca se pierde. -->
<record id="rule_ne_cotizacion_propias" model="ir.rule">
  <field name="name">Cotización: el vendedor ve las suyas</field>
  <field name="model_id" ref="model_l10n_pe_ne_cotizacion"/>
  <field name="domain_force">['|', ('user_id', '=', user.id), ('user_id', '=', False)]</field>
  <field name="groups" eval="[(4, ref('group_l10n_pe_ne_ventas'))]"/>
</record>

<!-- Caja/despacho/taller/supervisor/contador ven TODAS: el cliente llega a caja con
     la cotización de otro vendedor. Se declara explícito y no se deja "caer" por
     ausencia de regla (ausencia = ve todo, y una intención implícita no se revisa). -->
<record id="rule_ne_cotizacion_todas" model="ir.rule">
  <field name="name">Cotización: caja/despacho/taller/supervisor/contador ven todas</field>
  <field name="model_id" ref="model_l10n_pe_ne_cotizacion"/>
  <field name="domain_force">[(1, '=', 1)]</field>
  <field name="groups" eval="[(4, ref('group_l10n_pe_ne_caja')),
                              (4, ref('group_l10n_pe_ne_despacho')),
                              (4, ref('group_l10n_pe_ne_taller')),
                              (4, ref('group_l10n_pe_ne_supervisor')),
                              (4, ref('group_l10n_pe_ne_contador')),
                              (4, ref('group_l10n_pe_ne_emisor'))]"/>
</record>

<!-- CAJA: el cajero ve SOLO sus sesiones. Aquí la segregación por vista sí es real
     y sí importa (el arqueo de otro no es asunto suyo). -->
<record id="rule_ne_caja_sesion_propias" model="ir.rule">
  <field name="name">Caja: el cajero ve sus sesiones</field>
  <field name="model_id" ref="model_l10n_pe_ne_caja_sesion"/>
  <field name="domain_force">['|', ('usuario_apertura_id', '=', user.id),
                                   ('usuario_cierre_id', '=', user.id)]</field>
  <field name="groups" eval="[(4, ref('group_l10n_pe_ne_caja'))]"/>
</record>
<record id="rule_ne_caja_sesion_todas" model="ir.rule">
  <field name="name">Caja: supervisor y contador ven todas</field>
  <field name="model_id" ref="model_l10n_pe_ne_caja_sesion"/>
  <field name="domain_force">[(1, '=', 1)]</field>
  <field name="groups" eval="[(4, ref('group_l10n_pe_ne_supervisor')),
                              (4, ref('group_l10n_pe_ne_contador'))]"/>
</record>

<!-- COLA (modelo nuevo l10n_pe_ne.pedido): el taller ve la cola sin dueño + la suya.
     Esto es exactamente lo que mail.activity NO puede dar: su ir.rule nativa
     (mail_security.xml:240-244) limita a ('user_id','=',user.id) → un técnico no
     vería la cola de sus compañeros. Por eso el modelo es propio. -->
<record id="rule_ne_pedido_cola_taller" model="ir.rule">
  <field name="name">Pedido: el taller ve la cola y lo asignado a él</field>
  <field name="model_id" ref="model_l10n_pe_ne_pedido"/>
  <field name="domain_force">[('tipo', '=', 'orden_trabajo'),
                              '|', ('user_id', '=', False), ('user_id', '=', user.id)]</field>
  <field name="groups" eval="[(4, ref('group_l10n_pe_ne_taller'))]"/>
</record>
<record id="rule_ne_pedido_todos" model="ir.rule">
  <field name="name">Pedido: despacho/caja/supervisor ven toda la cola</field>
  <field name="model_id" ref="model_l10n_pe_ne_pedido"/>
  <field name="domain_force">[(1, '=', 1)]</field>
  <field name="groups" eval="[(4, ref('group_l10n_pe_ne_despacho')),
                              (4, ref('group_l10n_pe_ne_caja')),
                              (4, ref('group_l10n_pe_ne_supervisor'))]"/>
</record>

<!-- GASTO: el solicitante ve los suyos, el supervisor todos. -->
<record id="rule_ne_gasto_propios" model="ir.rule">
  <field name="name">Gasto: cada uno ve los suyos</field>
  <field name="model_id" ref="model_l10n_pe_ne_gasto"/>
  <field name="domain_force">[('solicitante_id', '=', user.id)]</field>
  <field name="groups" eval="[(4, ref('group_l10n_pe_ne_caja')),
                              (4, ref('group_l10n_pe_ne_ventas')),
                              (4, ref('group_l10n_pe_ne_despacho'))]"/>
</record>
<record id="rule_ne_gasto_todos" model="ir.rule">
  <field name="name">Gasto: supervisor/contador ven todos</field>
  <field name="model_id" ref="model_l10n_pe_ne_gasto"/>
  <field name="domain_force">[(1, '=', 1)]</field>
  <field name="groups" eval="[(4, ref('group_l10n_pe_ne_supervisor')),
                              (4, ref('group_l10n_pe_ne_contador')),
                              (4, ref('group_l10n_pe_ne_emisor'))]"/>
</record>
```

> **Precondición de campo, sin la cual las reglas de arriba son papel:** hoy `l10n_pe_ne.cotizacion` **no tiene `user_id`** (verificado, `l10n_pe_ne_cotizacion.py:23-51`) y `l10n_pe_ne.gasto` **no tiene `solicitante_id`** (`:14-26`). Hay que añadirlos (`default=lambda s: s.env.user`, **no** `create_uid`, que es auditoría y no dato de negocio) **antes** de cargar estas reglas. Un `-u` con reglas que referencian campos inexistentes revienta la instalación.

### 3.9 El `ir.model.access.csv`

Un solo archivo; lo secciono para leerlo. **Regla mental permanente: los ACL son UNIÓN sobre `all_group_ids` — el más permisivo gana.** Por eso `base` solo lleva lecturas y cada escritura cuelga del rol que la necesita.

**Base — lectura común (todos los roles operativos la heredan):**
```csv
id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink
access_ne_base_partner,ne.base.res.partner,base.model_res_partner,group_l10n_pe_ne_base,1,0,0,0
access_ne_base_product_template,ne.base.product.template,product.model_product_template,group_l10n_pe_ne_base,1,0,0,0
access_ne_base_product_product,ne.base.product.product,product.model_product_product,group_l10n_pe_ne_base,1,0,0,0
access_ne_base_uom,ne.base.uom.uom,uom.model_uom_uom,group_l10n_pe_ne_base,1,0,0,0
access_ne_base_cotizacion,ne.base.cotizacion,model_l10n_pe_ne_cotizacion,group_l10n_pe_ne_base,1,0,0,0
access_ne_base_cotizacion_line,ne.base.cotizacion.line,model_l10n_pe_ne_cotizacion_line,group_l10n_pe_ne_base,1,0,0,0
access_ne_base_pedido,ne.base.pedido,model_l10n_pe_ne_pedido,group_l10n_pe_ne_base,1,0,0,0
access_ne_base_pedido_line,ne.base.pedido.line,model_l10n_pe_ne_pedido_line,group_l10n_pe_ne_base,1,0,0,0
access_ne_base_stock_lot,ne.base.stock.lot,stock.model_stock_lot,group_l10n_pe_ne_base,1,0,0,0
access_ne_base_stock_move_line,ne.base.stock.move.line,stock.model_stock_move_line,group_l10n_pe_ne_base,1,0,0,0
```
> Nótese lo que **no** está: `l10n_pe_ne.caja.sesion` y `l10n_pe_ne.caja.movimiento` **no cuelgan de base**. El taller y el vendedor no ven caja **a nivel de ORM**. Es la única segregación dura que este diseño consigue barata, y hay que aprovecharla.

**Vendedor / Cotizador:**
```csv
access_ne_ventas_cotizacion,ne.ventas.cotizacion,model_l10n_pe_ne_cotizacion,group_l10n_pe_ne_ventas,1,1,1,1
access_ne_ventas_cotizacion_line,ne.ventas.cotizacion.line,model_l10n_pe_ne_cotizacion_line,group_l10n_pe_ne_ventas,1,1,1,1
access_ne_ventas_partner,ne.ventas.res.partner,base.model_res_partner,group_l10n_pe_ne_ventas,1,1,1,0
access_ne_ventas_pedido,ne.ventas.pedido,model_l10n_pe_ne_pedido,group_l10n_pe_ne_ventas,1,1,1,0
access_ne_ventas_pedido_line,ne.ventas.pedido.line,model_l10n_pe_ne_pedido_line,group_l10n_pe_ne_ventas,1,1,1,0
access_ne_ventas_gasto,ne.ventas.gasto,model_l10n_pe_ne_gasto,group_l10n_pe_ne_ventas,1,1,1,0
```
> `perm_unlink=1` en cotización es ACL, y el ACL no sabe de estados. **El borrado de una `convertida` lo tiene que parar un `unlink()` override en el modelo** (hoy `l10n_pe_ne_delete_cotizacion` (`:278`) borra sin guarda alguna — COT-035). El ACL abre la puerta; el modelo decide.

**Emisor** (conserva lo de hoy: crear cliente/producto al vuelo al emitir):
```csv
access_ne_emisor_partner,ne.emisor.res.partner,base.model_res_partner,group_l10n_pe_ne_emisor,1,1,1,0
access_ne_emisor_product_template,ne.emisor.product.template,product.model_product_template,group_l10n_pe_ne_emisor,1,1,1,0
access_ne_emisor_product_product,ne.emisor.product.product,product.model_product_product,group_l10n_pe_ne_emisor,1,1,1,0
access_ne_emisor_uom,ne.emisor.uom.uom,uom.model_uom_uom,group_l10n_pe_ne_emisor,1,1,1,0
access_ne_emisor_cotizacion,ne.emisor.cotizacion,model_l10n_pe_ne_cotizacion,group_l10n_pe_ne_emisor,1,1,0,0
access_ne_emisor_cotizacion_line,ne.emisor.cotizacion.line,model_l10n_pe_ne_cotizacion_line,group_l10n_pe_ne_emisor,1,1,0,0
access_ne_emisor_guia_remision,ne.emisor.guia_remision,model_l10n_pe_ne_guia_remision,group_l10n_pe_ne_emisor,1,1,1,1
access_ne_emisor_guia_remision_line,ne.emisor.guia_remision.line,model_l10n_pe_ne_guia_remision_line,group_l10n_pe_ne_emisor,1,1,1,1
access_ne_emisor_lote,ne.emisor.lote,model_l10n_pe_ne_lote,group_l10n_pe_ne_emisor,1,1,1,1
access_ne_emisor_lote_fila,ne.emisor.lote.fila,model_l10n_pe_ne_lote_fila,group_l10n_pe_ne_emisor,1,1,1,1
access_ne_emisor_gasto,ne.emisor.gasto,model_l10n_pe_ne_gasto,group_l10n_pe_ne_emisor,1,1,1,0
access_ne_emisor_stock_lot,ne.emisor.stock.lot,stock.model_stock_lot,group_l10n_pe_ne_emisor,1,1,1,0
```

**Cajero, Despacho, Taller, Supervisor, Contador:**
```csv
access_ne_caja_sesion,ne.caja.sesion,model_l10n_pe_ne_caja_sesion,group_l10n_pe_ne_caja,1,1,1,0
access_ne_caja_movimiento,ne.caja.movimiento,model_l10n_pe_ne_caja_movimiento,group_l10n_pe_ne_caja,1,1,1,0
access_ne_caja_gasto,ne.caja.gasto,model_l10n_pe_ne_gasto,group_l10n_pe_ne_caja,1,1,1,0
access_ne_caja_pedido,ne.caja.pedido,model_l10n_pe_ne_pedido,group_l10n_pe_ne_caja,1,1,0,0
access_ne_despacho_pedido,ne.despacho.pedido,model_l10n_pe_ne_pedido,group_l10n_pe_ne_despacho,1,1,1,0
access_ne_despacho_pedido_line,ne.despacho.pedido.line,model_l10n_pe_ne_pedido_line,group_l10n_pe_ne_despacho,1,1,1,0
access_ne_despacho_product_template,ne.despacho.product.template,product.model_product_template,group_l10n_pe_ne_despacho,1,1,0,0
access_ne_despacho_stock_lot,ne.despacho.stock.lot,stock.model_stock_lot,group_l10n_pe_ne_despacho,1,1,1,0
access_ne_despacho_ajuste,ne.despacho.ajuste,model_l10n_pe_ne_ajuste_inventario,group_l10n_pe_ne_despacho,1,1,1,0
access_ne_taller_pedido,ne.taller.pedido,model_l10n_pe_ne_pedido,group_l10n_pe_ne_taller,1,1,0,0
access_ne_taller_pedido_line,ne.taller.pedido.line,model_l10n_pe_ne_pedido_line,group_l10n_pe_ne_taller,1,1,0,0
access_ne_super_cotizacion,ne.super.cotizacion,model_l10n_pe_ne_cotizacion,group_l10n_pe_ne_supervisor,1,1,1,1
access_ne_super_cotizacion_line,ne.super.cotizacion.line,model_l10n_pe_ne_cotizacion_line,group_l10n_pe_ne_supervisor,1,1,1,1
access_ne_super_caja_sesion,ne.super.caja.sesion,model_l10n_pe_ne_caja_sesion,group_l10n_pe_ne_supervisor,1,1,0,0
access_ne_super_caja_movimiento,ne.super.caja.movimiento,model_l10n_pe_ne_caja_movimiento,group_l10n_pe_ne_supervisor,1,1,0,0
access_ne_super_gasto,ne.super.gasto,model_l10n_pe_ne_gasto,group_l10n_pe_ne_supervisor,1,1,1,1
access_ne_super_pedido,ne.super.pedido,model_l10n_pe_ne_pedido,group_l10n_pe_ne_supervisor,1,1,1,1
access_ne_super_partner,ne.super.res.partner,base.model_res_partner,group_l10n_pe_ne_supervisor,1,1,1,1
access_ne_super_product_template,ne.super.product.template,product.model_product_template,group_l10n_pe_ne_supervisor,1,1,1,1
access_ne_super_product_product,ne.super.product.product,product.model_product_product,group_l10n_pe_ne_supervisor,1,1,1,1
access_ne_super_ajuste,ne.super.ajuste,model_l10n_pe_ne_ajuste_inventario,group_l10n_pe_ne_supervisor,1,1,1,0
access_ne_contador_cotizacion,ne.contador.cotizacion,model_l10n_pe_ne_cotizacion,group_l10n_pe_ne_contador,1,0,0,0
access_ne_contador_caja_sesion,ne.contador.caja.sesion,model_l10n_pe_ne_caja_sesion,group_l10n_pe_ne_contador,1,0,0,0
access_ne_contador_caja_movimiento,ne.contador.caja.movimiento,model_l10n_pe_ne_caja_movimiento,group_l10n_pe_ne_contador,1,0,0,0
access_ne_contador_gasto,ne.contador.gasto,model_l10n_pe_ne_gasto,group_l10n_pe_ne_contador,1,0,0,0
access_ne_contador_stock_move_line,ne.contador.stock.move.line,stock.model_stock_move_line,group_l10n_pe_ne_contador,1,0,0,0
access_ne_contador_stock_lot,ne.contador.stock.lot,stock.model_stock_lot,group_l10n_pe_ne_contador,1,0,0,0
```

**Read-only por CAMPO (no se hace con ACL):** el precio de lista solo lo cambia el supervisor →
```python
# models/product_template.py
list_price = fields.Float(groups='l10n_pe_ne_biller.group_l10n_pe_ne_supervisor,'
                                 'l10n_pe_ne_biller.group_l10n_pe_ne_despacho')
```
> **Trampa:** eso rompe `_l10n_pe_ne_quick_product`, que crea productos al vuelo durante la emisión. El fix es que ese método escriba `list_price` con `.sudo()` (crear el producto **no** es cambiar el precio de lista), y que el gate de negocio esté en `l10n_pe_ne_update_producto`. Verificarlo con test antes de mergear.

---

## 4. Matriz rol × acción

`V`=Vendedor · `E`=Emisor · `C`=Cajero · `D`=Despacho · `T`=Taller · `A`=Anulación · `S`=Supervisor · `K`=Contador · `#`=`base.group_system`
★ = endpoint nuevo · **(g)** = además una **guarda de negocio** en el modelo que ningún grupo levanta (plazo SUNAT, saldo pagado, efectivo disponible…)

| Acción / endpoint | V | E | C | D | T | A | S | K | # |
|---|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|
| **Sesión** |
| `POST /login` · `/logout` · `GET /whoami` · `POST /change-password` | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ |
| `GET /config` `/distritos` `/tipo-cambio` `/negocio` `/negocio/logo` | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ |
| `POST /tipo-cambio` · `PUT /negocio` · `GET /series` | – | ✔ | – | – | – | ✔ | ✔ | – | ✔ |
| **Cotización (Caso 1, eslabón 1)** |
| `GET /cotizaciones` (lista) | 🔒propias | ✔ | ✔ | ✔ | – | ✔ | ✔ | ✔ | ✔ |
| `POST /cotizaciones` · `PUT /cotizaciones/<id>` | ✔ **(g)**<sup>1</sup> | – | – | – | – | – | ✔ | – | ✔ |
| `DELETE /cotizaciones/<id>` | ✔ **(g)**<sup>2</sup> | – | – | – | – | – | ✔ | – | ✔ |
| `GET /cotizaciones/<id>/detalle` · `/pdf` | 🔒 | ✔ | ✔ | ✔ | – | ✔ | ✔ | ✔ | ✔ |
| ~~`POST /cotizaciones/<id>/estado`~~ **← se elimina** | ✖ | ✖ | ✖ | ✖ | ✖ | ✖ | ✖ | ✖ | ✖ |
| `POST /cotizaciones/<id>/enviar\|aceptar\|rechazar` ★ | ✔ **(g)** | – | – | – | – | – | ✔ | – | ✔ |
| `POST /cotizaciones/<id>/solicitar-aprobacion` ★ | ✔ | – | – | – | – | – | ✔ | – | ✔ |
| `POST /cotizaciones/<id>/aprobar\|rechazar-aprobacion` ★ | – | – | – | – | – | – | ✔ **(g)**<sup>3</sup> | – | ✔ |
| `POST /cotizaciones/<id>/convertir` ★ | – | ✔ **(g)**<sup>4</sup> | – | – | – | ✔ | ✔ | – | ✔ |
| **Emisión** |
| `POST /emitir` (01/03/07/08) | – | ✔ **(g)**<sup>5</sup> | – | – | – | ✔ | – | – | ✔ |
| `POST /emitir` (20/40 retención/percepción) | – | ✔ | – | – | – | ✔ | – | – | ✔ |
| `GET /comprobantes` · `/<id>/detalle` · `/<id>/{pdf,ticket,xml,cdr}` | ✔ | ✔ | ✔ | ✔ | – | ✔ | ✔ | ✔ | ✔ |
| `POST /comprobantes/<id>/reenviar` · `/email` | – | ✔ | ✔ | – | – | ✔ | ✔ | – | ✔ |
| **`POST /anular`** · `GET /anulacion/<id>/cdr` | – | **✖ 403** | – | – | – | **✔ (g)**<sup>6</sup> | – | – | ✔ |
| `POST /devoluciones` ★ · `POST /devoluciones/<id>/autorizar` ★ | ✔ / – | ✔ / – | ✔ / – | ✔ / – | – | ✔ / ✔ | ✔ / ✔ | – | ✔ |
| **Caja (Caso 1, eslabón 2)** |
| `GET /caja` · `POST /caja/abrir` · `/movimientos` | – | – | ✔ **(g)**<sup>7</sup> | – | – | – | ✔ | – | ✔ |
| `POST /caja/cerrar` | – | – | ✔ | – | – | – | ✔ | – | ✔ |
| `GET /caja/historial` · `/caja/<id>/arqueo` | – | – | 🔒propias | – | – | – | ✔ todas | ✔ todas | ✔ |
| `POST /caja/<id>/aprobar-cierre` ★ | – | – | **✖** | – | – | – | ✔ **(g)**<sup>8</sup> | – | ✔ |
| `POST /cotizaciones/<id>/cobrar` ★ (**el handoff del Caso 1**) | – | – | ✔ **(g)**<sup>9</sup> | – | – | – | ✔ | – | ✔ |
| `POST /comprobantes/<id>/abono` ★ · `GET /cartera` ★ | – | – | ✔ | – | – | – | ✔ | ✔ ro | ✔ |
| **Cola / pedidos (Caso 1 eslabón 3 · Caso 2)** |
| `GET /cola` ★ | ✔ 🔒 | – | ✔ | ✔ todas | 🔒cola OT | – | ✔ todas | – | ✔ |
| `POST /pedidos` ★ | ✔ | ✔ | ✔ | ✔ | – | ✔ | ✔ | – | ✔ |
| `POST /pedidos/<id>/tomar\|avanzar\|terminar` ★ | – | – | – | ✔ | ✔ | – | ✔ | – | ✔ |
| `POST /pedidos/<id>/entregar` ★ | – | – | – | ✔ **(g)**<sup>10</sup> | **✖** | – | ✔ | – | ✔ |
| `POST /pedidos/<id>/vencer` ★ | – | – | – | – | – | – | ✔ | – | ✔ |
| **Almacén** |
| `GET /inventario/vencimientos` ★ · `/existencias` ★ | – | – | – | ✔ | – | – | ✔ | ✔ | ✔ |
| `POST /ajustes` ★ (proponer) | – | – | – | ✔ | – | – | ✔ | – | ✔ |
| `POST /ajustes/<id>/aprobar` ★ | – | – | – | **✖** | – | – | ✔ **(g)**<sup>3</sup> | – | ✔ |
| `POST /ajustes/<id>/aplicar` ★ | – | – | – | ✔ **(g)**<sup>11</sup> | – | – | ✔ | – | ✔ |
| `POST /recepciones` ★ · `/recepciones/<id>/conformar` ★ | – | – | – | ✔ | – | – | ✔ | – | ✔ |
| **Guías / masivo** |
| `GET/POST/PUT/DELETE /guias` · `/<id>/{detalle,emitir,consultar,pdf}` | – | ✔ **(g)**<sup>12</sup> | – | ✔ | – | ✔ | ✔ | – | ✔ |
| `GET /comprobantes/<id>/guia-prefill` | – | ✔ | – | ✔ | – | ✔ | ✔ | – | ✔ |
| `/lotes` (8 rutas) | – | ✔ | – | – | – | ✔ | ✔ | – | ✔ |
| **Maestros** |
| `GET /clientes` · `/lookup` · `/datos` | ✔ | ✔ | ✔ | ✔ | – | ✔ | ✔ | ✔ | ✔ |
| `POST /clientes` · `PUT /clientes/<id>` | ✔ | ✔ | ✔ | – | – | ✔ | ✔ | – | ✔ |
| **`DELETE /clientes/<id>`** | – | **✖** | – | – | – | ✖ | ✔ | – | ✔ |
| `GET /productos` · `/barcode/<code>` · `/plantilla` | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ |
| `POST /productos` · `/importar` · `/aplicar-tipos` | – | ✔ | – | ✔ | – | ✔ | ✔ | – | ✔ |
| `PUT /productos/<id>` — **campo `list_price`** | – | ✖ | – | ✔ | – | ✖ | ✔ | – | ✔ |
| **`DELETE /productos/<id>`** | – | **✖** | – | – | – | ✖ | ✔ | – | ✔ |
| **Gastos** |
| `GET /gastos` | 🔒propios | ✔ | 🔒propios | 🔒propios | – | ✔ | ✔ todos | ✔ todos | ✔ |
| `POST /gastos` · `PUT /gastos/<id>` | ✔ **(g)**<sup>13</sup> | ✔ | ✔ | ✔ | – | ✔ | ✔ | – | ✔ |
| `DELETE /gastos/<id>` | ✔ **(g)**<sup>14</sup> | ✔ | ✔ | – | – | ✔ | ✔ | – | ✔ |
| `POST /gastos/<id>/aprobar\|rechazar` ★ | – | – | – | – | – | – | ✔ **(g)**<sup>3</sup> | – | ✔ |
| **Compras** |
| `GET /compras` | – | ✔ | – | ✔ | – | ✔ | ✔ | ✔ | ✔ |
| `POST /compras` · `/importar-xml` · `PUT` · `DELETE` | – | ✔ | – | – | – | ✔ | ✔ | – | ✔ |
| **Reportes / libros** |
| `GET /resumen` · `/reportes/dashboard` · `/ventas` · `/export` | – | ✔ | – | – | – | ✔ | ✔ | ✔ | ✔ |
| `GET /reportes/ple-ventas\|ple-compras\|ple-inventario\|rvie-reemplazo` | – | ✔ | – | – | – | ✔ | ✔ | **✔** | ✔ |
| `POST /periodos/<YYYYMM>/cerrar` ★ | – | – | – | – | – | – | ✔ | ✔ | ✔ |
| `POST /periodos/<YYYYMM>/reabrir` ★ | – | – | – | – | – | – | ✔ **(g)**<sup>15</sup> | – | ✔ |
| **Administración** |
| `GET/POST /admin/tenants` · `GET /admin/users` · `POST .../reset-password` | – | – | – | – | – | – | – | – | ✔ |
| `GET /admin/roles` ★ · `PUT /admin/users/<id>/roles` ★ | – | – | – | – | – | – | ✔ 🔒su RUC | – | ✔ |
| `POST /reset/request` · `/reset/confirm` | *público (sin auth, por diseño — `main.py:456-470`)* |

**Guardas de negocio (`(g)`) — ningún grupo las levanta:**
1. Cotización `aprobada`/`convertida` congelada → `UserError`.
2. Solo `borrador` propia; `convertida` nunca (hoy COT-035 la borra).
3. `aprobador_id ≠ solicitante_id` (`@api.constrains`), salvo que no exista otro usuario con el grupo (§6).
4. Solo `aceptada`/`aprobada`; devuelve el borrador **resuelto por el addon** (incl. `productId`, que hoy falta y rompe la conversión: `l10n_pe_ne_cotizacion.py:119-122`) — y de paso mata la deducción RUC→factura / DNI→boleta que hoy vive en `Cotizaciones.tsx:167-169`.
5. Con `formaPago=Credito`: exige aprobación vigente y `partner.credit + total ≤ partner.credit_limit`.
6. `_l10n_pe_check_baja` completo (plazo, tipo, serie, boleta > S/700, NC vigentes). **Un permiso no es una excepción al plazo SUNAT.**
7. Retiro ≤ efectivo disponible (`l10n_pe_ne_caja.py:236-247`, ya existe).
8. `usuario_revision_id ∉ {cajero_id, usuario_cierre_id}`.
9. Exige sesión de caja **abierta y del propio usuario** → esto convierte en real el gate que hoy es 100% cliente (`POS.tsx:91-99`, `Caja.tsx:20-21`: *"La caja nunca bloquea una venta"*).
10. `payment_state == 'paid'` (nativo, reconciliado). **La precondición del Caso 1 y del Caso 2.**
11. Solo si `estado == 'autorizado'`.
12. Guía `enviado` no se edita (`ESTADOS_EMITIBLES`, ya existe).
13. Sobre el tope de caja chica (`res.company`) → nace `pendiente_aprobacion`.
14. `aprobado`/`pagado` no se borra: se anula con motivo (override de `unlink()`, no solo el método público).
15. Crea un `account.lock_exception` nativo (`user_id` + `reason` + `end_datetime`) = la auditoría "quién, cuándo, por qué" gratis.

---

## 5. Encaje con lo existente y MIGRACIÓN

### 5.1 `group_l10n_pe_ne_emisor` sobrevive, y es la pieza que sostiene todo

- **Sobrevive con su nombre, su XML id y su semántica.** Es lo que hace que la SPA, los tests y los tenants no se enteren.
- **Cambia una sola cosa: `implied_ids` pasa de `[account.group_account_user]` a `[group_l10n_pe_ne_base]`**, y `base` implica `account.group_account_user`. **`all_implied_ids` es transitivo y recursivo** (`res_groups.py:71-72`) → el conjunto efectivo de un emisor es **idéntico bit a bit** al de hoy. Cero pérdida.
- **No se convierte en implied de los nuevos.** Al revés: los nuevos son **hermanos** suyos sobre `base`. Si `caja` implicara `emisor`, un cajero podría emitir cualquier cosa y la segregación del Caso 1 moriría al nacer. La única excepción es `anulacion → emisor`, que **ya es así** y es correcta (quien anula, emite).
- **`base.group_system` no cambia.** Los 4 endpoints admin y los 4 métodos de modelo que lo chequean quedan igual.

### 5.2 Los usuarios ya provisionados

**Doctrina, copiada literal de la migración 19.0.1.4.0 que ya está en el repo:** *el upgrade no le quita a nadie una capacidad que ya tenía en silencio.*

```python
# migrations/19.0.1.7.0/post-roles.py
from odoo import SUPERUSER_ID, api

# Roles que HOY un emisor ya podía ejercer de facto (era todo-o-nada).
# Se los damos todos: restringir es decisión del admin, no efecto colateral del upgrade.
_ROLES = ('group_l10n_pe_ne_ventas', 'group_l10n_pe_ne_caja',
          'group_l10n_pe_ne_despacho', 'group_l10n_pe_ne_taller',
          'group_l10n_pe_ne_supervisor')


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    emisor = env.ref('l10n_pe_ne_biller.group_l10n_pe_ne_emisor', raise_if_not_found=False)
    if not emisor:
        return
    # all_user_ids (no user_ids): incluye a quien tiene emisor por implicación de
    # group_l10n_pe_ne_anulacion. Se lee ANTES de escribir: los grupos nuevos implican
    # base, que no cambia el conjunto, pero el patrón se respeta.
    usuarios = emisor.all_user_ids
    if not usuarios:
        return
    for xmlid in _ROLES:
        grupo = env.ref('l10n_pe_ne_biller.' + xmlid, raise_if_not_found=False)
        if grupo:
            grupo.write({'user_ids': [(4, uid) for uid in usuarios.ids]})
    # Contador NO se otorga: es un rol nuevo y más estrecho; no representa
    # ninguna capacidad que se esté quitando.
```

**Qué NO se rompe, punto por punto:**

| Riesgo | Por qué no ocurre |
|---|---|
| Un emisor pierde ACL | Los ACL que hoy cuelgan de `emisor` se reparten, pero la migración le da los 5 grupos → la **unión** de ACL es ≥ la de hoy. |
| Un emisor pierde visibilidad | Con `ventas` + `caja` + `supervisor`, las reglas se **OR-ean** → `[(1,'=',1)]` gana. Ve exactamente lo de hoy. |
| Se rompe la anulación | `group_l10n_pe_ne_anulacion` no se toca. Su migración de v1.4.0 ya corrió. |
| `hooks.py` deja de servir | Es `post_init_hook` (install-only) y mete al admin en `anulacion` → que implica `emisor` → que implica `base`. Sigue correcto. **Se le añade `supervisor`** para BD nuevas. |
| **`l10n_pe_ne_list_tenants` deja de ver gente** | **Sí se rompe si no se arregla.** `res_company.py:270` usa `('group_ids','in',grp.id)` = **explícitos**. Un futuro usuario con solo `group_l10n_pe_ne_caja` no aparece. **Fix obligatorio en el mismo PR: `('all_group_ids','in',grp.id)`** y buscar contra `group_l10n_pe_ne_base`, no contra `emisor` (un cajero puro no es emisor pero **es** personal del tenant). |
| `provision_tenant` crea usuarios mancos | `res_company.py:225,240` asigna solo `emisor`. Pasa a aceptar `roles: []` y por defecto asignar el preset **`duenio`** (§6) → el primer usuario del RUC lo puede todo, como hoy. |
| Un `-u` revierte algo | Las reglas nuevas van en el mismo `<data>` sin `noupdate`. `account.account_move_see_all` **no se toca** (está en `noupdate="1"`, y no hace falta: §1.1). |

### 5.3 La SPA: retrocompatible por construcción

`whoami` **añade** claves; no quita ni renombra `user/login/companyId/company/ruc/isAdmin/puedeAnular/mustChangePassword`. `Perfil` en `api.ts:141` sigue compilando y `Comprobantes.tsx:137` (`perfil?.puedeAnular !== false`) sigue funcionando exactamente igual, porque **el backend sí manda el flag** (`main.py:353`) — el "fail-open" que el contexto denuncia es un problema de `fact/`, no del canónico. **Un build viejo de la SPA contra el backend nuevo funciona**; lo único que pasa es que muestra botones que dan 403. Que es precisamente el orden correcto de despliegue: **primero el addon, después la SPA**.

---

## 6. Cómo lo consume la SPA sin una línea de lógica en TS

### 6.1 El addon calcula; el controller serializa; React pinta

```python
# models/res_users.py  — la ÚNICA fuente de verdad de permisos
_ROLES = {  # rol → xml id. El mapa vive aquí, no en TypeScript.
    'ventas': 'group_l10n_pe_ne_ventas',
    'emisor': 'group_l10n_pe_ne_emisor',
    'caja': 'group_l10n_pe_ne_caja',
    'despacho': 'group_l10n_pe_ne_despacho',
    'taller': 'group_l10n_pe_ne_taller',
    'anulacion': 'group_l10n_pe_ne_anulacion',
    'supervisor': 'group_l10n_pe_ne_supervisor',
    'contador': 'group_l10n_pe_ne_contador',
}

# Ruta del SPA → permiso que la habilita. También vive aquí: si el menú se decidiera
# en TS, la regla tendría dos dueños y divergiría (es el error de emitirSchema.ts).
_MENU = {
    '/': 'puedeVerInicio',        '/pos': 'puedeCobrar',
    '/caja': 'puedeOperarCaja',   '/emitir': 'puedeEmitir',
    '/comprobantes': 'puedeVerComprobantes', '/cotizaciones': 'puedeCotizar',
    '/guias': 'puedeEmitir',      '/masivo': 'puedeEmitir',
    '/cola': 'puedeVerCola',      '/analisis': 'puedeVerReportes',
    '/reportes': 'puedeVerLibros','/descargas': 'puedeVerReportes',
    '/clientes': 'puedeVerClientes','/productos': 'puedeVerProductos',
    '/compras': 'puedeVerCompras','/gastos': 'puedeRegistrarGasto',
    '/series': 'puedeEmitir',     '/negocio': 'puedeAdministrarNegocio',
    '/aprobaciones': 'puedeAprobar', '/admin/emisores': 'isAdmin',
}


class ResUsers(models.Model):
    _inherit = 'res.users'

    def l10n_pe_ne_perfil(self):
        """Permisos EFECTIVOS del usuario. Única autoridad: has_group sobre
        all_group_ids (implicación incluida). El controller solo serializa esto;
        la SPA solo lo pinta. El backend revalida SIEMPRE en cada endpoint."""
        self.ensure_one()
        g = lambda x: self.has_group('l10n_pe_ne_biller.' + _ROLES[x])
        admin = self.has_group('base.group_system')
        p = {
            'isAdmin': admin,
            # Compat: la SPA ya consume esta clave (api.ts:141). NO renombrar.
            'puedeAnular': g('anulacion') or admin,
            'puedeCotizar': g('ventas') or g('supervisor') or admin,
            'puedeEmitir': g('emisor') or admin,
            'puedeCobrar': (g('emisor') and g('caja')) or admin,
            'puedeOperarCaja': g('caja') or g('supervisor') or admin,
            'puedeDespachar': g('despacho') or g('supervisor') or admin,
            'puedeAtenderTaller': g('taller') or g('despacho') or g('supervisor') or admin,
            'puedeAprobar': g('supervisor') or admin,
            'puedeVerLibros': g('contador') or g('emisor') or g('supervisor') or admin,
            'puedeVerCola': g('taller') or g('despacho') or g('caja') or g('supervisor') or admin,
            'puedeRegistrarGasto': not g('contador'),
            'puedeAdministrarNegocio': g('emisor') or g('supervisor') or admin,
            'puedeVerComprobantes': True,   # §1.1: visibilidad = RUC, no rol
            'puedeVerClientes': True, 'puedeVerProductos': True,
            'puedeVerInicio': True,
            'puedeVerReportes': g('emisor') or g('supervisor') or g('contador') or admin,
            'puedeVerCompras': g('emisor') or g('despacho') or g('supervisor') or g('contador') or admin,
        }
        return {
            'roles': sorted(k for k in _ROLES if g(k)),
            'permisos': p,
            # Rutas que este usuario tiene derecho a ver. La SPA hace
            # NAV.filter(i => menu.includes(i.to)) y nada más.
            'menu': [ruta for ruta, perm in _MENU.items() if p.get(perm)],
        }
```

```python
# controllers/main.py — whoami: se AÑADEN claves, no se quita ninguna.
@http.route("/ne/api/whoami", **_GET)
def whoami(self, **kw):
    uid = self._identify()
    if not uid:
        return self._unauth()
    user = self._user(uid)
    return self._json({
        "user": user.name, "login": user.login,
        "companyId": user.company_id.id, "company": user.company_id.name,
        "ruc": user.company_id.vat or "",
        "isAdmin": user.has_group("base.group_system"),
        "puedeAnular": self._puede_anular(uid),          # se conserva tal cual
        "mustChangePassword": user.l10n_pe_ne_must_change_password,
        **user.l10n_pe_ne_perfil(),                      # roles + permisos + menu
    })
```

### 6.2 Qué cambia en la SPA (poco, y nada de ello es lógica)

```ts
// api.ts — el contrato crece; no se rompe.
export interface Perfil {
  user: string; login: string; companyId: number; company: string; ruc: string
  isAdmin?: boolean; puedeAnular?: boolean; expires?: string; mustChangePassword?: boolean
  roles?: string[]
  permisos?: Record<string, boolean>
  menu?: string[]   // rutas autorizadas, calculadas por el addon
}
```

```tsx
// App.tsx:89 — el gate deja de ser `isAdmin` y pasa a ser la lista del addon.
// menu === undefined  → backend legacy → se pinta todo (el addon igual da 403).
// menu === []         → usuario sin permisos → menú vacío. Distinto de undefined.
const permitida = (to: string) => !perfil?.menu || perfil.menu.includes(to)
const navItems = [...NAV, ...ADMIN_NAV].filter(i => 'section' in i || permitida(i.to))

// auth.tsx — ProtectedRoute con permiso, sin inventar reglas:
export function RequireRuta({ to, children }: { to: string; children: ReactNode }) {
  const { perfil, loading } = useAuth()
  if (loading) return <PageLoader />
  if (!perfil) return <Navigate to="/login" replace />
  if (perfil.menu && !perfil.menu.includes(to)) return <Navigate to="/" replace />
  return <>{children}</>
}
```

**Acciones POR REGISTRO** (el `.includes()` de `Comprobantes.tsx:591-611` y el `estado === 'aceptada'` de `Cotizaciones.tsx:356`): el `detalle` devuelve la lista ya resuelta por el addon — **estado × permiso × guarda de negocio, en un solo sitio**:

```python
def _l10n_pe_ne_acciones(self):
    """Qué puede hacer ESTE usuario con ESTE documento. Une los tres ejes que hoy
    la SPA cruza a mano: estado del documento, grupo del usuario y guarda fiscal."""
    self.ensure_one()
    p = self.env.user.l10n_pe_ne_perfil()['permisos']
    acc = []
    if self.l10n_pe_biller_state in ('en_proceso', 'enviado', 'aceptado'):
        acc += ['pdf', 'ticket', 'xml']
    if self.l10n_pe_biller_state in ('enviado', 'aceptado'):
        acc += ['cdr', 'email']
        if p['puedeAnular'] and not self._l10n_pe_ne_baja_bloqueada():
            acc.append('anular')        # plazo SUNAT incluido: una sola verdad
        if p['puedeEmitir'] and self.move_type == 'out_invoice':
            acc.append('nota_credito')
    if self.l10n_pe_biller_state in ('por_enviar', 'error', 'rechazado') and p['puedeEmitir']:
        acc.append('reenviar')
    return acc
```
Con esto **se borra** `lib/anulacion.ts` entero (el plazo de 7 días duplicado y auto-confeso como "posiblemente mal en los DOS lados") y los sets de estados de `Comprobantes.tsx`. **Regla que no se negocia: el `menu`/`acciones` es UX. El 403 lo sigue dando el endpoint, siempre.**

---

## 7. El caso PyME real: la tienda de 2 personas

**Requisito del usuario: "acumular roles sin fricción" y "que un negocio chico NO tenga que usar esto".** Tres mecanismos, y ninguno es opcional.

### 7.1 Los roles se acumulan por diseño, no por parche

Verificado en Odoo 19 (`res_groups.py:281-295`): `disjoint_ids` **solo** aplica a los grupos de tipo de usuario (`employee/portal/public`). **Los funcionales no tienen exclusividad.** Una persona con `{ventas, emisor, caja, despacho}` es legal, y las `ir.rule` se **OR-ean** → ve todo lo suyo *y* todo lo de caja. **La acumulación no degrada: amplía.** Por eso los grupos son hermanos sobre `base` y no una escalera de rangos: una escalera obliga a que "cajero" sea *más* que "vendedor", que es falso — son cosas distintas.

### 7.2 Perfiles (presets): el cargo es un conjunto, y el conjunto vive en el addon

```python
# models/res_users.py
_PERFILES = {
    'duenio':     ['emisor', 'ventas', 'caja', 'despacho', 'taller', 'anulacion', 'supervisor'],
    'vendedor':   ['ventas'],
    'cajero':     ['emisor', 'caja'],          # cobrar = emitir + cajón
    'mostrador':  ['ventas', 'emisor', 'caja', 'despacho'],   # librería de 2 personas
    'almacen':    ['despacho'],
    'tecnico':    ['taller'],
    'supervisor': ['supervisor'],
    'contador':   ['contador'],
}

@api.model
def l10n_pe_ne_list_perfiles(self):
    """Presets para la pantalla de Emisores. La SPA pinta checkboxes; no sabe qué
    grupo es cada cosa."""
    return [{'key': k, 'label': _LABELS[k], 'roles': v} for k, v in _PERFILES.items()]

@api.model
def l10n_pe_ne_set_roles(self, target_id, roles):
    """Asigna roles a un usuario del MISMO RUC. sudo() + whitelist: escribir group_ids
    por ACL es escalada de privilegios trivial (base_security.xml:141-146 hace visible
    a todo interno, y quien escribe group_ids se auto-otorga base.group_system).
    Por eso esto es un método del addon y NO un permiso."""
    if not (self.env.user.has_group('l10n_pe_ne_biller.group_l10n_pe_ne_supervisor')
            or self.env.user.has_group('base.group_system')):
        raise AccessError(_("Solo un supervisor puede asignar roles."))
    target = self.sudo().browse(int(target_id)).exists()
    if not target or target.share:
        raise UserError(_("Usuario no encontrado."))
    if not self.env.user.has_group('base.group_system'):
        # Scope duro por RUC: un supervisor NUNCA toca usuarios de otra empresa.
        if not (target.company_ids & self.env.user.company_ids):
            raise AccessError(_("No puedes gestionar usuarios de otra empresa."))
        if target.has_group('base.group_system'):
            raise AccessError(_("No puedes modificar a un administrador."))
    desconocidos = set(roles) - set(_ROLES)
    if desconocidos:                       # WHITELIST: base.group_system jamás entra
        raise UserError(_("Rol no válido: %s") % ', '.join(sorted(desconocidos)))
    todos = self.env.ref('l10n_pe_ne_biller.' + _ROLES[r]) for r in ...   # (6,0,[...])
    target.write({'group_ids': [(3, g.id) for g in _todos_ne] +
                               [(4, self.env.ref('l10n_pe_ne_biller.' + _ROLES[r]).id)
                                for r in roles]})
    self.env['res.users.apikeys'].sudo().search([('user_id', '=', target.id)]).unlink()
    return target.l10n_pe_ne_perfil()
```
> El último `unlink()` de las keys **importa**: quitarle un rol a alguien con un token vivo no surte efecto hasta las 12h del TTL. Revocar la key aplica el cambio **al siguiente request**. Es el mismo patrón que ya usa `l10n_pe_ne_admin_reset_password` (`res_users.py:54`).

### 7.3 El negocio chico no usa nada de esto (y esa es la parte más importante)

1. **Provisión = todo.** `l10n_pe_ne_provision_tenant` asigna por defecto el perfil **`duenio`**. Un RUC nuevo con un usuario: **funciona exactamente como hoy**, sin ver la palabra "rol" jamás.
2. **Los gates de aprobación nacen APAGADOS.** Todos los umbrales viven en `res.company` con default neutro:
   ```python
   l10n_pe_ne_caja_tolerancia = fields.Monetary(default=0.0)        # 0 = no observa nunca
   l10n_pe_ne_descuento_tolerancia_pct = fields.Float(default=0.0)  # 0 = gate apagado
   l10n_pe_ne_gasto_tope_aprobacion = fields.Monetary(default=0.0)  # 0 = sin aprobación
   l10n_pe_ne_control_recepcion = fields.Boolean(default=False)     # la bodega de 1 no se traba
   ```
   Expuestos en `GET /ne/api/config` → **muere `AVISO_DIF = 10` de `Caja.tsx:31`**, que es la política de control interno del dueño viviendo en el navegador.
3. **Cláusula de escape del auto-aprobador.** El invariante `aprobador ≠ solicitante` **se degrada solo** si no hay nadie más:
   ```python
   otros = self.env['res.users'].sudo().search_count([
       ('company_ids', 'in', self.company_id.id), ('active', '=', True),
       ('all_group_ids', 'in', self.env.ref('...group_l10n_pe_ne_supervisor').id),
       ('id', '!=', self.env.user.id)])
   if otros and self.solicitante_id == self.env.user:
       raise UserError(_("Otro supervisor debe aprobar tu propia solicitud."))
   self.auto_aprobado = not otros   # se registra y sale marcado en el reporte
   ```
   En la tienda de 2 personas el dueño se aprueba a sí mismo y **queda marcado**; en la de 6 el control es duro. Sin esto, un faltante de S/5 un domingo deja la tienda sin poder operar — y una PyME desinstala el producto antes que aceptar eso.

---

## 8. Orden de entrega (cada fase deja el sistema en pie)

| Fase | Qué | Por qué primero |
|---|---|---|
| **0** | `has_group` **dentro** de `l10n_pe_ne_quick_anular` / `action_l10n_pe_send_baja`; `list_tenants` → `all_group_ids`; `user_id` en cotización, `solicitante_id` en gasto | Cierra el hueco real (el modelo no es autoridad) y el bug latente. **Sin campos no hay `ir.rule`.** Nada de esto necesita roles. |
| **1** | `privilege` + `base` + 5 grupos + ACL repartido + `ir.rule` + migración `19.0.1.7.0` + `l10n_pe_ne_perfil()` + `whoami` + `set_roles` + presets | **Nadie pierde nada** (§5.2) y aún **no hay ningún gate nuevo**: el sistema se comporta idéntico. Es la fase de riesgo cero. |
| **2** | Gates en los 88 endpoints + SPA leyendo `menu`/`acciones` | Ya hay a quién gatear. Los umbrales siguen en 0 → sin fricción. |
| **3** | Máquina de transiciones de cotización (**mata `POST /cotizaciones/<id>/estado`** y la línea `vals['estado'] = payload['estado']` de `l10n_pe_ne_cotizacion.py:260-261`) + `POST /cotizaciones/<id>/cobrar` | **Caso 1** operativo. Sin cerrar esas dos puertas, un `curl` salta el proceso entero y los roles son decoración. |
| **4** | `l10n_pe_ne.pedido` (`tipo` = encargo\|orden_trabajo\|apartado) con `mail.thread` + cola + `/entregar` con `payment_state='paid'` | **Caso 2** + Caso 1 eslabón 3 + layaway, **un solo modelo**. |
| **5** | Aprobaciones (descuadre, descuento, egreso, crédito, devolución) + `contador` + cierre de periodo | Se activan por negocio, no por defecto. |

---

## 9. Lo que este diseño NO resuelve (dicho ahora, no en QA)

1. **`account.move` no se segrega por visibilidad.** Todo rol operativo ve todos los comprobantes de su RUC. Es una decisión (§1.1), no un olvido. Si algún día alguien pide "cada vendedor ve solo sus ventas", **eso es otro proyecto**: sobrescribir `account.account_move_see_all` y desconectar `group_account_invoice`.
2. **`base` implica `account.group_account_user` → todo rol tiene ACL de escritura sobre `account.move`.** El único freno es que solo existe `/ne/api` y que la API key tiene scope propio `l10n_pe_ne` (`main.py:32`) que **no habilita XML-RPC/JSON-RPC**. **Pero el usuario tiene contraseña y `base.group_user`: si el backend web de Odoo está expuesto, entra.** Cerrarlo es infraestructura (proxy/dbfilter), no ACL. **El único rol inmune es `contador`**, y no es casualidad: es el único tercero.
3. **Una sola caja abierta por RUC** (`l10n_pe_ne_caja.py:41-48`). El rol `caja` **no** habilita dos cajeros simultáneos: para eso hay que romper el amarre por `create_date` (`:57-67`) **primero** — sin `sesion_id` en `account.move`, dos sesiones abiertas reclaman las mismas ventas y **duplican el esperado en ambas**. Turnos **secuenciales** sí funcionan hoy; **simultáneos no**, y quitar el índice sin lo anterior no lo arregla: lo rompe.
4. **`aprobador ≠ solicitante` se degrada en la tienda de 2 personas** (§7.3). Es un compromiso consciente: el control queda *documentado*, no *impuesto*.
5. **Ningún modelo propio hereda `mail.thread`** (verificado: 3 hits de tracking en todo `models/`, todos en `account_move_biller.py`). Un proceso de aprobación sin auditoría de quién aprobó no vale nada. Cuesta **cero dependencias** (`mail` ya está en `depends`) y hay que meterlo en cotización, caja, pedido, gasto y ajuste **en la misma fase que su aprobación**, no después.