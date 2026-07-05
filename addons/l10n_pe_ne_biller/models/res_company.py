from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError


class ResCompany(models.Model):
    _inherit = 'res.company'

    l10n_pe_ne_cuenta_detraccion = fields.Char(
        string='Cuenta de detracciones (Banco de la Nación)',
        help="Número de cuenta del Banco de la Nación del emisor donde se depositan las detracciones.")
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
            company = Company.create(cvals)

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
            emisores = Users.search([('company_id', '=', c.id), ('group_ids', 'in', grp.id)])
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
