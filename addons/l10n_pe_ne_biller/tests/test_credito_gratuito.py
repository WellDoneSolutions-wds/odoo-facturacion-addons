from odoo.tests import TransactionCase, tagged

from .common import L10nPeSeedMixin


@tagged('post_install', '-at_install')
class TestCreditoGratuito(L10nPeSeedMixin, TransactionCase):
    """Venta al CRÉDITO con una línea GRATUITA (cat. 05 = 9996).

    Caso real (F001-247, rechazo SUNAT 3265): total 2950 con un ítem gratuito de 790. El
    'Monto neto pendiente de pago' del crédito salía en 2950 (incluía el gratuito), pero el
    'Importe total del comprobante' (mtoImpVenta / PayableAmount) excluye los gratuitos = 2160.
    SUNAT exige mtoNetoPendientePago <= mtoImpVenta → 2950 > 2160 = rechazo 3265.

    El neto pendiente ahora se basa en el importe a cobrar (que ya excluye gratuitos, anticipo
    y descuento que no afecta el IGV), no en la base de detracción.
    """

    def setUp(self):
        super().setUp()  # L10nPeSeedMixin siembra RUC + IGV (self.igv)
        self.company = self.env.company
        # La tax gratuita (9996) se auto-crea si el plan no la trae (mismo helper de producción).
        self.gratuito = self.env['account.move']._l10n_pe_ne_tax_by_code('9996')
        ruc_type = self.env['l10n_latam.identification.type'].search(
            [('l10n_pe_vat_code', '=', '6')], limit=1)
        self.partner = self.env['res.partner'].create({
            'name': 'INKA INVESTMENTS EIRL', 'vat': '20608171704',
            'l10n_latam_identification_type_id': ruc_type.id})
        self.product = self.env['product.product'].create(
            {'name': 'ESCALERA', 'default_code': 'E1'})

    def _move(self, **vals):
        base = {
            'move_type': 'out_invoice', 'partner_id': self.partner.id, 'invoice_date': '2026-07-29',
            'l10n_pe_serie': 'F001', 'l10n_pe_correlativo': '247',
            'invoice_line_ids': [
                # Gravado: base 500 + IGV 90 = 590 a cobrar.
                (0, 0, {'product_id': self.product.id, 'quantity': 1.0, 'price_unit': 500.0,
                        'tax_ids': [(6, 0, self.igv.ids)]}),
                # Gratuito 790: NO se cobra (fuera del PayableAmount).
                (0, 0, {'product_id': self.product.id, 'quantity': 1.0, 'price_unit': 790.0,
                        'name': 'REGALO', 'tax_ids': [(6, 0, self.gratuito.ids)]}),
            ]}
        base.update(vals)
        move = self.env['account.move'].create(base)
        move.action_post()
        return move

    def test_credito_con_gratuito_neto_pendiente_excluye_el_gratuito(self):
        move = self._move(l10n_pe_ne_forma_pago='Credito',
                          invoice_date_due='2026-09-29',
                          l10n_pe_ne_cuotas=[{'fecha': '2026-08-29', 'monto': 295.0},
                                             {'fecha': '2026-09-29', 'monto': 295.0}])
        payload = move._l10n_pe_build_invoice_request()
        neto = float(payload['datoPago']['mtoNetoPendientePago'])
        total = float(payload['cabecera']['sumImpVenta'])   # importe total del comprobante (payable)
        # El neto pendiente excluye el gratuito: 590 (gravado + IGV), no 1380.
        self.assertEqual(neto, 590.0, 'el neto pendiente NO debe incluir el ítem gratuito')
        self.assertEqual(total, 590.0, 'el mtoImpVenta ya excluye el gratuito')
        # La invariante que valida SUNAT (regla 3265).
        self.assertLessEqual(neto, total,
                             'mtoNetoPendientePago debe ser <= mtoImpVenta (SUNAT 3265)')
        # Y las cuotas cuadran con el neto (no se escalan hacia el total con gratuito).
        suma_cuotas = sum(float(c['mtoCuotaPago']) for c in payload['detallePago'])
        self.assertEqual(suma_cuotas, neto, 'sum(cuotas) == mtoNetoPendientePago')

    def test_detalle_total_excluye_el_gratuito_y_cuadra_con_el_desglose(self):
        # El "Total" del detalle (lo que ve el usuario) no debe inflarse con la línea gratuita:
        # debe ser lo que se cobra y coincidir con la suma de su propio desglose.
        move = self._move()
        t = move.l10n_pe_ne_comprobante_detalle()['totales']
        desglose = t['gravada'] + t['exonerada'] + t['inafecta'] + t['igv'] + t['icbper']
        self.assertEqual(t['total'], 590.0, 'el Total del detalle excluye el ítem gratuito')
        self.assertEqual(round(desglose, 2), t['total'],
                         'el Total debe cuadrar con su propio desglose')
