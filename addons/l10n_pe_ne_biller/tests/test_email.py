import base64
import io
import zipfile
from unittest.mock import patch

from odoo.tests import TransactionCase, tagged
from odoo.exceptions import UserError

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
class TestBillerEmail(TransactionCase):
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
        product = self.env['product.product'].create(
            {'name': 'DESARMADOR', 'default_code': 'P001'})
        self.move = self.env['account.move'].create({
            'move_type': 'out_invoice', 'partner_id': partner.id, 'invoice_date': '2026-06-19',
            'l10n_pe_serie': 'F001', 'l10n_pe_correlativo': '1',
            'invoice_line_ids': [(0, 0, {'product_id': product.id, 'quantity': 1.0,
                                         'price_unit': 7.20, 'tax_ids': [(6, 0, igv.ids)]})]})
        self.move.action_post()
        # email del emisor válido para email_from = company_id.email_formatted
        self.move.company_id.email = 'emisor@example.com'
        # Emitir aceptado: mockea la red (XML firmado + CDR ResponseCode 0 en header).
        signed = self._resp(
            200, '<?xml version="1.0"?><Invoice xmlns="urn:x"><ext:UBLExtensions/></Invoice>',
            headers={'X-Sunat-Cdr': _fake_cdr_b64('0', 'aceptada')})
        with patch(_TARGET, return_value=signed):
            self.move.action_l10n_pe_send_to_biller()

    def _resp(self, code, text, headers=None):
        return type('R', (), {'status_code': code, 'text': text, 'headers': headers or {}})()

    def _pdf_resp(self):
        return type('R', (), {'status_code': 200, 'content': b'%PDF-1.4 test', 'text': ''})()

    # ---- helper de precondición
    def test_is_aceptado_true_con_cdr_0(self):
        self.assertEqual(self.move.l10n_pe_biller_state, 'enviado')
        self.assertTrue(self.move._l10n_pe_ne_is_aceptado())

    def test_is_aceptado_false_sin_cdr(self):
        self.move.l10n_pe_biller_cdr = False
        self.assertFalse(self.move._l10n_pe_ne_is_aceptado())

    def test_is_aceptado_false_estado_no_enviado(self):
        self.move.l10n_pe_biller_state = 'rechazado'
        self.assertFalse(self.move._l10n_pe_ne_is_aceptado())

    def test_mail_template_existe(self):
        tmpl = self.env.ref(
            'l10n_pe_ne_biller.mail_template_comprobante', raise_if_not_found=False)
        self.assertTrue(tmpl, "La plantilla de correo debe existir")
        self.assertEqual(tmpl.model_id.model, 'account.move')
