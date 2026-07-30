from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestBancarizacion(TransactionCase):
    """Estado de bancarización (Ley 28194) en facturas: no_aplica / pendiente / bancarizado,
    derivado del total, la moneda y los medios de pago (efectivo no bancariza)."""

    def setUp(self):
        super().setUp()
        self.igv = self.env['account.tax'].search([
            ('company_id', '=', self.env.company.id), ('type_tax_use', '=', 'sale'),
            ('l10n_pe_edi_tax_code', '=', '1000')], limit=1)
        ruc = self.env['l10n_latam.identification.type'].search([('l10n_pe_vat_code', '=', '6')], limit=1)
        self.partner = self.env['res.partner'].create({'name': 'CLIENTE SAC', 'vat': '20100070970', 'l10n_latam_identification_type_id': ruc.id})
        self.product = self.env['product.product'].create({'name': 'SERVICIO', 'default_code': 'S1'})

    def _factura(self, serie='F001', precio=3000.0, medios=None, **vals):
        base = {'move_type': 'out_invoice', 'partner_id': self.partner.id, 'invoice_date': '2026-06-20',
                'l10n_pe_serie': serie, 'l10n_pe_correlativo': '9',
                'invoice_line_ids': [(0, 0, {'product_id': self.product.id, 'quantity': 1.0, 'price_unit': precio, 'tax_ids': [(6, 0, self.igv.ids)]})]}
        base.update(vals)
        m = self.env['account.move'].create(base); m.action_post()
        if medios is not None:
            m.l10n_pe_ne_medios_pago = medios
        return m

    def test_factura_alta_efectivo_pendiente(self):
        m = self._factura(precio=3000.0, medios=[{'medio': 'Efectivo', 'monto': 3540}])
        self.assertEqual(m._l10n_pe_ne_bancarizacion_estado(), 'pendiente')

    def test_factura_alta_transferencia_bancarizado(self):
        m = self._factura(precio=3000.0, medios=[{'medio': 'Transferencia', 'monto': 3540}])
        self.assertEqual(m._l10n_pe_ne_bancarizacion_estado(), 'bancarizado')

    def test_factura_baja_no_aplica(self):
        m = self._factura(precio=100.0, medios=[{'medio': 'Efectivo', 'monto': 118}])
        self.assertEqual(m._l10n_pe_ne_bancarizacion_estado(), 'no_aplica')

    def test_boleta_no_aplica(self):
        m = self._factura(serie='B001', precio=3000.0, medios=[{'medio': 'Efectivo', 'monto': 3540}])
        m.l10n_pe_ne_tipo_doc = '03'
        self.assertEqual(m._l10n_pe_ne_bancarizacion_estado(), 'no_aplica')

    def test_factura_alta_sin_medios_pendiente(self):
        self.assertEqual(self._factura(precio=3000.0)._l10n_pe_ne_bancarizacion_estado(), 'pendiente')
