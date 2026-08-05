# -*- coding: utf-8 -*-
"""R11 · Variantes: generación cartesiana, idempotencia, tope y muro."""
import json

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from .common import L10nPeSeedMixin


@tagged("post_install", "-at_install")
class TestVariantes(L10nPeSeedMixin, TransactionCase):
    def setUp(self):
        super().setUp()
        self.env.company.sudo().write({
            "l10n_pe_ne_rubros": json.dumps(["ropa-calzado"]),
            "l10n_pe_ne_modulos_override": "{}"})
        self.AM = self.env["account.move"]
        self.base = self.env["product.product"].create({
            "name": "Polo básico", "default_code": "POLO", "list_price": 39.9})

    def test_genera_cartesiano_y_es_idempotente(self):
        r = self.AM.l10n_pe_ne_generar_variantes({
            "productId": self.base.id,
            "atributos": {"Talla": ["S", "M"], "Color": ["Rojo", "Azul"]}})
        self.assertEqual(len(r["creados"]), 4)
        nombres = {c["nombre"] for c in r["creados"]}
        self.assertIn("Polo básico — S / Rojo", nombres)
        hijo = self.env["product.product"].browse(r["creados"][0]["id"])
        self.assertEqual(hijo.product_tmpl_id.l10n_pe_ne_variante_de,
                         self.base.product_tmpl_id)
        self.assertAlmostEqual(hijo.list_price, 39.9, places=2)   # hereda el precio
        self.assertIn("POLO-", hijo.default_code)
        # Re-ejecutar con una talla nueva: solo crea las 2 nuevas (S/M ya existen).
        r2 = self.AM.l10n_pe_ne_generar_variantes({
            "productId": self.base.id,
            "atributos": {"Talla": ["S", "M", "L"], "Color": ["Rojo", "Azul"]}})
        self.assertEqual(len(r2["creados"]), 2)
        self.assertEqual(r2["omitidos"], 4)

    def test_tope_de_combinaciones(self):
        with self.assertRaises(UserError):
            self.AM.l10n_pe_ne_generar_variantes({
                "productId": self.base.id,
                "atributos": {"Talla": [str(i) for i in range(10)],
                              "Color": [str(i) for i in range(10)]}})

    def test_muro_r11(self):
        self.env.company.sudo().l10n_pe_ne_rubros = json.dumps(["bodega"])
        user = self.env["res.users"].sudo().create({
            "name": "Emisor R11", "login": "emisor.r11@test",
            "company_id": self.env.company.id, "company_ids": [(6, 0, [self.env.company.id])],
            "group_ids": [(4, self.env.ref("base.group_user").id),
                          (4, self.env.ref("l10n_pe_ne_biller.group_l10n_pe_ne_emisor").id)],
        })
        with self.assertRaises(UserError):
            self.AM.with_user(user).l10n_pe_ne_generar_variantes({
                "productId": self.base.id, "atributos": {"Talla": ["S"]}})
