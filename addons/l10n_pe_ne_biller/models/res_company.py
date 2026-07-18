import logging

import requests

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

_logger = logging.getLogger(__name__)

# API pública gratuita de tipo de cambio SUNAT (configurable por parámetro del
# sistema). apis.net.pe v1 devuelve {compra, venta, fecha} y exige un Referer.
_TC_API_URL_DEFAULT = "https://api.apis.net.pe/v1/tipo-cambio-sunat"
_TC_API_REFERER_DEFAULT = "https://apis.net.pe/"


class ResCompany(models.Model):
    _inherit = 'res.company'

    # ===================================================== Alta de empresas
    # Modelo multi-DB: 1 empresa = 1 base de datos = 1 RUC. Dentro de una BD-tenant
    # NO se crean res.company adicionales — cada empresa es su propia base, dada de
    # alta por el provisioner. Una 2a empresa creada a mano (Ajustes -> Empresas)
    # nace con un RUC sin certificado ni registro en el biller y no puede facturar.
    # El provisioning sancionado (l10n_pe_ne_provision_tenant, modo multi-RUC) pasa
    # el bypass 'l10n_pe_ne_allow_company_create' por contexto.
    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.context.get('l10n_pe_ne_allow_company_create'):
            raise UserError(_(
                "En esta plataforma cada empresa es su propia base de datos (un RUC "
                "por base). No se crea una empresa adicional aquí: el alta de una "
                "empresa nueva se hace por el aprovisionador de tenants."))
        return super().create(vals_list)

    # =========================================================== Tipo de cambio
    # SUNAT exige declarar el TC 'venta' del día en operaciones en dólares. Se
    # obtiene el TC oficial desde una API pública y se CACHEA en la tabla nativa
    # `res.currency.rate` (así el PLE, la contabilidad y las conversiones a soles
    # salen bien solas). Diseño tolerante a fallos: si no hay internet o la API
    # cae, NUNCA rompe una emisión; degrada al último TC conocido o al manual.
    # `res.currency.rate.rate` = unidades de la divisa por 1 sol = 1/TC (así lo
    # lee el PLE: TC = 1/rate).

    def _l10n_pe_ne_usd(self):
        return self.env.ref("base.USD", raise_if_not_found=False)

    def _l10n_pe_ne_tc_extract(self, data):
        """Saca el TC 'venta' de la respuesta JSON, tolerante a distintas APIs."""
        if isinstance(data, list):
            data = data[0] if data else {}
        if not isinstance(data, dict):
            return None
        for k in ("venta", "precio_venta", "precioVenta", "sell", "sale", "value"):
            v = data.get(k)
            if v not in (None, "", 0):
                try:
                    return float(v)
                except (TypeError, ValueError):
                    continue
        return None

    def _l10n_pe_ne_tc_fetch(self, fecha):
        """GET del TC venta oficial para `fecha`. Devuelve float o None ante
        CUALQUIER fallo (sin URL, timeout, no-200, JSON raro). Nunca lanza."""
        ICP = self.env["ir.config_parameter"].sudo()
        url = (ICP.get_param("l10n_pe_ne.tc_api_url", _TC_API_URL_DEFAULT) or "").strip()
        if not url:
            return None
        token = (ICP.get_param("l10n_pe_ne.tc_api_token") or "").strip()
        referer = (ICP.get_param("l10n_pe_ne.tc_api_referer", _TC_API_REFERER_DEFAULT) or "").strip()
        headers = {"Accept": "application/json"}
        if referer:
            headers["Referer"] = referer
        if token:
            headers["Authorization"] = "Bearer " + token
        fstr = fields.Date.to_date(fecha).strftime("%Y-%m-%d")
        try:
            resp = requests.get(
                url, params={"fecha": fstr, "date": fstr}, headers=headers, timeout=(5, 10)
            )
            if resp.status_code != 200:
                _logger.warning("TC SUNAT: HTTP %s desde %s", resp.status_code, url)
                return None
            tc = self._l10n_pe_ne_tc_extract(resp.json())
            if not tc or tc <= 0:
                _logger.warning("TC SUNAT: respuesta sin 'venta' válido: %s", resp.text[:200])
                return None
            return tc
        except Exception as e:  # noqa: BLE001 — red/JSON: degradar, nunca romper
            _logger.warning("TC SUNAT: fallo al consultar %s (%s)", url, e)
            return None

    def _l10n_pe_ne_tc_store(self, fecha, tc):
        """Cachea el TC en res.currency.rate (USD @ fecha, por compañía raíz)."""
        self.ensure_one()
        usd = self._l10n_pe_ne_usd()
        if not usd or not tc:
            return
        fecha = fields.Date.to_date(fecha)
        Rate = self.env["res.currency.rate"].sudo()
        cid = self.root_id.id
        rec = Rate.search(
            [("currency_id", "=", usd.id), ("name", "=", fecha), ("company_id", "=", cid)],
            limit=1,
        )
        vals = {"rate": 1.0 / tc}
        if rec:
            rec.write(vals)
        else:
            Rate.create({"currency_id": usd.id, "name": fecha, "company_id": cid, **vals})

    def _l10n_pe_ne_tc_last_known(self, fecha):
        """Último TC (soles/dólar) conocido en o antes de `fecha`. Fallback."""
        self.ensure_one()
        usd = self._l10n_pe_ne_usd()
        if not usd:
            return None
        rec = self.env["res.currency.rate"].sudo().search(
            [
                ("currency_id", "=", usd.id),
                ("name", "<=", fields.Date.to_date(fecha)),
                ("company_id", "=", self.root_id.id),
            ],
            order="name desc",
            limit=1,
        )
        return (1.0 / rec.rate) if rec and rec.rate else None

    def _l10n_pe_ne_ensure_tc(self, fecha=None):
        """TC (soles por dólar) para `fecha`, robusto y sin excepciones de red:
        1) si ya está cacheado en res.currency.rate → lo usa (no llama a la API);
        2) si no, consulta la API y lo cachea;
        3) si la API falla → último TC conocido.
        Devuelve {tc, fecha, fuente}. `tc` es None si no hay nada aún."""
        self.ensure_one()
        fecha = fields.Date.to_date(fecha) if fecha else fields.Date.context_today(self)
        out = {"tc": None, "fecha": str(fecha), "fuente": "sin-datos"}
        usd = self._l10n_pe_ne_usd()
        if not usd:
            out["fuente"] = "sin-usd"
            return out
        Rate = self.env["res.currency.rate"].sudo()
        cached = Rate.search(
            [("currency_id", "=", usd.id), ("name", "=", fecha), ("company_id", "=", self.root_id.id)],
            limit=1,
        )
        if cached and cached.rate:
            return {"tc": round(1.0 / cached.rate, 3), "fecha": str(fecha), "fuente": "cache"}
        tc = self._l10n_pe_ne_tc_fetch(fecha)
        if tc:
            self._l10n_pe_ne_tc_store(fecha, tc)
            return {"tc": round(tc, 3), "fecha": str(fecha), "fuente": "sunat"}
        last = self._l10n_pe_ne_tc_last_known(fecha)
        if last:
            return {"tc": round(last, 3), "fecha": str(fecha), "fuente": "ultimo"}
        return out

    def l10n_pe_ne_tipo_cambio(self, fecha=None):
        """API para el front: TC de `fecha` (o de hoy). Ver `_l10n_pe_ne_ensure_tc`."""
        return self._l10n_pe_ne_ensure_tc(fecha)

    def l10n_pe_ne_set_tipo_cambio(self, payload):
        """Carga manual del TC (fallback cuando no hay internet). {fecha?, tc}."""
        payload = payload or {}
        try:
            tc = float(payload.get("tc"))
        except (TypeError, ValueError):
            raise UserError(_("Tipo de cambio inválido."))
        if tc <= 0:
            raise UserError(_("El tipo de cambio debe ser mayor que 0."))
        fecha = fields.Date.to_date(payload.get("fecha")) if payload.get("fecha") else fields.Date.context_today(self)
        self._l10n_pe_ne_tc_store(fecha, tc)
        return {"tc": round(tc, 3), "fecha": str(fecha), "fuente": "manual"}

    @api.model
    def _l10n_pe_ne_cron_tc(self):
        """Cron diario: cachea el TC de hoy para las compañías con moneda PEN.
        Tolerante: si la API falla, no hace nada (la próxima emisión usa el último)."""
        hoy = fields.Date.context_today(self)
        for company in self.env["res.company"].sudo().search([]):
            if company.currency_id.name != "PEN":
                continue
            try:
                company._l10n_pe_ne_ensure_tc(hoy)
            except Exception as e:  # noqa: BLE001
                _logger.warning("TC SUNAT cron: compañía %s falló (%s)", company.id, e)

    l10n_pe_ne_cuenta_detraccion = fields.Char(
        string='Cuenta de detracciones (Banco de la Nación)',
        help="Número de cuenta del Banco de la Nación del emisor donde se depositan las detracciones.")
    l10n_pe_ne_datos_pago = fields.Text(
        string='Datos de pago (cuentas bancarias)',
        help="Cuentas bancarias / CCI del emisor para que el cliente pague. Texto libre "
             "(una cuenta por línea, p.ej. 'BCP Soles 191-1234567-0-01 · CCI 00219100...'). "
             "Se imprime en la cotización.")
    l10n_pe_ne_api_key = fields.Char(
        string='API key del facturador', groups='base.group_system', copy=False,
        help="API key de autenticación de este emisor ante el microservicio (header X-Api-Key). "
             "Debe coincidir con la registrada en el servidor para el RUC de la compañía.")

    @api.model
    def l10n_pe_ne_provision_tenant(self, vals):
        """Da de alta (o actualiza) un EMISOR/tenant: una compañía por RUC + su
        usuario emisor en el grupo de NE Express. Idempotente por RUC y por login.

        Operación de ADMINISTRADOR: el aislamiento de datos entre emisores lo dan
        las reglas multi-compañía nativas; aquí solo se crea la company y el usuario.
        El certificado de firma NO vive en Odoo (lo tiene el ms-ne-biller por RUC);
        aquí se guarda la `apiKey` (X-Api-Key) con la que el emisor se autentica al
        microservicio.

        payload: {ruc, razonSocial, login, password, apiKey?, cuentaDetraccion?, userName?}
        """
        if not self.env.user.has_group('base.group_system'):
            raise AccessError(_("Solo un administrador puede aprovisionar emisores."))
        vals = vals or {}
        ruc = (vals.get('ruc') or '').strip()
        razon = (vals.get('razonSocial') or '').strip()
        login = (vals.get('login') or '').strip()
        password = vals.get('password') or ''
        if not ruc or not razon:
            raise UserError(_("Indica el RUC y la razón social del emisor."))
        if not login:
            raise UserError(_("Indica el usuario (login) del emisor."))

        # --- Compañía por RUC (crear o actualizar config) ---
        Company = self.env['res.company'].sudo()
        company = Company.search([('vat', '=', ruc)], limit=1)
        cvals = {'name': razon, 'vat': ruc}
        if vals.get('apiKey'):
            cvals['l10n_pe_ne_api_key'] = vals['apiKey']
        if vals.get('cuentaDetraccion'):
            cvals['l10n_pe_ne_cuenta_detraccion'] = vals['cuentaDetraccion']
        created_company = not company
        if company:
            company.write(cvals)
        else:
            company = Company.with_context(l10n_pe_ne_allow_company_create=True).create(cvals)

        # --- Usuario emisor (en el grupo NE Express, atado SOLO a su company) ---
        grp = self.env.ref('l10n_pe_ne_biller.group_l10n_pe_ne_emisor')
        Users = self.env['res.users'].sudo()
        user = Users.search([('login', '=', login)], limit=1)
        created_user = not user
        if user:
            uvals = {'company_ids': [(4, company.id)], 'company_id': company.id,
                     'group_ids': [(4, grp.id)]}
            if password:
                uvals['password'] = password
            user.write(uvals)
        else:
            if not password:
                raise UserError(_("Indica una contraseña para el nuevo usuario emisor."))
            user = Users.create({
                'name': (vals.get('userName') or razon).strip(),
                'login': login,
                'password': password,
                'tz': 'America/Lima',
                'company_id': company.id,
                'company_ids': [(6, 0, [company.id])],
                'group_ids': [(4, grp.id)],
            })

        return {
            'companyId': company.id,
            'company': company.name,
            'ruc': company.vat,
            'userId': user.id,
            'login': user.login,
            'createdCompany': created_company,
            'createdUser': created_user,
            'apiKeySet': bool(vals.get('apiKey')),
        }

    @api.model
    def l10n_pe_ne_list_tenants(self, limit=None, offset=None):
        """Lista de emisores/tenants para la pantalla de administración: cada
        compañía con sus usuarios emisores. NO expone la api_key (solo si está
        seteada). Solo para administradores.

        Paginación opt-in: con `offset` devuelve {items, total}; sin él, lista plana."""
        if not self.env.user.has_group('base.group_system'):
            raise AccessError(_("Solo un administrador puede ver los emisores."))
        grp = self.env.ref('l10n_pe_ne_biller.group_l10n_pe_ne_emisor')
        Users = self.env['res.users'].sudo()
        Company = self.env['res.company'].sudo()
        out = []
        for c in Company.search([], order='name', limit=limit, offset=offset or 0):
            # all_group_ids (no group_ids): incluye a quien tiene emisor por IMPLICACIÓN de un
            # rol (cajero/vendedor/… implican emisor). Con group_ids (solo explícitos) un usuario
            # con solo un rol quedaría invisible en el panel de emisores (V6 del pentest).
            emisores = Users.search([('company_id', '=', c.id), ('all_group_ids', 'in', grp.id)])
            out.append({
                'companyId': c.id,
                'company': c.name,
                'ruc': c.vat or '',
                'apiKeySet': bool(c.l10n_pe_ne_api_key),
                'cuentaDetraccion': c.l10n_pe_ne_cuenta_detraccion or '',
                'emisores': [{'id': u.id, 'login': u.login, 'name': u.name} for u in emisores],
            })
        if offset is None:
            return out
        return {"items": out, "total": Company.search_count([])}
