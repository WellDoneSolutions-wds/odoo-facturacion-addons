from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestFlota(TransactionCase):
    def test_vehiculo_upsert_por_placa(self):
        V = self.env["l10n_pe_ne.vehiculo"]
        v1 = V.l10n_pe_ne_upsert({"placa": "bet714", "entAutorizacion": "06", "numAutorizacion": "00786756"})
        self.assertEqual(v1.placa, "BET714")  # normaliza a mayúsculas
        v2 = V.l10n_pe_ne_upsert({"placa": "BET714", "numAutorizacion": "X1"})
        self.assertEqual(v1.id, v2.id)  # misma placa+company = mismo registro, actualizado
        self.assertEqual(v2.num_autorizacion, "X1")

    def test_conductor_upsert_por_doc(self):
        C = self.env["l10n_pe_ne.conductor"]
        c1 = C.l10n_pe_ne_upsert({"tipoDoc": "1", "numDoc": "71958406", "nombres": "Hernan",
                                  "apellidos": "Vilca Masco", "licencia": "U71958406"})
        c2 = C.l10n_pe_ne_upsert({"tipoDoc": "1", "numDoc": "71958406", "licencia": "NUEVA123"})
        self.assertEqual(c1.id, c2.id)
        self.assertEqual(c2.licencia, "NUEVA123")
        self.assertEqual(c2.nombres, "Hernan")  # lo no enviado no se pisa

    def test_serializacion(self):
        V = self.env["l10n_pe_ne.vehiculo"]
        V.l10n_pe_ne_upsert({"placa": "ABC123"})
        items = V.l10n_pe_ne_list()
        self.assertTrue(any(i["placa"] == "ABC123" for i in items))
