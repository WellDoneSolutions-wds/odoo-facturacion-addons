# -*- coding: utf-8 -*-
"""R10 · Agenda de citas: CRUD, validación de hora y muro por rubro."""
import json

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from .common import L10nPeSeedMixin


@tagged("post_install", "-at_install")
class TestCita(L10nPeSeedMixin, TransactionCase):
    def setUp(self):
        super().setUp()
        # rubro con agenda (spa-estetica trae R10) para que el muro deje pasar
        self.env.company.sudo().write({
            "l10n_pe_ne_rubros": json.dumps(["spa-estetica"]),
            "l10n_pe_ne_modulos_override": "{}"})
        self.Cita = self.env["l10n_pe_ne.cita"]

    def test_alta_lista_por_dia_y_orden_por_hora(self):
        hoy = str(fields.Date.context_today(self.Cita))
        self.Cita.l10n_pe_ne_save({"servicio": "Corte", "cliente": "ROSA", "fecha": hoy, "hora": "11:00"})
        self.Cita.l10n_pe_ne_save({"servicio": "Tinte", "cliente": "ANA", "fecha": hoy, "hora": "09:30"})
        agenda = self.Cita.l10n_pe_ne_list(hoy)
        self.assertEqual([c["hora"] for c in agenda], ["09:30", "11:00"])   # orden natural
        self.assertEqual(agenda[0]["estado"], "pendiente")

    def test_hora_invalida(self):
        hoy = str(fields.Date.context_today(self.Cita))
        with self.assertRaises(UserError):
            self.Cita.l10n_pe_ne_save({"servicio": "X", "cliente": "Y", "fecha": hoy, "hora": "25:00"})

    def test_editar_estado_y_eliminar(self):
        hoy = str(fields.Date.context_today(self.Cita))
        d = self.Cita.l10n_pe_ne_save({"servicio": "Baño", "cliente": "FIRULAIS", "fecha": hoy, "hora": "10:00"})
        d2 = self.Cita.l10n_pe_ne_save({**d, "estado": "atendida"})
        self.assertEqual(d2["estado"], "atendida")
        self.Cita.l10n_pe_ne_delete(d["id"])
        self.assertFalse(self.Cita.browse(d["id"]).exists())

    def test_muro_r10(self):
        # bodega no tiene agenda: el alta rebota (con un usuario NO admin).
        self.env.company.sudo().l10n_pe_ne_rubros = json.dumps(["bodega"])
        user = self.env["res.users"].sudo().create({
            "name": "Emisor R10", "login": "emisor.r10@test",
            "company_id": self.env.company.id, "company_ids": [(6, 0, [self.env.company.id])],
            "group_ids": [(4, self.env.ref("base.group_user").id),
                          (4, self.env.ref("l10n_pe_ne_biller.group_l10n_pe_ne_emisor").id)],
        })
        hoy = str(fields.Date.context_today(self.Cita))
        with self.assertRaises(UserError):
            self.Cita.with_user(user).l10n_pe_ne_save(
                {"servicio": "Corte", "cliente": "ROSA", "fecha": hoy, "hora": "11:00"})
