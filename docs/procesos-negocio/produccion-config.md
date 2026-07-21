# Configuración de producción — endurecimiento del Odoo que sirve `/ne/api`

Hallazgo A19 de la revisión adversarial: en desarrollo corremos Odoo con los defaults, y dos de
ellos son **inaceptables en producción**. Este documento es el checklist para el despliegue; el
ejemplo completo está al final.

## Los dos críticos (sin esto NO se sale a producción)

1. **`list_db = False`** — con el default (`True`), `/web/database/manager` lista y permite
   **crear/duplicar/borrar bases de datos a cualquiera** que conozca la master password (y la
   default es trivial). Apagado, el selector desaparece y el manager exige conocer el nombre
   exacto de la base.

2. **`admin_passwd` fuerte** — es la *master password* que protege las operaciones de base de
   datos (backup/restore/drop **por HTTP**). Genera una larga y aleatoria:

   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(32))"
   ```

   Guárdala en el gestor de secretos del despliegue, no en el repo. Odoo la almacena hasheada
   (pbkdf2) tras el primer arranque si la escribes en claro en el conf — mejor aún: escribe ya
   el hash.

## El resto del checklist

- **`db_filter = ^prod$`** (ajusta al nombre real): una sola base visible/servida. Sin filtro,
  un `Host:` manipulado puede aterrizar en otra base del cluster. (En Odoo 19 el flag CLI es
  `--db-filter`, con guion.)
- **`proxy_mode = True`** y servir SIEMPRE detrás de un reverse proxy con TLS (nginx/caddy).
  El addon emite API keys Bearer: sin TLS viajan en claro. El proxy además debe fijar
  `X-Forwarded-*` (proxy_mode los honra) y limitar el tamaño de request.
- **`workers >= 2`** (multiproceso) + `max_cron_threads = 1`: el modo threaded de desarrollo no
  aísla un request colgado. Con workers, configura `limit_time_cpu`/`limit_time_real` (p. ej.
  60/120) y `limit_memory_soft`/`hard` según la máquina.
- **Postgres NO expuesto**: `db_host` por red interna/socket, puerto 5432 cerrado al exterior,
  usuario de DB sin `CREATEDB` (Odoo solo lo necesita si crea bases — en prod no).
- **`log_level = info`** y logs rotados; nada de `--dev` en producción.
- **Parámetros del sistema (ICP) a revisar en la base de prod**:
  - `l10n_pe_ne_biller.url` → apuntar al micro real (no al mock del e2e).
  - `web.base.url` correcto y `web.base.url.freeze = True` (los PDF/enlaces salen de ahí).
- **Backups**: `pg_dump` diario + copia del filestore (`~/.local/share/Odoo/filestore/<db>`);
  el manager HTTP de backups queda neutralizado por los dos críticos de arriba.
- **Los addons NE** (`l10n_pe_ne_biller`, `l10n_pe_ne_roles`, `l10n_pe_partner_lookup`) van en
  `addons_path` de solo lectura para el usuario del servicio.

## `odoo.conf` de ejemplo (plantilla)

```ini
[options]
; ── los dos críticos (A19) ─────────────────────────────────────────────
list_db = False
admin_passwd = REEMPLAZA_CON_SECRETO_LARGO_O_HASH

; ── superficie y aislamiento ───────────────────────────────────────────
db_filter = ^prod$
db_host = 127.0.0.1
db_user = odoo_prod
db_password = SECRETO_DB
proxy_mode = True

; ── procesos y límites ─────────────────────────────────────────────────
workers = 4
max_cron_threads = 1
limit_time_cpu = 60
limit_time_real = 120
limit_memory_soft = 1342177280
limit_memory_hard = 1610612736

; ── addons y logs ──────────────────────────────────────────────────────
addons_path = /opt/odoo/addons,/opt/ne/addons
log_level = info
logfile = /var/log/odoo/odoo.log
```

> El e2e local (docker compose de `ne-express/apps/web-bff/e2e/stack`) NO usa esta plantilla a
> propósito: allí los defaults abiertos son cómodos y el stack no escucha fuera de localhost.
