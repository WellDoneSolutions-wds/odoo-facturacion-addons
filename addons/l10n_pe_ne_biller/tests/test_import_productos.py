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

    def _xlsx(self, headers, rows):
        buf = io.BytesIO()
        wb = xlsxwriter.Workbook(buf, {"in_memory": True})
        ws = wb.add_worksheet("Productos")
        ws.write_row(0, 0, headers)
        for i, row in enumerate(rows, 1):
            ws.write_row(i, 0, row)
        wb.close()
        return base64.b64encode(buf.getvalue()).decode()

    def _import(self, headers, rows, commit=True):
        return self.Move.l10n_pe_ne_importar_productos({
            "contentB64": self._xlsx(headers, rows), "commit": commit})

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

    # -- procesamiento por lotes (offset/limit) + aviso de nombre repetido -----
    def test_lotes_offset_limit_y_total_filas(self):
        """Con offset/limit procesa solo la tanda; totalFilas informa el total del archivo, para
        que el front pagine sin chocar con el timeout del gateway (504)."""
        rows = [["LOTE-%02d" % i, "P%d" % i, 10] for i in range(6)]
        r0 = self._import(["CÓDIGO", "NOMBRE", "PRECIO VENTA"], rows, commit=False)
        self.assertEqual(r0["totalFilas"], 6)
        # Primer lote de 4: procesa 4 (todas nuevas en dry-run).
        r1 = self.Move.l10n_pe_ne_importar_productos({
            "contentB64": self._xlsx(["CÓDIGO", "NOMBRE", "PRECIO VENTA"], rows),
            "commit": False, "offset": 0, "limit": 4})
        self.assertEqual(r1["creados"], 4)
        self.assertEqual(r1["totalFilas"], 6)
        # Segundo lote: las 2 restantes.
        r2 = self.Move.l10n_pe_ne_importar_productos({
            "contentB64": self._xlsx(["CÓDIGO", "NOMBRE", "PRECIO VENTA"], rows),
            "commit": False, "offset": 4, "limit": 4})
        self.assertEqual(r2["creados"], 2)

    def test_aviso_nombre_repetido_no_bloquea(self):
        res = self._import(["CÓDIGO", "NOMBRE", "PRECIO VENTA"],
                           [["NR-1", "Yogurt X", 10], ["NR-2", "Yogurt X", 12], ["NR-3", "Otro", 5]])
        self.assertFalse(res.get("errores"), res)          # no bloquea
        self.assertEqual(res["creados"], 3)                 # los 3 se crean (código distinto)
        self.assertTrue(any("se repiten" in a["msg"] for a in res["avisos"]), res)

    # -- plantilla completa: categoría/marca, stock, incluye igv, activo ------
    def test_categoria_subcategoria_marca_get_or_create(self):
        """CATEGORÍA/SUBCATEGORÍA/MARCA por nombre: crea lo que falta bajo el árbol y reutiliza
        (case-insensitive) sin duplicar entre productos."""
        res = self._import(
            ["CÓDIGO", "NOMBRE", "PRECIO VENTA", "CATEGORÍA", "SUBCATEGORÍA", "MARCA"],
            [["CM-1", "SHAMPOO X", 10, "Cuidado personal", "Shampoo Import", "MarcaZ"],
             ["CM-2", "SHAMPOO Y", 12, "cuidado personal", "shampoo import", "marcaz"]])
        self.assertFalse(res.get("errores"), res)
        p1, p2 = self._get("CM-1"), self._get("CM-2")
        # Ambos caen en la MISMA subcategoría (no se duplicó por mayúsculas/acentos)…
        self.assertTrue(p1.categ_id)
        self.assertEqual(p1.categ_id, p2.categ_id)
        self.assertEqual(p1.categ_id.name, "Shampoo Import")
        # El departamento se REUSA case-insensitive (la BD puede tenerlo como "Cuidado Personal").
        self.assertEqual(p1.categ_id.parent_id.name.lower(), "cuidado personal")
        # …y en la misma marca.
        self.assertTrue(p1.l10n_pe_ne_marca_id)
        self.assertEqual(p1.l10n_pe_ne_marca_id, p2.l10n_pe_ne_marca_id)
        self.assertEqual(
            self.env["l10n_pe_ne.marca"].search_count([("name", "=ilike", "marcaz")]), 1)

    def test_incluye_igv_no_suma_igv_solo_si_gravado(self):
        """INCLUYE IGV = No → el precio viene SIN IGV: +18% si es GRAVADO, tal cual si no."""
        res = self._import(
            ["CÓDIGO", "NOMBRE", "PRECIO VENTA", "INCLUYE IGV", "AFECTACIÓN"],
            [["IGV-G", "GRAV", 100, "No", "GRAVADO"],
             ["IGV-E", "EXO", 100, "No", "EXONERADO"],
             ["IGV-S", "CONIGV", 118, "Sí", "GRAVADO"]])
        self.assertFalse(res.get("errores"), res)
        self.assertEqual(self._get("IGV-G").list_price, 118.0)   # gravado → +18%
        self.assertEqual(self._get("IGV-E").list_price, 100.0)   # exonerado → sin cambio
        self.assertEqual(self._get("IGV-S").list_price, 118.0)   # ya con IGV → tal cual

    def test_controla_stock_minimo_y_rastreo(self):
        res = self._import(
            ["CÓDIGO", "NOMBRE", "PRECIO VENTA", "CONTROLA STOCK", "STOCK MÍNIMO", "RASTREO"],
            [["ST-1", "CLAVO", 5, "Sí", 8, "Por lote"]])
        self.assertFalse(res.get("errores"), res)
        p = self._get("ST-1")
        self.assertTrue(p.is_storable)
        self.assertEqual(p.l10n_pe_ne_stock_minimo, 8.0)
        self.assertEqual(p.tracking, "lot")

    def test_detraccion_del_desplegable_toma_solo_el_codigo(self):
        """El desplegable trae 'CÓDIGO · descripción'; el parser guarda solo el código. También
        acepta el código pelado y rechaza basura."""
        res = self._import(
            ["CÓDIGO", "NOMBRE", "PRECIO VENTA", "DETRACCIÓN"],
            [["DET-1", "FLETE", 800, "027 · Servicio de transporte de carga"],
             ["DET-2", "ALQUILER", 800, "019"]])
        self.assertFalse(res.get("errores"), res)
        self.assertEqual(self._get("DET-1").l10n_pe_ne_detraccion_cod, "027")
        self.assertEqual(self._get("DET-2").l10n_pe_ne_detraccion_cod, "019")
        bad = self._import(["CÓDIGO", "NOMBRE", "PRECIO VENTA", "DETRACCIÓN"],
                           [["DET-X", "X", 800, "transporte"]])
        self.assertTrue(bad.get("errores"), bad)

    def test_activo_no_archiva_el_producto(self):
        res = self._import(
            ["CÓDIGO", "NOMBRE", "PRECIO VENTA", "ACTIVO"],
            [["ACT-N", "ARCHIVADO", 5, "No"], ["ACT-S", "VIGENTE", 5, "Sí"]])
        self.assertFalse(res.get("errores"), res)
        self.assertFalse(self.Product.with_context(active_test=False).search(
            [("default_code", "=", "ACT-N")]).active)
        self.assertTrue(self._get("ACT-S").active)
