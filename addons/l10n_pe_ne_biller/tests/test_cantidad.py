from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestBillerCantidad(TransactionCase):
    """Cantidad con 3 decimales para la venta al peso de balanza (QA-020): no se trunca a 2.
    Antes, con la precisión de UoM de Odoo en 2, 18.375 kg se emitía como 18.38 y el total salía
    S/ 180.13 en vez de los S/ 180.08 del peso exacto."""

    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.igv = self.env['account.tax'].search([
            ('company_id', '=', self.company.id), ('type_tax_use', '=', 'sale'),
            ('l10n_pe_edi_tax_code', '=', '1000')], limit=1)
        ruc_type = self.env['l10n_latam.identification.type'].search(
            [('l10n_pe_vat_code', '=', '6')], limit=1)
        self.partner = self.env['res.partner'].create({
            'name': 'CLIENTE SAC', 'vat': '20100070970',
            'l10n_latam_identification_type_id': ruc_type.id})
        self.product = self.env['product.product'].create({'name': 'POLLO', 'default_code': 'P1'})

    def _move(self, qty, unit_code='NIU'):
        move = self.env['account.move'].create({
            'move_type': 'out_invoice', 'partner_id': self.partner.id,
            'invoice_date': '2026-06-20', 'l10n_pe_serie': 'F001', 'l10n_pe_correlativo': '9',
            'invoice_line_ids': [(0, 0, {
                'product_id': self.product.id, 'quantity': qty, 'price_unit': 9.80,
                'l10n_pe_ne_unit_code': unit_code, 'tax_ids': [(6, 0, self.igv.ids)]})]})
        move.action_post()
        return move

    def test_peso_conserva_3_decimales(self):
        """La cantidad de una venta al peso (KGM) se almacena y emite con 3 decimales (18.375),
        y el valor de venta refleja el peso exacto (no el redondeado a 2)."""
        move = self._move(18.375, 'KGM')
        self.assertEqual(move.invoice_line_ids[0].quantity, 18.375)  # no truncada a 18.38
        det = move._l10n_pe_build_invoice_request()['detalle'][0]
        self.assertEqual(det['ctdUnidadItem'], '18.375')
        # 18.375 × 9.80 = 180.075 (con IGV) → valor de venta 152.61 (no 152.65 de 18.38)
        self.assertEqual(det['mtoValorVentaItem'], '152.61')
        self.assertEqual(det['mtoIgvItem'], '27.47')

    def test_conteo_sigue_en_2_decimales(self):
        """Una unidad de conteo (NIU) mantiene 2 decimales: 2.5 → '2.50'."""
        det = self._move(2.5, 'NIU')._l10n_pe_build_invoice_request()['detalle'][0]
        self.assertEqual(det['ctdUnidadItem'], '2.50')

    def test_fmt_cant(self):
        """Formateador de cantidad: hasta 3 decimales, mínimo 2 (conteos → 2.00)."""
        m = self.env['account.move']
        self.assertEqual(m._l10n_pe_fmt_cant(18.375), '18.375')
        self.assertEqual(m._l10n_pe_fmt_cant(3.46), '3.46')
        self.assertEqual(m._l10n_pe_fmt_cant(2), '2.00')
        self.assertEqual(m._l10n_pe_fmt_cant(0.125), '0.125')
