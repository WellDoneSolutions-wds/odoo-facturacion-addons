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
        with self.assertRaises(AccessError):
            lote.fila_ids.with_user(self.user_b).read(["estado"])

    def test_masivo_param_default_y_override(self):
        lote = self._lote_simple()
        self.assertEqual(lote._masivo_param("masivo_max_filas", 500), 500)
        self.env["ir.config_parameter"].sudo().set_param("l10n_pe_ne.masivo_max_filas", "10")
        self.assertEqual(lote._masivo_param("masivo_max_filas", 500), 10)
        self.env["ir.config_parameter"].sudo().set_param("l10n_pe_ne.masivo_max_filas", "0")
        self.assertEqual(lote._masivo_param("masivo_max_filas", 500), 500, "valor <=0 cae al default")

    # -------------------------------------------------- QW10 Task 2: parse + validación
    def test_plantilla_descargable(self):
        out = self.Lote.l10n_pe_ne_plantilla()
        self.assertTrue(out["filename"].endswith(".xlsx"))
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(base64.b64decode(out["contentB64"])))
        self.assertIn("Ventas", wb.sheetnames)
        self.assertIn("Instrucciones", wb.sheetnames)
        ws = wb["Ventas"]
        self.assertEqual([ws.cell(1, c + 1).value for c in range(15)], _HEADERS)

    def test_parse_y_agrupado(self):
        b64 = _xlsx_b64([
            ["V-1", "FACTURA", "F001", "01/07/2026", "RUC", "20100070970", "FERRETERIA LA UNION SAC", "CEM-01", "CEMENTO", 2, 33.90, 0, "GRAVADO", "NO", "PEN"],
            ["V-1", "FACTURA", "F001", "01/07/2026", "RUC", "20100070970", "FERRETERIA LA UNION SAC", "CLV-02", "CLAVOS", 5, 4.50, 10, "GRAVADO", "NO", "PEN"],
            ["", "BOLETA", "B001", "01/07/2026", "DNI", "45678912", "ROSA QUISPE", "", "PINTURA", 1, 45.00, 0, "GRAVADO", "SI", "PEN"],
        ])
        rep = self.Lote.l10n_pe_ne_crear_lote({"filename": "v.xlsx", "contentB64": b64})
        self.assertEqual(rep["estado"], "validado")
        self.assertEqual(rep["errores"], [])
        self.assertEqual(rep["totalFilas"], 3)
        self.assertEqual(rep["totalComprobantes"], 2)
        lote = self.Lote.browse(rep["id"])
        f1 = lote.fila_ids.filtered(lambda f: f.secuencia == 1)
        p1 = json.loads(f1.payload_json)
        self.assertEqual(p1["tipoDoc"], "01")
        self.assertEqual(p1["serie"], "F001")
        self.assertEqual(p1["cliente"], {"tipoDoc": "6", "numDoc": "20100070970", "razonSocial": "FERRETERIA LA UNION SAC"})
        self.assertEqual(len(p1["lineas"]), 2)
        self.assertEqual(p1["lineas"][0]["taxCode"], "1000")
        self.assertEqual(p1["lineas"][0]["productCod"], "CEM-01")
        self.assertEqual(f1.filas_excel, "2-3")
        p2 = json.loads(lote.fila_ids.filtered(lambda f: f.secuencia == 2).payload_json)
        self.assertEqual(p2["tipoDoc"], "03")
        self.assertEqual(p2["cliente"]["tipoDoc"], "1")
        self.assertTrue(p2["lineas"][0]["icbper"])

    def test_validacion_reporte_por_fila(self):
        b64 = _xlsx_b64([
            ["", "FACTURA", "F001", "", "RUC", "20100070971", "MAL RUC SAC", "", "ITEM", 1, 10.0, 0, "GRAVADO", "NO", "PEN"],   # fila 2: RUC dígito malo
            ["", "BOLETA", "", "", "", "", "", "", "ITEM", 0, 5.0, 0, "GRAVADO", "NO", "PEN"],                                    # fila 3: cantidad 0
            ["", "FACTURA", "B001", "", "RUC", "20100070970", "OK SAC", "", "ITEM", 1, 10.0, 0, "GRAVADO", "NO", "PEN"],          # fila 4: serie B en factura
        ])
        rep = self.Lote.l10n_pe_ne_crear_lote({"filename": "v.xlsx", "contentB64": b64})
        self.assertEqual(rep["estado"], "con_errores")
        filas_err = {e["filaExcel"] for e in rep["errores"]}
        self.assertTrue({2, 3, 4} <= filas_err)
        msg2 = next(e["mensaje"] for e in rep["errores"] if e["filaExcel"] == 2)
        self.assertIn("dígito verificador", msg2)
        lote = self.Lote.browse(rep["id"])
        self.assertFalse(lote.fila_ids, "un lote con errores no crea filas procesables")

    def test_limites_y_duplicado(self):
        self.env["ir.config_parameter"].sudo().set_param("l10n_pe_ne.masivo_max_filas", "2")
        big = _xlsx_b64([["", "BOLETA", "", "", "", "", "", "", "IT", 1, 1.0, 0, "GRAVADO", "NO", "PEN"]] * 3)
        with self.assertRaises(UserError):
            self.Lote.l10n_pe_ne_crear_lote({"filename": "big.xlsx", "contentB64": big})
        self.env["ir.config_parameter"].sudo().set_param("l10n_pe_ne.masivo_max_filas", "500")
        b64 = _xlsx_b64([["", "BOLETA", "", "", "", "", "", "", "AGUA", 1, 1.5, 0, "GRAVADO", "NO", "PEN"]])
        r1 = self.Lote.l10n_pe_ne_crear_lote({"filename": "a.xlsx", "contentB64": b64})
        r2 = self.Lote.l10n_pe_ne_crear_lote({"filename": "a2.xlsx", "contentB64": b64})
        self.assertIsNone(r1["duplicadoDe"])
        self.assertEqual(r2["duplicadoDe"], r1["id"])
