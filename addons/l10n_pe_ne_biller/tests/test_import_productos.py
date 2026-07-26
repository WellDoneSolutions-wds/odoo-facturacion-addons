import io
import base64
import xlsxwriter
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestImportProductos(TransactionCase):
    """Import de catálogo por Excel:
      - columna TIPO (BIEN/SERVICIO) que manda el tipo Odoo; vacía = deducir de la unidad,
        y unidad vacía + TIPO=SERVICIO → ZZ (Odoo y SUNAT no se contradicen);
      - UPSERT por CÓDIGO con 'vacío = mantener' al actualizar (no resetea unidad/afectación/bolsa);
      - aviso de CÓDIGO repetido dentro del archivo (la última fila gana, se cuenta una vez)."""

    def setUp(self):
        super().setUp()
        self.Move = self.env['account.move']
        self.Product = self.env['product.product']

    def _import(self, headers, rows, commit=True):
        buf = io.BytesIO()
        wb = xlsxwriter.Workbook(buf, {"in_memory": True})
        ws = wb.add_worksheet("Productos")
        ws.write_row(0, 0, headers)
        for i, row in enumerate(rows, 1):
            ws.write_row(i, 0, row)
        wb.close()
        return self.Move.l10n_pe_ne_importar_productos({
            "contentB64": base64.b64encode(buf.getvalue()).decode(), "commit": commit})

    def _get(self, cod):
        return self.Product.search([('default_code', '=', cod)], limit=1)

    def _afe(self, p):
        """Código de afectación (cat. 07) del producto, excluyendo el ICBPER (7152)."""
        t = p.taxes_id.filtered(
            lambda t: t.type_tax_use == "sale" and t.l10n_pe_edi_tax_code and t.l10n_pe_edi_tax_code != "7152")
        return t[:1].l10n_pe_edi_tax_code or "1000"

    def _tiene_icbper(self, p):
        return bool(p.taxes_id.filtered(lambda t: t.l10n_pe_edi_tax_code == "7152"))

    # -- columna TIPO ---------------------------------------------------------
    def test_tipo_servicio_sin_unidad_da_service_y_ZZ(self):
        res = self._import(
            ["CÓDIGO", "NOMBRE", "TIPO", "UNIDAD", "PRECIO VENTA"],
            [["T-SRV", "CONSULTORÍA", "SERVICIO", "", 100]])
        self.assertFalse(res.get("errores"), res)
        p = self._get("T-SRV")
        self.assertEqual(p.type, "service")
        self.assertEqual(p.l10n_pe_ne_unit_code, "ZZ")

    def test_tipo_bien_sin_unidad_da_consu_y_NIU(self):
        res = self._import(
            ["CÓDIGO", "NOMBRE", "TIPO", "UNIDAD", "PRECIO VENTA"],
            [["T-BN", "CEMENTO", "BIEN", "", 30]])
        self.assertFalse(res.get("errores"), res)
        p = self._get("T-BN")
        self.assertEqual(p.type, "consu")
        self.assertEqual(p.l10n_pe_ne_unit_code, "NIU")

    def test_tipo_vacio_se_deduce_de_la_unidad(self):
        res = self._import(
            ["CÓDIGO", "NOMBRE", "TIPO", "UNIDAD", "PRECIO VENTA"],
            [["T-Z", "TRANSPORTE", "", "SERVICIO", 100],
             ["T-K", "CLAVOS", "", "KILOGRAMO", 5]])
        self.assertFalse(res.get("errores"), res)
        self.assertEqual(self._get("T-Z").type, "service")
        self.assertEqual(self._get("T-K").type, "consu")

    def test_tipo_ausente_en_update_no_reclasifica(self):
        self._import(["CÓDIGO", "NOMBRE", "TIPO", "PRECIO VENTA"], [["T-UP", "SRV", "SERVICIO", 100]])
        self.assertEqual(self._get("T-UP").type, "service")
        self._import(["CÓDIGO", "NOMBRE", "PRECIO VENTA"], [["T-UP", "SRV", 200]])
        p = self._get("T-UP")
        self.assertEqual(p.type, "service")   # no cambió a bien
        self.assertEqual(p.list_price, 200.0)

    # -- vacío = mantener -----------------------------------------------------
    def test_update_vacio_mantiene_unidad_y_afectacion(self):
        self._import(
            ["CÓDIGO", "NOMBRE", "UNIDAD", "PRECIO VENTA", "AFECTACIÓN"],
            [["KP", "AZÚCAR", "KILOGRAMO", 4, "EXONERADO"]])
        p = self._get("KP")
        self.assertEqual(p.l10n_pe_ne_unit_code, "KGM")
        self.assertEqual(self._afe(p), "9997")
        # re-import solo con precio nuevo (unidad y afectación vacías) → mantiene ambas.
        self._import(
            ["CÓDIGO", "NOMBRE", "UNIDAD", "PRECIO VENTA", "AFECTACIÓN"],
            [["KP", "AZÚCAR", "", 5, ""]])
        p = self._get("KP")
        self.assertEqual(p.list_price, 5.0)
        self.assertEqual(p.l10n_pe_ne_unit_code, "KGM")   # mantiene, NO resetea a NIU
        self.assertEqual(self._afe(p), "9997")            # mantiene, NO resetea a GRAVADO

    def test_update_cambiar_afectacion_conserva_la_bolsa(self):
        self._import(
            ["CÓDIGO", "NOMBRE", "UNIDAD", "PRECIO VENTA", "AFECTACIÓN", "BOLSA"],
            [["BG", "BOLSA", "UNIDAD", 0.5, "GRAVADO", "SI"]])
        self.assertTrue(self._tiene_icbper(self._get("BG")))
        # cambia la afectación dejando BOLSA vacía → conserva el ICBPER (afectación y bolsa
        # comparten campo, pero la bolsa vacía = mantener).
        self._import(
            ["CÓDIGO", "NOMBRE", "UNIDAD", "PRECIO VENTA", "AFECTACIÓN", "BOLSA"],
            [["BG", "BOLSA", "", 0.6, "EXONERADO", ""]])
        p = self._get("BG")
        self.assertTrue(self._tiene_icbper(p))     # conserva bolsa
        self.assertEqual(self._afe(p), "9997")     # afectación sí cambió a exonerado

    # -- duplicado dentro del archivo -----------------------------------------
    def test_codigo_repetido_avisa_gana_la_ultima_y_cuenta_una_vez(self):
        res = self._import(
            ["CÓDIGO", "NOMBRE", "PRECIO VENTA"],
            [["DUP", "PRIMERO", 10], ["DUP", "SEGUNDO", 20]])
        self.assertFalse(res.get("errores"), res)
        self.assertTrue(any("repetido" in a["msg"].lower() for a in res.get("avisos", [])), res)
        self.assertEqual(res["creados"], 1)   # un solo producto, no 2
        p = self._get("DUP")
        self.assertEqual(p.name, "SEGUNDO")   # la última fila gana
        self.assertEqual(p.list_price, 20.0)

    def test_codigo_repetido_dry_run_cuenta_una_vez(self):
        res = self._import(
            ["CÓDIGO", "NOMBRE", "PRECIO VENTA"],
            [["DRY", "A", 10], ["DRY", "B", 20]], commit=False)
        self.assertEqual(res["creados"], 1)
