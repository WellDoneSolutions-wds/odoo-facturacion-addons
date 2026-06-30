from odoo import _, fields, models
from odoo.exceptions import UserError


class L10nPePartnerLookupWizard(models.TransientModel):
    _name = 'l10n_pe.partner.lookup.wizard'
    _description = "Buscar cliente por DNI/RUC"

    move_id = fields.Many2one('account.move', required=True, ondelete='cascade')
    doc_number = fields.Char("DNI / RUC", required=True)
    lookup_state = fields.Selection(
        selection=[
            ('search', "Buscar"),
            ('found', "Encontrado"),
            ('existing', "Ya existe"),
            ('not_found', "No encontrado"),
        ],
        default='search',
    )

    existing_partner_id = fields.Many2one('res.partner', readonly=True)
    preview_name = fields.Char("Nombre / Razón social", readonly=True)
    preview_doc_type = fields.Char("Tipo de documento", readonly=True)
    preview_address = fields.Char("Dirección", readonly=True)
    preview_state = fields.Char("Estado", readonly=True)

    def _reopen(self):
        """Reabre el mismo asistente conservando su estado."""
        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }

    def action_search(self):
        self.ensure_one()
        partner_model = self.env['res.partner']
        number = (self.doc_number or '').strip()
        if not number:
            raise UserError(_("Ingresa un número de documento."))

        existing = partner_model._l10n_pe_find_partner(number)
        if existing:
            self.write({
                'lookup_state': 'existing',
                'existing_partner_id': existing.id,
                'preview_name': existing.name,
            })
            return self._reopen()

        data = partner_model._l10n_pe_query_external_db(number)
        if not data:
            self.lookup_state = 'not_found'
            return self._reopen()

        self.write({
            'lookup_state': 'found',
            'preview_name': data['name'],
            'preview_doc_type': data.get('doc_type'),
            'preview_address': data.get('address'),
            'preview_state': data.get('state'),
        })
        return self._reopen()

    def action_reset(self):
        """Permite hacer una nueva búsqueda sin cerrar el asistente."""
        self.ensure_one()
        self.write({
            'lookup_state': 'search',
            'existing_partner_id': False,
            'preview_name': False,
            'preview_doc_type': False,
            'preview_address': False,
            'preview_state': False,
        })
        return self._reopen()

    def action_confirm(self):
        self.ensure_one()
        partner_model = self.env['res.partner']
        number = (self.doc_number or '').strip()

        if self.lookup_state == 'existing' and self.existing_partner_id:
            partner = self.existing_partner_id
        elif self.lookup_state == 'found':
            data = {
                'doc_number': number,
                'doc_type': self.preview_doc_type,
                'name': self.preview_name,
                'address': self.preview_address,
                'state': self.preview_state,
            }
            partner = partner_model.create(
                partner_model._l10n_pe_prepare_partner_vals(data))
        else:
            raise UserError(_("Primero busca un documento válido."))

        self.move_id.partner_id = partner.id
        return {'type': 'ir.actions.act_window_close'}
