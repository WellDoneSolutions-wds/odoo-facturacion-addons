from odoo.tests import TransactionCase, tagged

from .common import L10nPeSeedMixin


@tagged('post_install', '-at_install')
class TestConvenio(L10nPeSeedMixin, TransactionCase):
    """F6 · Convenio / tercero pagador (SIS, aseguradora).

    Un comprobante al paciente por el total; la parte cubierta por el tercero reduce el neto
    (copago) y queda como cuenta por cobrar al tercero. No cambia el total ni el IGV.
    """

    def setUp(self):
        super().setUp()  # RUC + IGV
        ruc_type = self.env['l10n_latam.identification.type'].search(
            [('l10n_pe_vat_code', '=', '6')], limit=1)
        self.partner = self.env['res.partner'].create({
            'name': 'PACIENTE', 'vat': '20100070970',
            'l10n_latam_identification_type_id': ruc_type.id})
        self.product = self.env['product.product'].create({'name': 'MEDICAMENTO', 'default_code': 'M1'})

    def _move(self, base=100.0, cubierto=0.0, tercero='SIS'):
        vals = {
            'move_type': 'out_invoice', 'partner_id': self.partner.id, 'invoice_date': '2026-07-29',
            'l10n_pe_serie': 'F001', 'l10n_pe_correlativo': '1',
            'invoice_line_ids': [(0, 0, {'product_id': self.product.id, 'quantity': 1.0,
                                         'price_unit': base, 'tax_ids': [(6, 0, self.igv.ids)]})],
        }
        if cubierto:
            vals.update({'l10n_pe_ne_tercero_pagador': tercero, 'l10n_pe_ne_monto_cubierto': cubierto})
        move = self.env['account.move'].create(vals)
        move.action_post()
        return move

    def test_cubierto_reduce_el_neto_a_copago(self):
        # base 100 → total 118 (c/IGV). El tercero cubre 70 → copago = 48.
        m = self._move(base=100.0, cubierto=70.0)
        cobrar = m._l10n_pe_importe_cobrar()  # 118
        self.assertAlmostEqual(m._l10n_pe_neto_pendiente(), round(cobrar - 70.0, 2), delta=0.01)
        self.assertLessEqual(m._l10n_pe_neto_pendiente(), cobrar + 0.005)  # invariante 3265

    def test_detalle_expone_convenio_y_copago(self):
        m = self._move(base=100.0, cubierto=70.0, tercero='ESSALUD')
        conv = m.l10n_pe_ne_comprobante_detalle()['convenio']
        self.assertEqual(conv['tercero'], 'ESSALUD')
        self.assertEqual(conv['montoCubierto'], 70.0)
        self.assertAlmostEqual(conv['copago'], m._l10n_pe_importe_cobrar() - 70.0, delta=0.01)

    def test_cubierto_mayor_al_total_bloquea(self):
        m = self._move(base=100.0, cubierto=999.0)  # > 118
        codes = {f['code'] for f in m._l10n_pe_ne_validaciones()}
        self.assertIn('convenio-cubierto', codes)

    def test_sin_convenio_no_expone_nada(self):
        m = self._move(base=100.0)
        self.assertIsNone(m.l10n_pe_ne_comprobante_detalle()['convenio'])
        self.assertNotIn('convenio-cubierto', {f['code'] for f in m._l10n_pe_ne_validaciones()})
