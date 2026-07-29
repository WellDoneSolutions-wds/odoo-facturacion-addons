from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestRedondeoEfectivo(TransactionCase):
    """Redondeo de efectivo (Ley 29571 / retiro de monedas < S/ 0.10): ajuste de CAJA, no del
    comprobante. El amount_total y el XML no cambian; el ticket muestra 'A pagar efectivo' y el
    arqueo espera 'amount_total + redondeo' de efectivo. Flag/modo configurables por compañía."""

    def setUp(self):
        super().setUp()
        self.Move = self.env["account.move"]
        self.company = self.env.company
        self.igv = self.env["account.tax"].search([
            ("company_id", "=", self.company.id), ("type_tax_use", "=", "sale"),
            ("l10n_pe_edi_tax_code", "=", "1000")], limit=1)
        ruc_type = self.env["l10n_latam.identification.type"].search(
            [("l10n_pe_vat_code", "=", "6")], limit=1)
        self.partner = self.env["res.partner"].create({
            "name": "CLIENTE SAC", "vat": "20100070970",
            "l10n_latam_identification_type_id": ruc_type.id})
        self.product = self.env["product.product"].create({"name": "PRODUCTO", "default_code": "P1"})

    # ---------------------------------------------------------------- config / negocio
    def test_config_default_activo_favor(self):
        cfg = self.Move.l10n_pe_ne_config()
        self.assertTrue(cfg["redondeoActivo"])
        self.assertEqual(cfg["redondeoModo"], "favor")

    def test_negocio_round_trip(self):
        self.Move.l10n_pe_ne_update_negocio({"redondeoActivo": False, "redondeoModo": "cercano"})
        neg = self.Move.l10n_pe_ne_negocio()
        self.assertFalse(neg["redondeoActivo"])
        self.assertEqual(neg["redondeoModo"], "cercano")
        self.assertFalse(self.company.l10n_pe_ne_redondeo_activo)
        self.assertEqual(self.company.l10n_pe_ne_redondeo_modo, "cercano")

    # ---------------------------------------------------------------- helper solo-efectivo
    def test_solo_efectivo(self):
        f = self.Move._l10n_pe_ne_solo_efectivo
        self.assertTrue(f([]))  # sin medios → efectivo inferido
        self.assertTrue(f([{"medio": "Efectivo", "monto": 10}]))
        self.assertFalse(f([{"medio": "Yape", "monto": 10}]))
        self.assertFalse(f([{"medio": "Efectivo", "monto": 5}, {"medio": "Tarjeta", "monto": 5}]))

    # ---------------------------------------------------------------- ticket 80mm
    def _factura(self, redondeo=None, medios=None):
        vals = {
            "move_type": "out_invoice", "partner_id": self.partner.id, "invoice_date": "2026-06-20",
            "l10n_pe_serie": "B001", "l10n_pe_correlativo": "1",
            "invoice_line_ids": [(0, 0, {
                "product_id": self.product.id, "quantity": 1.0,
                "price_unit": 8.85, "tax_ids": [(6, 0, self.igv.ids)]})],
        }
        if medios is not None:
            vals["l10n_pe_ne_medios_pago"] = medios
        if redondeo is not None:
            vals["l10n_pe_ne_redondeo"] = redondeo
        move = self.Move.create(vals)
        move.action_post()
        return move

    def test_ticket_muestra_redondeo_y_a_pagar(self):
        # 8.85 + IGV = 10.44; a favor del consumidor -> a pagar 10.40, redondeo -0.04.
        move = self._factura(redondeo=-0.04, medios=[{"medio": "Efectivo", "monto": 10.40}])
        self.assertEqual(move.amount_total, 10.44)  # el comprobante NO cambia
        txt = move._l10n_pe_ne_ticket_adicional()
        self.assertIn("Redondeo: S/ -0.04", txt)
        self.assertIn("A pagar efectivo: S/ 10.40", txt)
        self.assertNotIn("Vuelto", txt)  # pagó justo el redondeado

    def test_ticket_vuelto_sobre_redondeado(self):
        # Paga con S/ 20.00 un total redondeado a 10.40 -> vuelto 9.60 (no 9.56).
        move = self._factura(redondeo=-0.04, medios=[{"medio": "Efectivo", "monto": 20.0}])
        txt = move._l10n_pe_ne_ticket_adicional()
        self.assertIn("Vuelto: S/ 9.60", txt)

    def test_ticket_sin_redondeo_no_muestra_lineas(self):
        move = self._factura(medios=[{"medio": "Yape", "monto": 10.44}])
        txt = move._l10n_pe_ne_ticket_adicional()
        self.assertNotIn("Redondeo", txt)
        self.assertNotIn("A pagar efectivo", txt)

    # ---------------------------------------------------------------- caja: total cobrado
    def test_caja_suma_redondeo_al_total(self):
        move = self._factura(redondeo=-0.04, medios=[{"medio": "Efectivo", "monto": 10.40}])
        # El arqueo cuenta lo realmente cobrado en efectivo: amount_total + redondeo.
        total = (move.amount_total or 0.0) + (move.l10n_pe_ne_redondeo or 0.0)
        self.assertAlmostEqual(total, 10.40, places=2)
