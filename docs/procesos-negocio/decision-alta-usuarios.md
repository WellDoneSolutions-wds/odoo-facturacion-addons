# Decisión de diseño — Alta de usuarios y roles por el dueño del RUC

> Cierra **P3** de [preguntas-abiertas.md](preguntas-abiertas.md).
> Decisión del usuario (2026-07-17): la sugerencia recomendada — lo hace **el dueño del RUC**, con métodos `.sudo()` del addon y whitelist, nunca con ACL sobre `res.users`.

## La decisión

Un **addon nuevo `l10n_pe_ne_roles`** (`depends: ['l10n_pe_ne_biller']`, el facturador no se toca) donde el **dueño del RUC** da de alta, edita, desactiva y asigna roles a la gente **de su RUC y solo de su RUC**. Se implementa como **métodos `.sudo()` con whitelist de grupos otorgables**, y **cero filas de `ir.model.access.csv`**. Esa ausencia de ACL es el diseño entero, no un detalle.

## Por qué NO se puede hacer con grupo + ACL (verificado en Odoo 19)

Tres hechos del código nativo, cada uno mata la alternativa "dale una fila ACL al dueño":

1. **`res_users_rule` es global y NO aísla por compañía** (`odoo19: …/base/security/base_security.xml:141-146`): su dominio es `['|',('share','=',False),('company_ids','in',company_ids)]` — un OR cuya primera rama deja pasar a **todo usuario interno de cualquier RUC**. La capa de reglas de Odoo no aísla usuarios entre tenants (aísla `account.move`, pero no `res.users`).
2. **La única fila ACL con `perm_write` sobre `res.users` es la de `group_erp_manager`** (`base/security/ir.model.access.csv:86`), que además arrastra CRUD completo sobre `res.groups` (→ editarse `implied_ids` y auto-otorgarse cualquier grupo) y `res_company_rule_erp_manager` con dominio `[(1,'=',1)]` (→ ver todos los RUC). `group_user` sobre `res.users` es read-only.
3. **Odoo no tiene ningún check anti-escalada al escribir `group_ids`**: las únicas `@api.constrains` son `_check_disjoint_groups` y `_check_at_least_one_administrator`. **No existe "no puedes otorgar un grupo que tú no tienes".**

**Conclusión:** una fila `model_res_users,group_duenio,1,1,1,1` no da "el dueño gestiona a su gente" — da "el dueño lee los usuarios de todos los tenants y se escribe `base.group_system` en un request". Por eso la gestión de usuarios es un **método `sudo()`**, y en el momento en que se hace `.sudo().write({'group_ids': …})`, **el addon es el único control de escalada que existe**: Odoo no cubre las espaldas. La whitelist no es buena práctica, es el único mecanismo.

## Las piezas

- **`group_l10n_pe_ne_duenio`** — un marcador de capacidad que solo lee el Python del addon. Cero ACL, cero `ir.rule`. Cuelga de un `res.groups.privilege` "NE Express" (Odoo 19 usa `privilege_id`, no `category_id`). Implica `group_l10n_pe_ne_emisor` hoy (mañana `supervisor`, cuando aterrice la fase 1 de [roles.md](roles.md)).
- **Whitelist `_ROLES`** — dict en Python (cambiarla exige PR + review + deploy, no un `UPDATE`). **No contiene `duenio`**: eso es lo que hace `set_roles` seguro por construcción, y de regalo impide que `set_roles` **degrade** a un dueño (solo quita grupos que están en la whitelist). Un dueño no se clona ni se destituye por la ruta de roles.
- **`_PROHIBIDOS`** — `base.group_system` y `base.group_erp_manager`, comprobados por **cierre transitivo** (`all_implied_ids`), con un test que afirma el invariante: ningún valor de la whitelist puede implicar transitivamente un prohibido.
- **Dos choke points** — todo método empieza resolviendo (1) la compañía del dueño (exactamente una; si alcanza dos, "su RUC" no está definido → se niega), y (2) el usuario objetivo, validado en la **misma** compañía por **inclusión** (no por intersección — ver deuda heredada abajo).
- **`base.group_user` explícito siempre** en el `create`: sin él, `_compute_share` pone `share=True` y el usuario nace **portal** (invisible e inútil para la SPA). Bug real que el diseño evita.
- **Endpoints** `/ne/api/equipo/*` siguiendo el patrón del addon (`_identify`, `_run`, `_fail`).

## Cómo se otorga `duenio` (nunca por la whitelist)

| Vía | Quién | Cuándo |
|---|---|---|
| override de `l10n_pe_ne_provision_tenant` | `base.group_system` | al crear el RUC — el primer usuario es su dueño |
| `migrations/19.0.1.0.0/post-duenio.py` | el upgrade | tenants que ya existen |
| `l10n_pe_ne_duenio_add_codueno` | un dueño, **con su contraseña** (re-auth) | co-dueño / relevo |

## Revocación y sesión: la verdad sobre la ventana

Verificado: `_check_apikey_credentials` valida `u.active` en el propio SQL de auth (`res_users.py:1736-1747`), y `_login` busca con `active_test=True`. **Desactivar al usuario corta su API key en el siguiente request** y le impide re-loguear. El "riesgo del empleado saliente con token vivo" es acotado: el TTL real del token es de horas (`_TTL_HOURS_DEFAULT`), no los 365 días del `api_key_duration` (que es solo el tope de la UI de Odoo, y el login lo esquiva). La revocación efectiva ya existe; H-4 solo añade **quién la dispara**.

## Resultado del pentest — H-4 aguanta, con 7 arreglos antes de merge

Un pentester con acceso legítimo de "Dueño" atacó los cuatro objetivos duros — **escritura cross-tenant, `base.group_system`, otorgar un grupo peligroso fuera de whitelist, sobrevivir a la propia baja — y los cuatro están genuinamente cerrados**, verificados línea por línea contra Odoo 19. La decisión de **no** llevar `ir.model.access.csv` es lo que salva el producto. Pero quedaron 7 vectores vivos que hay que cerrar:

| # | Vector | Severidad | Cierre |
|---|---|---|---|
| **V3** | **Auto-lockout del RUC**: dos bajas concurrentes de los 2 últimos dueños pasan ambas la guarda "queda ≥1 dueño" (TOCTOU sin bloqueo) → RUC sin dueño, huérfano | **Alta** — hay que arreglar antes de merge | `SELECT … FOR UPDATE` sobre los dueños, o `@api.constrains` post-write que recuente y aborte la transacción |
| **V5** | La red anti-escalada NO cubre 2 de las 4 rutas que otorgan `duenio` (`provision_tenant` y la migración escriben `group_ids` sin el check transitivo) | **Alta** — rompe el invariante que el diseño usa como argumento | La misma red post-escritura (`target.all_group_ids & _PROHIBIDOS`) en el override y en la migración |
| **V1** | **Oráculo de enumeración de logins cross-tenant**: el `search_count` de unicidad de login distingue "ya existe" de "alta OK" → un dueño enumera qué logins existen en toda la plataforma | Media (divulgación, no escritura) | Mensaje genérico + rate-limit por dueño; dejar que el `IntegrityError` de `UNIQUE(login)` rechace el alta |
| **V2** | **Bypass del tope de asientos**: el cupo solo se chequea en el alta; desactivar→crear→reactivar supera el límite del plan | Media (evasión de cobro) | Llamar al cupo también en `set_activo(True)` |
| **V6** | H-4 detona un bug latente en `list_tenants`: busca por `group_ids` (explícitos) en vez de `all_group_ids` → un usuario con un rol que no implique `emisor` queda invisible al admin de plataforma | Media (regresión de observabilidad) | `all_group_ids` + buscar contra un `group_l10n_pe_ne_base` |
| **V4** | `duenio` es contagioso y reversible entre pares (un codueño puede destituir al fundador); es poder legítimo del dueño, no un fallo, pero el doc no lo admite | Baja (gobernanza) | Documentarlo explícitamente en el PR |
| **V7** | El override de `whoami` hace `json.loads(super().whoami())` — acople frágil que tumbaría el `whoami` entero si el biller cambia la forma | Baja (robustez) | `try/except` con fallback a la respuesta intacta |

**Prioridad antes de merge: V3 y V5** (baratos, tocan invariantes que el propio diseño declara), luego V1 y V2.

## Deuda heredada que H-4 roza

`l10n_pe_ne_admin_reset_password` usa **intersección** de compañías (`res_users.py:47`), tolerable para un `base.group_system` (que ya ve toda la BD) pero peligroso como patrón: es el copy-paste que convertiría a un futuro método-de-dueño en fuga cross-tenant. H-4 usa **inclusión** a propósito. Mientras el patrón de intersección siga vivo en el repo, merece un hallazgo propio.

## Preguntas que esta decisión abre

- **¿Hay tope de usuarios por RUC / se cobra por usuario?** El diseño deja el gancho (`l10n_pe_ne_max_usuarios` + V2), pero el número y el modelo de cobro los define el negocio, no el código.
- **¿Un dueño puede tener más de un RUC?** El choke point exige exactamente una compañía. Si un dueño gestiona varios RUC, hay que decidir cómo (probablemente un `duenio` por compañía, no un multi-compañía).
