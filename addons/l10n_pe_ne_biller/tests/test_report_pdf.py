import io
import zipfile
from unittest.mock import patch

from odoo.tests import TransactionCase, tagged
from odoo.exceptions import UserError

_TARGET = 'odoo.addons.l10n_pe_ne_biller.models.account_move_biller.requests.post'


@tagged('post_install', '-at_install')
class TestBillerReportPdf(TransactionCase):
    """Representación impresa (PDF) y descargas: el addon pide el PDF al micro (/report/pdf) con el
    XML firmado, lo cachea como adjunto y expone acciones de descarga. El micro se mockea."""

    def setUp(self):
        super().setUp()
        self.env.company.sudo().l10n_pe_ne_api_key = 'test-key'
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
        # Simula un comprobante ya enviado: adjunta el XML firmado y marca el estado.
        att = self.env['ir.attachment'].create({
            'name': '20605145648-F001-00000001.xml', 'res_model': 'account.move',
            'res_id': self.move.id, 'mimetype': 'application/xml',
            'raw': b'<?xml version="1.0"?><Invoice xmlns="urn:x"/>'})
        self.move.l10n_pe_biller_xml = att.id
        self.move.l10n_pe_biller_state = 'enviado'

    def _pdf_resp(self, code=200, content=b'%PDF-1.4 fake pdf bytes'):
        return type('R', (), {'status_code': code, 'content': content, 'text': '', 'headers': {}})()

    def test_download_pdf_genera_y_cachea(self):
        with patch(_TARGET, return_value=self._pdf_resp()) as mp:
            act = self.move.action_l10n_pe_download_pdf()
        self.assertTrue(self.move.l10n_pe_biller_pdf, "Debe adjuntar el PDF")
        self.assertEqual(self.move.l10n_pe_biller_pdf.raw[:4], b'%PDF')
        self.assertEqual(self.move.l10n_pe_biller_pdf.mimetype, 'application/pdf')
        self.assertEqual(act['type'], 'ir.actions.act_url')
        self.assertIn('/web/content/', act['url'])
        mp.assert_called_once()
        # Verifica endpoint, payload y autenticación enviados al micro.
        self.assertIn('/report/pdf', mp.call_args[0][0])
        kwargs = mp.call_args.kwargs
        self.assertEqual(kwargs['json']['tipoDoc'], '01')
        self.assertEqual(kwargs['json']['ruc'], self.env.company.vat)
        self.assertEqual(kwargs['headers']['X-Api-Key'], 'test-key')
        # Cache: una segunda descarga NO vuelve a llamar al micro.
        with patch(_TARGET) as mp2:
            self.move.action_l10n_pe_download_pdf()
            mp2.assert_not_called()

    def test_download_pdf_micro_falla_rechaza(self):
        with patch(_TARGET, return_value=self._pdf_resp(code=500, content=b'boom')):
            with self.assertRaises(UserError):
                self.move.action_l10n_pe_download_pdf()
        self.assertFalse(self.move.l10n_pe_biller_pdf)

    def test_download_pdf_sin_xml_rechaza(self):
        self.move.l10n_pe_biller_xml = False
        with self.assertRaises(UserError):
            self.move.action_l10n_pe_download_pdf()

    def test_download_xml_devuelve_url(self):
        act = self.move.action_l10n_pe_download_xml()
        self.assertEqual(act['type'], 'ir.actions.act_url')
        self.assertIn('/web/content/%s' % self.move.l10n_pe_biller_xml.id, act['url'])

    def test_download_zip_incluye_xml_y_pdf(self):
        with patch(_TARGET, return_value=self._pdf_resp()):
            act = self.move.action_l10n_pe_download_zip()
        self.assertEqual(act['type'], 'ir.actions.act_url')
        att_id = int(act['url'].split('/web/content/')[1].split('?')[0])
        names = zipfile.ZipFile(io.BytesIO(self.env['ir.attachment'].browse(att_id).raw)).namelist()
        self.assertTrue(any(n.endswith('.xml') for n in names), names)
        self.assertTrue(any(n.endswith('.pdf') for n in names), names)
