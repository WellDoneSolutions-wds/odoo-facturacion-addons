# -*- coding: utf-8 -*-
"""La inicial al contado de una venta al crédito se muestra en el impreso (ticket + A4).

El inicial ya pagado NO va al XML SUNAT (allí solo van el saldo pendiente y las cuotas), así que
sin este bloque print-only el impreso mostraría cuotas que suman menos que el total, con un hueco
sin explicar. Ver _l10n_pe_ne_inicial_credito_lineas."""
from odoo.tests import TransactionCase, tagged

from .common import L10nPeSeedMixin


@tagged("post_install", "-at_install")
class TestInicialCreditoImpreso(L10nPeSeedMixin, TransactionCase):
    def setUp(self):
        super().setUp()  # L10nPeSeedMixin: RUC + IGV (self.igv)
        ruc_type = self.env["l10n_latam.identification.type"].search(
            [("l10n_pe_vat_code", "=", "6")], limit=1)
        self.partner = self.env["res.partner"].create({
            "name": "CLIENTE SAC", "vat": "20100070970",
            "l10n_latam_identification_type_id": ruc_type.id})
        self.product = self.env["product.product"].create({"name": "ITEM", "default_code": "I1"})

    def _factura(self, credito_cuotas=None, inicial=0.0):
        vals = {
            "move_type": "out_invoice", "partner_id": self.partner.id,
            "invoice_date": "2026-08-04", "l10n_pe_serie": "F001", "l10n_pe_correlativo": "1",
            "invoice_line_ids": [(0, 0, {
                "product_id": self.product.id, "quantity": 1.0, "price_unit": 100.0,
                "tax_ids": [(6, 0, self.igv.ids)]})],
        }
        if credito_cuotas is not None:
            vals.update({"l10n_pe_ne_forma_pago": "Credito", "l10n_pe_ne_cuotas": credito_cuotas})
        if inicial:
            vals["l10n_pe_ne_inicial_contado"] = inicial
        move = self.env["account.move"].create(vals)
        move.action_post()
        return move

    def test_credito_con_inicial_muestra_inicial_y_saldo(self):
        # Total 118 (100 + IGV). Inicial 18 → saldo a crédito 100 en 2 cuotas de 50.
        m = self._factura(
            credito_cuotas=[{"fecha": "2026-09-04", "monto": 50}, {"fecha": "2026-10-04", "monto": 50}],
            inicial=18)
        self.assertEqual(m._l10n_pe_ne_inicial_credito_lineas(), [
            "Inicial pagada al contado: S/ 18.00",
            "Saldo a crédito: S/ 100.00",
        ])
        # Aparece en el ticket 80mm (adicionalTxt) y en el A4 (MEDIOS_PAGO).
        self.assertIn("Inicial pagada al contado: S/ 18.00", m._l10n_pe_ne_ticket_adicional())
        self.assertIn("Saldo a crédito: S/ 100.00", m._l10n_pe_ne_medios_pago_a4())

    def test_credito_sin_inicial_no_muestra_bloque(self):
        m = self._factura(credito_cuotas=[{"fecha": "2026-09-04", "monto": 118}])
        self.assertEqual(m._l10n_pe_ne_inicial_credito_lineas(), [])

    def test_contado_no_muestra_inicial(self):
        m = self._factura()  # sin forma de pago a crédito
        self.assertEqual(m._l10n_pe_ne_inicial_credito_lineas(), [])
