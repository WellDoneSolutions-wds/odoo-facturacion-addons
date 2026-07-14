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
