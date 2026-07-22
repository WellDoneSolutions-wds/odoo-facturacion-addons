from odoo.exceptions import UserError, ValidationError
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestPercepcionCatalogo(TransactionCase):
    """Percepción del IGV (Apéndice 1, Ley 29173) como dato de config + catálogo: el negocio
    declara si es AGENTE designado (gate de toda la detección en Emitir) y el producto lleva
    su tasa sugerida (2% general, 1% combustibles). La emisión no cambia."""

    def setUp(self):
        super().setUp()
        self.Move = self.env['account.move']

    def test_agente_percepcion_negocio_round_trip(self):
        self.assertFalse(self.Move.l10n_pe_ne_negocio()['agentePercepcion'])
        self.Move.l10n_pe_ne_update_negocio({'agentePercepcion': True})
        self.assertTrue(self.Move.l10n_pe_ne_negocio()['agentePercepcion'])
        self.assertTrue(self.env.company.l10n_pe_ne_agente_percepcion)

    def test_config_expone_agente(self):
        self.env.company.l10n_pe_ne_agente_percepcion = True
        self.assertTrue(self.Move.l10n_pe_ne_config()['agentePercepcion'])

    def test_crear_producto_con_percepcion(self):
        d = self.Move.l10n_pe_ne_create_producto({
            'descripcion': 'GASEOSA 3L', 'precio': 10.0, 'taxCode': '1000', 'percepTasa': 2.0,
        })
        self.assertEqual(d['percepTasa'], 2.0)

    def test_producto_sin_percepcion_expone_cero(self):
        d = self.Move.l10n_pe_ne_create_producto({'descripcion': 'CLAVOS', 'precio': 5.0})
        self.assertEqual(d['percepTasa'], 0.0)

    def test_percep_tasa_no_numerica_da_error(self):
        """Un percepTasa no numérico debe dar un UserError claro, no un 500 críptico."""
        with self.assertRaises(UserError):
            self.Move.l10n_pe_ne_create_producto({'descripcion': 'X', 'percepTasa': 'abc'})

    def test_percep_tasa_coma_decimal_api(self):
        """La API de productos tolera coma decimal igual que el import masivo."""
        d = self.Move.l10n_pe_ne_create_producto({'descripcion': 'ACEITE COMA API', 'percepTasa': '1,5'})
        self.assertEqual(d['percepTasa'], 1.5)

    def test_percep_tasa_fuera_de_rango_constraint(self):
        """Defensa en profundidad: fuera de 0-10 debe fallar también a nivel de modelo,
        no solo en la validación del import masivo / la API."""
        tmpl = self.env['product.template'].create({'name': 'PRODUCTO RANGO'})
        with self.assertRaises(ValidationError):
            tmpl.write({'l10n_pe_ne_percepcion_tasa': 50})

    def test_update_cambia_y_limpia_percepcion(self):
        d = self.Move.l10n_pe_ne_create_producto({'descripcion': 'CERVEZA', 'precio': 8.0, 'percepTasa': 2.0})
        d2 = self.Move.l10n_pe_ne_update_producto({'id': d['id'], 'percepTasa': 1.0})
        self.assertEqual(d2['percepTasa'], 1.0)
        d3 = self.Move.l10n_pe_ne_update_producto({'id': d['id'], 'percepTasa': 0})
        self.assertEqual(d3['percepTasa'], 0.0)

    # -- import masivo (columna PERCEPCION % de la plantilla) ------------------------------
    def test_import_columna_percepcion(self):
        import io, base64, xlsxwriter

        # -- con valor (+ celda vacía en producto nuevo) ------------------------------------
        buf = io.BytesIO()
        wb = xlsxwriter.Workbook(buf, {"in_memory": True})
        ws = wb.add_worksheet("Productos")
        ws.write_row(0, 0, ["CÓDIGO", "NOMBRE", "UNIDAD", "PRECIO VENTA", "AFECTACIÓN", "PERCEPCION %"])
        ws.write_row(1, 0, ["PEC001", "GASEOSA 3L IMPORT", "UNIDAD", 10, "GRAVADO", 2])
        ws.write_row(2, 0, ["PEC002", "CLAVOS IMPORT", "UNIDAD", 5, "GRAVADO", ""])
        wb.close()
        res = self.Move.l10n_pe_ne_importar_productos({
            "contentB64": base64.b64encode(buf.getvalue()).decode(),
            "commit": True,
        })
        self.assertFalse(res.get("errores"), res)
        p1 = self.env['product.product'].search([('default_code', '=', 'PEC001')], limit=1)
        self.assertEqual(p1.l10n_pe_ne_percepcion_tasa, 2.0)
        p2 = self.env['product.product'].search([('default_code', '=', 'PEC002')], limit=1)
        self.assertFalse(p2.l10n_pe_ne_percepcion_tasa)

        # -- inválida (> 10) da error de fila -----------------------------------------------
        buf = io.BytesIO()
        wb = xlsxwriter.Workbook(buf, {"in_memory": True})
        ws = wb.add_worksheet("Productos")
        ws.write_row(0, 0, ["CÓDIGO", "NOMBRE", "UNIDAD", "PRECIO VENTA", "AFECTACIÓN", "PERCEPCION %"])
        ws.write_row(1, 0, ["PEC003", "PRODUCTO X", "UNIDAD", 20, "GRAVADO", 15])
        wb.close()
        res = self.Move.l10n_pe_ne_importar_productos({
            "contentB64": base64.b64encode(buf.getvalue()).decode(),
            "commit": True,
        })
        self.assertTrue(res.get("errores"))
        self.assertIn("mayor a 0 y hasta 10", res["errores"][0]["msg"])
        self.assertFalse(
            self.env['product.product'].search([('default_code', '=', 'PEC003')], limit=1),
            "la fila inválida no debe crear/actualizar el producto")

        # -- columna ausente (plantilla vieja) no borra la tasa ya guardada ----------------
        d = self.Move.l10n_pe_ne_create_producto({
            'descripcion': 'GASEOSA REIMPORT', 'codigo': 'PEC004',
            'precio': 10.0, 'taxCode': '1000', 'percepTasa': 2.0,
        })
        self.assertEqual(d['percepTasa'], 2.0)
        buf = io.BytesIO()
        wb = xlsxwriter.Workbook(buf, {"in_memory": True})
        ws = wb.add_worksheet("Productos")
        # Plantilla vieja: SIN columna PERCEPCION % (solo actualiza precio).
        ws.write_row(0, 0, ["CÓDIGO", "NOMBRE", "UNIDAD", "PRECIO VENTA", "AFECTACIÓN"])
        ws.write_row(1, 0, ["PEC004", "GASEOSA REIMPORT", "UNIDAD", 12, "GRAVADO"])
        wb.close()
        res = self.Move.l10n_pe_ne_importar_productos({
            "contentB64": base64.b64encode(buf.getvalue()).decode(),
            "commit": True,
        })
        self.assertFalse(res.get("errores"), res)
        p4 = self.env['product.product'].browse(d['id'])
        self.assertEqual(p4.l10n_pe_ne_percepcion_tasa, 2.0)
        self.assertEqual(p4.list_price, 12.0)

        # -- columna presente, celda vacía SÍ limpia la tasa en un producto existente ------
        d2 = self.Move.l10n_pe_ne_create_producto({
            'descripcion': 'CERVEZA REIMPORT', 'codigo': 'PEC005',
            'precio': 8.0, 'taxCode': '1000', 'percepTasa': 1.0,
        })
        self.assertEqual(d2['percepTasa'], 1.0)
        buf = io.BytesIO()
        wb = xlsxwriter.Workbook(buf, {"in_memory": True})
        ws = wb.add_worksheet("Productos")
        ws.write_row(0, 0, ["CÓDIGO", "NOMBRE", "UNIDAD", "PRECIO VENTA", "AFECTACIÓN", "PERCEPCION %"])
        ws.write_row(1, 0, ["PEC005", "CERVEZA REIMPORT", "UNIDAD", 8, "GRAVADO", ""])
        wb.close()
        res = self.Move.l10n_pe_ne_importar_productos({
            "contentB64": base64.b64encode(buf.getvalue()).decode(),
            "commit": True,
        })
        self.assertFalse(res.get("errores"), res)
        p5 = self.env['product.product'].browse(d2['id'])
        self.assertFalse(p5.l10n_pe_ne_percepcion_tasa)

    def test_import_percepcion_celda_cero_procesa_fila_y_limpia(self):
        """0 = "no sujeto" (mismo significado que vacío/percepTasa: 0 en la API): la fila SE
        PROCESA igual (precio incluido), no se descarta como si fuera un valor inválido."""
        import io, base64, xlsxwriter

        d = self.Move.l10n_pe_ne_create_producto({
            'descripcion': 'YOGURT REIMPORT', 'codigo': 'PEC006',
            'precio': 6.0, 'taxCode': '1000', 'percepTasa': 2.0,
        })
        self.assertEqual(d['percepTasa'], 2.0)
        buf = io.BytesIO()
        wb = xlsxwriter.Workbook(buf, {"in_memory": True})
        ws = wb.add_worksheet("Productos")
        ws.write_row(0, 0, ["CÓDIGO", "NOMBRE", "UNIDAD", "PRECIO VENTA", "AFECTACIÓN", "PERCEPCION %"])
        ws.write_row(1, 0, ["PEC006", "YOGURT REIMPORT", "UNIDAD", 9, "GRAVADO", 0])
        wb.close()
        res = self.Move.l10n_pe_ne_importar_productos({
            "contentB64": base64.b64encode(buf.getvalue()).decode(),
            "commit": True,
        })
        self.assertFalse(res.get("errores"), res)
        p6 = self.env['product.product'].browse(d['id'])
        self.assertFalse(p6.l10n_pe_ne_percepcion_tasa)
        self.assertEqual(p6.list_price, 9.0)  # la fila SE PROCESÓ: precio también se actualizó

    def test_import_percepcion_coma_decimal(self):
        import io, base64, xlsxwriter

        buf = io.BytesIO()
        wb = xlsxwriter.Workbook(buf, {"in_memory": True})
        ws = wb.add_worksheet("Productos")
        ws.write_row(0, 0, ["CÓDIGO", "NOMBRE", "UNIDAD", "PRECIO VENTA", "AFECTACIÓN", "PERCEPCION %"])
        ws.write_row(1, 0, ["PEC007", "ACEITE COMA", "UNIDAD", 15, "GRAVADO", "1,5"])
        wb.close()
        res = self.Move.l10n_pe_ne_importar_productos({
            "contentB64": base64.b64encode(buf.getvalue()).decode(),
            "commit": True,
        })
        self.assertFalse(res.get("errores"), res)
        p7 = self.env['product.product'].search([('default_code', '=', 'PEC007')], limit=1)
        self.assertEqual(p7.l10n_pe_ne_percepcion_tasa, 1.5)
