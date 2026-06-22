from unittest.mock import patch

from odoo.tests import TransactionCase, tagged

_TARGET = 'odoo.addons.l10n_pe_ne_biller.models.account_payment_retencion.requests.post'


@tagged('post_install', '-at_install')
class TestBillerRetencion(TransactionCase):
    """Comprobante de Retención (20) desde un pago saliente a proveedor reconciliado con su factura."""

    def setUp(self):
        super().setUp()
        self.company = self.env.company
        ruc_type = self.env['l10n_latam.identification.type'].search(
            [('l10n_pe_vat_code', '=', '6')], limit=1)
        pe = self.env.ref('base.pe')
        self.supplier = self.env['res.partner'].create({
            'name': 'PROVEEDOR SAC', 'vat': '20131312955', 'country_id': pe.id,
            'l10n_latam_identification_type_id': ruc_type.id})
        self.customer = self.env['res.partner'].create({
            'name': 'CLIENTE SAC', 'vat': '20100070970', 'country_id': pe.id,
            'l10n_latam_identification_type_id': ruc_type.id})
        self.product = self.env['product.product'].create({'name': 'SERVICIO', 'default_code': 'S1'})

    def _payment_for_bill(self, amount=1000.0):
        pe = self.env.ref('base.pe')
        doctype = self.env['l10n_latam.document.type'].search(
            [('code', '=', '01'), ('country_id', '=', pe.id)], limit=1)
        bill = self.env['account.move'].create({
            'move_type': 'in_invoice', 'partner_id': self.supplier.id, 'invoice_date': '2026-06-18',
            'l10n_latam_document_type_id': doctype.id, 'l10n_latam_document_number': 'F500-00000001',
            'invoice_line_ids': [(0, 0, {'product_id': self.product.id, 'quantity': 1.0,
                                         'price_unit': amount, 'tax_ids': [(6, 0, [])]})]})
        bill.action_post()
        register = self.env['account.payment.register'].with_context(
            active_model='account.move', active_ids=bill.ids).create({})
        payments = register._create_payments()
        return payments[0]

    def test_retencion_payload(self):
        pay = self._payment_for_bill(1000.0)
        pay.l10n_pe_ret_correlativo = '1'
        payload = pay._l10n_pe_ret_payload()
        self.assertEqual(payload['id']['serie'], 'R001')
        self.assertEqual(payload['id']['correlativo'], '00000001')
        cab = payload['cabecera']
        self.assertEqual(cab['codRegRetencion'], '01')
        self.assertEqual(cab['tasRetencion'], '3.00')
        self.assertEqual(cab['nroDocIdeReceptor'], '20131312955')
        self.assertEqual(cab['mtoTotRetencion'], '30.00')        # 3% de 1000
        self.assertEqual(cab['mtoImpTotPagRetencion'], '970.00')  # neto
        self.assertEqual(len(payload['detalle']), 1)
        d = payload['detalle'][0]
        self.assertEqual(d['mtoImpTotDocRelacionado'], '1000.00')
        self.assertEqual(d['mtoRetDocRelacionado'], '30.00')
        self.assertEqual(d['mtoTotPagNetoDocRelacionado'], '970.00')
        self.assertEqual(d['tipDocRelacionado'], '01')
        self.assertEqual(d['nroDocRelacionado'], 'F500-00000001')

    def test_retencion_send_mocked(self):
        pay = self._payment_for_bill(1000.0)
        pay.l10n_pe_ret_correlativo = '1'
        ok = type('R', (), {'status_code': 200, 'text': '<?xml?><Retention/>', 'headers': {}})()
        with patch(_TARGET, return_value=ok) as mp:
            pay.action_l10n_pe_send_retencion()
        self.assertEqual(pay.l10n_pe_ret_state, 'enviado')
        self.assertTrue(pay.l10n_pe_ret_xml, "Debe adjuntar el XML de la retención")
        mp.assert_called_once()

    # --- Percepción (40) ---
    def _payment_for_sale(self, amount=1000.0):
        inv = self.env['account.move'].create({
            'move_type': 'out_invoice', 'partner_id': self.customer.id, 'invoice_date': '2026-06-18',
            'l10n_pe_serie': 'F001', 'l10n_pe_correlativo': '50',
            'invoice_line_ids': [(0, 0, {'product_id': self.product.id, 'quantity': 1.0,
                                         'price_unit': amount, 'tax_ids': [(6, 0, [])]})]})
        inv.action_post()
        register = self.env['account.payment.register'].with_context(
            active_model='account.move', active_ids=inv.ids).create({})
        payments = register._create_payments()
        return payments[0]

    def test_percepcion_payload(self):
        pay = self._payment_for_sale(1000.0)
        pay.l10n_pe_per_correlativo = '1'
        payload = pay._l10n_pe_per_payload()
        self.assertEqual(payload['id']['serie'], 'P001')
        self.assertEqual(payload['id']['correlativo'], '00000001')
        cab = payload['cabecera']
        self.assertEqual(cab['codRegPercepcion'], '01')
        self.assertEqual(cab['tasPercepcion'], '2.00')
        self.assertEqual(cab['nroDocIdeReceptor'], '20100070970')
        self.assertEqual(cab['mtoTotPercepcion'], '20.00')          # 2% de 1000
        self.assertEqual(cab['mtoImpTotPagPercepcion'], '1020.00')  # total + percepción
        d = payload['detalle'][0]
        self.assertEqual(d['mtoImpTotDocRelacionado'], '1000.00')
        self.assertEqual(d['mtoPerDocRelacionado'], '20.00')
        self.assertEqual(d['mtoTotPagNetoDocRelacionado'], '1020.00')
        self.assertEqual(d['nroDocRelacionado'], 'F001-00000050')

    def test_percepcion_send_mocked(self):
        pay = self._payment_for_sale(1000.0)
        pay.l10n_pe_per_correlativo = '1'
        ok = type('R', (), {'status_code': 200, 'text': '<?xml?><Perception/>', 'headers': {}})()
        with patch(_TARGET, return_value=ok) as mp:
            pay.action_l10n_pe_send_percepcion()
        self.assertEqual(pay.l10n_pe_per_state, 'enviado')
        self.assertTrue(pay.l10n_pe_per_xml, "Debe adjuntar el XML de la percepción")
        mp.assert_called_once()
