# Admin con el grupo "Emisor NE Express" por defecto en tenants nuevos

- **Fecha:** 2026-07-09
- **Addon:** `l10n_pe_ne_biller`
- **Estado:** Diseño aprobado, pendiente de implementación

## Contexto

Cada tenant nuevo es una base de datos Odoo que, al crearse, trae el usuario
administrador estándar `base.user_admin` (login `admin`). Ese admin no pertenece
al grupo `l10n_pe_ne_biller.group_l10n_pe_ne_emisor` ("Emisor NE Express"), que
es el rol que habilita emitir/anular comprobantes y gestionar clientes/productos
vía la API `/ne/api`.

Antes, el addon sembraba un usuario emisor demo (`emisor` / `emisor-ne-2026`) vía
`data`, que se creaba en toda instalación (incluida producción). Ese seed se
eliminó por ser una credencial por defecto compartida y versionada en claro (ver
`data/l10n_pe_ne_emisor_user.xml`). Como consecuencia, un tenant nuevo ya no trae
ninguna identidad con el grupo emisor lista para operar.

## Objetivo

Que el usuario `admin` de cada **tenant nuevo** quede, por defecto, dentro del
grupo `group_l10n_pe_ne_emisor` al instalar el addon, sin sembrar ninguna
credencial ni usuario adicional.

### En alcance

- Añadir el grupo `group_l10n_pe_ne_emisor` a `base.user_admin` **una vez, en la
  instalación** del addon en una BD nueva.

### Fuera de alcance

- **No** se aplica retroactivamente a BDs/tenants ya existentes (no hay migración
  ni backfill). Decisión explícita del usuario: "solo tenants nuevos".
- No se modifica `res.company.l10n_pe_ne_provision_tenant` (el alta de emisores
  por RUC) ni el emisor demo del arranque local (`levantar-docker.sh`).
- No se altera ningún otro usuario ni el aislamiento multi-compañía.

## Decisión de diseño

Se implementa con un **`post_init_hook`** (Python), porque es el único mecanismo
que corre **exclusivamente en la instalación** (`-i`) y no en los upgrades
(`-u`). Eso coincide exactamente con el alcance "solo tenants nuevos": un `-u` del
addon sobre una BD ya existente no re-ejecuta el hook, así que los tenants viejos
no se ven afectados.

## Componentes

### 1. `hooks.py` (nuevo, en la raíz del addon)

```python
from odoo import api, SUPERUSER_ID


def post_init_hook(env):
    """Al instalar el addon en una BD nueva, deja al admin (base.user_admin)
    dentro del grupo 'Emisor NE Express' para que pueda operar la API /ne/api
    sin sembrar ningún usuario/credencial por defecto. Solo en install."""
    admin = env.ref('base.user_admin', raise_if_not_found=False)
    group = env.ref('l10n_pe_ne_biller.group_l10n_pe_ne_emisor',
                    raise_if_not_found=False)
    if admin and group:
        admin.write({'group_ids': [(4, group.id)]})
```

Notas:
- El comando `(4, group.id)` **añade** el grupo sin quitarle al admin ninguno de
  los suyos. Es idempotente.
- Firma `post_init_hook(env)` (Odoo 17+/19). El `env` ya viene con privilegios de
  superusuario durante la instalación.

### 2. `__manifest__.py`

- Registrar el hook: `'post_init_hook': 'post_init_hook'`.
- Subir la versión: `19.0.1.2.0` → `19.0.1.3.0` (cambio de comportamiento en
  install).

### 3. `__init__.py` (raíz del addon)

- Importar el hook para que el manifest lo resuelva: `from .hooks import post_init_hook`
  (se mantienen los imports actuales de `models` y `controllers`).

## Manejo de errores

- Ambos `env.ref(...)` usan `raise_if_not_found=False`. Si por cualquier motivo el
  admin o el grupo no existen, el hook no hace nada y **no** aborta la instalación.
- La escritura es sobre un único usuario conocido; no hay efectos colaterales sobre
  otros usuarios, compañías ni datos.

## Pruebas

- **Test automatizado** (clase tageada en `tests/`, p. ej. `test_admin_group.py`):
  tras la instalación del módulo en la BD de test (donde el hook ya corrió),
  afirmar que el admin pertenece al grupo:
  ```python
  admin = self.env.ref('base.user_admin')
  self.assertTrue(admin.has_group('l10n_pe_ne_biller.group_l10n_pe_ne_emisor'))
  ```
- **Verificación manual complementaria:** correr `levantar-docker.sh` sobre una BD
  nueva y confirmar que el usuario `admin` ya aparece con el grupo "Emisor NE
  Express" (Ajustes → Usuarios → admin → grupos).

## Alternativas consideradas

- **Record XML que actualiza `base.user_admin`** (declarativo, sin Python): los
  archivos `data` se cargan también en cada `-u`, por lo que la primera vez que el
  record llegara a una BD ya existente, su próximo upgrade le añadiría el grupo al
  admin — se sale del alcance "solo tenants nuevos". Además arrastra las sutilezas
  de escribir sobre un record `noupdate` de otro módulo. Descartado.
- **Hacer que un grupo base "implique" el grupo emisor** (`implied_ids`): afectaría
  a *todos* los usuarios, no solo al admin. No es lo pedido. Descartado.

## Riesgos / notas

- El admin ya tiene, de por sí, acceso a todas las compañías; sumarle el grupo
  emisor (que hereda `account.group_account_user`) no cambia su nivel de privilegio
  de forma relevante — solo lo habilita explícitamente para la API `/ne/api`.
- Al ser install-only, si en el futuro se quisiera nivelar tenants existentes,
  haría falta una migración aparte (hoy fuera de alcance).
