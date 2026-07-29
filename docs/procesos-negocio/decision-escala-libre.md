# Decisión de diseño — Escala libre (1 usuario → N usuarios)

> Cierra **P1** de [preguntas-abiertas.md](preguntas-abiertas.md).
> Respuesta del usuario (2026-07-17): *"no hay una empresa en particular, hay q pensar en diferentes escenarios, puede ser una sola persona q tenga todos los perfiles, o mas usuarios"*.

## La regla, en una línea

**Ningún proceso puede EXIGIR que dos pasos los haga gente distinta.** El producto es SaaS multi-tenant y debe funcionar igual con 1 usuario que lleva todos los sombreros (el cliente **modal** de la PyME peruana) que con 12 usuarios segregados — sin una sola rama que pregunte cuánta gente hay.

## Por qué es una regla de corrección, no de UX

> Para todo proceso P y todo estado *s* alcanzable, el conjunto de usuarios capaces de avanzar *s* debe ser **no vacío** en un tenant de un solo usuario que tiene todos los roles.

Un catálogo que viola esto no produce "fricción": produce **documentos atascados para siempre**. El argumento formal es de **monotonía** — verificado contra el motor de Odoo, no contra nuestro código:

- `has_group(u, G)` es **monótona** en los grupos de `u`: añadir roles solo puede volver `False → True`. Odoo lo garantiza: `all_implied_ids` es recursivo (`odoo19: …/base/models/res_groups.py`), las `ir.rule` con `groups` se **OR-ean** (`ir_rule.py` → `Domain.OR(group_domains)`), los ACL son unión. Acumular roles **amplía** capacidad, nunca la estrecha. El usuario con todos los roles es el máximo del retículo: **pasa toda compuerta `has_group` por construcción.**
- `usuario_A ≠ usuario_B` (identidad) **no es monótona**: no se satisface añadiendo roles, se satisface añadiendo **personas**. Es el único predicado que se vuelve más restrictivo cuanto más chico es el cliente. Con `|U| = 1`, `aprobador ≠ solicitante` es `u ≠ u = False`: el conjunto de habilitados es **∅**. No es un control estricto — es un **deadlock disfrazado de control**.

**Con 1 usuario, la segregación de funciones es matemáticamente imposible.** No difícil: imposible. Escribir `aprobador ≠ solicitante` no compra control, compra un `raise` que nadie puede levantar.

## Qué se valida en una transición — tres ejes, ninguno es la identidad de nadie

```
1. ESTADO    ¿existe la transición desde donde estoy?     → UserError   (máquina de estados)
2. CAPACIDAD ¿este usuario tiene el grupo?                → AccessError (has_group)  ← ÚNICO predicado sobre el usuario
3. REALIDAD  ¿el mundo lo permite? plazo SUNAT, saldo,
             efectivo disponible, stock                    → UserError   (guarda)
```

El **eje 3** es la razón de que borrar la segregación no afloje nada de lo que importa: los controles duros del producto **ya son guardas de realidad**, no de identidad. `_l10n_pe_check_baja` no es un permiso — ningún grupo levanta el plazo de SUNAT. El tope de retiro ≤ efectivo disponible (`l10n_pe_ne_caja.py:239-247`) no es un permiso — el dueño con todos los roles **tampoco** puede retirar plata que no está en el cajón. Eso ya está bien y funciona igual con 1 usuario que con 50.

## Qué se borra, literalmente (deadlocks con 1 usuario)

Las comparaciones de identidad que el catálogo/roles proponían **se borran, no se degradan ni se condicionan**:

- `catalogo.md` (CN-03): `autorizador ≠ create_uid`
- `roles.md` §3.3 / §4: `usuario_revision_id ∉ {cajero_id, usuario_cierre_id}` (descuadre)
- `roles.md` §3.6 / §4 / tabla: `aprobador_id ≠ solicitante_id`
- `roles.md` §7.3: la "cláusula de escape" `if search_count(otros) and solicitante == user: raise` — **la peor**: hace que la política cambie sola un martes porque contrataste a alguien; el test que pasa con 1 usuario no prueba el código que corre con 2.

## Las colas con 1 usuario: **bandeja**, no handoff

Una cola es una **proyección de estado**, no una asignación de persona. La cola de cobro es `estado='aceptada' AND comprobante_id = False` — verdad exista o no un cajero. El **mismo dominio** sirve a los dos casos sin una rama, por el OR de `ir.rule`:

- Cajero puro → `[(1,'=',1)]` (ve todo).
- Vendedor puro → `['|',('user_id','=',uid),('user_id','=',False)]` (lo suyo + la cola).
- Dueño con ambos roles → `Domain.OR([...])` = **TRUE**. Ve todo.

En la SPA la cola se llama **"Pendientes"**, no "Mi trabajo asignado". El contador (aceptadas sin cobrar, cobradas sin entregar) es útil para 1 y para 12. **Un tenant chico nunca lee la palabra "handoff".**

Y sí existe **"cobrar y entregar" en un clic** — pero no como atajo (`if tiene_todo: saltar_estados`, que sería una segunda máquina de estados que solo corre en tenants chicos y hace divergir los reportes). Es un **fold sobre transiciones atómicas**: el documento atraviesa todos los estados intermedios (la cola de despacho existe aunque dure 8 ms), cada uno con sus tres ejes y su bitácora. La indistinguibilidad entre "lo hicieron tres personas en dos horas" y "una en 400 ms" **es** la escala libre.

## Los gates: un solo mecanismo `off | aviso | bloqueo` por RUC

Toda compuerta de aprobación es un parámetro de `res.company` (multi-tenant; `ir.config_parameter` es global a la BD y no sirve) con tres modos:

| Modo | Qué hace |
|---|---|
| `off` | No pasa nada. **Default de todo tenant nuevo.** |
| `aviso` | No bloquea. Marca la excepción con motivo y cae en la cola de revisión. |
| `bloquea` | Bloquea salvo que quien opera pueda aprobar (y entonces auto-aprueba **registrado**). |

Reglas del motor:
- **`umbral` y `modo` son ejes ortogonales.** El antipatrón `tolerancia=0 → apagado` (propuesto en `roles.md`) hace **inexpresable** la política más estricta ("no tolero ni un céntimo") y le entrega al dueño lo contrario de lo que tecleó. `modo='off'` apaga; `modo='bloquea' + umbral=0` es tolerancia cero. Son dos campos.
- **La aprobación NO añade estados.** Nada de `pendiente_aprobacion` / `pendiente_revision` en el Selection (un estado que existe solo para esperar a un humano que quizá no exista es la violación blanda). La aprobación es un **atributo** (`aprobador_id`, `fecha_aprobacion`, `es_auto_aprobacion`). Consecuencia buena: la caja cerrada está cerrada, el índice único de sesión no se toca, y el Selection que pinta la SPA no crece.
- **Cuando el aprobador es quien pide: se auto-aprueba en un clic y queda escrito** (`es_auto_aprobacion=True`). No se salta el gate — se atraviesa y se firma. Con 1 usuario es el 100% de los casos. Un gate que se salta sin dejar rastro miente; este deja rastro.

Reducción honesta: de las ~11 compuertas del catálogo, **7 son gates reales** (descuadre, descuento, crédito, gasto, devolución, merma, depósito). `egreso` ≡ `gasto`; `anulación` ya es un grupo que funciona (no se toca); `reapertura` es nativa (`account.lock.exception`); `recepción` es un interruptor de flujo, no una aprobación. Ver la tabla completa en [decision-integridad-datos.md](decision-integridad-datos.md) §gates.

## El escape hatch de segregación: explícito, con su modo de fallo escrito

Para el cliente que **sí** tiene gente y quiere cuatro-ojos: un booleano `res.company.l10n_pe_ne_exigir_segregacion` (**default `False`**). Con él encendido, quien registra no puede aprobar lo suyo. Su `help` dice, en la propia UI, que **puede atascar documentos si no hay dos aprobadores reales**. Nunca es el default y **nunca depende de `search_count()` en runtime**: la política no cambia sola cuando contratas a alguien.

Cómo se resuelve la tensión que esto abre (un flag que hay que acordarse de encender no se enciende nunca): el **alta del segundo usuario dispara una propuesta al dueño** ("ahora que tienes equipo, ¿quieres exigir que las aprobaciones las firme otra persona?") — un aviso, no un cambio de semántica. Se conserva la monotonía y se resuelve el problema real.

## El default de un tenant nuevo

Un usuario con **todos los roles** (preset `duenio`) y **todos los gates en `off`**. Justificación: hay emisores vivos hoy que podían hacer todo (era todo-o-nada); restringir es decisión del dueño, no efecto colateral del upgrade. La segregación se **compra** encendiendo gates y el flag, a sabiendas.

## La SPA no decide nada

El addon calcula qué puede hacer este usuario con este documento ahora (`_l10n_pe_ne_acciones()`: estado × grupo × guarda × política, en un solo sitio) y `whoami` serializa los permisos. La SPA hace `.map()` y pinta. Al que tiene todos los roles le aparece "Cobrar y entregar"; al cajero puro solo "Cobrar"; al despachador puro solo "Entregar". Mismo endpoint, misma respuesta, cero `if` en el navegador. Aquí muere `AVISO_DIF = 10` de `Caja.tsx:31` (ver [hallazgos.md](hallazgos.md) H5).

## El invariante es ejecutable

Una regla que solo vive en un `.md` se reintroduce en el tercer PR. Va como test (`test_escala_libre.py`): para cada modelo con el mixin, el usuario con todos los roles debe poder avanzar desde todo estado no terminal, y ninguna guarda puede contener una comparación de identidades (`env.user !=`, `aprobador_id !=`, `not in (self.cajero_id`, …). Si se pone rojo, alguien metió una compuerta que exige dos personas y el producto dejó de funcionar para el cliente modal.

## Lo que esta decisión concede (honesto)

La segregación por identidad **también** fracasa en la práctica peruana con 5 empleados: el "supervisor" es el cuñado o el vendedor más antiguo, y aprueba todo sin mirar porque es sábado y hay cola. Produce una firma sin lectura: teatro con dos actores en vez de uno, y más caro. Por eso la regla de las dos personas nunca fue el control a defender. **El control real para el cliente que tiene gente no es la segregación — es la integridad del dato** (que sea verdadero, atribuible e inmutable): ver [decision-integridad-datos.md](decision-integridad-datos.md).
