from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestDireccionesEstablec(TransactionCase):
    """Establecimientos anexos y direcciones de cliente atados a un distrito de Perú:
    el ubigeo de 6 dígitos sale automático (ya no se tipea a mano)."""

    def _miraflores(self):
        d = self.env["l10n_pe.res.city.district"].search([("code", "=", "150122")], limit=1)
        self.assertTrue(d, "debe existir el distrito 150122 (Miraflores) en los datos base")
        return d

    # --------------------------------------------------------- establecimiento
    def test_establecimiento_con_distrito_sincroniza_ubigeo(self):
        d = self._miraflores()
        E = self.env["l10n_pe_ne.establecimiento"]
        rec = E.l10n_pe_ne_upsert({"codigo": "0002", "direccion": "X", "distritoId": d.id})
        self.assertEqual(rec.ubigeo, "150122")
        self.assertEqual(rec.distrito_id.id, d.id)
        row = next(i for i in E.l10n_pe_ne_list() if i["codigo"] == "0002")
        self.assertEqual(row["distrito"], "Miraflores")
        self.assertEqual(row["distritoId"], d.id)
        self.assertEqual(row["ubigeo"], "150122")

    def test_establecimiento_sin_distrito_conserva_ubigeo_tipeado(self):
        E = self.env["l10n_pe_ne.establecimiento"]
        rec = E.l10n_pe_ne_upsert({"codigo": "0003", "direccion": "Y", "ubigeo": "150110"})
        self.assertEqual(rec.ubigeo, "150110")
        self.assertFalse(rec.distrito_id)

    # --------------------------------------------------------- direcciones cliente
    def test_crear_direccion_cliente(self):
        d = self._miraflores()
        p = self.env["res.partner"].create({"name": "Cliente Dir SAC", "vat": "20601030013"})
        E = self.env["l10n_pe_ne.establecimiento"]
        row = E.l10n_pe_ne_crear_direccion(p.id, {"direccion": "Av X", "distritoId": d.id})
        self.assertGreater(row["id"], 0)
        child = self.env["res.partner"].browse(row["id"])
        self.assertEqual(child.parent_id, p)
        self.assertEqual(child.type, "delivery")
        self.assertEqual(child.street, "Av X")
        self.assertEqual(child.l10n_pe_district.code, "150122")
        dirs = E.l10n_pe_ne_direcciones_partner(p.id)
        self.assertTrue(any(x["id"] == row["id"] and x["ubigeo"] == "150122" for x in dirs))

    def test_editar_direccion_cliente(self):
        d = self._miraflores()
        p = self.env["res.partner"].create({"name": "Cliente Dir SAC", "vat": "20601030013"})
        E = self.env["l10n_pe_ne.establecimiento"]
        row = E.l10n_pe_ne_crear_direccion(p.id, {"direccion": "Av X", "distritoId": d.id})
        edited = E.l10n_pe_ne_editar_direccion(row["id"], {"direccion": "Av Y"})
        self.assertEqual(edited["direccion"], "Av Y")
        self.assertEqual(self.env["res.partner"].browse(row["id"]).street, "Av Y")

    def test_eliminar_direccion_cliente_archiva(self):
        d = self._miraflores()
        p = self.env["res.partner"].create({"name": "Cliente Dir SAC", "vat": "20601030013"})
        E = self.env["l10n_pe_ne.establecimiento"]
        row = E.l10n_pe_ne_crear_direccion(p.id, {"direccion": "Av X", "distritoId": d.id})
        res = E.l10n_pe_ne_eliminar_direccion(row["id"])
        self.assertTrue(res["ok"])
        dirs = E.l10n_pe_ne_direcciones_partner(p.id)
        self.assertFalse(any(x["id"] == row["id"] for x in dirs))
        child = self.env["res.partner"].browse(row["id"])
        self.assertFalse(child.active)

    def test_crear_direccion_sin_distrito_falla(self):
        p = self.env["res.partner"].create({"name": "Cliente Dir SAC", "vat": "20601030013"})
        E = self.env["l10n_pe_ne.establecimiento"]
        with self.assertRaisesRegex(UserError, "distrito"):
            E.l10n_pe_ne_crear_direccion(p.id, {"direccion": "Av X"})

    def test_editar_direccion_no_permite_editar_principal(self):
        p = self.env["res.partner"].create({"name": "Cliente Dir SAC", "vat": "20601030013",
                                            "street": "Jr. Bolognesi 125, Miraflores"})
        E = self.env["l10n_pe_ne.establecimiento"]
        with self.assertRaisesRegex(UserError, "no encontrada"):
            E.l10n_pe_ne_editar_direccion(p.id, {"direccion": "Av Z"})
