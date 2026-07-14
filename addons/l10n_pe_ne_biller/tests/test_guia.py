import base64
import io
import zipfile
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestGuiaBase(TransactionCase):
    def setUp(self):
        super().setUp()
        self.Guia = self.env["l10n_pe_ne.guia_remision"]
        self.cliente = self.env["res.partner"].create({"name": "Cliente GRE", "vat": "20601030013"})
        self.producto = self.env["product.product"].create({"name": "Caja de tornillos"})

    def _vals(self, **extra):
        vals = {
            "partner_id": self.cliente.id,
            "ubigeo_partida": "150101", "dir_partida": "Av. Uno 100",
            "ubigeo_llegada": "150102", "dir_llegada": "Av. Dos 200",
            "num_placa": "ABC123", "conductor_num_doc": "12345678",
            "conductor_nombres": "Juan", "conductor_apellidos": "Pérez",
            "conductor_licencia": "Q12345678",
            "line_ids": [(0, 0, {"descripcion": "Caja de tornillos", "cantidad": 2,
                                  "product_id": self.producto.id})],
        }
        vals.update(extra)
        return vals


class TestGuiaNumeracion(TestGuiaBase):
    def test_correlativo_por_serie(self):
        g1 = self.Guia.create(self._vals())
        g2 = self.Guia.create(self._vals())
        g3 = self.Guia.create(self._vals(serie="T002"))
        self.assertEqual(g1.name, "T001-1")
        self.assertEqual(g2.name, "T001-2")
        self.assertEqual(g3.name, "T002-1")  # cada serie arranca en 1

    def test_correlativo_por_compania(self):
        self.Guia.create(self._vals())  # T001-1 en la compañía base
        otra = self.env["res.company"].create({"name": "Otra Empresa SAC", "vat": "20999999991"})
        g = self.Guia.with_company(otra).create(self._vals(company_id=otra.id))
        self.assertEqual(g.name, "T001-1")  # no comparte secuencia entre RUCs

    def test_siembra_tras_correlativo_existente(self):
        # Migración: guías numeradas por la secuencia global vieja no deben colisionar.
        g_viejo = self.Guia.create(self._vals())
        g_viejo.write({"serie": "T009", "correlativo": "41", "name": "T009-41"})
        g = self.Guia.create(self._vals(serie="T009"))
        self.assertEqual(g.name, "T009-42")

    def test_batch_create_misma_serie_nueva(self):
        g1, g2 = self.Guia.create([self._vals(serie="T005"), self._vals(serie="T005")])
        self.assertEqual(g1.name, "T005-1")
        self.assertEqual(g2.name, "T005-2")

    def test_indice_unico_secuencia_guia(self):
        # La carrera de creación concurrente debe morir en IntegrityError, no duplicar.
        from odoo.tools import mute_logger
        Seq = self.env["ir.sequence"].sudo()
        vals = {"name": "GRE T777 (test)", "code": "l10n_pe.ne.guia_remision.T777",
                "company_id": self.env.company.id, "padding": 1, "implementation": "no_gap"}
        Seq.create(vals)
        with mute_logger("odoo.sql_db"), self.assertRaises(Exception) as ctx:
            with self.env.cr.savepoint():
                Seq.create(dict(vals, name="GRE T777 duplicada"))
        self.assertIn("ir_sequence_gre_code_company_uniq", str(ctx.exception))


class TestGuiaMultiCompany(TestGuiaBase):
    def test_rule_aisla_companias(self):
        self.Guia.create(self._vals())
        otra = self.env["res.company"].create({"name": "Otra SAC", "vat": "20999999991"})
        user_b = self.env["res.users"].create({
            "name": "Emisor B", "login": "emisor_b_gre",
            "company_id": otra.id, "company_ids": [(6, 0, [otra.id])],
            "group_ids": [(4, self.env.ref("l10n_pe_ne_biller.group_l10n_pe_ne_emisor").id)],
        })
        visibles = self.Guia.with_user(user_b).with_company(otra).search([])
        self.assertFalse(visibles, "un emisor de otra compañía no debe ver estas guías")

    def test_list_filtra_por_company_activa(self):
        self.Guia.create(self._vals())
        otra = self.env["res.company"].create({"name": "Otra SAC 2", "vat": "20999999992"})
        res = self.Guia.with_company(otra).l10n_pe_ne_list_guias(offset=0)
        self.assertEqual(res["total"], 0)
