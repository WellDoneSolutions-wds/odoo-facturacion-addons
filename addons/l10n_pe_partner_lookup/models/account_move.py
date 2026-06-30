from odoo import _, models


class AccountMove(models.Model):
    _inherit = 'account.move'

    def action_l10n_pe_open_partner_lookup(self):
        """Abre el asistente de búsqueda de cliente por DNI/RUC."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Buscar cliente por DNI/RUC"),
            'res_model': 'l10n_pe.partner.lookup.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_move_id': self.id},
        }
