# Admin con grupo "Emisor NE Express" por defecto — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Que al instalar el addon `l10n_pe_ne_biller` en una base de datos nueva, el usuario administrador (`base.user_admin`) quede automáticamente dentro del grupo `group_l10n_pe_ne_emisor` ("Emisor NE Express").

**Architecture:** Un `post_init_hook` (Python) que corre una sola vez en la instalación del módulo (`-i`), no en upgrades (`-u`). El hook añade el grupo al admin con el comando ORM `(4, id)` (idempotente). Se registra en el `__manifest__.py` y se expone desde el `__init__.py` del addon. Se cubre con un test que llama al hook directamente (independiente de install-vs-upgrade).

**Tech Stack:** Odoo 19 (Python), framework de tests de Odoo (`TransactionCase`, tags), addon `l10n_pe_ne_biller`.

## Global Constraints

- Odoo 19; addon `l10n_pe_ne_biller`. Rutas relativas a la raíz del repo `odoo-facturacion-addons`.
- Solo tenants nuevos: la lógica va en `post_init_hook` (install-only). **No** hay backfill/migración para BDs existentes.
- Añadir el grupo con `(4, group.id)` — añade sin quitar otros grupos; idempotente.
- Guardas `raise_if_not_found=False` en ambos `env.ref(...)`: si falta admin o grupo, el hook no hace nada y no aborta la instalación.
- No tocar `res.company.l10n_pe_ne_provision_tenant`, ni el emisor demo local (`levantar-docker.sh`), ni otros usuarios/compañías.
- Versión del manifest: `19.0.1.2.0` → `19.0.1.3.0`.
- El grupo objetivo es `l10n_pe_ne_biller.group_l10n_pe_ne_emisor` (name "Emisor NE Express"), definido en `security/l10n_pe_ne_security.xml`.

---

### Task 1: `post_init_hook` que añade el grupo emisor al admin

**Files:**
- Create: `addons/l10n_pe_ne_biller/hooks.py`
- Modify: `addons/l10n_pe_ne_biller/__init__.py`
- Modify: `addons/l10n_pe_ne_biller/__manifest__.py:3` (versión) y clave nueva `post_init_hook`
- Create: `addons/l10n_pe_ne_biller/tests/test_admin_group.py`
- Modify: `addons/l10n_pe_ne_biller/tests/__init__.py:1`

**Interfaces:**
- Produces: `post_init_hook(env)` en `odoo.addons.l10n_pe_ne_biller.hooks`, re-exportado como atributo del paquete `odoo.addons.l10n_pe_ne_biller.post_init_hook` (así lo resuelve Odoo desde el manifest). Efecto: `base.user_admin` pertenece a `l10n_pe_ne_biller.group_l10n_pe_ne_emisor`.

- [ ] **Step 1: Escribir el test que falla**

Crear `addons/l10n_pe_ne_biller/tests/test_admin_group.py`:

```python
from odoo.tests import TransactionCase, tagged
from odoo.addons.l10n_pe_ne_biller.hooks import post_init_hook


@tagged('post_install', '-at_install')
class TestAdminEmisorGroup(TransactionCase):
    def test_post_init_hook_deja_admin_en_grupo_emisor(self):
        group = self.env.ref('l10n_pe_ne_biller.group_l10n_pe_ne_emisor')
        admin = self.env.ref('base.user_admin')

        # Partimos de un estado conocido: admin SIN el grupo emisor.
        admin.write({'group_ids': [(3, group.id)]})
        self.assertNotIn(
            group, admin.group_ids,
            "Precondición: el admin no debería tener el grupo antes del hook")

        # El hook (install-only en producción) debe dejarlo dentro del grupo.
        post_init_hook(self.env)
        self.assertIn(
            group, admin.group_ids,
            "El admin debe quedar en el grupo Emisor NE Express tras el hook")
```

Registrar el módulo de test en `addons/l10n_pe_ne_biller/tests/__init__.py` (línea 1). Reemplazar:

```python
from . import test_install, test_mapper, test_send, test_documents, test_affectation, test_serie, test_retencion, test_detraccion, test_descuento, test_anticipo, test_baja, test_report_pdf, test_password_reset, test_email, test_caja, test_caja_http, test_masivo
```

por:

```python
from . import test_install, test_mapper, test_send, test_documents, test_affectation, test_serie, test_retencion, test_detraccion, test_descuento, test_anticipo, test_baja, test_report_pdf, test_password_reset, test_email, test_caja, test_caja_http, test_masivo, test_admin_group
```

- [ ] **Step 2: Correr el test y verificar que falla**

Requiere el stack docker local arriba (ver `levantar-docker.sh`, BD `odoo_ne_biller`).

Run:
```bash
docker compose exec -T odoo odoo \
  -c /etc/odoo/odoo.conf -d odoo_ne_biller \
  -u l10n_pe_ne_biller --test-enable \
  --test-tags /l10n_pe_ne_biller:TestAdminEmisorGroup \
  --db_host db --db_port 5432 --db_user odoo --db_password odoo \
  --no-http --stop-after-init 2>&1 | tail -30
```
Expected: FALLA. El import `from odoo.addons.l10n_pe_ne_biller.hooks import post_init_hook` no resuelve (aún no existe `hooks.py`) → error al cargar el módulo de test (`ModuleNotFoundError: ...l10n_pe_ne_biller.hooks`).

- [ ] **Step 3: Crear el hook**

Crear `addons/l10n_pe_ne_biller/hooks.py`:

```python
def post_init_hook(env):
    """Al INSTALAR el addon en una BD nueva, deja al admin (base.user_admin)
    dentro del grupo 'Emisor NE Express' para que pueda operar la API /ne/api
    sin sembrar ningún usuario ni credencial por defecto.

    Install-only (registrado como post_init_hook): un -u sobre una BD ya
    existente NO lo re-ejecuta, así que los tenants viejos no se ven afectados.
    (4, id) añade el grupo sin quitarle al admin ninguno de los suyos; idempotente.
    """
    admin = env.ref('base.user_admin', raise_if_not_found=False)
    group = env.ref('l10n_pe_ne_biller.group_l10n_pe_ne_emisor',
                    raise_if_not_found=False)
    if admin and group:
        admin.write({'group_ids': [(4, group.id)]})
```

- [ ] **Step 4: Exponer el hook desde el paquete del addon**

Modificar `addons/l10n_pe_ne_biller/__init__.py`. Reemplazar:

```python
from . import models
from . import controllers
```

por:

```python
from . import models
from . import controllers
from .hooks import post_init_hook
```

(El `from .hooks import post_init_hook` hace que Odoo pueda resolver `post_init_hook` como atributo del paquete `odoo.addons.l10n_pe_ne_biller`, que es como el manifest lo referencia por nombre.)

- [ ] **Step 5: Registrar el hook y subir la versión en el manifest**

Modificar `addons/l10n_pe_ne_biller/__manifest__.py`.

(a) Línea 3, cambiar la versión:

```python
    'version': '19.0.1.3.0',
```

(b) Añadir la clave `post_init_hook` (por ejemplo, justo después de la línea `'summary': ...,`):

```python
    'post_init_hook': 'post_init_hook',
```

- [ ] **Step 6: Correr el test y verificar que pasa**

Run:
```bash
docker compose exec -T odoo odoo \
  -c /etc/odoo/odoo.conf -d odoo_ne_biller \
  -u l10n_pe_ne_biller --test-enable \
  --test-tags /l10n_pe_ne_biller:TestAdminEmisorGroup \
  --db_host db --db_port 5432 --db_user odoo --db_password odoo \
  --no-http --stop-after-init 2>&1 | tail -30
```
Expected: PASA. En el log aparece la ejecución de `TestAdminEmisorGroup.test_post_init_hook_deja_admin_en_grupo_emisor` sin fallos ni errores (Odoo reporta `0 failed, 0 error(s)` para el módulo). El test llama al hook directamente, por eso pasa aunque `-u` no ejecute el hook de instalación.

- [ ] **Step 7: Verificación manual en BD nueva (opcional pero recomendada)**

Levantar el addon en una BD nueva y comprobar que el admin trae el grupo:
```bash
docker compose exec -T db psql -U odoo -d postgres -c "CREATE DATABASE odoo_ne_verify;" 2>/dev/null; \
docker compose exec -T odoo odoo \
  -c /etc/odoo/odoo.conf -d odoo_ne_verify \
  -i l10n_pe_ne_biller \
  --db_host db --db_port 5432 --db_user odoo --db_password odoo \
  --no-http --stop-after-init 2>&1 | tail -5
docker compose exec -T odoo odoo shell \
  -c /etc/odoo/odoo.conf -d odoo_ne_verify \
  --db_host db --db_port 5432 --db_user odoo --db_password odoo \
  --no-http <<'PY'
admin = env.ref('base.user_admin')
print('admin en grupo emisor:', admin.has_group('l10n_pe_ne_biller.group_l10n_pe_ne_emisor'))
PY
```
Expected: `admin en grupo emisor: True`. (Luego se puede borrar `odoo_ne_verify`.)

- [ ] **Step 8: Commit**

```bash
git add addons/l10n_pe_ne_biller/hooks.py \
        addons/l10n_pe_ne_biller/__init__.py \
        addons/l10n_pe_ne_biller/__manifest__.py \
        addons/l10n_pe_ne_biller/tests/test_admin_group.py \
        addons/l10n_pe_ne_biller/tests/__init__.py
git commit -m "feat(l10n_pe_ne_biller): admin en grupo Emisor NE Express al instalar (tenants nuevos)" \
           -m "post_init_hook que añade group_l10n_pe_ne_emisor a base.user_admin en la instalación (install-only, sin backfill a BDs existentes). Idempotente vía (4,id). Sube versión a 19.0.1.3.0." \
           -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Self-Review

**1. Spec coverage:**
- "Añadir el grupo al admin en la instalación" → Task 1, Steps 3-5. ✓
- "post_init_hook (install-only)" → Step 5 registra `post_init_hook`; Step 3 lo implementa. ✓
- "(4, id) idempotente, sin quitar otros grupos" → Step 3. ✓
- "Guardas raise_if_not_found=False" → Step 3. ✓
- "Manifest: registrar hook + versión 19.0.1.2.0 → 19.0.1.3.0" → Step 5. ✓
- "Test tageado que verifica admin en el grupo tras el hook" → Steps 1, 6. ✓
- "Verificación manual en BD nueva" → Step 7. ✓
- "Fuera de alcance: sin backfill, sin tocar provision_tenant ni emisor demo local" → respetado (ningún step los toca). ✓

**2. Placeholder scan:** Sin TBD/TODO; todo el código y comandos están completos. ✓

**3. Type consistency:** `post_init_hook(env)` se define en Step 3 y se consume igual en el test (Step 1) y en el `__init__.py` (Step 4). Grupo `l10n_pe_ne_biller.group_l10n_pe_ne_emisor` y usuario `base.user_admin` usados consistentemente. ✓
