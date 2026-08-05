# -*- coding: utf-8 -*-
"""V09 · Apartado (layaway): abonos, saldo, entrega con emisión y muro."""
import json
from contextlib import contextmanager
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from .common import EnvioSincronoMixin, L10nPeSeedMixin

_TARGET = "odoo.addons.l10n_pe_ne_biller.models.account_move_biller.requests.post"


@contextmanager
def _biller_ok():
    ok = type("R", (), {"status_code": 200, "text": '<?xml version="1.0"?><Invoice/>',
                        "headers": {}})()
    with patch(_TARGET, return_value=ok):
        yield


@tagged("post_install", "-at_install")
class TestApartado(EnvioSincronoMixin, L10nPeSeedMixin, TransactionCase):
    def setUp(self):
        super().setUp()
        # rubro con layaway (apartado trae V09)
        self.env.company.sudo().write({
            "l10n_pe_ne_rubros": json.dumps(["apartado"]),
            "l10n_pe_ne_modulos_override": "{}"})
        self.Ap = self.env["l10n_pe_ne.apartado"]

    def _alta(self, total=300.0):
        return self.Ap.l10n_pe_ne_save({
            "cliente": "MARIA QUISPE", "descripcion": "Refrigeradora LG 250L", "total": total})

    def test_abonos_y_saldo(self):
        d = self._alta(300)
        d = self.Ap.l10n_pe_ne_abonar(d["id"], {"monto": 100, "medio": "Efectivo"})
        d = self.Ap.l10n_pe_ne_abonar(d["id"], {"monto": 50, "medio": "Yape"})
        self.assertEqual(d["abonado"], 150.0)
        self.assertEqual(d["saldo"], 150.0)
        self.assertEqual(len(d["abonos"]), 2)
        with self.assertRaises(UserError):   # un abono no puede exceder el saldo
            self.Ap.l10n_pe_ne_abonar(d["id"], {"monto": 200})

    def test_entregar_exige_saldo_cero_y_emite(self):
        d = self._alta(236)
        with self.assertRaises(UserError):
            self.Ap.l10n_pe_ne_entregar(d["id"])   # aún debe todo
        self.Ap.l10n_pe_ne_abonar(d["id"], {"monto": 236})
        with _biller_ok():
            d2 = self.Ap.l10n_pe_ne_entregar(d["id"])
        self.assertEqual(d2["estado"], "entregado")
        ap = self.Ap.browse(d["id"])
        self.assertTrue(ap.move_id)
        # La boleta final sale por el TOTAL pactado (S/ 236 con IGV).
        self.assertAlmostEqual(ap.move_id.amount_total, 236.0, delta=0.02)
        with self.assertRaises(UserError):   # entregado no se cancela
            self.Ap.l10n_pe_ne_cancelar(d["id"])

    def test_cancelar_activo(self):
        d = self._alta(100)
        d2 = self.Ap.l10n_pe_ne_cancelar(d["id"])
        self.assertEqual(d2["estado"], "cancelado")

    def test_muro_v09(self):
        self.env.company.sudo().l10n_pe_ne_rubros = json.dumps(["bodega"])
        user = self.env["res.users"].sudo().create({
            "name": "Emisor V09", "login": "emisor.v09@test",
            "company_id": self.env.company.id, "company_ids": [(6, 0, [self.env.company.id])],
            "group_ids": [(4, self.env.ref("base.group_user").id),
                          (4, self.env.ref("l10n_pe_ne_biller.group_l10n_pe_ne_emisor").id)],
        })
        with self.assertRaises(UserError):
            self.Ap.with_user(user).l10n_pe_ne_save({
                "cliente": "X", "descripcion": "Y", "total": 100})
