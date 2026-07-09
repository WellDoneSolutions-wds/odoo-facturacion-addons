from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestBillerDocuments(TransactionCase):
    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.igv = self.env['account.tax'].search([
            ('company_id', '=', self.company.id), ('type_tax_use', '=', 'sale'),
            ('l10n_pe_edi_tax_code', '=', '1000')], limit=1)
        IdType = self.env['l10n_latam.identification.type']
        self.ruc_type = IdType.search([('l10n_pe_vat_code', '=', '6')], limit=1)
        self.dni_type = IdType.search([('l10n_pe_vat_code', '=', '1')], limit=1)
        self.product = self.env['product.product'].create(
            {'name': 'DESARMADOR', 'default_code': 'P001'})

    def _partner(self, name, vat, idtype):
        return self.env['res.partner'].create({
            'name': name, 'vat': vat, 'l10n_latam_identification_type_id': idtype.id})

    def _invoice(self, partner, serie='F001', corr='1', move_type='out_invoice', **kw):
        vals = {
            'move_type': move_type, 'partner_id': partner.id, 'invoice_date': '2026-06-19',
            'l10n_pe_serie': serie, 'l10n_pe_correlativo': corr,
            'invoice_line_ids': [(0, 0, {'product_id': self.product.id, 'quantity': 1.0,
                                         'price_unit': 7.20, 'tax_ids': [(6, 0, self.igv.ids)]})]}
        vals.update(kw)
        move = self.env['account.move'].create(vals)
        move.action_post()
        return move

    def test_factura_ruc(self):
        move = self._invoice(self._partner('CLIENTE SAC', '20605145648', self.ruc_type))
        self.assertEqual(move._l10n_pe_document_type(), '01')
        endpoint, payload = move._l10n_pe_target()
        self.assertEqual(endpoint, 'factura')
        self.assertEqual(payload['id']['documentType'], '01')

    def test_boleta_a_cliente_ruc_por_tipo_elegido(self):
        """Un cliente con RUC puede pedir Boleta: el tipo elegido en el comprobante manda
        sobre el documento de identidad (antes se emitía Factura F001 mostrando Boleta)."""
        boleta_type = self.env.ref('l10n_pe.document_type02')
        move = self._invoice(self._partner('CLIENTE SAC BOLETA', '20605145648', self.ruc_type),
                             serie='B001',
                             l10n_latam_document_type_id=boleta_type.id)
        self.assertEqual(move._l10n_pe_document_type(), '03')
        endpoint, payload = move._l10n_pe_target()
        self.assertEqual(payload['id']['documentType'], '03')
        self.assertEqual(payload['id']['serie'], 'B001')
        self.assertEqual(payload['cabecera']['tipDocUsuario'], '6')

    def test_boleta_dni(self):
        move = self._invoice(self._partner('CONSUMIDOR FINAL', '12345678', self.dni_type),
                             serie='B001')
        self.assertEqual(move._l10n_pe_document_type(), '03')
        endpoint, payload = move._l10n_pe_target()
        self.assertEqual(endpoint, 'factura')
        self.assertEqual(payload['id']['documentType'], '03')
        self.assertEqual(payload['cabecera']['tipDocUsuario'], '1')

    def test_nota_credito(self):
        inv = self._invoice(self._partner('CLIENTE SAC', '20605145648', self.ruc_type),
                            serie='F001', corr='5')
        nc_doctype = self.env['l10n_latam.document.type'].search(
            [('internal_type', '=', 'credit_note')], limit=1)
        nc = inv._reverse_moves([{
            'invoice_date': inv.invoice_date, 'l10n_pe_serie': 'FC01',
            'l10n_pe_correlativo': '1', 'l10n_pe_motivo_code': '01',
            'l10n_latam_document_type_id': nc_doctype.id}])
        nc.action_post()
        self.assertEqual(nc.move_type, 'out_refund')
        self.assertEqual(nc._l10n_pe_document_type(), '07')
        endpoint, payload = nc._l10n_pe_target()
        self.assertEqual(endpoint, 'notaCredito')
        self.assertNotIn('documentType', payload['id'])
        self.assertEqual(payload['cabecera']['numDocAfectado'], 'F001-00000005')
        self.assertEqual(payload['cabecera']['tipDocAfectado'], '01')
        self.assertEqual(payload['cabecera']['codMotivo'], '01')
        self.assertNotIn('desMotivo', payload['cabecera'])
        # NC: el SFS exige forma de pago "Credito" con cuota = total (ver _l10n_pe_build_note_request)
        self.assertEqual(payload['datoPago']['formaPago'], 'Credito')
        self.assertEqual(payload['datoPago']['tipMonedaMtoNetoPendientePago'], 'PEN')
        self.assertEqual(payload['detallePago'][0]['mtoCuotaPago'], '8.50')

    def test_nota_debito(self):
        inv = self._invoice(self._partner('CLIENTE SAC', '20605145648', self.ruc_type),
                            serie='F001', corr='7')
        nd = self._invoice(inv.partner_id, serie='FD01', corr='1',
                           debit_origin_id=inv.id, l10n_pe_motivo_code='02')
        self.assertEqual(nd._l10n_pe_document_type(), '08')
        endpoint, payload = nd._l10n_pe_target()
        self.assertEqual(endpoint, 'notaDebito')
        self.assertNotIn('documentType', payload['id'])
        self.assertEqual(payload['cabecera']['numDocAfectado'], 'F001-00000007')
        self.assertEqual(payload['cabecera']['tipDocAfectado'], '01')
        self.assertEqual(payload['cabecera']['codMotivo'], '02')
        self.assertEqual(payload['cabecera']['desMotivo'], 'Aumento en el valor')
        self.assertNotIn('datoPago', payload)  # notas no llevan forma de pago (evita errorCode 2071)
