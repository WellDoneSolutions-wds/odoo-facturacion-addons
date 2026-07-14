from unittest.mock import MagicMock, patch

from odoo.tests import TransactionCase, tagged

_TARGET = 'odoo.addons.l10n_pe_ne_biller.models.account_move_biller.requests.post'


def _s3_mock(body):
    """Cliente S3 fake: get_object devuelve `body` como stream."""
    s3c = MagicMock()
    stream = MagicMock()
    stream.read.return_value = body
    s3c.get_object.return_value = {"Body": stream}
    return s3c


@tagged('post_install', '-at_install')
class TestAsyncPdf(TransactionCase):
    """PDF pre-generado por el worker async (pdf_s3_key del item DynamoDB):
    debe quedar etiquetado con la versión del template (description=pdfver:N)
    para que la primera descarga vía API lo SIRVA en vez de descartarlo por el
    cache-busting de _l10n_pe_get_pdf_attachment y re-renderizar contra el micro."""

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
        # Comprobante async ya firmado por el worker: XML adjunto, aún en_proceso.
        att = self.env['ir.attachment'].create({
            'name': '20605145648-F001-00000001.xml', 'res_model': 'account.move',
            'res_id': self.move.id, 'mimetype': 'application/xml',
            'raw': b'<?xml version="1.0"?><Invoice xmlns="urn:x"/>'})
        self.move.l10n_pe_biller_xml = att.id
        self.move.l10n_pe_biller_state = 'en_proceso'
        self.item = {"pdf_s3_key": {"S": "async/db/%s/document.pdf" % self.move.id}}

    def test_attach_async_pdf_etiqueta_version_y_cachea(self):
        s3c = _s3_mock(b'%PDF-1.4 pdf del worker')
        self.move._l10n_pe_attach_async_pdf(s3c, 'bucket', self.item)
        att = self.move.l10n_pe_biller_pdf
        self.assertTrue(att, "adjunta el PDF del worker")
        self.assertEqual(att.raw[:4], b'%PDF')
        # La etiqueta de versión hace que el cache-busting lo reconozca como vigente.
        self.assertEqual(att.description, 'pdfver:1')
        # Primera descarga vía API: SIRVE el PDF del worker sin re-render en el micro.
        with patch(_TARGET) as mp:
            servido = self.move._l10n_pe_get_pdf_attachment()
            mp.assert_not_called()
        self.assertEqual(servido.id, att.id, "no lo descarta ni regenera")

    def test_attach_async_pdf_respeta_pdf_ver_actual(self):
        # Con el template versionado en 2, el PDF del worker se etiqueta 'pdfver:2'.
        self.env['ir.config_parameter'].sudo().set_param('l10n_pe_ne_biller.pdf_ver', '2')
        s3c = _s3_mock(b'%PDF-1.4 pdf del worker')
        self.move._l10n_pe_attach_async_pdf(s3c, 'bucket', self.item)
        self.assertEqual(self.move.l10n_pe_biller_pdf.description, 'pdfver:2')

    def test_attach_async_pdf_ignora_bytes_no_pdf(self):
        # S3 con contenido corrupto (p.ej. un error guardado como objeto): NO se adjunta;
        # la descarga cae al camino on-demand de siempre.
        s3c = _s3_mock(b'<html>error</html>')
        self.move._l10n_pe_attach_async_pdf(s3c, 'bucket', self.item)
        self.assertFalse(self.move.l10n_pe_biller_pdf)

    def test_reemision_invalida_pdf_viejo(self):
        # Rechazo → re-emisión con XML corregido: el PDF cacheado del intento
        # rechazado (ya etiquetado como vigente) NO debe sobrevivir al XML nuevo.
        s3c = _s3_mock(b'%PDF-1.4 pdf del intento rechazado')
        self.move._l10n_pe_attach_async_pdf(s3c, 'bucket', self.item)
        pdf_viejo = self.move.l10n_pe_biller_pdf
        self.assertTrue(pdf_viejo)
        self.move.l10n_pe_biller_state = 'rechazado'
        # El cron aplica el resultado del intento nuevo (XML distinto).
        self.move._l10n_pe_apply_emission_response(
            True, '<?xml version="1.0"?><Invoice xmlns="urn:x">corregido</Invoice>', '')
        self.assertFalse(self.move.l10n_pe_biller_pdf,
                         "el PDF del intento rechazado se invalida con el XML nuevo")
        self.assertFalse(pdf_viejo.exists())
        # Y el PDF nuevo del worker ahora SÍ puede adjuntarse (el guard ya no lo bloquea).
        s3c2 = _s3_mock(b'%PDF-1.4 pdf del intento corregido')
        self.move._l10n_pe_attach_async_pdf(s3c2, 'bucket', self.item)
        self.assertEqual(self.move.l10n_pe_biller_pdf.raw, b'%PDF-1.4 pdf del intento corregido')

    def test_mismo_xml_conserva_pdf(self):
        # Flujo async normal: el item 'enviado' trae el MISMO XML que ya se adjuntó
        # en la ventana firmado → el PDF del worker se conserva (no hay re-render).
        s3c = _s3_mock(b'%PDF-1.4 pdf del worker')
        self.move._l10n_pe_attach_async_pdf(s3c, 'bucket', self.item)
        pdf = self.move.l10n_pe_biller_pdf
        mismo_xml = (self.move.l10n_pe_biller_xml.raw or b'').decode('utf-8')
        self.move._l10n_pe_apply_emission_response(True, mismo_xml, '')
        self.assertTrue(pdf.exists(), "mismo XML → el PDF cacheado sigue vigente")
        self.assertEqual(self.move.l10n_pe_biller_pdf.id, pdf.id)

    def test_enqueue_reemision_limpia_artefactos(self):
        # Encolar una re-emisión limpia XML y PDFs del intento anterior: durante la
        # ventana en_proceso no debe servirse la representación vieja, y el attach
        # del PDF nuevo no debe quedar bloqueado.
        s3c = _s3_mock(b'%PDF-1.4 pdf viejo')
        self.move._l10n_pe_attach_async_pdf(s3c, 'bucket', self.item)
        self.move.l10n_pe_biller_state = 'rechazado'
        icp = self.env['ir.config_parameter'].sudo()
        icp.set_param('l10n_pe_ne_biller.sqs_queue_url', 'https://sqs/mock')
        with patch('odoo.addons.l10n_pe_ne_biller.models.account_move_biller.boto3') as mb:
            self.move._l10n_pe_enqueue_emission(icp)
            mb.client.assert_called()
        self.assertEqual(self.move.l10n_pe_biller_state, 'en_proceso')
        self.assertFalse(self.move.l10n_pe_biller_xml, "el XML del intento rechazado se descarta")
        self.assertFalse(self.move.l10n_pe_biller_pdf, "el PDF del intento rechazado se descarta")

    def test_attach_async_pdf_noop_sin_key_o_con_pdf(self):
        # Sin pdf_s3_key en el item: no toca S3.
        s3c = _s3_mock(b'%PDF-1.4 x')
        self.move._l10n_pe_attach_async_pdf(s3c, 'bucket', {"pdf_s3_key": {"S": ""}})
        s3c.get_object.assert_not_called()
        # Con un PDF ya adjunto: tampoco (no pisa lo que haya).
        previo = self.env['ir.attachment'].create({
            'name': 'x.pdf', 'res_model': 'account.move', 'res_id': self.move.id,
            'mimetype': 'application/pdf', 'raw': b'%PDF-1.4 previo'})
        self.move.l10n_pe_biller_pdf = previo.id
        s3c.get_object.reset_mock()
        self.move._l10n_pe_attach_async_pdf(s3c, 'bucket', self.item)
        s3c.get_object.assert_not_called()
        self.assertEqual(self.move.l10n_pe_biller_pdf.id, previo.id)
