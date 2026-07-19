# -*- coding: utf-8 -*-
"""API HTTP del addon para la app React (NE Express).

Reemplaza al BFF NestJS: React llama directo a Odoo, sin orquestador intermedio.
Toda la lógica de negocio vive en los métodos públicos del modelo
(``account.move`` / ``account.payment``); este controller solo:

  * traduce HTTP ↔ método de modelo (mismas rutas y contratos que tenía el BFF),
  * autentica con IDENTIDAD REAL de Odoo (login por usuario → API key nativa
    ``res.users.apikeys`` con scope propio; el Bearer se valida en cada request),
  * opera como el usuario autenticado (``with_user(uid)``), NO como superusuario,
    de modo que ACLs y reglas multi-compañía (aislamiento por RUC) aplican,
  * resuelve CORS para el origen del SPA.

Rutas bajo ``/ne/api/...`` para que React solo cambie su URL base.
"""

import base64
import datetime
import json
import logging

import psycopg2
from odoo import http
from odoo.exceptions import AccessDenied, AccessError, UserError, ValidationError
from odoo.http import request

_logger = logging.getLogger(__name__)

# Scope PROPIO de las API keys de NE Express. NUNCA None ni 'rpc': así estas keys
# autentican solo este API y NO habilitan XML-RPC/JSON-RPC general de Odoo.
_SCOPE = "l10n_pe_ne"
_KEY_NAME = "NE Express"
# Vida de la API key emitida en el login (re-login al expirar). Configurable en
# caliente vía el param de sistema 'l10n_pe_ne.token_ttl_hours'; este es el default.
_TTL_HOURS_DEFAULT = 12.0

# Códigos pg de concurrencia que el `retrying` de Odoo sabe reintentar: NO los
# tragamos (serialization_failure, deadlock_detected) → se relanzan.
_PG_RETRY = ("40001", "40P01")

# Mensaje amigable (ES) para constraints conocidas; el resto cae al texto pg legible.
_CONSTRAINT_MSGS = {
    "account_move_unique_name_latam": "Ya existe un comprobante con ese número para ese cliente (número duplicado).",
    "l10n_pe_ne_caja_sesion_unica_abierta": "Ya hay una caja abierta para tu negocio. Ciérrala antes de abrir otra.",
}

# Tipo de archivo descargable → (content-type, extensión de archivo).
_FILE_KINDS = {
    "pdf": ("application/pdf", "pdf"),
    "ticket": ("application/pdf", "pdf"),  # ticket 80mm; extensión .pdf
    "xml": ("application/xml", "xml"),
    "cdr": ("application/zip", "zip"),
}

# Decoradores comunes: type=http (JSON crudo, no envoltura JSON-RPC),
# auth=public (la BD se resuelve por dbfilter; la identidad real la da el Bearer),
# cors=* (Odoo resuelve el preflight OPTIONS e incluye Authorization), csrf desactivado.
_GET = dict(
    type="http",
    auth="public",
    methods=["GET"],
    cors="*",
    csrf=False,
    save_session=False,
)
_POST = dict(
    type="http",
    auth="public",
    methods=["POST"],
    cors="*",
    csrf=False,
    save_session=False,
)
_PUT = dict(
    type="http",
    auth="public",
    methods=["PUT"],
    cors="*",
    csrf=False,
    save_session=False,
)
_DEL = dict(
    type="http",
    auth="public",
    methods=["DELETE"],
    cors="*",
    csrf=False,
    save_session=False,
)


class L10nPeNeApi(http.Controller):
    # ------------------------------------------------------------------ utils
    def _bearer(self):
        auth = request.httprequest.headers.get("Authorization", "") or ""
        return auth[7:].strip() if auth[:7].lower() == "bearer " else auth.strip()

    def _identify(self):
        """Valida el Bearer contra una API key nativa de Odoo con nuestro scope.
        Devuelve el uid (int) o None. Lookup timing-safe por índice (raw SQL)."""
        token = self._bearer()
        if not token:
            return None
        return (
            request.env["res.users.apikeys"]
            .sudo()
            ._check_credentials(scope=_SCOPE, key=token)
        )

    def _ttl_hours(self):
        """Vida (horas) de la API key del login, desde el param de sistema
        'l10n_pe_ne.token_ttl_hours' (default 12). Valor inválido/≤0 → default."""
        raw = (
            request.env["ir.config_parameter"]
            .sudo()
            .get_param("l10n_pe_ne.token_ttl_hours", _TTL_HOURS_DEFAULT)
        )
        try:
            h = float(raw)
        except (TypeError, ValueError):
            h = _TTL_HOURS_DEFAULT
        return h if h > 0 else _TTL_HOURS_DEFAULT

    def _user(self, uid):
        return request.env["res.users"].sudo().browse(uid)

    def _puede_anular(self, uid):
        """La baja ante SUNAT (RA/RC) es irreversible y separa del grupo Emisor: un cajero
        factura pero no da de baja. El grupo de anulación IMPLICA el de emisor, así que
        quien lo tiene puede ambas cosas."""
        return self._user(uid).has_group(
            "l10n_pe_ne_biller.group_l10n_pe_ne_anulacion"
        )

    def _move(self, uid):
        u = self._user(uid)
        return request.env["account.move"].with_user(uid).with_company(u.company_id)

    def _payment(self, uid):
        u = self._user(uid)
        return request.env["account.payment"].with_user(uid).with_company(u.company_id)

    def _partner(self, uid):
        u = self._user(uid)
        return request.env['res.partner'].with_user(uid).with_company(u.company_id)

    def _gasto(self, uid):
        u = self._user(uid)
        return request.env["l10n_pe_ne.gasto"].with_user(uid).with_company(u.company_id)

    def _cotizacion(self, uid):
        u = self._user(uid)
        return request.env["l10n_pe_ne.cotizacion"].with_user(uid).with_company(u.company_id)

    def _guia(self, uid):
        u = self._user(uid)
        return request.env["l10n_pe_ne.guia_remision"].with_user(uid).with_company(u.company_id)

    def _flota(self, uid, model):
        u = self._user(uid)
        return request.env[model].with_user(uid).with_company(u.company_id)

    def _estab(self, uid):
        u = self._user(uid)
        return request.env["l10n_pe_ne.establecimiento"].with_user(uid).with_company(u.company_id)

    def _caja(self, uid):
        u = self._user(uid)
        return request.env["l10n_pe_ne.caja.sesion"].with_user(uid).with_company(u.company_id)

    def _lote(self, uid):
        u = self._user(uid)
        return request.env["l10n_pe_ne.lote"].with_user(uid).with_company(u.company_id)

    def _company(self, uid):
        u = self._user(uid)
        return u.company_id.with_user(uid).with_company(u.company_id)

    def _body(self):
        raw = request.httprequest.get_data() or b""
        return json.loads(raw) if raw else {}

    def _page_args(self, kw, default_size=10, max_size=200):
        """Paginación OPT-IN para listados. Devuelve
        {limit, offset, page, pageSize} si el cliente mandó 'page' o 'pageSize';
        None en modo legacy (sin esos params → el modelo devuelve lista plana).
        Clampa pageSize a [1, max_size] y page a >= 1 (una URL manipulada no
        puede pedir páginas gigantes)."""
        raw_size = kw.get("pageSize")
        if raw_size is None:
            raw_size = kw.get("page_size")
        if kw.get("page") is None and raw_size is None:
            return None

        def _int(v, default):
            try:
                return int(v)
            except (TypeError, ValueError):
                return default

        page = max(1, _int(kw.get("page"), 1))
        size = max(1, min(_int(raw_size, default_size), max_size))
        return {"limit": size, "offset": (page - 1) * size, "page": page, "pageSize": size}

    def _json(self, data, status=200):
        return request.make_json_response(data, status=status)

    def _err(self, exc, status=400):
        return request.make_json_response(
            {"message": str(exc) or "Error en Odoo"}, status=status
        )

    def _unauth(self):
        return self._err("No autenticado o sesión expirada", status=401)

    def _run(self, func):
        """Ejecuta una operación que ESCRIBE y devuelve el resultado como JSON.

        Fuerza el flush para que cualquier error diferido (constraints de BD,
        validaciones) aflore AQUÍ y no en el commit final de Odoo —que daría un
        500 HTML imposible de leer para React—. Traduce el error a un JSON 400
        con mensaje legible (vía _fail), dejando la transacción limpia."""
        try:
            res = func()
            request.env.cr.flush()  # surface deferred DB errors within this try
            return self._json(res)
        except Exception as e:  # noqa: BLE001 - el error se reenvía como JSON al cliente
            return self._fail(e)

    def _fail(self, e):
        # Errores de concurrencia: relanzar para que el retrying de Odoo reintente.
        if (
            isinstance(e, psycopg2.OperationalError)
            and getattr(e, "pgcode", None) in _PG_RETRY
        ):
            raise e
        # Revertir para dejar la transacción limpia (si no, el commit final de Odoo
        # volvería a fallar con 500) y responder con un mensaje legible.
        request.env.cr.rollback()
        # Cross-tenant / sin permiso: la regla multi-compañía nativa niega el acceso.
        if isinstance(e, AccessError):
            _logger.info(
                "NE acceso denegado: %s", str(e).splitlines()[0] if str(e) else e
            )
            return self._err(
                "No tienes acceso a este recurso (puede pertenecer a otra empresa).",
                status=403,
            )
        if isinstance(e, (UserError, ValidationError)):
            msg = (e.args[0] if e.args else str(e)) or "Operación no válida"
        elif isinstance(e, psycopg2.IntegrityError):
            diag = getattr(e, "diag", None)
            cname = (getattr(diag, "constraint_name", "") or "").strip()
            if cname in _CONSTRAINT_MSGS:
                msg = _CONSTRAINT_MSGS[cname]
            else:
                primary = (getattr(diag, "message_primary", "") or "").strip()
                detail = (getattr(diag, "message_detail", "") or "").strip()
                msg = "No se pudo guardar: " + (
                    primary or "viola una restricción de la base de datos"
                )
                if detail:
                    msg += " — " + detail
        else:
            msg = str(e) or "Error en Odoo"
        _logger.warning("NE op falló: %s", msg)
        return self._err(msg)

    def _serve_file(self, uid, model, rec_id, kind, method, prefix):
        ct_ext = _FILE_KINDS.get(kind)
        if not ct_ext:
            return self._err("Tipo de archivo no soportado", status=404)
        u = self._user(uid)
        rec = (
            request.env[model]
            .with_user(uid)
            .with_company(u.company_id)
            .browse(int(rec_id))
        )
        files = getattr(rec, method)(kind=kind) or {}
        b64 = files.get(kind)
        if not b64:
            return self._err(f"El {prefix} {rec_id} no tiene {kind}", status=404)
        ct, ext = ct_ext
        return request.make_response(
            base64.b64decode(b64),
            headers=[
                ("Content-Type", ct),
                (
                    "Content-Disposition",
                    f'attachment; filename="{prefix}-{rec_id}.{ext}"',
                ),
            ],
        )

    # -------------------------------------------------------------- auth/sesión
    @http.route("/ne/api/login", **_POST)
    def login(self, **kw):
        """Login por usuario+contraseña de Odoo → mintea una API key nativa (scope
        propio, con expiración) y la devuelve en plano UNA vez. React la usará como
        Bearer. No se crean cookies ni estado de sesión."""
        try:
            body = self._body()
            login = (body.get("login") or "").strip()
            password = body.get("password") or ""
            if not login or not password:
                return self._err("Indica usuario y contraseña", status=400)
            try:
                auth = request.env["res.users"].authenticate(
                    {"type": "password", "login": login, "password": password},
                    {"interactive": False},
                )
            except AccessDenied:
                return self._err("Credenciales inválidas", status=401)
            uid = auth["uid"] if isinstance(auth, dict) else auth
            if not uid:
                return self._err("Credenciales inválidas", status=401)
            user = self._user(uid)
            exp = datetime.datetime.now() + datetime.timedelta(hours=self._ttl_hours())
            # with_user(uid): la key se ata al uid real. sudo(): permite fijar el TTL
            # (is_system por su) sin depender del límite del grupo del usuario.
            token = (
                request.env["res.users.apikeys"]
                .with_user(uid)
                .sudo()
                ._generate(_SCOPE, f"{_KEY_NAME} ({exp:%Y-%m-%d %H:%M})", exp)
            )
            # Perfil desde la fuente única (res.users.l10n_pe_ne_perfil, extendida por el
            # addon de roles) + los campos propios del login (token/expiración).
            return self._json({
                **user.l10n_pe_ne_perfil(),
                "token": token,
                "expires": exp.isoformat(),
            })
        except Exception as e:  # noqa: BLE001
            _logger.exception("NE login error")
            return self._err(e)

    @http.route("/ne/api/whoami", **_GET)
    def whoami(self, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        user = self._user(uid)
        return self._json(user.l10n_pe_ne_perfil())

    @http.route("/ne/api/logout", **_POST)
    def logout(self, **kw):
        """Revoca la API key del Bearer (borra la fila → revocación real)."""
        token = self._bearer()
        if token:
            try:
                request.env["res.users.apikeys"].sudo().revoke(token)
            except Exception:  # noqa: BLE001 - token ya inválido/expirado: idempotente
                pass
        return self._json({"ok": True})

    # ----------------------------------------------------------------- admin
    @http.route("/ne/api/admin/tenants", **_GET)
    def list_tenants(self, **kw):
        """Lista los emisores/tenants (compañías + sus usuarios). Solo admin."""
        uid = self._identify()
        if not uid:
            return self._unauth()
        if not self._user(uid).has_group("base.group_system"):
            return self._err(
                "Solo un administrador puede ver los emisores.", status=403
            )
        try:
            pg = self._page_args(kw)
            res = request.env["res.company"].with_user(uid).l10n_pe_ne_list_tenants(
                limit=pg["limit"] if pg else None,
                offset=pg["offset"] if pg else None,
            )
            if pg:
                res = {**res, "page": pg["page"], "pageSize": pg["pageSize"]}
            return self._json(res)
        except Exception as e:  # noqa: BLE001
            return self._fail(e)

    @http.route("/ne/api/admin/tenants", **_POST)
    def provision_tenant(self, **kw):
        """Aprovisiona un emisor/tenant: compañía por RUC + usuario emisor.
        Solo para administradores (base.group_system); un emisor normal → 403."""
        uid = self._identify()
        if not uid:
            return self._unauth()
        if not self._user(uid).has_group("base.group_system"):
            return self._err(
                "Solo un administrador puede aprovisionar emisores.", status=403
            )
        return self._run(
            lambda: (
                request.env["res.company"]
                .with_user(uid)
                .l10n_pe_ne_provision_tenant(self._body())
            )
        )

    # ------------------------------------------------------------ contraseñas
    @http.route("/ne/api/admin/users", **_GET)
    def list_users(self, **kw):
        """Usuarios internos que el admin puede gestionar (para resetear su clave)."""
        uid = self._identify()
        if not uid:
            return self._unauth()
        if not self._user(uid).has_group("base.group_system"):
            return self._err("Solo un administrador puede ver los usuarios.", status=403)
        try:
            return self._json(
                request.env["res.users"].with_user(uid).l10n_pe_ne_list_manageable_users()
            )
        except Exception as e:  # noqa: BLE001
            return self._fail(e)

    @http.route("/ne/api/admin/users/<int:target_id>/reset-password", **_POST)
    def admin_reset_password(self, target_id, **kw):
        """Un admin resetea la clave de un usuario. Devuelve la clave nueva una vez."""
        uid = self._identify()
        if not uid:
            return self._unauth()
        if not self._user(uid).has_group("base.group_system"):
            return self._err("Solo un administrador puede resetear contraseñas.", status=403)
        password = self._body().get("password") or None
        return self._run(
            lambda: request.env["res.users"]
            .with_user(uid)
            .l10n_pe_ne_admin_reset_password(target_id, new_password=password)
        )

    @http.route("/ne/api/change-password", **_POST)
    def change_password(self, **kw):
        """El usuario logueado cambia su propia contraseña."""
        uid = self._identify()
        if not uid:
            return self._unauth()
        body = self._body()
        return self._run(
            lambda: request.env["res.users"]
            .with_user(uid)
            .l10n_pe_ne_change_own_password(body.get("current") or "", body.get("new") or "")
        )

    # -------------------------------------------------- reset self-service (Fase 2)
    @http.route("/ne/api/reset/request", **_POST)
    def reset_request(self, **kw):
        """Solicita reset por email. Respuesta genérica (sin enumeración)."""
        body = self._body()
        origin = body.get("origin") or request.httprequest.headers.get("Origin") or ""
        return self._run(lambda: request.env["res.users"].l10n_pe_ne_request_password_reset(
            body.get("login") or "", origin))

    @http.route("/ne/api/reset/confirm", **_POST)
    def reset_confirm(self, **kw):
        """Confirma el reset con el token del email + la clave nueva."""
        body = self._body()
        return self._run(lambda: request.env["res.users"].l10n_pe_ne_confirm_password_reset(
            body.get("token") or "", body.get("password") or ""))

    # ---------------------------------------------------------------- config
    @http.route("/ne/api/config", **_GET)
    def config(self, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        try:
            return self._json(self._move(uid).l10n_pe_ne_config())
        except Exception as e:  # noqa: BLE001
            return self._fail(e)

    @http.route("/ne/api/tipo-cambio", **_GET)
    def tipo_cambio(self, fecha=None, **kw):
        """TC SUNAT (venta) para la fecha dada o la de hoy. {tc, fecha, fuente}."""
        uid = self._identify()
        if not uid:
            return self._unauth()
        try:
            return self._json(self._company(uid).l10n_pe_ne_tipo_cambio(fecha))
        except Exception as e:  # noqa: BLE001
            return self._fail(e)

    @http.route("/ne/api/tipo-cambio", **_POST)
    def set_tipo_cambio(self, **kw):
        """Carga manual del TC (fallback sin internet). Body {fecha?, tc}."""
        uid = self._identify()
        if not uid:
            return self._unauth()
        return self._run(lambda: self._company(uid).l10n_pe_ne_set_tipo_cambio(self._body()))

    @http.route("/ne/api/series", **_GET)
    def series(self, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        try:
            pg = self._page_args(kw)
            res = self._move(uid).l10n_pe_ne_series(
                limit=pg["limit"] if pg else None,
                offset=pg["offset"] if pg else None,
            )
            if pg:
                res = {**res, "page": pg["page"], "pageSize": pg["pageSize"]}
            return self._json(res)
        except Exception as e:  # noqa: BLE001
            return self._fail(e)

    # ---------------------------------------------------------- datos negocio
    @http.route("/ne/api/negocio", **_GET)
    def negocio(self, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        try:
            return self._json(self._move(uid).l10n_pe_ne_negocio())
        except Exception as e:  # noqa: BLE001
            return self._fail(e)

    @http.route("/ne/api/negocio", **_PUT)
    def update_negocio(self, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        return self._run(
            lambda: self._move(uid).l10n_pe_ne_update_negocio(self._body())
        )

    @http.route("/ne/api/negocio/logo", **_GET)
    def negocio_logo(self, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        try:
            raw, ct = self._move(uid).l10n_pe_ne_get_logo()
            if not raw:
                return self._err("El negocio no tiene logo", status=404)
            return request.make_response(
                raw, headers=[("Content-Type", ct), ("Cache-Control", "no-store")]
            )
        except Exception as e:  # noqa: BLE001
            return self._fail(e)

    @http.route("/ne/api/distritos", **_GET)
    def distritos(self, q=None, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        try:
            return self._json(self._move(uid).l10n_pe_ne_buscar_distrito(q=q or None))
        except Exception as e:  # noqa: BLE001
            return self._fail(e)

    @http.route("/ne/api/resumen", **_GET)
    def resumen(self, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        try:
            return self._json(self._move(uid).l10n_pe_ne_resumen())
        except Exception as e:  # noqa: BLE001
            return self._fail(e)

    # --------------------------------------------------------------- reportes
    @http.route("/ne/api/reportes/ple-ventas", **_GET)
    def ple_ventas(self, periodo=None, **kw):
        """PLE 14.1 (Registro de Ventas) del periodo YYYYMM. Devuelve
        {filename, contentB64, count, total} — el txt que el contador sube a SUNAT."""
        uid = self._identify()
        if not uid:
            return self._unauth()
        try:
            return self._json(self._move(uid).l10n_pe_ne_ple_ventas(periodo))
        except Exception as e:  # noqa: BLE001
            return self._fail(e)

    @http.route("/ne/api/reportes/ple-compras", **_GET)
    def ple_compras(self, periodo=None, **kw):
        """PLE 8.1 (Registro de Compras) del periodo YYYYMM. Devuelve
        {filename, contentB64, count, total} — el txt que el contador sube a SUNAT.

        ⚠ Estructura pendiente de validación contable (ver la nota en el modelo)."""
        uid = self._identify()
        if not uid:
            return self._unauth()
        try:
            return self._json(self._move(uid).l10n_pe_ne_ple_compras(periodo))
        except Exception as e:  # noqa: BLE001
            return self._fail(e)

    @http.route("/ne/api/reportes/ple-inventario", **_GET)
    def ple_inventario(self, periodo=None, **kw):
        """PLE 12.1 (Inventario Permanente en Unidades Físicas) del periodo YYYYMM.

        ⚠ Estructura pendiente de validación contable (ver la nota en el modelo)."""
        uid = self._identify()
        if not uid:
            return self._unauth()
        try:
            return self._json(self._move(uid).l10n_pe_ne_ple_inventario(periodo))
        except Exception as e:  # noqa: BLE001
            return self._fail(e)

    @http.route("/ne/api/reportes/rvie-reemplazo", **_GET)
    def rvie_reemplazo(self, periodo=None, **kw):
        """SIRE RVIE — archivo de reemplazo de la propuesta (ZIP) del periodo YYYYMM."""
        uid = self._identify()
        if not uid:
            return self._unauth()
        try:
            return self._json(self._move(uid).l10n_pe_ne_rvie_reemplazo(periodo))
        except Exception as e:  # noqa: BLE001
            return self._fail(e)

    @http.route("/ne/api/reportes/dashboard", **_GET)
    def dashboard(self, periodo=None, **kw):
        """Datos del dashboard: serie diaria de ventas + desglose por tipo + KPIs."""
        uid = self._identify()
        if not uid:
            return self._unauth()
        try:
            return self._json(self._move(uid).l10n_pe_ne_dashboard(periodo))
        except Exception as e:  # noqa: BLE001
            return self._fail(e)

    @http.route("/ne/api/reportes/ventas", **_GET)
    def reporte_ventas(self, periodo=None, **kw):
        """Reportes de ventas: resumen, hoy, top por producto y por cliente."""
        uid = self._identify()
        if not uid:
            return self._unauth()
        try:
            return self._json(self._move(uid).l10n_pe_ne_reporte_ventas(periodo))
        except Exception as e:  # noqa: BLE001
            return self._fail(e)

    @http.route("/ne/api/reportes/export", **_GET)
    def export(self, tipo="ventas", periodo=None, **kw):
        """Centro de descargas: exporta ventas|productos|clientes a XLSX (base64)."""
        uid = self._identify()
        if not uid:
            return self._unauth()
        try:
            return self._json(self._move(uid).l10n_pe_ne_export(tipo, periodo))
        except Exception as e:  # noqa: BLE001
            return self._fail(e)

    # ----------------------------------------------------------- comprobantes
    @http.route("/ne/api/comprobantes", **_GET)
    def list_comprobantes(self, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        try:
            pg = self._page_args(kw)
            res = self._move(uid).l10n_pe_ne_quick_list(
                query=kw.get("q") or None,
                desde=kw.get("desde") or None,
                hasta=kw.get("hasta") or None,
                estado=kw.get("estado") or None,
                tipo=kw.get("tipo") or None,
                forma_pago=kw.get("formaPago") or None,
                monto_min=kw.get("montoMin") or None,
                monto_max=kw.get("montoMax") or None,
                serie=kw.get("serie") or None,
                moneda=kw.get("moneda") or None,
                limit=pg["limit"] if pg else 100,
                offset=pg["offset"] if pg else None,
            )
            if pg:
                res = {**res, "page": pg["page"], "pageSize": pg["pageSize"]}
            return self._json(res)
        except Exception as e:  # noqa: BLE001
            return self._fail(e)

    @http.route("/ne/api/comprobantes/<int:rec_id>/detalle", **_GET)
    def comprobante_detalle(self, rec_id, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        try:
            rec = self._move(uid).browse(rec_id).exists()
            if not rec:
                return self._err("Comprobante no encontrado", 404)
            return self._json(rec.l10n_pe_ne_comprobante_detalle())
        except Exception as e:  # noqa: BLE001
            return self._fail(e)

    @http.route("/ne/api/emitir", **_POST)
    def emitir(self, **kw):
        """Emite un comprobante. Rutea por tipoDoc: 20/40 son otro-CPE
        (account.payment, retención/percepción); el resto son account.move."""
        uid = self._identify()
        if not uid:
            return self._unauth()

        def op():
            payload = self._body()
            tipo = payload.get("tipoDoc")
            if tipo == "20":
                return self._payment(uid).l10n_pe_ne_quick_retencion(payload)
            if tipo == "40":
                return self._payment(uid).l10n_pe_ne_quick_percepcion(payload)
            return self._move(uid).l10n_pe_ne_quick_emit(payload)

        return self._run(op)

    @http.route("/ne/api/anular", **_POST)
    def anular(self, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        if not self._puede_anular(uid):
            return self._err(
                "No tienes permiso para anular comprobantes. Pídelo a un administrador.",
                status=403,
            )
        return self._run(lambda: self._move(uid).l10n_pe_ne_quick_anular(self._body()))

    @http.route("/ne/api/comprobantes/<int:rec_id>/<string:kind>", **_GET)
    def file(self, rec_id, kind, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        try:
            return self._serve_file(
                uid, "account.move", rec_id, kind, "l10n_pe_ne_get_files", "comprobante"
            )
        except Exception as e:  # noqa: BLE001
            return self._fail(e)

    @http.route("/ne/api/comprobantes/<int:rec_id>/email", **_POST)
    def email_comprobante(self, rec_id, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()

        def op():
            b = self._body()
            return self._move(uid).browse(rec_id).l10n_pe_ne_email_comprobante(
                to=b.get("to"), cc=b.get("cc")
            )

        return self._run(op)

    @http.route("/ne/api/comprobantes/<int:rec_id>/reenviar", **_POST)
    def reenviar_comprobante(self, rec_id, **kw):
        """Reenvía a SUNAT un comprobante pendiente (por_enviar) o que quedó en
        error/rechazado. Reutiliza el mismo move (misma serie-correlativo)."""
        uid = self._identify()
        if not uid:
            return self._unauth()

        def op():
            move = self._move(uid).browse(rec_id)
            move.action_l10n_pe_send_to_biller()
            return move.l10n_pe_ne_quick_result()

        return self._run(op)

    @http.route("/ne/api/otrocpe/<int:rec_id>/<string:kind>", **_GET)
    def otrocpe_file(self, rec_id, kind, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        try:
            return self._serve_file(
                uid, "account.payment", rec_id, kind, "l10n_pe_ne_get_files", "otrocpe"
            )
        except Exception as e:  # noqa: BLE001
            return self._fail(e)

    @http.route("/ne/api/anulacion/<int:rec_id>/cdr", **_GET)
    def anulacion_cdr(self, rec_id, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        try:
            return self._serve_file(
                uid,
                "account.move",
                rec_id,
                "cdr",
                "l10n_pe_ne_get_baja_files",
                "anulacion",
            )
        except Exception as e:  # noqa: BLE001
            return self._fail(e)

    # --------------------------------------------------------------- clientes
    @http.route("/ne/api/clientes", **_GET)
    def list_clientes(self, q=None, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        try:
            pg = self._page_args(kw)
            res = self._move(uid).l10n_pe_ne_list_clientes(
                query=q or None,
                limit=pg["limit"] if pg else 50,
                offset=pg["offset"] if pg else None,
            )
            if pg:
                res = {**res, "page": pg["page"], "pageSize": pg["pageSize"]}
            return self._json(res)
        except Exception as e:  # noqa: BLE001
            return self._fail(e)

    @http.route("/ne/api/clientes", **_POST)
    def create_cliente(self, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        return self._run(
            lambda: self._move(uid).l10n_pe_ne_create_cliente(self._body())
        )

    @http.route("/ne/api/clientes/<int:rec_id>", **_PUT)
    def update_cliente(self, rec_id, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        return self._run(
            lambda: self._move(uid).l10n_pe_ne_update_cliente(
                dict(self._body(), id=int(rec_id))
            )
        )

    @http.route("/ne/api/clientes/<int:rec_id>", **_DEL)
    def delete_cliente(self, rec_id, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        return self._run(lambda: self._move(uid).l10n_pe_ne_delete_cliente(int(rec_id)))

    @http.route("/ne/api/clientes/<int:partner_id>/direcciones", **_GET)
    def cliente_direcciones(self, partner_id, **kw):
        """Direcciones registradas del cliente (principal + hijas delivery/other)
        para poblar el punto de llegada del wizard de la GRE sin tipear a mano."""
        uid = self._identify()
        if not uid:
            return self._unauth()
        try:
            return self._json(self._estab(uid).l10n_pe_ne_direcciones_partner(partner_id))
        except Exception as e:  # noqa: BLE001
            return self._fail(e)

    @http.route("/ne/api/clientes/<int:partner_id>/direcciones", **_POST)
    def crear_direccion_cliente(self, partner_id, **kw):
        """Crea una dirección adicional (hija) del cliente, atada a un distrito."""
        uid = self._identify()
        if not uid:
            return self._unauth()
        return self._run(
            lambda: self._estab(uid).l10n_pe_ne_crear_direccion(partner_id, self._body())
        )

    @http.route("/ne/api/clientes/<int:partner_id>/direcciones/<int:addr_id>", **_PUT)
    def editar_direccion_cliente(self, partner_id, addr_id, **kw):
        """Edita una dirección (hija) existente del cliente."""
        uid = self._identify()
        if not uid:
            return self._unauth()
        return self._run(
            lambda: self._estab(uid).l10n_pe_ne_editar_direccion(partner_id, addr_id, self._body())
        )

    @http.route("/ne/api/clientes/<int:partner_id>/direcciones/<int:addr_id>", **_DEL)
    def eliminar_direccion_cliente(self, partner_id, addr_id, **kw):
        """Elimina (archiva) una dirección (hija) del cliente."""
        uid = self._identify()
        if not uid:
            return self._unauth()
        return self._run(
            lambda: self._estab(uid).l10n_pe_ne_eliminar_direccion(partner_id, addr_id)
        )

    # ------------------------------------------------ clientes: lookup externo
    # Reutiliza el motor del addon l10n_pe_partner_lookup (búsqueda por DNI/RUC en
    # API HTTP / DynamoDB / SUNAT) SIN duplicarlo. Integración OPCIONAL: si ese
    # addon no está instalado, los métodos no existen en res.partner → degradamos
    # (GET → [], POST → 501) para no acoplar duro l10n_pe_ne_biller con él.
    @http.route('/ne/api/clientes/lookup', **_GET)
    def lookup_cliente(self, doc=None, **kw):
        """Sugerencia en vivo mientras se tipea: si 'doc' es un DNI(8)/RUC(11) que
        aún NO existe en Odoo, consulta la fuente externa. Devuelve
        [{doc_number, name, label}] o [] (no aplica / no hay addon / error suave)."""
        uid = self._identify()
        if not uid:
            return self._unauth()
        partners = self._partner(uid)
        if not hasattr(partners, 'l10n_pe_get_field_suggestions'):
            return self._json([])
        try:
            return self._json(partners.l10n_pe_get_field_suggestions(doc or ''))
        except Exception as e:  # noqa: BLE001
            return self._fail(e)

    @http.route('/ne/api/clientes/lookup', **_POST)
    def create_cliente_lookup(self, **kw):
        """Trae el documento de la fuente externa y crea (o reusa) el cliente.
        Devuelve el cliente en el MISMO shape que GET /ne/api/clientes."""
        uid = self._identify()
        if not uid:
            return self._unauth()
        partners = self._partner(uid)
        if not hasattr(partners, 'l10n_pe_create_partner_from_document'):
            return self._err('La búsqueda por DNI/RUC no está disponible '
                             '(instala el addon l10n_pe_partner_lookup).', status=501)

        def _do():
            doc = (self._body().get('doc') or '').strip()
            if not doc:
                raise UserError('Indica el documento (DNI/RUC).')
            res = partners.l10n_pe_create_partner_from_document(doc)
            if not res:
                raise UserError('No se encontró el documento %s en la fuente externa.' % doc)
            partner = self._partner(uid).browse(res['id'])
            if not partner.customer_rank:
                partner.customer_rank = 1  # que aparezca luego en el buscador local
            return self._move(uid)._l10n_pe_ne_partner_dict(partner)
        return self._run(_do)

    @http.route('/ne/api/clientes/datos', **_GET)
    def lookup_cliente_datos(self, doc=None, **kw):
        """Datos del documento para AUTOCOMPLETAR el formulario 'Nuevo cliente' SIN
        crear el partner (la creación ocurre luego con POST /ne/api/clientes).
        Devuelve {} si no aplica / no está el addon / no se encuentra."""
        uid = self._identify()
        if not uid:
            return self._unauth()
        partners = self._partner(uid)
        if not hasattr(partners, 'l10n_pe_lookup_partner_data'):
            return self._json({})
        try:
            return self._json(partners.l10n_pe_lookup_partner_data(doc or ''))
        except Exception as e:  # noqa: BLE001
            return self._fail(e)

    # -------------------------------------------------------------- productos
    @http.route("/ne/api/productos", **_GET)
    def list_productos(self, q=None, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        try:
            pg = self._page_args(kw)
            res = self._move(uid).l10n_pe_ne_list_productos(
                query=q or None,
                limit=pg["limit"] if pg else 50,
                offset=pg["offset"] if pg else None,
            )
            if pg:
                res = {**res, "page": pg["page"], "pageSize": pg["pageSize"]}
            return self._json(res)
        except Exception as e:  # noqa: BLE001
            return self._fail(e)

    @http.route("/ne/api/productos/barcode/<string:code>", **_GET)
    def producto_barcode(self, code, **kw):
        """Resuelve un producto por código de barras escaneado (POS). 404 si no hay match."""
        uid = self._identify()
        if not uid:
            return self._unauth()
        try:
            prod = self._move(uid).l10n_pe_ne_producto_por_barcode(code)
            if not prod:
                return self._err("Producto no encontrado para ese código", 404)
            return self._json(prod)
        except Exception as e:  # noqa: BLE001
            return self._fail(e)

    @http.route("/ne/api/productos", **_POST)
    def create_producto(self, **kw):
        _logger.info("create_producto: %s", self._identify())
        uid = self._identify()
        if not uid:
            return self._unauth()
        return self._run(
            lambda: self._move(uid).l10n_pe_ne_create_producto(self._body())
        )

    @http.route("/ne/api/negocio/margen", **_GET)
    def negocio_margen(self, **kw):
        """Margen de venta por defecto del negocio (%), para precargar el cálculo del precio
        cuando se crea un producto desde una compra."""
        uid = self._identify()
        if not uid:
            return self._unauth()
        return self._json({"margen": self._move(uid)._l10n_pe_ne_margen_default()})

    @http.route("/ne/api/productos/revisar-tipos", **_GET)
    def revisar_tipos(self, **kw):
        """Productos que quedaron como servicio por el default viejo y parecen bienes.
        PROPONE: no cambia nada. Aplicar es otra llamada, con los ids que el usuario elija."""
        uid = self._identify()
        if not uid:
            return self._unauth()
        return self._run(lambda: self._move(uid).l10n_pe_ne_revisar_tipos(self._body() if False else None))

    @http.route("/ne/api/productos/aplicar-tipos", **_POST)
    def aplicar_tipos(self, **kw):
        """Reclasifica SOLO los ids confirmados por el usuario."""
        uid = self._identify()
        if not uid:
            return self._unauth()
        return self._run(lambda: self._move(uid).l10n_pe_ne_aplicar_tipos(self._body()))

    @http.route("/ne/api/productos/plantilla", **_GET)
    def productos_plantilla(self, **kw):
        """Descarga la plantilla xlsx para importar productos."""
        uid = self._identify()
        if not uid:
            return self._unauth()
        return self._run(lambda: self._move(uid).l10n_pe_ne_plantilla_productos())

    @http.route("/ne/api/productos/importar", **_POST)
    def productos_importar(self, **kw):
        """Importa/actualiza productos desde el xlsx. Body {contentB64, commit}."""
        uid = self._identify()
        if not uid:
            return self._unauth()
        return self._run(lambda: self._move(uid).l10n_pe_ne_importar_productos(self._body()))

    @http.route("/ne/api/productos/<int:rec_id>", **_PUT)
    def update_producto(self, rec_id, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        return self._run(
            lambda: self._move(uid).l10n_pe_ne_update_producto(
                dict(self._body(), id=int(rec_id))
            )
        )

    @http.route("/ne/api/productos/<int:rec_id>", **_DEL)
    def delete_producto(self, rec_id, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        return self._run(
            lambda: self._move(uid).l10n_pe_ne_delete_producto(int(rec_id))
        )

    # ----------------------------------------------------------------- gastos
    @http.route("/ne/api/gastos", **_GET)
    def list_gastos(self, q=None, periodo=None, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        try:
            pg = self._page_args(kw)
            res = self._gasto(uid).l10n_pe_ne_list_gastos(
                query=q or None,
                periodo=periodo or None,
                limit=pg["limit"] if pg else 300,
                offset=pg["offset"] if pg else None,
            )
            if pg:
                res = {**res, "page": pg["page"], "pageSize": pg["pageSize"]}
            return self._json(res)
        except Exception as e:  # noqa: BLE001
            return self._fail(e)

    @http.route("/ne/api/gastos", **_POST)
    def create_gasto(self, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        return self._run(lambda: self._gasto(uid).l10n_pe_ne_create_gasto(self._body()))

    @http.route("/ne/api/gastos/<int:rec_id>", **_PUT)
    def update_gasto(self, rec_id, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        return self._run(
            lambda: self._gasto(uid).l10n_pe_ne_update_gasto(
                dict(self._body(), id=int(rec_id))
            )
        )

    @http.route("/ne/api/gastos/<int:rec_id>", **_DEL)
    def delete_gasto(self, rec_id, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        return self._run(lambda: self._gasto(uid).l10n_pe_ne_delete_gasto(int(rec_id)))

    @http.route("/ne/api/gastos/<int:rec_id>/reversar", **_POST)
    def reversar_gasto(self, rec_id, **kw):
        # D-2 (integridad): el gasto es append-only; corregir = contra-asiento.
        uid = self._identify()
        if not uid:
            return self._unauth()
        return self._run(
            lambda: self._gasto(uid).l10n_pe_ne_reversar_gasto(
                int(rec_id), (self._body() or {}).get("motivo")))

    # ----------------------------------------------------------------- compras
    @http.route("/ne/api/compras", **_GET)
    def list_compras(self, q=None, periodo=None, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        try:
            pg = self._page_args(kw)
            res = self._move(uid).l10n_pe_ne_list_compras(
                query=q or None,
                periodo=periodo or None,
                limit=pg["limit"] if pg else 200,
                offset=pg["offset"] if pg else None,
            )
            if pg:
                res = {**res, "page": pg["page"], "pageSize": pg["pageSize"]}
            return self._json(res)
        except Exception as e:  # noqa: BLE001
            return self._fail(e)

    @http.route("/ne/api/compras", **_POST)
    def create_compra(self, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        return self._run(lambda: self._move(uid).l10n_pe_ne_create_compra(self._body()))

    @http.route("/ne/api/compras/importar-xml", **_POST)
    def importar_compra_xml(self, **kw):
        """Lee el XML de la factura del proveedor y devuelve el payload de la compra para que
        el usuario lo revise. NO registra: el mapeo de productos necesita a un humano."""
        uid = self._identify()
        if not uid:
            return self._unauth()
        return self._run(
            lambda: self._move(uid).l10n_pe_ne_importar_compra_xml(self._body())
        )

    @http.route("/ne/api/compras/<int:rec_id>", **_PUT)
    def update_compra(self, rec_id, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        return self._run(
            lambda: self._move(uid).l10n_pe_ne_update_compra(int(rec_id), self._body())
        )

    @http.route("/ne/api/compras/<int:rec_id>", **_DEL)
    def delete_compra(self, rec_id, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        return self._run(lambda: self._move(uid).l10n_pe_ne_delete_compra(int(rec_id)))

    # ------------------------------------------------------------- cotizaciones
    # Documento comercial (proforma), NO comprobante SUNAT. Modelo l10n_pe_ne.cotizacion.
    @http.route("/ne/api/cotizaciones", **_GET)
    def list_cotizaciones(self, q=None, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        try:
            pg = self._page_args(kw)
            res = self._cotizacion(uid).l10n_pe_ne_list_cotizaciones(
                query=q or None,
                limit=pg["limit"] if pg else 100,
                offset=pg["offset"] if pg else None,
            )
            if pg:
                res = {**res, "page": pg["page"], "pageSize": pg["pageSize"]}
            return self._json(res)
        except Exception as e:  # noqa: BLE001
            return self._fail(e)

    @http.route("/ne/api/cotizaciones", **_POST)
    def create_cotizacion(self, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        return self._run(
            lambda: self._cotizacion(uid).l10n_pe_ne_quick_cotizar(self._body())
        )

    @http.route("/ne/api/cotizaciones/<int:rec_id>", **_PUT)
    def update_cotizacion(self, rec_id, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        return self._run(
            lambda: self._cotizacion(uid).l10n_pe_ne_update_cotizacion(
                dict(self._body(), id=int(rec_id))
            )
        )

    @http.route("/ne/api/cotizaciones/<int:rec_id>", **_DEL)
    def delete_cotizacion(self, rec_id, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        return self._run(
            lambda: self._cotizacion(uid).l10n_pe_ne_delete_cotizacion(int(rec_id))
        )

    # -------------------------------------------------------- guías de remisión
    # GRE Remitente (tipo 09). Documento de traslado, NO es un comprobante account.move.
    @http.route("/ne/api/guias", **_GET)
    def list_guias(self, q=None, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        try:
            pg = self._page_args(kw)
            res = self._guia(uid).l10n_pe_ne_list_guias(
                query=q or None,
                limit=pg["limit"] if pg else 100,
                offset=pg["offset"] if pg else None,
            )
            if pg:
                res = {**res, "page": pg["page"], "pageSize": pg["pageSize"]}
            return self._json(res)
        except Exception as e:  # noqa: BLE001
            return self._fail(e)

    @http.route("/ne/api/guias", **_POST)
    def create_guia(self, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        return self._run(lambda: self._guia(uid).l10n_pe_ne_quick_guia(self._body()))

    @http.route("/ne/api/guias/<int:rec_id>", **_PUT)
    def update_guia(self, rec_id, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        return self._run(
            lambda: self._guia(uid).l10n_pe_ne_update_guia(dict(self._body(), id=int(rec_id)))
        )

    @http.route("/ne/api/guias/<int:rec_id>", **_DEL)
    def delete_guia(self, rec_id, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        return self._run(lambda: self._guia(uid).l10n_pe_ne_delete_guia(int(rec_id)))

    @http.route("/ne/api/guias/<int:rec_id>/detalle", **_GET)
    def guia_detalle(self, rec_id, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        try:
            g = self._guia(uid).browse(rec_id).exists()
            if not g:
                return self._err("Guía no encontrada", status=404)
            return self._json(g.l10n_pe_ne_guia_detalle())
        except Exception as e:  # noqa: BLE001
            return self._fail(e)

    @http.route("/ne/api/guias/<int:rec_id>/emitir", **_POST)
    def emitir_guia(self, rec_id, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        return self._run(lambda: self._guia(uid).browse(int(rec_id)).l10n_pe_ne_emitir_guia())

    @http.route("/ne/api/guias/<int:rec_id>/consultar", **_POST)
    def consultar_guia(self, rec_id, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        return self._run(
            lambda: self._guia(uid).browse(int(rec_id)).l10n_pe_ne_consultar_ticket()
        )

    @http.route("/ne/api/comprobantes/<int:rec_id>/guia-prefill", **_GET)
    def guia_prefill(self, rec_id, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        try:
            return self._json(self._guia(uid).l10n_pe_ne_guia_prefill(int(rec_id)))
        except Exception as e:  # noqa: BLE001
            return self._fail(e)

    @http.route("/ne/api/guias/<int:rec_id>/<string:kind>", **_GET)
    def guia_file(self, rec_id, kind, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        try:
            return self._serve_file(
                uid, "l10n_pe_ne.guia_remision", rec_id, kind, "l10n_pe_ne_get_files", "guia"
            )
        except Exception as e:  # noqa: BLE001
            return self._fail(e)

    # ------------------------------------------------- maestros frecuentes (GRE)
    # Vehículos y conductores 'frecuentes' que el wizard de guías reutiliza al
    # emitir (mismo criterio que el wizard de SUNAT: se guardan al usarlos).
    @http.route("/ne/api/vehiculos", **_GET)
    def list_vehiculos(self, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        try:
            return self._json(self._flota(uid, "l10n_pe_ne.vehiculo").l10n_pe_ne_list())
        except Exception as e:  # noqa: BLE001
            return self._fail(e)

    @http.route("/ne/api/vehiculos", **_POST)
    def upsert_vehiculo(self, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        return self._run(
            lambda: self._flota(uid, "l10n_pe_ne.vehiculo")
            .l10n_pe_ne_upsert(self._body())
            ._l10n_pe_ne_dict()
        )

    @http.route("/ne/api/vehiculos/<int:rec_id>", **_DEL)
    def delete_vehiculo(self, rec_id, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        return self._run(
            lambda: self._flota(uid, "l10n_pe_ne.vehiculo")
            .l10n_pe_ne_delete_vehiculo(rec_id)
        )

    @http.route("/ne/api/conductores", **_GET)
    def list_conductores(self, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        try:
            return self._json(self._flota(uid, "l10n_pe_ne.conductor").l10n_pe_ne_list())
        except Exception as e:  # noqa: BLE001
            return self._fail(e)

    @http.route("/ne/api/conductores", **_POST)
    def upsert_conductor(self, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        return self._run(
            lambda: self._flota(uid, "l10n_pe_ne.conductor")
            .l10n_pe_ne_upsert(self._body())
            ._l10n_pe_ne_dict()
        )

    @http.route("/ne/api/conductores/<int:rec_id>", **_DEL)
    def delete_conductor(self, rec_id, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        return self._run(
            lambda: self._flota(uid, "l10n_pe_ne.conductor")
            .l10n_pe_ne_delete_conductor(rec_id)
        )

    # Establecimientos anexos del emisor: SIEMPRE incluye primero el domicilio
    # fiscal (código '0000') derivado de la compañía, luego los propios creados.
    @http.route("/ne/api/establecimientos", **_GET)
    def list_establecimientos(self, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        try:
            return self._json(self._estab(uid).l10n_pe_ne_list())
        except Exception as e:  # noqa: BLE001
            return self._fail(e)

    @http.route("/ne/api/establecimientos", **_POST)
    def upsert_establecimiento(self, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        return self._run(
            lambda: self._estab(uid).l10n_pe_ne_upsert(self._body())._l10n_pe_ne_dict()
        )

    @http.route("/ne/api/establecimientos/<int:rec_id>", **_DEL)
    def delete_establecimiento(self, rec_id, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        return self._run(
            lambda: self._estab(uid).l10n_pe_ne_delete_establecimiento(rec_id)
        )

    # ------------------------------------------------------------------- caja
    @http.route("/ne/api/caja", **_GET)
    def caja_actual(self, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        try:
            return self._json(self._caja(uid).l10n_pe_ne_caja_actual())
        except Exception as e:  # noqa: BLE001
            return self._fail(e)

    @http.route("/ne/api/caja/abrir", **_POST)
    def caja_abrir(self, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        return self._run(lambda: self._caja(uid).l10n_pe_ne_abrir_caja(self._body()))

    @http.route("/ne/api/caja/movimientos", **_POST)
    def caja_movimiento(self, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        return self._run(lambda: self._caja(uid).l10n_pe_ne_caja_movimiento(self._body()))

    @http.route("/ne/api/caja/cerrar", **_POST)
    def caja_cerrar(self, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        return self._run(lambda: self._caja(uid).l10n_pe_ne_cerrar_caja(self._body()))

    @http.route("/ne/api/caja/historial", **_GET)
    def caja_historial(self, periodo=None, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        try:
            return self._json(self._caja(uid).l10n_pe_ne_list_cajas(periodo=periodo or None))
        except Exception as e:  # noqa: BLE001
            return self._fail(e)

    @http.route("/ne/api/caja/<int:rec_id>/arqueo", **_GET)
    def caja_arqueo(self, rec_id, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        try:
            rec = self._caja(uid).browse(int(rec_id)).exists()
            if not rec:
                return self._err("Sesión de caja no encontrada", 404)
            # Cross-tenant: leer campos dispara AccessError (ir.rule) -> _fail -> 403.
            return self._json(rec._l10n_pe_ne_arqueo_dict())
        except Exception as e:  # noqa: BLE001
            return self._fail(e)

    # ----------------------------------------------------- Emisión masiva (lotes)
    @http.route("/ne/api/lotes", **_GET)
    def list_lotes(self, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        try:
            return self._json(self._lote(uid).l10n_pe_ne_list_lotes())
        except Exception as e:  # noqa: BLE001
            return self._fail(e)

    @http.route("/ne/api/lotes", **_POST)
    def create_lote(self, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        return self._run(lambda: self._lote(uid).l10n_pe_ne_crear_lote(self._body()))

    @http.route("/ne/api/lotes/plantilla", **_GET)
    def lote_plantilla(self, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        try:
            return self._json(self._lote(uid).l10n_pe_ne_plantilla())
        except Exception as e:  # noqa: BLE001
            return self._fail(e)

    @http.route("/ne/api/lotes/<int:rec_id>", **_GET)
    def lote_detalle(self, rec_id, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        rec = self._lote(uid).browse(int(rec_id))
        if not rec.exists():
            return self._err("El lote %s no existe" % rec_id, status=404)
        try:
            return self._json(rec.l10n_pe_ne_lote_detalle())
        except Exception as e:  # noqa: BLE001
            return self._fail(e)

    @http.route("/ne/api/lotes/<int:rec_id>/procesar", **_POST)
    def lote_procesar(self, rec_id, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        rec = self._lote(uid).browse(int(rec_id))
        if not rec.exists():
            return self._err("El lote %s no existe" % rec_id, status=404)
        body = self._body()
        return self._run(lambda: rec.l10n_pe_ne_procesar(max_filas=int(body.get("max") or 1)))

    @http.route("/ne/api/lotes/<int:rec_id>/reintentar", **_POST)
    def lote_reintentar(self, rec_id, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        rec = self._lote(uid).browse(int(rec_id))
        if not rec.exists():
            return self._err("El lote %s no existe" % rec_id, status=404)
        return self._run(lambda: rec.l10n_pe_ne_reintentar())

    @http.route("/ne/api/lotes/<int:rec_id>/cancelar", **_POST)
    def lote_cancelar(self, rec_id, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        rec = self._lote(uid).browse(int(rec_id))
        if not rec.exists():
            return self._err("El lote %s no existe" % rec_id, status=404)
        return self._run(lambda: rec.l10n_pe_ne_cancelar())

    @http.route("/ne/api/lotes/<int:rec_id>/resultados", **_GET)
    def lote_resultados(self, rec_id, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        rec = self._lote(uid).browse(int(rec_id))
        if not rec.exists():
            return self._err("El lote %s no existe" % rec_id, status=404)
        try:
            return self._json(rec.l10n_pe_ne_resultados())
        except Exception as e:  # noqa: BLE001
            return self._fail(e)

    

    

    @http.route("/ne/api/cotizaciones/<int:rec_id>/detalle", **_GET)
    def cotizacion_detalle(self, rec_id, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        try:
            cot = self._cotizacion(uid).browse(rec_id).exists()
            if not cot:
                return self._err("Cotización no encontrada", 404)
            return self._json(cot.l10n_pe_ne_cotizacion_detalle())
        except Exception as e:  # noqa: BLE001
            return self._fail(e)

    @http.route("/ne/api/cotizaciones/<int:rec_id>/estado", **_POST)
    def cotizacion_estado(self, rec_id, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()

        def op():
            cot = self._cotizacion(uid).browse(int(rec_id)).exists()
            if not cot:
                raise UserError("Cotización no encontrada.")
            return cot.l10n_pe_ne_set_estado((self._body() or {}).get("estado") or "")

        return self._run(op)

    @http.route("/ne/api/cotizaciones/<int:rec_id>/pdf", **_GET)
    def cotizacion_pdf(self, rec_id, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        try:
            cot = self._cotizacion(uid).browse(rec_id).exists()
            if not cot:
                return self._err("Cotización no encontrada", 404)
            pdf, _ctype = cot.env["ir.actions.report"]._render_qweb_pdf(
                "l10n_pe_ne_biller.action_report_cotizacion", res_ids=cot.ids
            )
            return request.make_response(
                pdf,
                headers=[
                    ("Content-Type", "application/pdf"),
                    (
                        "Content-Disposition",
                        'inline; filename="%s.pdf"' % (cot.name or "cotizacion"),
                    ),
                ],
            )
        except Exception as e:  # noqa: BLE001
            return self._fail(e)
