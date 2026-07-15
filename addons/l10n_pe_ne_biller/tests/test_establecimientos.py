from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestEstablecimientos(TransactionCase):
    def test_lista_incluye_domicilio_fiscal(self):
        E = self.env["l10n_pe_ne.establecimiento"]
        E.create({"codigo": "0002", "ubigeo": "150110", "direccion": "Av. Unger 1055, Comas"})
        items = E.l10n_pe_ne_list()
        self.assertEqual(items[-1]["codigo"], "0002")  # los propios van tras el fiscal (si hay)
        self.assertTrue(all({"id", "codigo", "ubigeo", "direccion"} <= set(i) for i in items))

    def test_direcciones_del_partner(self):
        p = self.env["res.partner"].create({"name": "Cliente Dir SAC", "vat": "20601030013",
                                            "street": "Jr. Bolognesi 125, Miraflores"})
        self.env["res.partner"].create({"name": "Almacén Comas", "parent_id": p.id,
                                        "type": "delivery", "street": "Av. Tupac 500, Comas"})
        dirs = self.env["l10n_pe_ne.establecimiento"].l10n_pe_ne_direcciones_partner(p.id)
        self.assertEqual(len(dirs), 2)
        self.assertEqual(dirs[0]["direccion"], "Jr. Bolognesi 125, Miraflores")
        self.assertEqual(dirs[1]["tipo"], "delivery")
