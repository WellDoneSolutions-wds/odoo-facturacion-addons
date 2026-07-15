"""Establecimientos anexos del emisor (código SUNAT de 4 dígitos) y direcciones
registradas de los clientes — los 'puntos' del wizard de SUNAT sin tipear a mano."""
from odoo import api, fields, models


class L10nPeNeEstablecimiento(models.Model):
    _name = 'l10n_pe_ne.establecimiento'
    _description = 'Establecimiento anexo (GRE)'
    _order = 'codigo'

    codigo = fields.Char(required=True, help='Código de establecimiento anexo SUNAT (0000 = domicilio fiscal).')
    ubigeo = fields.Char(required=True)
    direccion = fields.Char(required=True)
    company_id = fields.Many2one('res.company', required=True, index=True,
                                 default=lambda s: s.env.company)

    _codigo_company_uniq = models.Constraint(
        'unique(codigo, company_id)',
        'Ya existe un establecimiento con ese código.',
    )

    def _l10n_pe_ne_dict(self):
        self.ensure_one()
        return {'id': self.id, 'codigo': self.codigo, 'ubigeo': self.ubigeo,
                'direccion': self.direccion}

    @api.model
    def l10n_pe_ne_list(self):
        company = self.env.company
        out = []
        part = company.partner_id
        distrito = part.l10n_pe_district if hasattr(part, 'l10n_pe_district') else False
        if part.street:
            out.append({'id': 0, 'codigo': '0000',
                        'ubigeo': (distrito.code if distrito else '') or '',
                        'direccion': part.street})
        out += [e._l10n_pe_ne_dict()
                for e in self.search([('company_id', '=', company.id)]) if e.codigo != '0000']
        return out

    @api.model
    def l10n_pe_ne_upsert(self, payload):
        codigo = (payload.get('codigo') or '').strip()
        rec = self.search([('codigo', '=', codigo), ('company_id', '=', self.env.company.id)], limit=1)
        vals = {'codigo': codigo, 'ubigeo': payload.get('ubigeo') or '',
                'direccion': payload.get('direccion') or ''}
        if rec:
            rec.write(vals)
        else:
            rec = self.create(dict(vals, company_id=self.env.company.id))
        return rec

    @api.model
    def l10n_pe_ne_delete_establecimiento(self, rec_id):
        e = self.browse(int(rec_id or 0)).exists()
        if e:
            e.unlink()
        return {'ok': True, 'modo': 'eliminado'}

    @api.model
    def l10n_pe_ne_direcciones_partner(self, partner_id):
        """Dirección principal + hijas (delivery/other) del partner, con ubigeo si lo tiene."""
        p = self.env['res.partner'].browse(int(partner_id)).exists()
        if not p:
            return []
        def fila(x, tipo):
            distrito = x.l10n_pe_district if hasattr(x, 'l10n_pe_district') else False
            return {'ubigeo': (distrito.code if distrito else '') or '',
                    'direccion': x.street or '', 'tipo': tipo}
        out = [fila(p, 'principal')] if p.street else []
        for h in p.child_ids.filtered(lambda c: c.type in ('delivery', 'other') and c.street):
            out.append(fila(h, h.type))
        return out
