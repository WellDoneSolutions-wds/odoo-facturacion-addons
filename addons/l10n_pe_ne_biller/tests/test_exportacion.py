from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestExportacion(TransactionCase):
    """Factura de exportación (SUNAT tipOperacion 0200): todas las líneas con afectación IGV 40
    (código 9995, sin IGV). Se emite como Factura (01) aunque el adquirente extranjero no tenga
    RUC, y la cabecera lleva el país del cliente (codPaisCliente) para el AdditionalHeader."""

    def setUp(self):
        super().setUp()
        self.company = self.env.company

        def sale_tax(code):
            return self.env["account.tax"].search([
                ("company_id", "=", self.company.id), ("type_tax_use", "=", "sale"),
                ("l10n_pe_edi_tax_code", "=", code)], limit=1)

        self.igv = sale_tax("1000")
        self.exp = sale_tax("9995")
        # Adquirente extranjero: sin RUC, identificado con pasaporte, país EE.UU.
        us = self.env.ref("base.us")
        pas = self.env["l10n_latam.identification.type"].search(
            [("l10n_pe_vat_code", "=", "7")], limit=1)  # 7 = Pasaporte
        self.foreign = self.env["res.partner"].create({
            "name": "FRESH IMPORTS LLC", "country_id": us.id,
            "l10n_latam_identification_type_id": pas.id or False, "vat": "X1234567"})
        self.product = self.env["product.product"].create({"name": "PALTA HASS", "default_code": "EXP1"})

    def _line(self, tax, price=5000.0):
        return (0, 0, {"product_id": self.product.id, "quantity": 1.0,
                       "price_unit": price, "tax_ids": [(6, 0, tax.ids)]})

    def _invoice(self, lines, partner=None):
        move = self.env["account.move"].create({
            "move_type": "out_invoice", "partner_id": (partner or self.foreign).id,
            "invoice_date": "2026-06-20", "l10n_pe_serie": "F001", "l10n_pe_correlativo": "1",
            "invoice_line_ids": lines})
        move.action_post()
        return move

    def test_tip_operacion_0200_cuando_todo_es_exportacion(self):
        if not self.exp:
            self.skipTest("No hay tax de exportación (9995) en el plan")
        move = self._invoice([self._line(self.exp)])
        self.assertEqual(move._l10n_pe_tipo_operacion(), "0200")

    def test_documento_es_factura_01_sin_ruc(self):
        if not self.exp:
            self.skipTest("No hay tax de exportación (9995) en el plan")
        move = self._invoice([self._line(self.exp)])
        # Adquirente sin RUC pero exportación → Factura (01), no Boleta (03).
        self.assertEqual(move._l10n_pe_document_type(), "01")
        self.assertEqual(move._l10n_pe_serie_prefix(), "F")

    def test_cabecera_lleva_pais_del_cliente(self):
        if not self.exp:
            self.skipTest("No hay tax de exportación (9995) en el plan")
        payload = self._invoice([self._line(self.exp)])._l10n_pe_build_invoice_request()
        cab = payload["cabecera"]
        self.assertEqual(cab["tipOperacion"], "0200")
        self.assertEqual(cab["adicionalCabecera"]["codPaisCliente"], "US")
        # Afectación de la línea = 40 (exportación), sin IGV.
        det = payload["detalle"][0]
        self.assertEqual(det["tipAfeIGV"], "40")

    def test_igv_cero_en_exportacion(self):
        if not self.exp:
            self.skipTest("No hay tax de exportación (9995) en el plan")
        move = self._invoice([self._line(self.exp)])
        self.assertEqual(move.amount_tax, 0.0)
        self.assertEqual(move.amount_total, 5000.0)

    def test_mixta_no_es_exportacion(self):
        if not self.exp:
            self.skipTest("No hay tax de exportación (9995) en el plan")
        move = self._invoice([self._line(self.exp), self._line(self.igv, 118.0)])
        # Con una línea gravada, ya no es exportación pura → venta interna 0101, sin país.
        self.assertEqual(move._l10n_pe_tipo_operacion(), "0101")
        adic = move._l10n_pe_adicional_cabecera() or {}
        self.assertNotIn("codPaisCliente", adic)
