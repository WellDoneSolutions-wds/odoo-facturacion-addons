# -*- coding: utf-8 -*-
"""V11 · Facturación recurrente: emisión por cron, avance de fecha y validaciones."""
import json
from contextlib import contextmanager
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from .common import EnvioSincronoMixin, L10nPeSeedMixin

_TARGET = "odoo.addons.l10n_pe_ne_biller.models.account_move_biller.requests.post"


@contextmanager
def _biller_ok():
    """Simula el biller aceptando el XML (mismo doble que usan los tests de emisión)."""
    ok = type("R", (), {"status_code": 200, "text": '<?xml version="1.0"?><Invoice/>',
                        "headers": {}})()
    with patch(_TARGET, return_value=ok):
        yield


@tagged("post_install", "-at_install")
class TestRecurrencia(EnvioSincronoMixin, L10nPeSeedMixin, TransactionCase):
    def _rec(self, **kw):
        partner = kw.pop("partner", None) or self.env["res.partner"].create({
            "name": "SOCIO GYM", "vat": "44556677", "company_id": self.env.company.id})
        vals = {
            "name": "Membresía mensual", "partner_id": partner.id, "monto": 118.0,
            "tax_code": "1000", "tipo_doc": "03", "periodicidad": "mensual",
            "dia_emision": 1, "proxima_fecha": fields.Date.context_today(self.env["res.users"]),
            "company_id": self.env.company.id,
        }
        vals.update(kw)
        return self.env["l10n_pe_ne.recurrencia"].create(vals)

    def test_emitir_avanza_fecha_y_referencia(self):
        rec = self._rec()
        hoy = fields.Date.context_today(rec)
        with _biller_ok():
            res = rec.l10n_pe_ne_emitir_una()
        self.assertTrue(res.get("id"))
        self.assertTrue(rec.ultima_move_id)
        self.assertFalse(rec.ultimo_error)
        self.assertGreater(rec.proxima_fecha, hoy)          # avanzó al siguiente período
        self.assertEqual(rec.proxima_fecha.day, rec.dia_emision)
        # La boleta salió por el motor normal: total = monto CON IGV de la membresía.
        self.assertAlmostEqual(rec.ultima_move_id.amount_total, 118.0, places=2)

    def test_cron_emite_pendientes_y_salta_futuras(self):
        rec_hoy = self._rec()
        rec_futura = self._rec(
            partner=self.env["res.partner"].create({
                "name": "SOCIO 2", "vat": "44556678", "company_id": self.env.company.id}),
            proxima_fecha=fields.Date.add(fields.Date.context_today(self.env["res.users"]), months=1))
        with _biller_ok():
            self.env["l10n_pe_ne.recurrencia"]._l10n_pe_ne_cron_recurrencias()
        self.assertTrue(rec_hoy.ultima_move_id)
        self.assertFalse(rec_futura.ultima_move_id)   # aún no le toca

    def test_cron_error_no_avanza_y_queda_visible(self):
        rec = self._rec(monto=118.0)
        fecha0 = rec.proxima_fecha
        # Sabotea la emisión: sin diario de ventas la emisión revienta con UserError.
        self.env["account.journal"].search(
            [("type", "=", "sale"), ("company_id", "=", self.env.company.id)]).write(
            {"type": "general"})
        self.env["l10n_pe_ne.recurrencia"]._l10n_pe_ne_cron_recurrencias()
        self.assertTrue(rec.ultimo_error)
        self.assertEqual(rec.proxima_fecha, fecha0)   # NO avanzó: reintenta mañana

    def test_factura_exige_ruc(self):
        with self.assertRaises(UserError):
            self._rec(tipo_doc="01")   # el socio tiene DNI, no RUC

    def test_dia_29_no_permitido(self):
        with self.assertRaises(UserError):
            self._rec(dia_emision=29)

    def test_save_respeta_muro_v11(self):
        # bodega no trae V11: el alta por API debe rebotar con el muro de la Capa 1.
        self.env.company.sudo().write({
            "l10n_pe_ne_rubros": json.dumps(["bodega"]),
            "l10n_pe_ne_modulos_override": "{}"})
        user = self.env["res.users"].sudo().create({
            "name": "Emisor V11", "login": "emisor.v11@test",
            "company_id": self.env.company.id, "company_ids": [(6, 0, [self.env.company.id])],
            "group_ids": [(4, self.env.ref("base.group_user").id),
                          (4, self.env.ref("l10n_pe_ne_biller.group_l10n_pe_ne_emisor").id)],
        })
        partner = self.env["res.partner"].create({
            "name": "SOCIO 3", "vat": "44556679", "company_id": self.env.company.id})
        with self.assertRaises(UserError):
            self.env["l10n_pe_ne.recurrencia"].with_user(user).l10n_pe_ne_save({
                "concepto": "Membresía", "clienteId": partner.id, "monto": 100})
        # gimnasio SÍ trae V11: pasa.
        self.env.company.sudo().l10n_pe_ne_rubros = json.dumps(["gimnasio"])
        d = self.env["l10n_pe_ne.recurrencia"].with_user(user).l10n_pe_ne_save({
            "concepto": "Membresía", "clienteId": partner.id, "monto": 100})
        self.assertTrue(d["id"])
        self.assertEqual(d["proximaFecha"][:4], str(fields.Date.context_today(self.env["res.users"]).year))
