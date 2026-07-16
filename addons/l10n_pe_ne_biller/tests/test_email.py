import base64
import io
import zipfile
from unittest.mock import patch

from odoo.tests import TransactionCase, tagged

from .common import EnvioSincronoMixin
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
class TestBillerEmail(EnvioSincronoMixin, TransactionCase):
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

    def _find_mail(self):
        return self.env['mail.mail'].search(
            [('model', '=', 'account.move'), ('res_id', '=', self.move.id),
             ('subject', 'like', 'Comprobante%')],
            order='id desc', limit=1)

    def test_rechaza_sin_cdr_aceptado(self):
        self.move.l10n_pe_biller_cdr = False
        with self.assertRaises(UserError):
            self.move.l10n_pe_ne_email_comprobante(to='a@b.com')

    def test_rechaza_sin_destinatario(self):
        self.move.partner_id.email = False
        with self.assertRaises(UserError):
            self.move.l10n_pe_ne_email_comprobante()

    def test_usa_email_del_partner(self):
        self.move.partner_id.email = 'cliente@example.com'
        with patch(_TARGET, return_value=self._pdf_resp()):
            res = self.move.l10n_pe_ne_email_comprobante()
        self.assertEqual(res, {'ok': True, 'to': 'cliente@example.com'})
        self.assertIn('cliente@example.com', self._find_mail().email_to or '')

    def test_override_destinatario(self):
        self.move.partner_id.email = 'cliente@example.com'
        with patch(_TARGET, return_value=self._pdf_resp()):
            res = self.move.l10n_pe_ne_email_comprobante(to='otro@dest.com')
        self.assertEqual(res['to'], 'otro@dest.com')

    def test_adjunta_pdf_y_xml(self):
        self.move.partner_id.email = 'cliente@example.com'
        with patch(_TARGET, return_value=self._pdf_resp()) as mp:
            self.move.l10n_pe_ne_email_comprobante()
            self.move.l10n_pe_ne_email_comprobante()  # 2a vez: reusa PDF cacheado
        self.assertEqual(mp.call_count, 1, "El PDF cacheado no debe volver a pedirse al micro")
        mail = self._find_mail()
        mimetypes = mail.attachment_ids.mapped('mimetype')
        self.assertEqual(len(mail.attachment_ids), 2)
        self.assertIn('application/pdf', mimetypes)
        self.assertIn('application/xml', mimetypes)

    def test_envia_mail_con_asunto(self):
        self.move.partner_id.email = 'cliente@example.com'
        with patch(_TARGET, return_value=self._pdf_resp()):
            self.move.l10n_pe_ne_email_comprobante()
        mail = self._find_mail()
        self.assertTrue(mail)
        self.assertIn(self.move.l10n_pe_ne_serie_emit, mail.subject or '')


from odoo.tests import HttpCase


@tagged('post_install', '-at_install')
class TestBillerEmailRoutes(HttpCase):
    def test_email_requiere_auth(self):
        r = self.url_open('/ne/api/comprobantes/1/email', data='{}',
                          headers={'Content-Type': 'application/json'})
        self.assertEqual(r.status_code, 401)
