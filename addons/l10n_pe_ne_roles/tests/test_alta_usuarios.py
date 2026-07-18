from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestAltaUsuarios(TransactionCase):
    """H-4: el dueño del RUC da de alta y gestiona a su gente por métodos sudo() con whitelist.
    Incluye la regresión del pentest (los 4 objetivos duros + V1-V3)."""

    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.Users = self.env["res.users"]
        self.duenio = self._crear("duenio_h4", ["l10n_pe_ne_roles.group_l10n_pe_ne_duenio"])

    def _crear(self, login, grupos=(), company=None):
        company = company or self.company
        return self.env["res.users"].create({
            "name": login, "login": login,
            "company_id": company.id, "company_ids": [(6, 0, [company.id])],
            "group_ids": [(4, self.env.ref("base.group_user").id)]
                         + [(4, self.env.ref(g).id) for g in grupos],
        })

    def _duenio(self):
        return self.Users.with_user(self.duenio)

    # ── funcional ──────────────────────────────────────────────────────────────
    def test_alta_crea_interno_con_roles(self):
        r = self._duenio().l10n_pe_ne_duenio_alta("Cajera Rosa", "rosa_h4", roles=["caja"])
        self.assertIn("password", r)
        nuevo = self.Users.sudo().browse(r["id"])
        # hecho 5: nace INTERNO (base.group_user), no portal
        self.assertTrue(nuevo.has_group("base.group_user"))
        self.assertFalse(nuevo.share)
        self.assertTrue(nuevo.has_group("l10n_pe_ne_roles.group_l10n_pe_ne_caja"))
        self.assertEqual(nuevo.company_ids, self.company)
        self.assertTrue(nuevo.l10n_pe_ne_must_change_password)
        self.assertIn(nuevo.id, [u["id"] for u in self._duenio().l10n_pe_ne_duenio_list_equipo()])

    def test_set_roles_reemplaza_whitelist(self):
        u = self._crear("op_h4", ["l10n_pe_ne_roles.group_l10n_pe_ne_caja"])
        self._duenio().l10n_pe_ne_duenio_set_roles(u.id, ["ventas", "despacho"])
        self.assertTrue(u.has_group("l10n_pe_ne_roles.group_l10n_pe_ne_ventas"))
        self.assertTrue(u.has_group("l10n_pe_ne_roles.group_l10n_pe_ne_despacho"))
        self.assertFalse(u.has_group("l10n_pe_ne_roles.group_l10n_pe_ne_caja"))  # quitado
        self.assertTrue(u.has_group("base.group_user"))  # el interno se conserva

    def test_reset_password_devuelve_temporal(self):
        u = self._crear("op2_h4", ["l10n_pe_ne_roles.group_l10n_pe_ne_ventas"])
        r = self._duenio().l10n_pe_ne_duenio_reset_password(u.id)
        self.assertEqual(r["login"], u.login)
        self.assertTrue(r["password"])
        self.assertTrue(u.l10n_pe_ne_must_change_password)

    def test_desactivar_reactivar(self):
        u = self._crear("op3_h4", ["l10n_pe_ne_roles.group_l10n_pe_ne_caja"])
        self._duenio().l10n_pe_ne_duenio_set_activo(u.id, False)
        self.assertFalse(u.active)
        self._duenio().l10n_pe_ne_duenio_set_activo(u.id, True)
        self.assertTrue(u.active)

    def test_add_codueno_exige_reauth(self):
        u = self._crear("op4_h4", ["l10n_pe_ne_roles.group_l10n_pe_ne_ventas"])
        # sin la contraseña correcta del dueño → AccessError
        with self.assertRaises(AccessError):
            self._duenio().l10n_pe_ne_duenio_add_codueno(u.id, "clave-incorrecta")

    # ── pentest / seguridad ──────────────────────────────────────────────────────
    def test_no_duenio_no_gestiona(self):
        cajero = self._crear("cajero_h4", ["l10n_pe_ne_roles.group_l10n_pe_ne_caja"])
        with self.assertRaises(AccessError):
            self.Users.with_user(cajero).l10n_pe_ne_duenio_list_equipo()

    def test_whitelist_rechaza_grupo_prohibido(self):
        # 'system' no es una clave de rol de la whitelist -> UserError (no se puede ni nombrar)
        with self.assertRaises(UserError):
            self._duenio().l10n_pe_ne_duenio_alta("x", "x_h4", roles=["system"])

    def test_set_roles_no_otorga_duenio(self):
        u = self._crear("op5_h4", ["l10n_pe_ne_roles.group_l10n_pe_ne_caja"])
        # 'duenio' no está en la whitelist -> UserError; y no se le añade
        with self.assertRaises(UserError):
            self._duenio().l10n_pe_ne_duenio_set_roles(u.id, ["duenio"])
        self._duenio().l10n_pe_ne_duenio_set_roles(u.id, ["ventas"])
        self.assertFalse(u.has_group("l10n_pe_ne_roles.group_l10n_pe_ne_duenio"))

    def test_no_gestiona_otro_ruc(self):
        otra = self.env["res.company"].with_context(
            l10n_pe_ne_allow_company_create=True).create({"name": "OTRO RUC H4"})
        ajeno = self._crear("ajeno_h4", ["l10n_pe_ne_roles.group_l10n_pe_ne_caja"], company=otra)
        with self.assertRaises(AccessError):
            self._duenio().l10n_pe_ne_duenio_set_roles(ajeno.id, ["ventas"])

    def test_no_gestiona_admin_de_plataforma(self):
        admin = self.env.ref("base.user_admin")
        with self.assertRaises(AccessError):
            self._duenio().l10n_pe_ne_duenio_reset_password(admin.id)

    def test_no_se_desactiva_a_si_mismo(self):
        with self.assertRaisesRegex(UserError, "ti mismo"):
            self._duenio().l10n_pe_ne_duenio_set_activo(self.duenio.id, False)

    def test_v3_no_ultimo_duenio(self):
        # self.duenio es el único dueño NO-sistema (el admin no cuenta). Excluirlo deja 0 -> lanza.
        with self.assertRaisesRegex(UserError, "último dueño"):
            self._duenio()._l10n_pe_ne_check_no_ultimo_duenio(self.company, excluir=self.duenio)
        # con un segundo dueño ya no es el último
        self._crear("d2_h4", ["l10n_pe_ne_roles.group_l10n_pe_ne_duenio"])
        self._duenio()._l10n_pe_ne_check_no_ultimo_duenio(self.company, excluir=self.duenio)

    def test_v2_cupo_de_usuarios(self):
        # tope 1: ya hay al menos el dueño activo -> el alta se bloquea
        self.company.l10n_pe_ne_max_usuarios = 1
        with self.assertRaisesRegex(UserError, "máximo"):
            self._duenio().l10n_pe_ne_duenio_alta("Rosa", "rosa2_h4", roles=["caja"])
        # sin tope (0) el alta pasa
        self.company.l10n_pe_ne_max_usuarios = 0
        r = self._duenio().l10n_pe_ne_duenio_alta("Rosa", "rosa2_h4", roles=["caja"])
        self.assertTrue(r["id"])
