# -*- coding: utf-8 -*-
"""Nota de venta (NE Express) — venta REAL cobrada SIN comprobante SUNAT. NO es CPE: no firma
XML, no va a SUNAT, no consume correlativo fiscal. Documento interno de control (sin valor
tributario). A diferencia de la cotización (una oferta), es una venta cobrada: alimenta la caja.
Modelo propio simplificado (misma filosofía que l10n_pe_ne.cotizacion / l10n_pe_ne.gasto): TODA
la lógica —CRUD, totales, serialización, PDF— vive en el addon; React solo llama /ne/api."""

import base64

from odoo import _, api, fields, models
from odoo.exceptions import UserError

IGV_RATE = 0.18  # IGV Perú 18%


class L10nPeNeNotaVenta(models.Model):
    _name = 'l10n_pe_ne.nota_venta'
    _description = 'Nota de venta (NE Express)'
    _order = 'fecha desc, id desc'

    name = fields.Char(string='Número', required=True, copy=False, readonly=True,
                       default=lambda s: _('Nueva'))
    # Cliente OPCIONAL: muchas ventas sin comprobante son al paso. Sin partner se usa cliente_nombre
    # (o "Cliente varios" en el impreso). A diferencia de la cotización (cliente obligatorio).
    partner_id = fields.Many2one('res.partner', string='Cliente', index=True)
    cliente_nombre = fields.Char(string='Cliente (texto)')
    fecha = fields.Date(string='Fecha', required=True, default=fields.Date.context_today)
    estado = fields.Selection([
        ('borrador', 'Borrador'),
        ('registrada', 'Registrada'),
        ('convertida', 'Convertida'),
        ('anulada', 'Anulada'),
    ], string='Estado', default='borrador', required=True)
    # Medios de pago del cobro (mismo shape que account.move.l10n_pe_ne_medios_pago) + redondeo de
    # efectivo (≤ 0, dato de caja). NO hay XML: son dato de caja/impreso.
    medios_pago = fields.Json(string='Medios de pago', copy=False)
    redondeo = fields.Monetary(string='Redondeo efectivo', currency_field='currency_id',
                               help='Ajuste (≤ 0) del efectivo cobrado por redondeo al décimo.')
    comprobante_id = fields.Many2one('account.move', string='Comprobante emitido', copy=False,
                                     index=True, help='Comprobante generado al convertir la nota.')
    caja_sesion_id = fields.Many2one('l10n_pe_ne.caja.sesion', string='Sesión de caja',
                                     copy=False, index=True)
    notas = fields.Text(string='Notas')
    currency_id = fields.Many2one('res.currency', required=True,
                                  default=lambda s: s.env.company.currency_id)
    company_id = fields.Many2one('res.company', required=True, index=True,
                                 default=lambda s: s.env.company)
    line_ids = fields.One2many('l10n_pe_ne.nota_venta.line', 'nota_venta_id',
                               string='Líneas', copy=True)
    amount_untaxed = fields.Monetary(string='Valor venta', compute='_compute_amounts',
                                     store=True, currency_field='currency_id')
    amount_tax = fields.Monetary(string='IGV', compute='_compute_amounts', store=True,
                                 currency_field='currency_id')
    amount_total = fields.Monetary(string='Total', compute='_compute_amounts', store=True,
                                   currency_field='currency_id')
    amount_op_gravada = fields.Monetary(string='Op. gravada', compute='_compute_amounts',
                                        store=True, currency_field='currency_id')
    amount_op_no_gravada = fields.Monetary(string='Op. exonerada/inafecta',
                                           compute='_compute_amounts', store=True,
                                           currency_field='currency_id')

    @api.depends('line_ids.subtotal', 'line_ids.afecto_igv')
    def _compute_amounts(self):
        # Espejo EXACTO de l10n_pe_ne.cotizacion._compute_amounts (precio unitario CON IGV: el
        # subtotal de línea ya es el bruto que paga el cliente; el gravado se descompone en
        # base (bruto/1.18) + IGV; lo no gravado ya es base. Total == suma de brutos).
        for nv in self:
            bruto_gravado = sum(l.subtotal for l in nv.line_ids if l.afecto_igv)
            no_gravado = sum(l.subtotal for l in nv.line_ids if not l.afecto_igv)
            base_gravado = round(bruto_gravado / (1 + IGV_RATE), 2)
            nv.amount_total = round(bruto_gravado + no_gravado, 2)
            nv.amount_tax = round(bruto_gravado - base_gravado, 2)
            nv.amount_untaxed = round(nv.amount_total - nv.amount_tax, 2)
            nv.amount_op_gravada = base_gravado
            nv.amount_op_no_gravada = round(no_gravado, 2)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('name') or vals.get('name') == _('Nueva'):
                vals['name'] = self.env['ir.sequence'].next_by_code('l10n_pe.ne.nota_venta') or _('Nueva')
        return super().create(vals_list)

    # ------------------------------------------------------------- serialización
    def _l10n_pe_ne_nota_venta_dict(self):
        self.ensure_one()
        return {
            'id': self.id, 'numero': self.name,
            'cliente': self.partner_id.name or self.cliente_nombre or 'Cliente varios',
            'clienteId': self.partner_id.id or None,
            'clienteDoc': self.partner_id.vat or '',
            'fecha': self.fecha.strftime('%Y-%m-%d') if self.fecha else '',
            'estado': self.estado, 'moneda': self.currency_id.name or 'PEN',
            'total': self.amount_total, 'items': len(self.line_ids),
            'comprobanteId': self.comprobante_id.id if self.comprobante_id else None,
            'comprobanteNumero': self._l10n_pe_ne_comprobante_numero(),
        }

    def _l10n_pe_ne_comprobante_numero(self):
        """'serie-correlativo' del comprobante vinculado (o '' si no hay)."""
        m = self.comprobante_id
        if not m:
            return ''
        serie = m.l10n_pe_ne_serie_emit or m.l10n_pe_serie or ''
        corr = m.l10n_pe_ne_corr_emit or ''
        return ('%s-%s' % (serie, corr)) if (serie or corr) else (m.name or '')

    def l10n_pe_ne_nota_venta_detalle(self):
        """Detalle completo para la vista/PDF: cabecera + líneas + totales + medios."""
        self.ensure_one()
        return {
            **self._l10n_pe_ne_nota_venta_dict(),
            'notas': self.notas or '',
            'valorVenta': self.amount_untaxed, 'igv': self.amount_tax,
            'opGravada': self.amount_op_gravada, 'opNoGravada': self.amount_op_no_gravada,
            'redondeo': self.redondeo or 0.0, 'medios': self.medios_pago or [],
            'lineas': [{
                'descripcion': l.descripcion or (l.product_id.display_name or ''),
                'cantidad': l.cantidad, 'precio': l.precio_unitario, 'descuento': l.descuento,
                'subtotal': l.subtotal, 'afectoIgv': l.afecto_igv,
                'unidad': l.product_id.l10n_pe_ne_unit_code or 'NIU',
                'productId': l.product_id.id or None, 'codigo': l.product_id.default_code or '',
            } for l in self.line_ids],
        }

    # ---------------------------------------------------------- helpers (espejo cotización)
    def _l10n_pe_ne_build_lines(self, lineas):
        """Traduce las líneas simplificadas de React a comandos O2M de Odoo (idéntico a cotización)."""
        vals = []
        for it in (lineas or []):
            desc = (it.get('descripcion') or '').strip()
            prod = False
            if it.get('productId'):
                prod = self.env['product.product'].browse(int(it['productId'])).exists()
                if prod and not desc:
                    desc = prod.display_name
            if not desc:
                raise UserError(_('Cada ítem necesita una descripción (o un producto).'))
            vals.append((0, 0, {
                'product_id': prod.id if prod else False, 'descripcion': desc,
                'cantidad': float(it.get('cantidad') or 1),
                'precio_unitario': float(it.get('precio') or 0),
                'descuento': float(it.get('descuento') or 0),
                'afecto_igv': bool(it.get('afectoIgv', True)),
            }))
        return vals

    def _l10n_pe_ne_moneda_currency(self, payload):
        nombre = (payload.get('moneda') or 'PEN').strip().upper()
        if nombre not in ('PEN', 'USD'):
            nombre = 'PEN'
        return self.env['res.currency'].with_context(active_test=False).search(
            [('name', '=', nombre)], limit=1)

    def _l10n_pe_ne_resolve_partner_opt(self, payload):
        """Cliente OPCIONAL (a diferencia de la cotización): devuelve el partner o False."""
        if payload.get('clienteId'):
            p = self.env['res.partner'].browse(int(payload['clienteId'])).exists()
            if p:
                return p
        c = payload.get('cliente')
        if isinstance(c, dict) and c.get('numDoc'):
            p = self.env['account.move']._l10n_pe_ne_quick_partner(c)
            if p and not p.customer_rank:
                p.customer_rank = 1
            return p
        return False

    def _l10n_pe_ne_caja_abierta(self):
        """Sesión de caja ABIERTA de la compañía (para amarrar la venta al arqueo), o vacío."""
        return self.env['l10n_pe_ne.caja.sesion'].search(
            [('company_id', '=', self.env.company.id), ('estado', '=', 'abierta')], limit=1)

    # ------------------------------------------------------------- API React
    @api.model
    def l10n_pe_ne_quick_venta(self, payload):
        """Crea una nota de venta COBRADA (estado 'registrada') desde el payload de React:
        {clienteId? | cliente:{...}?, items:[{productId|descripcion, cantidad, precio, afectoIgv}],
        medios:[{medio,monto,numOp}]?, redondeo?, fecha?, notas?}. Cliente OPCIONAL."""
        payload = payload or {}
        lines = self._l10n_pe_ne_build_lines(payload.get('items') or payload.get('lineas'))
        if not lines:
            raise UserError(_('La nota de venta necesita al menos un ítem.'))
        partner = self._l10n_pe_ne_resolve_partner_opt(payload)
        cliente_nombre = ''
        if not partner:
            c = payload.get('cliente')
            cliente_nombre = (c.get('razonSocial') if isinstance(c, dict) else c) or ''
        nv = self.create({
            'company_id': self.env.company.id,
            'currency_id': (self._l10n_pe_ne_moneda_currency(payload).id
                            or self.env.company.currency_id.id),
            'partner_id': partner.id if partner else False,
            'cliente_nombre': cliente_nombre or False,
            'fecha': payload.get('fecha') or fields.Date.context_today(self),
            'estado': 'registrada',
            'medios_pago': payload.get('medios') or payload.get('mediosPago') or False,
            'redondeo': float(payload.get('redondeo') or 0.0),
            'notas': payload.get('notas') or False,
            'caja_sesion_id': self._l10n_pe_ne_caja_abierta().id or False,
            'line_ids': lines,
        })
        return nv._l10n_pe_ne_nota_venta_dict()

    # Transiciones comerciales válidas. NUNCA admite →convertida (eso lo escribe SOLO
    # l10n_pe_ne_vincular_comprobante al emitir la boleta/factura).
    _L10N_PE_NE_TRANSICIONES = {
        'borrador': {'registrada', 'anulada'},
        'registrada': {'anulada'},
    }

    @api.model
    def l10n_pe_ne_list_notas_venta(self, query=None, limit=100, offset=None):
        domain = []
        if query:
            q = query.strip()
            domain += ['|', '|', ('name', 'ilike', q),
                       ('partner_id.name', 'ilike', q), ('cliente_nombre', 'ilike', q)]
        recs = self.search(domain, order='fecha desc, id desc', limit=limit, offset=offset or 0)
        items = [n._l10n_pe_ne_nota_venta_dict() for n in recs]
        return items if offset is None else {'items': items, 'total': self.search_count(domain)}

    @api.model
    def l10n_pe_ne_update_nota_venta(self, payload):
        """Reemplaza cabecera + líneas de una nota existente (por id). Bloqueado si convertida/anulada."""
        payload = payload or {}
        nv = self.browse(int(payload.get('id') or 0)).exists()
        if not nv:
            raise UserError(_('Nota de venta no encontrada.'))
        if nv.estado in ('convertida', 'anulada') or nv.comprobante_id:
            raise UserError(_('La nota de venta %(n)s ya no se puede editar (%(e)s).',
                              n=nv.name, e=nv.estado))
        vals = {}
        if 'clienteId' in payload or 'cliente' in payload:
            partner = self._l10n_pe_ne_resolve_partner_opt(payload)
            vals['partner_id'] = partner.id if partner else False
            if not partner:
                c = payload.get('cliente')
                vals['cliente_nombre'] = ((c.get('razonSocial') if isinstance(c, dict) else c) or '') or False
        if payload.get('fecha'):
            vals['fecha'] = payload['fecha']
        if 'notas' in payload:
            vals['notas'] = payload.get('notas') or False
        if payload.get('items') is not None or payload.get('lineas') is not None:
            vals['line_ids'] = [(5, 0, 0)] + self._l10n_pe_ne_build_lines(
                payload.get('items') or payload.get('lineas'))
        nv.write(vals)
        return nv._l10n_pe_ne_nota_venta_dict()

    def l10n_pe_ne_set_estado_nota_venta(self, estado):
        """Cambia el estado por una TRANSICIÓN válida (anular). convertida/anulada son terminales."""
        self.ensure_one()
        if estado not in self._L10N_PE_NE_TRANSICIONES.get(self.estado, set()):
            raise UserError(_('No se puede pasar de «%(o)s» a «%(d)s».', o=self.estado, d=estado))
        self.estado = estado
        return self._l10n_pe_ne_nota_venta_dict()

    def l10n_pe_ne_vincular_comprobante(self, comprobante_id):
        """Vincula el comprobante emitido y marca la nota como 'convertida' (inmutable).
        Lo llama quick_emit cuando la emisión vino de 'Convertir a comprobante'."""
        self.ensure_one()
        self.write({'comprobante_id': int(comprobante_id), 'estado': 'convertida'})
        return self._l10n_pe_ne_nota_venta_dict()

    def l10n_pe_ne_get_pdf_b64(self, formato='A4'):
        """Renderiza el PDF (reporte QWeb — Task 5) y lo devuelve en base64. TICKET o A4."""
        self.ensure_one()
        report = ('l10n_pe_ne_biller.action_report_nota_venta_ticket' if formato == 'TICKET'
                  else 'l10n_pe_ne_biller.action_report_nota_venta')
        pdf, _c = self.env['ir.actions.report']._render_qweb_pdf(report, res_ids=self.ids)
        return base64.b64encode(pdf).decode()


class L10nPeNeNotaVentaLine(models.Model):
    _name = 'l10n_pe_ne.nota_venta.line'
    _description = 'Línea de nota de venta (NE Express)'
    _order = 'id'

    nota_venta_id = fields.Many2one('l10n_pe_ne.nota_venta', string='Nota de venta',
                                    required=True, ondelete='cascade', index=True)
    product_id = fields.Many2one('product.product', string='Producto')
    descripcion = fields.Char(string='Descripción', required=True)
    cantidad = fields.Float(string='Cantidad', default=1.0)
    precio_unitario = fields.Monetary(string='P. unitario', currency_field='currency_id')
    descuento = fields.Float(string='Descuento %', default=0.0,
                             help='Descuento porcentual aplicado a la línea (0–100).')
    afecto_igv = fields.Boolean(string='Afecto a IGV', default=True)
    subtotal = fields.Monetary(string='Subtotal', compute='_compute_subtotal', store=True,
                               currency_field='currency_id')
    currency_id = fields.Many2one(related='nota_venta_id.currency_id', store=True)
    company_id = fields.Many2one(related='nota_venta_id.company_id', store=True, index=True)

    @api.depends('cantidad', 'precio_unitario', 'descuento')
    def _compute_subtotal(self):
        for line in self:
            bruto = (line.cantidad or 0.0) * (line.precio_unitario or 0.0)
            factor = 1.0 - min(max(line.descuento or 0.0, 0.0), 100.0) / 100.0
            line.subtotal = round(bruto * factor, 2)
