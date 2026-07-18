"""Establecimientos anexos del emisor (código SUNAT de 4 dígitos) y direcciones
registradas de los clientes — los 'puntos' del wizard de SUNAT sin tipear a mano."""
from odoo import _, api, fields, models
from odoo.exceptions import UserError


class L10nPeNeEstablecimiento(models.Model):
    _name = 'l10n_pe_ne.establecimiento'
    _description = 'Establecimiento anexo (GRE)'
    _order = 'codigo'

    codigo = fields.Char(required=True, help='Código de establecimiento anexo SUNAT (0000 = domicilio fiscal).')
    ubigeo = fields.Char(required=True)
    direccion = fields.Char(required=True)
    distrito_id = fields.Many2one('l10n_pe.res.city.district', string='Distrito')
    company_id = fields.Many2one('res.company', required=True, index=True,
                                 default=lambda s: s.env.company)

    _codigo_company_uniq = models.Constraint(
        'unique(codigo, company_id)',
        'Ya existe un establecimiento con ese código.',
    )

    def _l10n_pe_ne_dict(self):
        self.ensure_one()
        d = self.distrito_id
        return {'id': self.id, 'codigo': self.codigo, 'ubigeo': self.ubigeo,
                'direccion': self.direccion, 'distritoId': d.id or None,
                'distrito': d.name or '', 'provincia': d.city_id.name or '',
                'departamento': d.city_id.state_id.name or ''}

    @api.model
    def l10n_pe_ne_list(self):
        company = self.env.company
        out = []
        part = company.partner_id
        distrito = part.l10n_pe_district if hasattr(part, 'l10n_pe_district') else False
        if part.street:
            out.append({'id': 0, 'codigo': '0000',
                        'ubigeo': (distrito.code if distrito else '') or '',
                        'direccion': part.street,
                        'distritoId': distrito.id if distrito else None,
                        'distrito': distrito.name if distrito else '',
                        'provincia': distrito.city_id.name if distrito else '',
                        'departamento': distrito.city_id.state_id.name if distrito else ''})
        out += [e._l10n_pe_ne_dict()
                for e in self.search([('company_id', '=', company.id)]) if e.codigo != '0000']
        return out

    @api.model
    def l10n_pe_ne_upsert(self, payload):
        codigo = (payload.get('codigo') or '').strip()
        if codigo == '0000':
            raise UserError(_("El código '0000' es el domicilio fiscal (automático); use otro código."))
        rec = self.search([('codigo', '=', codigo), ('company_id', '=', self.env.company.id)], limit=1)
        vals = {'codigo': codigo, 'direccion': payload.get('direccion') or ''}
        distrito_id = payload.get('distritoId')
        if distrito_id:
            d = self.env['l10n_pe.res.city.district'].browse(int(distrito_id)).exists()
            if not d:
                raise UserError(_("El distrito indicado no existe."))
            vals['distrito_id'] = d.id
            vals['ubigeo'] = d.code or ''
        else:
            # Sin distritoId: comportamiento previo (ubigeo tipeado a mano). Se limpia
            # distrito_id para que ubigeo y distrito no queden desincronizados (el dict
            # reportaría el distrito viejo contra un ubigeo nuevo).
            vals['ubigeo'] = payload.get('ubigeo') or ''
            vals['distrito_id'] = False
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

    # ------------------------------------------------------- direcciones cliente
    def _l10n_pe_ne_direccion_dict(self, partner, tipo, rec_id):
        distrito = partner.l10n_pe_district if hasattr(partner, 'l10n_pe_district') else False
        return {'id': rec_id, 'direccion': partner.street or '',
                'ubigeo': (distrito.code if distrito else '') or '',
                'distritoId': distrito.id if distrito else None,
                'distrito': distrito.name if distrito else '',
                'provincia': distrito.city_id.name if distrito else '',
                'departamento': distrito.city_id.state_id.name if distrito else '',
                'tipo': tipo}

    def _l10n_pe_ne_resolver_distrito(self, distrito_id):
        d = self.env['l10n_pe.res.city.district'].browse(int(distrito_id)).exists()
        if not d:
            raise UserError(_('El distrito indicado no existe.'))
        return d

    def _l10n_pe_ne_aplicar_distrito(self, vals, distrito):
        """Igual que l10n_pe_ne_update_negocio: fijar el distrito sincroniza también
        provincia (city) y departamento (state) para que la dirección quede consistente."""
        vals['l10n_pe_district'] = distrito.id
        if distrito.city_id:
            vals['city'] = distrito.city_id.name
            if distrito.city_id.state_id:
                vals['state_id'] = distrito.city_id.state_id.id
            if distrito.city_id.country_id:
                vals['country_id'] = distrito.city_id.country_id.id

    @api.model
    def l10n_pe_ne_direcciones_partner(self, partner_id):
        """Dirección principal (id 0, solo lectura: se gestiona en la ficha del cliente) +
        hijas (delivery/other) del partner, con distrito/ubigeo si lo tienen."""
        p = self.env['res.partner'].browse(int(partner_id)).exists()
        if not p:
            return []
        out = [self._l10n_pe_ne_direccion_dict(p, 'principal', 0)] if p.street else []
        for h in p.child_ids.filtered(lambda c: c.type in ('delivery', 'other')):
            out.append(self._l10n_pe_ne_direccion_dict(h, h.type, h.id))
        return out

    @api.model
    def l10n_pe_ne_crear_direccion(self, partner_id, payload):
        """Crea una dirección adicional (hija delivery) del cliente, atada a un distrito
        para que el ubigeo de 6 dígitos salga automático."""
        payload = payload or {}
        parent = self.env['res.partner'].browse(int(partner_id or 0)).exists()
        if not parent:
            raise UserError(_('Cliente no encontrado.'))
        direccion = (payload.get('direccion') or '').strip()
        if not direccion:
            raise UserError(_('Indica la dirección.'))
        distrito_id = payload.get('distritoId')
        if not distrito_id:
            raise UserError(_('Indica el distrito.'))
        distrito = self._l10n_pe_ne_resolver_distrito(distrito_id)
        # company_id explícito: sin él el hijo queda company_id=False = visible/editable por
        # TODOS los tenants (la regla multi-company nativa deja pasar los sin compañía). Mismo
        # motivo por el que el partner padre lo fija (ver account_move_biller _quick_partner).
        vals = {'parent_id': parent.id, 'type': 'delivery', 'company_id': self.env.company.id,
                'name': parent.name or _('Dirección'), 'street': direccion}
        self._l10n_pe_ne_aplicar_distrito(vals, distrito)
        child = self.env['res.partner'].create(vals)
        return self._l10n_pe_ne_direccion_dict(child, child.type, child.id)

    def _l10n_pe_ne_direccion_hija(self, partner_id, addr_id):
        """Resuelve una dirección hija SIEMPRE a través de su padre (aislado por compañía por
        la record rule nativa) y filtrando por tipo. Así un tenant no puede editar/archivar una
        dirección de otro por id (los hijos podrían tener company_id=False heredado/antiguo)."""
        parent = self.env['res.partner'].browse(int(partner_id or 0)).exists()
        child = parent and parent.child_ids.filtered(
            lambda c: c.id == int(addr_id or 0) and c.type in ('delivery', 'other'))
        if not child:
            raise UserError(_('Dirección no encontrada.'))
        return child

    @api.model
    def l10n_pe_ne_editar_direccion(self, partner_id, addr_id, payload):
        """Edita una dirección hija existente (dirección y/o distrito). No permite tocar
        la dirección principal (el partner mismo): esa se edita desde la ficha del cliente."""
        payload = payload or {}
        child = self._l10n_pe_ne_direccion_hija(partner_id, addr_id)
        vals = {}
        if 'direccion' in payload:
            direccion = (payload.get('direccion') or '').strip()
            if not direccion:
                raise UserError(_('Indica la dirección.'))
            vals['street'] = direccion
        distrito_id = payload.get('distritoId')
        if distrito_id:
            distrito = self._l10n_pe_ne_resolver_distrito(distrito_id)
            self._l10n_pe_ne_aplicar_distrito(vals, distrito)
        if vals:
            child.write(vals)
        return self._l10n_pe_ne_direccion_dict(child, child.type, child.id)

    @api.model
    def l10n_pe_ne_eliminar_direccion(self, partner_id, addr_id):
        """Archiva (active=False) la dirección hija: unlink está bloqueado por ACL
        para el emisor, así que 'eliminar' aquí siempre es archivar."""
        child = self._l10n_pe_ne_direccion_hija(partner_id, addr_id)
        child.active = False
        return {'ok': True, 'modo': 'eliminado'}
