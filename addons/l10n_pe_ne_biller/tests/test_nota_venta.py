from odoo.tests import TransactionCase, tagged
from odoo.exceptions import UserError


@tagged('post_install', '-at_install')
class TestNotaVenta(TransactionCase):
    """Nota de venta (NE Express): venta REAL cobrada SIN comprobante SUNAT. Documento interno,
    no CPE. Espejo de la cotización pero orientado a venta (alimenta la caja, cliente opcional)."""

    def setUp(self):
        super().setUp()
        self.NV = self.env['l10n_pe_ne.nota_venta']
        ruc_type = self.env['l10n_latam.identification.type'].search(
            [('l10n_pe_vat_code', '=', '6')], limit=1)
        self.partner = self.env['res.partner'].create({
            'name': 'CLIENTE SAC', 'vat': '20100070970',
            'l10n_latam_identification_type_id': ruc_type.id})
        self.prod = self.env['product.product'].create({'name': 'PROD', 'default_code': 'P1'})

    def _nota(self, **over):
        vals = {'partner_id': self.partner.id,
                'line_ids': [(0, 0, {'descripcion': 'Servicio', 'cantidad': 1.0,
                                     'precio_unitario': 118.0, 'afecto_igv': True})]}
        vals.update(over)
        return self.NV.create(vals)

    def test_totales_con_igv(self):
        # Precio CON IGV 118 -> total 118, IGV 18, valor venta 100 (espejo de la cotización).
        nv = self._nota()
        self.assertEqual(nv.amount_total, 118.0)
        self.assertEqual(nv.amount_tax, 18.0)
        self.assertEqual(nv.amount_op_gravada, 100.0)

    def test_correlativo_serie_nv(self):
        nv = self._nota()
        self.assertTrue(nv.name.startswith('NV01-'), nv.name)

    def test_linea_no_gravada_no_suma_igv(self):
        nv = self._nota(line_ids=[(0, 0, {'descripcion': 'Exon', 'cantidad': 1.0,
                                          'precio_unitario': 50.0, 'afecto_igv': False})])
        self.assertEqual(nv.amount_total, 50.0)
        self.assertEqual(nv.amount_tax, 0.0)
        self.assertEqual(nv.amount_op_no_gravada, 50.0)

    # ---------------------------------------------------------------- Task 2: API
    def test_quick_venta_registrada_y_medios(self):
        nv = self.NV.l10n_pe_ne_quick_venta({
            'clienteId': self.partner.id,
            'items': [{'descripcion': 'X', 'cantidad': 2, 'precio': 59.0, 'afectoIgv': True}],
            'medios': [{'medio': 'Efectivo', 'monto': 118.0}], 'redondeo': 0.0})
        rec = self.NV.browse(nv['id'])
        self.assertEqual(rec.estado, 'registrada')
        self.assertEqual(rec.amount_total, 118.0)
        self.assertEqual(rec.medios_pago, [{'medio': 'Efectivo', 'monto': 118.0}])

    def test_quick_venta_cliente_opcional(self):
        nv = self.NV.l10n_pe_ne_quick_venta({
            'items': [{'descripcion': 'X', 'cantidad': 1, 'precio': 10.0}]})
        rec = self.NV.browse(nv['id'])
        self.assertFalse(rec.partner_id)
        self.assertEqual(rec.l10n_pe_ne_nota_venta_detalle()['cliente'], 'Cliente varios')

    def test_anular_y_inmutable(self):
        nv = self._nota(estado='registrada')
        nv.l10n_pe_ne_set_estado_nota_venta('anulada')
        self.assertEqual(nv.estado, 'anulada')
        with self.assertRaises(UserError):
            nv.l10n_pe_ne_set_estado_nota_venta('registrada')

    def test_convertida_bloquea_update(self):
        nv = self._nota(estado='registrada')
        move = self.env['account.move'].create({'move_type': 'out_invoice', 'partner_id': self.partner.id})
        nv.l10n_pe_ne_vincular_comprobante(move.id)
        self.assertEqual(nv.estado, 'convertida')
        self.assertEqual(nv.comprobante_id, move)
        with self.assertRaises(UserError):
            self.NV.l10n_pe_ne_update_nota_venta({'id': nv.id, 'items': [{'descripcion': 'Y', 'cantidad': 1, 'precio': 5}]})
