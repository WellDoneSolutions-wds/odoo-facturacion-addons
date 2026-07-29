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
    # D-3 (integridad): quién lo registró. Se puebla solo al crear; los gastos históricos
    # caen al create_uid en el dict (ver _l10n_pe_ne_gasto_dict).
    usuario_id = fields.Many2one('res.users', string='Usuario', index=True,
                                 default=lambda s: s.env.user)
    # D-2 (integridad): el gasto es APPEND-ONLY. Corregir = registrar un contra-asiento
    # (un gasto en negativo que apunta al original vía este campo), nunca editar/borrar.
    gasto_reversado_id = fields.Many2one('l10n_pe_ne.gasto', string='Reversa a',
                                         index=True, ondelete='set null', copy=False)

    # Campos de negocio que, una vez creado el gasto, no se pueden reescribir (D-2).
    _CAMPOS_INMUTABLES = ('monto', 'descripcion', 'fecha', 'cuenta', 'currency_id')

    def _l10n_pe_ne_gasto_dict(self):
        self.ensure_one()
        return {
            'id': self.id,
            'fecha': self.fecha.strftime('%Y-%m-%d') if self.fecha else '',
            'descripcion': self.descripcion or '',
            'cuenta': self.cuenta or '',
            'monto': self.monto or 0.0,
            'moneda': self.currency_id.name or 'PEN',
            'usuario': (self.usuario_id or self.create_uid).name or '',
            'esReversa': bool(self.gasto_reversado_id),
            'reversaDe': self.gasto_reversado_id.id or None,
        }

    # -------------------------------------------------------- inmutabilidad (D-2)
    def write(self, vals):
        """Append-only: no se reescriben los campos de negocio de un gasto ya registrado.
        El contexto l10n_pe_ne_bypass_lock deja pasar migraciones/mantenimiento del sistema."""
        if not self.env.context.get('l10n_pe_ne_bypass_lock') and \
                any(c in vals for c in self._CAMPOS_INMUTABLES):
            raise UserError(_(
                "Un gasto no se puede editar una vez registrado. Para corregirlo, regístralo "
                "de nuevo con el monto en negativo (reversa)."))
        return super().write(vals)

    def unlink(self):
        if not self.env.context.get('l10n_pe_ne_bypass_lock'):
            raise UserError(_(
                "Un gasto no se puede eliminar. Para anularlo, regístralo de nuevo con el "
                "monto en negativo (reversa)."))
        return super().unlink()

    @api.model
    def l10n_pe_ne_list_gastos(self, query=None, periodo=None, limit=300, offset=None):
        """Lista de gastos (opcional por texto o periodo YYYYMM).

        Paginación opt-in: con `offset` devuelve {items, total}; sin él, lista plana
        (así l10n_pe_ne_total_gastos sigue sumando sobre el array completo)."""
        domain = []
        if query:
            domain += ['|', ('descripcion', 'ilike', query), ('cuenta', 'ilike', query)]
        if periodo and len(str(periodo)) == 6 and str(periodo).isdigit():
            y, m = int(periodo[:4]), int(periodo[4:6])
            last = calendar.monthrange(y, m)[1]
            domain += [('fecha', '>=', '%04d-%02d-01' % (y, m)),
                       ('fecha', '<=', '%04d-%02d-%02d' % (y, m, last))]
        recs = self.search(domain, limit=limit, offset=offset or 0)
        items = [g._l10n_pe_ne_gasto_dict() for g in recs]
        if offset is None:
            return items
        return {"items": items, "total": self.search_count(domain)}

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
        """Append-only (D-2): un gasto ya registrado no se edita. Se conserva el endpoint
        para dar un error claro a los clientes que aún llamen a editar."""
        raise UserError(_(
            "Un gasto no se puede editar una vez registrado. Para corregirlo, usa la reversa "
            "(un contra-asiento con el monto en negativo)."))

    @api.model
    def l10n_pe_ne_delete_gasto(self, rec_id):
        """Append-only (D-2): un gasto no se borra; se reversa. Delega en la reversa para no
        romper a un cliente que aún llame a eliminar (el resultado ahora es la reversa creada)."""
        return self.l10n_pe_ne_reversar_gasto(rec_id)

    @api.model
    def l10n_pe_ne_reversar_gasto(self, rec_id, motivo=None):
        """Contra-asiento (D-2): crea un gasto en negativo que anula al original y lo referencia.
        Es la única forma de corregir un gasto. Idempotente por original: no se reversa dos veces."""
        orig = self.browse(int(rec_id or 0)).exists()
        if not orig:
            raise UserError(_("Gasto no encontrado."))
        if orig.gasto_reversado_id:
            raise UserError(_("Este movimiento ya es una reversa; no se reversa una reversa."))
        if self.search_count([('gasto_reversado_id', '=', orig.id)]):
            raise UserError(_("Este gasto ya fue reversado."))
        rev = self.create({
            'descripcion': (motivo or '').strip() or _("Reversa de: %s", orig.descripcion or ''),
            'monto': -orig.monto,
            'cuenta': orig.cuenta,
            'currency_id': orig.currency_id.id,
            'fecha': fields.Date.context_today(self),
            'gasto_reversado_id': orig.id,
        })
        return rev._l10n_pe_ne_gasto_dict()
