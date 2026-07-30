from odoo.tests import TransactionCase, tagged

from .common import L10nPeSeedMixin


@tagged('post_install', '-at_install')
class TestControlados(L10nPeSeedMixin, TransactionCase):
    """F5 · Productos controlados: la venta exige receta retenida (número + colegiatura CMP).
    Sin receta se bloquea; con receta se emite y se anota en la línea del comprobante."""

    def setUp(self):
        super().setUp()  # RUC + IGV
        ruc_type = self.env['l10n_latam.identification.type'].search(
            [('l10n_pe_vat_code', '=', '6')], limit=1)
        self.partner = self.env['res.partner'].create({
            'name': 'PACIENTE', 'vat': '20100070970',
            'l10n_latam_identification_type_id': ruc_type.id})
        self.controlado = self.env['product.product'].create({
            'name': 'CLONAZEPAM', 'default_code': 'CTRL1', 'l10n_pe_ne_controlado': True})
        self.normal = self.env['product.product'].create({'name': 'VITAMINA C', 'default_code': 'N1'})

    def _move(self, prod, receta=None):
        vals = {
            'move_type': 'out_invoice', 'partner_id': self.partner.id, 'invoice_date': '2026-07-29',
            'l10n_pe_serie': 'F001', 'l10n_pe_correlativo': '1',
            'invoice_line_ids': [(0, 0, {'product_id': prod.id, 'quantity': 1.0,
                                         'price_unit': 50, 'tax_ids': [(6, 0, self.igv.ids)]})],
        }
        if receta:
            vals.update({'l10n_pe_ne_receta_numero': receta[0], 'l10n_pe_ne_receta_colegiatura': receta[1]})
        move = self.env['account.move'].create(vals)
        move.action_post()
        return move

    def test_controlado_sin_receta_bloquea(self):
        move = self._move(self.controlado)
        self.assertIn('controlado-receta', {f['code'] for f in move._l10n_pe_ne_validaciones()})

    def test_controlado_con_receta_no_bloquea_y_anota(self):
        move = self._move(self.controlado, receta=('R-001', 'CMP-45678'))
        errores = [f for f in move._l10n_pe_ne_validaciones() if f['nivel'] == 'error']
        self.assertFalse(errores)
        des = move._l10n_pe_detalle()[0]['desItem']
        self.assertIn('Receta R-001', des)
        self.assertIn('CMP-45678', des)
        self.assertTrue(move.l10n_pe_ne_comprobante_detalle()['receta'])

    def test_producto_normal_no_exige_receta(self):
        move = self._move(self.normal)
        self.assertNotIn('controlado-receta', {f['code'] for f in move._l10n_pe_ne_validaciones()})
        self.assertIsNone(move.l10n_pe_ne_comprobante_detalle()['receta'])
