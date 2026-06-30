# -*- coding: utf-8 -*-
"""Gasto simple (NE Express) — egreso del negocio (efectivo/Yape/banco), estilo POS.

Modelo propio de Odoo: TODA la lógica (CRUD + serialización) vive en el addon; React
solo llama. Aislado por compañía (regla multi-compañía en security). Alimenta la
utilidad neta del dashboard ('Con gastos')."""
import calendar

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class L10nPeNeGasto(models.Model):
    _name = 'l10n_pe_ne.gasto'
    _description = 'Gasto (NE Express)'
    _order = 'fecha desc, id desc'

    fecha = fields.Date(string='Fecha', required=True, default=fields.Date.context_today)
    descripcion = fields.Char(string='Descripción', required=True)
    cuenta = fields.Char(string='Cuenta / Medio', default='Efectivo',
                         help="Medio del egreso: Efectivo, Yape, Plin, BCP, Interbank, etc.")
    monto = fields.Monetary(string='Monto', required=True, currency_field='currency_id')
    currency_id = fields.Many2one('res.currency', required=True,
                                  default=lambda s: s.env.company.currency_id)
    company_id = fields.Many2one('res.company', required=True, index=True,
                                 default=lambda s: s.env.company)

    def _l10n_pe_ne_gasto_dict(self):
        self.ensure_one()
        return {
            'id': self.id,
            'fecha': self.fecha.strftime('%Y-%m-%d') if self.fecha else '',
            'descripcion': self.descripcion or '',
            'cuenta': self.cuenta or '',
            'monto': self.monto or 0.0,
            'moneda': self.currency_id.name or 'PEN',
        }

    @api.model
    def l10n_pe_ne_list_gastos(self, query=None, periodo=None, limit=300):
        """Lista de gastos (opcional por texto o periodo YYYYMM)."""
        domain = []
        if query:
            domain += ['|', ('descripcion', 'ilike', query), ('cuenta', 'ilike', query)]
        if periodo and len(str(periodo)) == 6 and str(periodo).isdigit():
            y, m = int(periodo[:4]), int(periodo[4:6])
            last = calendar.monthrange(y, m)[1]
            domain += [('fecha', '>=', '%04d-%02d-01' % (y, m)),
                       ('fecha', '<=', '%04d-%02d-%02d' % (y, m, last))]
        return [g._l10n_pe_ne_gasto_dict() for g in self.search(domain, limit=limit)]

    @api.model
    def l10n_pe_ne_total_gastos(self, periodo):
        """Total de gastos del periodo YYYYMM (para la utilidad neta del dashboard)."""
        gastos = self.l10n_pe_ne_list_gastos(periodo=periodo, limit=100000)
        return round(sum(g['monto'] for g in gastos), 2)

    @api.model
    def l10n_pe_ne_create_gasto(self, gasto):
        gasto = gasto or {}
        if not (gasto.get('descripcion') or '').strip():
            raise UserError(_("El gasto necesita una descripción."))
        vals = {
            'descripcion': gasto['descripcion'].strip(),
            'monto': float(gasto.get('monto') or 0),
            'cuenta': (gasto.get('cuenta') or 'Efectivo').strip(),
        }
        if gasto.get('fecha'):
            vals['fecha'] = gasto['fecha']
        return self.create(vals)._l10n_pe_ne_gasto_dict()

    @api.model
    def l10n_pe_ne_update_gasto(self, gasto):
        gasto = gasto or {}
        g = self.browse(int(gasto.get('id') or 0)).exists()
        if not g:
            raise UserError(_("Gasto no encontrado."))
        vals = {}
        if gasto.get('descripcion'):
            vals['descripcion'] = gasto['descripcion'].strip()
        if gasto.get('cuenta'):
            vals['cuenta'] = gasto['cuenta'].strip()
        if gasto.get('monto') is not None:
            vals['monto'] = float(gasto.get('monto') or 0)
        if gasto.get('fecha'):
            vals['fecha'] = gasto['fecha']
        if vals:
            g.write(vals)
        return g._l10n_pe_ne_gasto_dict()

    @api.model
    def l10n_pe_ne_delete_gasto(self, rec_id):
        g = self.browse(int(rec_id or 0)).exists()
        if g:
            g.unlink()
        return {'ok': True, 'modo': 'eliminado'}
