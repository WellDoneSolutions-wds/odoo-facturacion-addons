from odoo.tests import TransactionCase, tagged

from .common import L10nPeSeedMixin


@tagged('post_install', '-at_install')
class TestLineaValorCero(L10nPeSeedMixin, TransactionCase):
    """SUNAT 2028: una línea de operación onerosa (gravada/exonerada/inafecta) con importe 0 se
    rechaza. La regla L1 `2028` la ataja antes de emitir con un mensaje accionable; el gratuito
    (9996), que sí admite valor 0, no dispara la regla."""

    def setUp(self):
        super().setUp()  # RUC + IGV (self.igv)
        self.Move = self.env['account.move']
        ruc_type = self.env['l10n_latam.identification.type'].search(
            [('l10n_pe_vat_code', '=', '6')], limit=1)
        self.partner = self.env['res.partner'].create({
            'name': 'CLIENTE', 'vat': '20100070970',
            'l10n_latam_identification_type_id': ruc_type.id})
        self.product = self.env['product.product'].create({'name': 'ITEM', 'default_code': 'I1'})

    def _move(self, lineas):
        # lineas = [(precio, tax_ids)]
        m = self.Move.create({
            'move_type': 'out_invoice', 'partner_id': self.partner.id, 'invoice_date': '2026-07-30',
            'l10n_pe_serie': 'F001', 'l10n_pe_correlativo': '1',
            'invoice_line_ids': [(0, 0, {'product_id': self.product.id, 'quantity': 1.0,
                                         'price_unit': p, 'tax_ids': [(6, 0, t.ids)]})
                                 for p, t in lineas],
        })
        m.action_post()
        return m

    def _codes(self, m):
        return {f['code'] for f in m._l10n_pe_ne_validaciones()}

    def test_linea_gravada_en_cero_bloquea(self):
        m = self._move([(100.0, self.igv), (0.0, self.igv)])  # 2ª línea gravada a 0
        self.assertIn('2028', self._codes(m))
        err = [f for f in m._l10n_pe_ne_validaciones() if f['code'] == '2028']
        self.assertEqual(err[0]['nivel'], 'error')

    def test_todas_con_precio_no_bloquea(self):
        m = self._move([(100.0, self.igv), (5.0, self.igv)])
        self.assertNotIn('2028', self._codes(m))

    def test_nc_correccion_motivo_03_no_bloquea(self):
        # NC de corrección por descripción (motivo 03): líneas a valor 0 por diseño, SUNAT las acepta.
        m = self._move([(0.0, self.igv)])
        m.l10n_pe_motivo_code = '03'
        self.assertNotIn('2028', self._codes(m))

    def test_linea_gratuita_en_cero_no_bloquea(self):
        # Gratuito (9996): precio 0 es válido (importe referencial). No dispara la regla.
        grat = self.env['account.tax'].search(
            [('l10n_pe_edi_tax_code', '=', '9996'), ('type_tax_use', '=', 'sale')], limit=1)
        if not grat:
            self.skipTest('sin tax gratuito 9996 sembrado')
        m = self._move([(100.0, self.igv), (0.0, grat)])
        self.assertNotIn('2028', self._codes(m))
