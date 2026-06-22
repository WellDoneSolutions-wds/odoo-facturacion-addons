from odoo import fields, models


class AccountJournal(models.Model):
    _inherit = 'account.journal'

    l10n_pe_ne_serie = fields.Char(
        string='Serie SUNAT',
        help="Serie del comprobante para este diario (ej. F001 facturas, B001 boletas). Los moves "
             "del diario toman esta serie por defecto; el correlativo se auto-incrementa del número "
             "del asiento (folio) si no se fija a mano.")
