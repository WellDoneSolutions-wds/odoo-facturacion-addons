import io
import base64
import xlsxwriter
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestImportClientes(TransactionCase):
    """Import de clientes por Excel: UPSERT por número de documento, deducción del tipo por
    longitud, validación RUC(11)/DNI(8) y 'vacío = mantener' al actualizar."""

    def setUp(self):
        super().setUp()
        self.Move = self.env['account.move']
        self.Partner = self.env['res.partner']

    def _import(self, headers, rows, commit=True):
        buf = io.BytesIO()
        wb = xlsxwriter.Workbook(buf, {"in_memory": True})
        ws = wb.add_worksheet("Clientes")
        ws.write_row(0, 0, headers)
        for i, row in enumerate(rows, 1):
            ws.write_row(i, 0, row)
        wb.close()
        return self.Move.l10n_pe_ne_importar_clientes({
            "contentB64": base64.b64encode(buf.getvalue()).decode(), "commit": commit})

    def _get(self, num):
        return self.Partner.with_context(active_test=False).search([('vat', '=', num)], limit=1)

    HEAD = ["TIPO DE DOCUMENTO", "NÚMERO DE DOCUMENTO", "RAZÓN SOCIAL O NOMBRE", "EMAIL", "TELÉFONO", "DIRECCIÓN"]

    def test_crea_ruc_con_tipo_de_identificacion(self):
        res = self._import(self.HEAD, [["RUC", "20123456789", "ACME SAC", "a@acme.pe", "", "Av. 1"]])
        self.assertFalse(res.get("errores"), res)
        p = self._get("20123456789")
        self.assertEqual(p.name, "ACME SAC")
        self.assertEqual(p.l10n_latam_identification_type_id.l10n_pe_vat_code, "6")
        self.assertEqual(p.email, "a@acme.pe")
        self.assertTrue(p.customer_rank)

    def test_deduce_tipo_por_longitud(self):
        res = self._import(["NÚMERO DE DOCUMENTO", "RAZÓN SOCIAL O NOMBRE"],
                           [["20999999999", "EMPRESA X"], ["44556677", "PERSONA Y"]])
        self.assertFalse(res.get("errores"), res)
        self.assertEqual(self._get("20999999999").l10n_latam_identification_type_id.l10n_pe_vat_code, "6")
        self.assertEqual(self._get("44556677").l10n_latam_identification_type_id.l10n_pe_vat_code, "1")

    def test_upsert_por_documento_no_duplica(self):
        self._import(self.HEAD, [["RUC", "20111111111", "PRIMERO", "", "", ""]])
        res = self._import(self.HEAD, [["RUC", "20111111111", "SEGUNDO", "", "", ""]])
        self.assertFalse(res.get("errores"), res)
        self.assertEqual(res["actualizados"], 1)
        self.assertEqual(res["creados"], 0)
        self.assertEqual(self.Partner.search_count([('vat', '=', "20111111111")]), 1)
        self.assertEqual(self._get("20111111111").name, "SEGUNDO")

    def test_valida_ruc_11_y_dni_8(self):
        res = self._import(self.HEAD, [["RUC", "2012345", "MAL RUC", "", "", ""],
                                       ["DNI", "123", "MAL DNI", "", "", ""]])
        self.assertEqual(len(res.get("errores", [])), 2, res)
        self.assertEqual(res["creados"], 0)

    def test_update_vacio_mantiene_email(self):
        self._import(self.HEAD, [["RUC", "20222222222", "CON MAIL", "keep@x.pe", "", ""]])
        res = self._import(["NÚMERO DE DOCUMENTO", "RAZÓN SOCIAL O NOMBRE"],
                           [["20222222222", "CON MAIL"]])
        self.assertFalse(res.get("errores"), res)
        self.assertEqual(self._get("20222222222").email, "keep@x.pe")   # no lo borró

    def test_falta_numero_o_nombre_es_error(self):
        res = self._import(self.HEAD, [["RUC", "", "SIN NUM", "", "", ""],
                                       ["RUC", "20333333333", "", "", "", ""]])
        self.assertEqual(len(res.get("errores", [])), 2, res)
