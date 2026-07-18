from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestRolesPerfil(TransactionCase):
    """H-2/H-3: los grupos de rol existen y el perfil expone la capacidad por rol
    (has_group), sin comparar identidades. La SPA pinta el menú desde esto."""

    _GRUPOS = [
        "l10n_pe_ne_roles.group_l10n_pe_ne_ventas",
        "l10n_pe_ne_roles.group_l10n_pe_ne_caja",
        "l10n_pe_ne_roles.group_l10n_pe_ne_despacho",
        "l10n_pe_ne_roles.group_l10n_pe_ne_taller",
        "l10n_pe_ne_roles.group_l10n_pe_ne_supervisor",
        "l10n_pe_ne_roles.group_l10n_pe_ne_contador",
        "l10n_pe_ne_roles.group_l10n_pe_ne_duenio",
    ]

    def setUp(self):
        super().setUp()
        self.company = self.env.company

    def _usuario(self, login, grupos):
        return self.env["res.users"].create({
            "name": login, "login": login,
            "company_id": self.company.id, "company_ids": [(6, 0, [self.company.id])],
            "group_ids": [(4, self.env.ref(g).id) for g in grupos],
        })

    def test_grupos_existen_bajo_el_privilege(self):
        priv = self.env.ref("l10n_pe_ne_roles.privilege_ne_express")
        for xmlid in self._GRUPOS:
            grupo = self.env.ref(xmlid)
            self.assertTrue(grupo, "falta el grupo %s" % xmlid)
            self.assertEqual(grupo.privilege_id, priv, "%s no cuelga del privilege" % xmlid)

    def test_implicaciones(self):
        emisor = self.env.ref("l10n_pe_ne_biller.group_l10n_pe_ne_emisor")
        # los operativos implican emisor
        for xmlid in ("group_l10n_pe_ne_ventas", "group_l10n_pe_ne_caja",
                      "group_l10n_pe_ne_despacho", "group_l10n_pe_ne_taller",
                      "group_l10n_pe_ne_supervisor"):
            grupo = self.env.ref("l10n_pe_ne_roles." + xmlid)
            self.assertIn(emisor, grupo.all_implied_ids, "%s no implica emisor" % xmlid)
        # duenio implica supervisor (y por transitividad, emisor)
        duenio = self.env.ref("l10n_pe_ne_roles.group_l10n_pe_ne_duenio")
        self.assertIn(self.env.ref("l10n_pe_ne_roles.group_l10n_pe_ne_supervisor"),
                      duenio.all_implied_ids)
        self.assertIn(emisor, duenio.all_implied_ids)
        # contador NO implica emisor (es solo lectura), sí account readonly
        contador = self.env.ref("l10n_pe_ne_roles.group_l10n_pe_ne_contador")
        self.assertNotIn(emisor, contador.all_implied_ids)
        self.assertIn(self.env.ref("account.group_account_readonly"), contador.all_implied_ids)

    def test_perfil_capacidad_por_rol(self):
        """Un cajero puro ve puedeCobrar=True y puedeCotizar=False: segregación por rol en el
        menú, aunque el ACL sea compartido (emisor)."""
        cajero = self._usuario("cajero_it3", ["l10n_pe_ne_roles.group_l10n_pe_ne_caja"])
        p = cajero.l10n_pe_ne_perfil()
        # base (heredado del biller vía super)
        self.assertEqual(p["ruc"], self.company.vat or "")
        self.assertIn("puedeAnular", p)
        # capacidades por rol
        self.assertTrue(p["puedeCobrar"])
        self.assertFalse(p["puedeCotizar"])
        self.assertFalse(p["puedeDespachar"])
        self.assertFalse(p["esContador"])
        self.assertFalse(p["esDuenio"])

    def test_perfil_contador(self):
        contador = self._usuario("contador_it3", ["l10n_pe_ne_roles.group_l10n_pe_ne_contador"])
        p = contador.l10n_pe_ne_perfil()
        self.assertTrue(p["esContador"])
        self.assertFalse(p["puedeCobrar"])
        self.assertFalse(p["puedeCotizar"])

    def test_perfil_duenio_acumula(self):
        """El dueño, por implicación, tiene la capacidad de supervisor (y opera)."""
        duenio = self._usuario("duenio_it3", ["l10n_pe_ne_roles.group_l10n_pe_ne_duenio"])
        p = duenio.l10n_pe_ne_perfil()
        self.assertTrue(p["esDuenio"])
        self.assertTrue(p["puedeSupervisar"])   # por implied_ids duenio->supervisor
