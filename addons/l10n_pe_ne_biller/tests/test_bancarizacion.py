from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestBancarizacion(TransactionCase):
    """Estado de bancarización (Ley 28194) en facturas: no_aplica / pendiente / bancarizado,
    derivado del total, la moneda y los medios de pago (efectivo no bancariza)."""

    def setUp(self):
        super().setUp()
        self.igv = self.env['account.tax'].search([
            ('company_id', '=', self.env.company.id), ('type_tax_use', '=', 'sale'),
            ('l10n_pe_edi_tax_code', '=', '1000')], limit=1)
        ruc = self.env['l10n_latam.identification.type'].search([('l10n_pe_vat_code', '=', '6')], limit=1)
        self.partner = self.env['res.partner'].create({'name': 'CLIENTE SAC', 'vat': '20100070970', 'l10n_latam_identification_type_id': ruc.id})
        self.product = self.env['product.product'].create({'name': 'SERVICIO', 'default_code': 'S1'})

    def _factura(self, serie='F001', precio=3000.0, medios=None, **vals):
        base = {'move_type': 'out_invoice', 'partner_id': self.partner.id, 'invoice_date': '2026-06-20',
                'l10n_pe_serie': serie, 'l10n_pe_correlativo': '9',
                'invoice_line_ids': [(0, 0, {'product_id': self.product.id, 'quantity': 1.0, 'price_unit': precio, 'tax_ids': [(6, 0, self.igv.ids)]})]}
        base.update(vals)
        m = self.env['account.move'].create(base); m.action_post()
        if medios is not None:
            m.l10n_pe_ne_medios_pago = medios
        return m

    def test_factura_alta_efectivo_pendiente(self):
        m = self._factura(precio=3000.0, medios=[{'medio': 'Efectivo', 'monto': 3540}])
        self.assertEqual(m._l10n_pe_ne_bancarizacion_estado(), 'pendiente')

    def test_factura_alta_transferencia_bancarizado(self):
        m = self._factura(precio=3000.0, medios=[{'medio': 'Transferencia', 'monto': 3540}])
        self.assertEqual(m._l10n_pe_ne_bancarizacion_estado(), 'bancarizado')

    def test_factura_baja_no_aplica(self):
        m = self._factura(precio=100.0, medios=[{'medio': 'Efectivo', 'monto': 118}])
        self.assertEqual(m._l10n_pe_ne_bancarizacion_estado(), 'no_aplica')

    def test_boleta_no_aplica(self):
        m = self._factura(serie='B001', precio=3000.0, medios=[{'medio': 'Efectivo', 'monto': 3540}])
        m.l10n_pe_ne_tipo_doc = '03'
        self.assertEqual(m._l10n_pe_ne_bancarizacion_estado(), 'no_aplica')

    def test_factura_alta_sin_medios_pendiente(self):
        self.assertEqual(self._factura(precio=3000.0)._l10n_pe_ne_bancarizacion_estado(), 'pendiente')

    def test_marcar_bancarizado_guarda_constancia(self):
        m = self._factura(precio=3000.0, medios=[{'medio': 'Efectivo', 'monto': 3540}])
        m.l10n_pe_ne_bancarizacion = 'pendiente'
        m.l10n_pe_ne_marcar_bancarizado({'constancia': 'OP-0099', 'fecha': '2026-06-25', 'medio': 'Transferencia'})
        self.assertEqual(m.l10n_pe_ne_bancarizacion, 'bancarizado')
        self.assertEqual(m.l10n_pe_ne_bancarizacion_constancia, 'OP-0099')

    def test_quick_list_filtra_por_bancarizacion(self):
        # Sin `offset`, l10n_pe_ne_quick_list devuelve la lista plana (paginación opt-in);
        # ver su docstring. Se itera `res` directo en vez de `res['items']`.
        m = self._factura(precio=3000.0, medios=[{'medio': 'Efectivo', 'monto': 3540}])
        m.l10n_pe_ne_bancarizacion = 'pendiente'
        res = self.env['account.move'].l10n_pe_ne_quick_list(bancarizacion='pendiente')
        self.assertIn(m.id, [r['id'] for r in res])
        self.assertEqual(next(r for r in res if r['id'] == m.id)['bancarizacion'], 'pendiente')
        res2 = self.env['account.move'].l10n_pe_ne_quick_list(bancarizacion='bancarizado')
        self.assertNotIn(m.id, [r['id'] for r in res2])

    import base64 as _b64
    _PDF = _b64.b64encode(b"%PDF-1.4\n%mock voucher\n").decode()
    _PNG = _b64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 40).decode()

    def _factura_banc(self):
        m = self._factura(precio=3000.0, medios=[{'medio': 'Transferencia', 'monto': 3540}])
        m.l10n_pe_ne_bancarizacion = 'pendiente'
        return m

    def test_marcar_guarda_documento(self):
        m = self._factura_banc()
        m.l10n_pe_ne_marcar_bancarizado({'constancia': 'OP-1', 'doc': self._PDF, 'docName': 'voucher.pdf'})
        self.assertEqual(m.l10n_pe_ne_bancarizacion, 'bancarizado')
        self.assertEqual(m.l10n_pe_ne_bancarizacion_doc_name, 'voucher.pdf')
        self.assertTrue(m.l10n_pe_ne_bancarizacion_doc)

    def test_marcar_sin_doc_no_borra_el_existente(self):
        m = self._factura_banc()
        m.l10n_pe_ne_marcar_bancarizado({'doc': self._PDF, 'docName': 'v.pdf'})
        m.l10n_pe_ne_marcar_bancarizado({'constancia': 'OP-2'})   # sin doc
        self.assertEqual(m.l10n_pe_ne_bancarizacion_doc_name, 'v.pdf')
        self.assertTrue(m.l10n_pe_ne_bancarizacion_doc)

    def test_marcar_reemplaza_documento(self):
        m = self._factura_banc()
        m.l10n_pe_ne_marcar_bancarizado({'doc': self._PDF, 'docName': 'a.pdf'})
        m.l10n_pe_ne_marcar_bancarizado({'doc': self._PNG, 'docName': 'b.png'})
        self.assertEqual(m.l10n_pe_ne_bancarizacion_doc_name, 'b.png')

    def test_marcar_doc_muy_grande_bloquea(self):
        from odoo.exceptions import UserError
        m = self._factura_banc()
        grande = self._b64.b64encode(b"%PDF-" + b"0" * (5 * 1024 * 1024 + 10)).decode()
        with self.assertRaises(UserError):
            m.l10n_pe_ne_marcar_bancarizado({'doc': grande, 'docName': 'big.pdf'})
        self.assertFalse(m.l10n_pe_ne_bancarizacion_doc)

    def test_marcar_doc_tipo_no_permitido_bloquea(self):
        from odoo.exceptions import UserError
        m = self._factura_banc()
        exe = self._b64.b64encode(b"MZ\x90\x00malware").decode()
        with self.assertRaises(UserError):
            m.l10n_pe_ne_marcar_bancarizado({'doc': exe, 'docName': 'x.pdf'})

    def test_detalle_expone_bancarizacion(self):
        m = self._factura_banc()
        m.l10n_pe_ne_marcar_bancarizado({'constancia': 'OP-9', 'medio': 'Transferencia', 'doc': self._PDF, 'docName': 'v.pdf'})
        d = m.l10n_pe_ne_comprobante_detalle()
        self.assertEqual(d['bancarizacion'], 'bancarizado')
        self.assertEqual(d['bancarizacionConstancia'], 'OP-9')
        self.assertEqual(d['bancarizacionDocNombre'], 'v.pdf')
