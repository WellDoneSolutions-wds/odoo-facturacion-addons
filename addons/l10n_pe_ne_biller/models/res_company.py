from odoo import fields, models


class ResCompany(models.Model):
    _inherit = 'res.company'

    l10n_pe_ne_cuenta_detraccion = fields.Char(
        string='Cuenta de detracciones (Banco de la Nación)',
        help="Número de cuenta del Banco de la Nación del emisor donde se depositan las detracciones.")
    l10n_pe_ne_api_key = fields.Char(
        string='API key del facturador', groups='base.group_system', copy=False,
        help="API key de autenticación de este emisor ante el microservicio (header X-Api-Key). "
             "Debe coincidir con la registrada en el servidor para el RUC de la compañía.")
