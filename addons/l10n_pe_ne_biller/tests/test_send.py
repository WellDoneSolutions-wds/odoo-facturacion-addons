import base64
import io
import zipfile
from unittest.mock import patch
from odoo.tests import TransactionCase, tagged

from .common import EnvioSincronoMixin

_TARGET = 'odoo.addons.l10n_pe_ne_biller.models.account_move_biller.requests.post'


def _fake_cdr_b64(code='0', desc='La Factura F001-1 ha sido aceptada'):
    xml = ('<?xml version="1.0"?><ApplicationResponse '
           'xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">'
           '<cbc:ResponseCode>%s</cbc:ResponseCode><cbc:Description>%s</cbc:Description>'
           '</ApplicationResponse>' % (code, desc))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        zf.writestr('R20605145648-01-F001-1.xml', xml)
    return base64.b64encode(buf.getvalue()).decode()


@tagged('post_install', '-at_install')
class TestBillerSend(EnvioSincronoMixin, TransactionCase):
    def setUp(self):
        super().setUp()
        igv = self.env['account.tax'].search([
            ('company_id', '=', self.env.company.id), ('type_tax_use', '=', 'sale'),
            ('l10n_pe_edi_tax_code', '=', '1000')], limit=1)
        ruc_type = self.env['l10n_latam.identification.type'].search(
            [('l10n_pe_vat_code', '=', '6')], limit=1)
        partner = self.env['res.partner'].create({
            'name': 'CLIENTE SAC', 'vat': '20605145648',
            'l10n_latam_identification_type_id': ruc_type.id})
        product = self.env['product.product'].create({'name': 'DESARMADOR', 'default_code': 'P001'})
        self.move = self.env['account.move'].create({
            'move_type': 'out_invoice', 'partner_id': partner.id, 'invoice_date': '2026-06-19',
            'l10n_pe_serie': 'F001', 'l10n_pe_correlativo': '1',
            'invoice_line_ids': [(0, 0, {'product_id': product.id, 'quantity': 1.0,
                                         'price_unit': 7.20, 'tax_ids': [(6, 0, igv.ids)]})]})
        self.move.action_post()

    def _resp(self, code, text, headers=None):
        return type('R', (), {'status_code': code, 'text': text, 'headers': headers or {}})()

    def test_send_success(self):
        ok = self._resp(200, '<?xml version="1.0"?><Invoice xmlns="urn:x"><ext:UBLExtensions/></Invoice>')
        with patch(_TARGET, return_value=ok) as mp:
            self.move.action_l10n_pe_send_to_biller()
        self.assertEqual(self.move.l10n_pe_biller_state, 'enviado')
        self.assertTrue(self.move.l10n_pe_biller_xml, "Debe adjuntar el XML")
        mp.assert_called_once()

    def test_send_success_almacena_cdr(self):
        ok = self._resp(200, '<?xml version="1.0"?><Invoice xmlns="urn:x"><ext:UBLExtensions/></Invoice>',
                        headers={'X-Sunat-Cdr': _fake_cdr_b64('0', 'aceptada')})
        with patch(_TARGET, return_value=ok):
            self.move.action_l10n_pe_send_to_biller()
        self.assertEqual(self.move.l10n_pe_biller_state, 'enviado')
        self.assertTrue(self.move.l10n_pe_biller_cdr, "Debe adjuntar el CDR")
        self.assertEqual(self.move.l10n_pe_biller_cdr.mimetype, 'application/zip')
        self.assertIn('ResponseCode 0', self.move.l10n_pe_biller_message)

    def test_send_rejected(self):
        bad = self._resp(400, 'XSLT validation error: 2335')
        with patch(_TARGET, return_value=bad):
            self.move.action_l10n_pe_send_to_biller()
        self.assertEqual(self.move.l10n_pe_biller_state, 'rechazado')
        self.assertIn('2335', self.move.l10n_pe_biller_message)
