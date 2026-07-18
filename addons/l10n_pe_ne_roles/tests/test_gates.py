from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestGates(TransactionCase):
    """Motor de gates de política por RUC (iteración 4): off/aviso/bloquea, modo y umbral
    ortogonales, y el gate de escritura por supervisor/dueño."""

    def setUp(self):
        super().setUp()
        self.company = self.env.company

    def test_default_todo_off(self):
        """Un tenant nuevo no bloquea nada: todos los gates nacen 'off'."""
        for key in ("descuadre", "descuento", "credito", "gasto", "devolucion", "merma", "deposito"):
            self.assertEqual(self.company.l10n_pe_ne_gate(key), "off")

    def test_modo_y_umbral_ortogonales(self):
        # aviso con umbral 100: por debajo/igual no dispara; por encima -> aviso
        self.company.l10n_pe_ne_gate_descuadre = "aviso"
        self.company.l10n_pe_ne_umbral_descuadre = 100.0
        self.assertEqual(self.company.l10n_pe_ne_gate("descuadre", 50), "off")
        self.assertEqual(self.company.l10n_pe_ne_gate("descuadre", 100), "off")
        self.assertEqual(self.company.l10n_pe_ne_gate("descuadre", 150), "aviso")
        # bloquea + umbral 0 = tolerancia CERO (dispara con cualquier magnitud > 0)
        self.company.l10n_pe_ne_gate_descuadre = "bloquea"
        self.company.l10n_pe_ne_umbral_descuadre = 0.0
        self.assertEqual(self.company.l10n_pe_ne_gate("descuadre", 0), "off")
        self.assertEqual(self.company.l10n_pe_ne_gate("descuadre", 0.01), "bloquea")
        # off apaga aunque el umbral esté escrito
        self.company.l10n_pe_ne_gate_descuadre = "off"
        self.company.l10n_pe_ne_umbral_descuadre = 50.0
        self.assertEqual(self.company.l10n_pe_ne_gate("descuadre", 9999), "off")

    def test_gate_sin_umbral(self):
        # devolucion no tiene umbral: en 'bloquea' siempre dispara
        self.company.l10n_pe_ne_gate_devolucion = "bloquea"
        self.assertEqual(self.company.l10n_pe_ne_gate("devolucion"), "bloquea")

    def test_politicas_dict(self):
        self.company.l10n_pe_ne_gate_descuento = "aviso"
        self.company.l10n_pe_ne_umbral_descuento = 5.0
        d = self.company.l10n_pe_ne_politicas_dict()
        self.assertIn("descuento", d)
        self.assertEqual(d["descuento"]["modo"], "aviso")
        self.assertEqual(d["descuento"]["umbral"], 5.0)
        self.assertEqual(d["descuento"]["unidad"], "pct")
        self.assertTrue(d["descuento"]["aviso"])          # frase redactada por el backend
        self.assertIn("exigirSegregacion", d)
        self.assertFalse(d["exigirSegregacion"])

    def test_gate_desconocido(self):
        with self.assertRaises(UserError):
            self.company.l10n_pe_ne_gate("inventado")

    def test_set_politica_por_supervisor(self):
        # como admin (system) se permite
        self.env["res.company"].l10n_pe_ne_set_politica("gasto", "bloquea", 200.0)
        self.assertEqual(self.company.l10n_pe_ne_gate_gasto, "bloquea")
        self.assertEqual(self.company.l10n_pe_ne_umbral_gasto, 200.0)

    def test_set_politica_rechaza_no_supervisor(self):
        cajero = self.env["res.users"].create({
            "name": "cajero_g4", "login": "cajero_g4",
            "company_id": self.company.id, "company_ids": [(6, 0, [self.company.id])],
            "group_ids": [(4, self.env.ref("l10n_pe_ne_roles.group_l10n_pe_ne_caja").id)],
        })
        with self.assertRaises(AccessError):
            self.env["res.company"].with_user(cajero).l10n_pe_ne_set_politica("gasto", "bloquea", 10)

    def test_set_politica_modo_invalido(self):
        with self.assertRaises(UserError):
            self.env["res.company"].l10n_pe_ne_set_politica("gasto", "no_existe")

    def test_config_incluye_politicas(self):
        """/ne/api/config (l10n_pe_ne_config) ahora trae las políticas, sin quitar igv/icbperRate."""
        cfg = self.env["account.move"].l10n_pe_ne_config()
        self.assertIn("igv", cfg)
        self.assertIn("icbperRate", cfg)
        self.assertIn("politicas", cfg)
        self.assertIn("descuadre", cfg["politicas"])
