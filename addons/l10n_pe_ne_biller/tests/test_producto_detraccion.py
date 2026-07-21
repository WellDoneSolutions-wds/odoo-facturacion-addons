from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestProductoDetraccion(TransactionCase):
    """Sujeto a detracción (cat. 54) como dato del CATÁLOGO: se marca una vez en el
    producto y Emitir lo usa para detectar operaciones mixtas (RS 183-2004 art. 19:
    los comprobantes de operaciones sujetas al SPOT no incluyen operaciones distintas).
    Solo el CÓDIGO vive aquí — la tasa cambia por resolución y la sugiere el front."""

    def setUp(self):
        super().setUp()
        self.Move = self.env['account.move']

    def test_crear_producto_con_detraccion(self):
        d = self.Move.l10n_pe_ne_create_producto({
            'descripcion': 'SERVICIO DE TRANSPORTE DE CARGA',
            'precio': 500.0, 'taxCode': '1000', 'tipo': 'servicio',
            'detraCod': '027',
        })
        self.assertEqual(d['detraCod'], '027')
        p = self.env['product.product'].browse(d['id'])
        self.assertEqual(p.l10n_pe_ne_detraccion_cod, '027')

    def test_producto_sin_detraccion_expone_vacio(self):
        d = self.Move.l10n_pe_ne_create_producto({
            'descripcion': 'CEMENTO SOL', 'precio': 30.0, 'taxCode': '1000',
        })
        self.assertEqual(d['detraCod'], '')

    def test_update_cambia_y_limpia(self):
        d = self.Move.l10n_pe_ne_create_producto({
            'descripcion': 'ALQUILER DE MAQUINARIA', 'precio': 1000.0, 'detraCod': '022',
        })
        d2 = self.Move.l10n_pe_ne_update_producto({'id': d['id'], 'detraCod': '019'})
        self.assertEqual(d2['detraCod'], '019')
        d3 = self.Move.l10n_pe_ne_update_producto({'id': d['id'], 'detraCod': ''})
        self.assertEqual(d3['detraCod'], '')

    # -- import masivo (columna DETRACCIÓN de la plantilla) -------------------------------
    def test_import_con_columna_detraccion(self):
        import io, base64, xlsxwriter
        buf = io.BytesIO()
        wb = xlsxwriter.Workbook(buf, {"in_memory": True})
        ws = wb.add_worksheet("Productos")
        ws.write_row(0, 0, ["CÓDIGO", "NOMBRE", "UNIDAD", "PRECIO VENTA", "AFECTACIÓN", "DETRACCIÓN"])
        ws.write_row(1, 0, ["DTR001", "TRANSPORTE DE CARGA LIMA-AQP", "UNIDAD", 800, "GRAVADO", "027"])
        ws.write_row(2, 0, ["DTR002", "CLAVOS 2 PULG", "UNIDAD", 5, "GRAVADO", ""])
        wb.close()
        res = self.Move.l10n_pe_ne_importar_productos({
            "contentB64": base64.b64encode(buf.getvalue()).decode(),
            "commit": True,
        })
        self.assertFalse(res.get("errores"), res)
        p1 = self.env['product.product'].search([('default_code', '=', 'DTR001')], limit=1)
        self.assertEqual(p1.l10n_pe_ne_detraccion_cod, '027')
        p2 = self.env['product.product'].search([('default_code', '=', 'DTR002')], limit=1)
        self.assertFalse(p2.l10n_pe_ne_detraccion_cod)

    def test_import_detraccion_invalida_da_error_de_fila(self):
        import io, base64, xlsxwriter
        buf = io.BytesIO()
        wb = xlsxwriter.Workbook(buf, {"in_memory": True})
        ws = wb.add_worksheet("Productos")
        ws.write_row(0, 0, ["CÓDIGO", "NOMBRE", "UNIDAD", "PRECIO VENTA", "AFECTACIÓN", "DETRACCIÓN"])
        ws.write_row(1, 0, ["DTR003", "SERVICIO X", "UNIDAD", 100, "GRAVADO", "27"])  # 2 dígitos
        wb.close()
        res = self.Move.l10n_pe_ne_importar_productos({
            "contentB64": base64.b64encode(buf.getvalue()).decode(),
            "commit": True,
        })
        self.assertTrue(res.get("errores"))

    def test_reimportar_sin_columna_detraccion_no_borra_codigo(self):
        """Plantilla vieja (sin columna DETRACCIÓN) reimportada sobre un producto que YA
        tiene código de detracción: el UPSERT por CÓDIGO no debe borrarlo. La celda vacía y la
        columna ausente lucen igual (cell() devuelve None en ambos casos) así que sin guardar
        contra el dict de headers reales (`idx`) no hay forma de distinguir 'el usuario limpió
        el campo' de 'el usuario ni trajo la columna'."""
        import io, base64, xlsxwriter

        d = self.Move.l10n_pe_ne_create_producto({
            'descripcion': 'TRANSPORTE DE CARGA REIMPORT', 'codigo': 'DTR004',
            'precio': 500.0, 'taxCode': '1000', 'tipo': 'servicio', 'detraCod': '027',
        })
        self.assertEqual(d['detraCod'], '027')

        buf = io.BytesIO()
        wb = xlsxwriter.Workbook(buf, {"in_memory": True})
        ws = wb.add_worksheet("Productos")
        # Plantilla vieja: SIN columna DETRACCIÓN (solo actualiza precio).
        ws.write_row(0, 0, ["CÓDIGO", "NOMBRE", "UNIDAD", "PRECIO VENTA", "AFECTACIÓN"])
        ws.write_row(1, 0, ["DTR004", "TRANSPORTE DE CARGA REIMPORT", "UNIDAD", 999, "GRAVADO"])
        wb.close()
        res = self.Move.l10n_pe_ne_importar_productos({
            "contentB64": base64.b64encode(buf.getvalue()).decode(),
            "commit": True,
        })
        self.assertFalse(res.get("errores"), res)

        p = self.env['product.product'].browse(d['id'])
        self.assertEqual(p.l10n_pe_ne_detraccion_cod, '027')
        self.assertEqual(p.list_price, 999.0)
