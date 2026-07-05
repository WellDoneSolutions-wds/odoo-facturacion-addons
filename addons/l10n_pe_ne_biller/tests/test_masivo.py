# -*- coding: utf-8 -*-
import base64
import io
import json

import xlsxwriter
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, tagged

# Patch donde se USA requests.post (dentro del módulo del modelo base), igual que test_send.py.
_TARGET = "odoo.addons.l10n_pe_ne_biller.models.account_move_biller.requests.post"
_HEADERS = ["venta", "tipo", "serie", "fecha", "tipo doc cliente", "num doc cliente",
            "cliente", "codigo producto", "producto", "cantidad", "precio unitario",
            "descuento %", "afectacion", "bolsa", "moneda"]
# XML "firmado" mínimo que action_l10n_pe_send_to_biller reconoce como aceptado (contiene <Invoice).
_SIGNED = '<?xml version="1.0"?><Invoice xmlns="urn:x"><ext:UBLExtensions/></Invoice>'


def _xlsx_b64(rows, headers=_HEADERS):
    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {"in_memory": True})
    ws = wb.add_worksheet("Ventas")
    for c, h in enumerate(headers):
        ws.write(0, c, h)
    for r, row in enumerate(rows, 1):
        for c, val in enumerate(row):
            ws.write(r, c, val)
    wb.close()
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _resp(code, text, headers=None):
    return type("R", (), {"status_code": code, "text": text, "headers": headers or {}})()


@tagged("post_install", "-at_install")
class TestMasivo(TransactionCase):
    def setUp(self):
        super().setUp()
        self.Lote = self.env["l10n_pe_ne.lote"]
        self.Fila = self.env["l10n_pe_ne.lote.fila"]
        self.company_b = self.env["res.company"].create(
            {"name": "OTRO RUC SAC", "vat": "20999999991"})
        grp = self.env.ref("l10n_pe_ne_biller.group_l10n_pe_ne_emisor")
        self.user_b = self.env["res.users"].create({
            "name": "Emisor B", "login": "emisor_b_masivo",
            "company_id": self.company_b.id, "company_ids": [(6, 0, [self.company_b.id])],
            "group_ids": [(4, grp.id)]})

    def _lote_simple(self, estado="validado"):
        lote = self.Lote.create(
            {"name": "x.xlsx", "estado": estado, "total_comprobantes": 1, "total_filas": 1})
        self.Fila.create(
            {"lote_id": lote.id, "secuencia": 1, "payload_json": "{}", "estado": "pendiente"})
        return lote

    def test_defaults_y_aislamiento(self):
        lote = self._lote_simple()
        self.assertEqual(lote.estado, "validado")
        self.assertEqual(lote.company_id, self.env.company)
        self.assertEqual(lote.fila_ids.company_id, self.env.company,
                         "la fila hereda company_id del lote (related store)")
        # Compañía B no ve el lote ni la fila (regla ir.rule global).
        self.assertFalse(self.Lote.with_user(self.user_b).search([("id", "=", lote.id)]))
        self.assertFalse(self.Fila.with_user(self.user_b).search([("lote_id", "=", lote.id)]))
        with self.assertRaises(AccessError):
            lote.with_user(self.user_b).read(["estado"])

    def test_masivo_param_default_y_override(self):
        lote = self._lote_simple()
        self.assertEqual(lote._masivo_param("masivo_max_filas", 500), 500)
        self.env["ir.config_parameter"].sudo().set_param("l10n_pe_ne.masivo_max_filas", "10")
        self.assertEqual(lote._masivo_param("masivo_max_filas", 500), 10)
        self.env["ir.config_parameter"].sudo().set_param("l10n_pe_ne.masivo_max_filas", "0")
        self.assertEqual(lote._masivo_param("masivo_max_filas", 500), 500, "valor <=0 cae al default")
