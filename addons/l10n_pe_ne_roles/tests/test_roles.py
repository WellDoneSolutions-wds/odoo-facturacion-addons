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

    # ─────────────────────────────────────────────── H-3b · menú por rol
    _VE_TODAS = (
        "vePos", "veCaja", "veEmitir", "veComprobantes", "veCotizaciones",
        "veOrdenes", "veGuias", "veMasivo", "veAnalisis", "veLibros",
        "veVinculadas", "veDescargas", "veClientes", "veProductos", "veCompras",
        "veGastos", "veSeries", "veFrecuentes", "veNegocio")
    # El supervisor enciende todo MENOS la Venta rápida: el POS es cobrar, y el
    # supervisor puro no cobra (aprueba). El dueño hereda exactamente lo mismo.
    _VE_SUPERVISOR = tuple(c for c in _VE_TODAS if c != "vePos")

    def _assert_menu(self, perfil, visibles):
        """El perfil enciende EXACTAMENTE `visibles`; el resto va en false explícito."""
        for clave in self._VE_TODAS:
            self.assertIn(clave, perfil, "falta la clave %s en el perfil" % clave)
            if clave in visibles:
                self.assertTrue(perfil[clave], "%s debía ser visible" % clave)
            else:
                self.assertFalse(perfil[clave], "%s debía estar oculto" % clave)

    def test_menu_vendedor(self):
        """El vendedor cotiza y crea órdenes: NO ve caja, ni emisión, ni guías, ni masiva,
        ni reportes, ni compras (correcciones de la revisión de negocio)."""
        p = self._usuario(
            "menu_vend", ["l10n_pe_ne_roles.group_l10n_pe_ne_ventas"]).l10n_pe_ne_perfil()
        self._assert_menu(p, {"veComprobantes", "veCotizaciones", "veOrdenes",
                              "veClientes", "veProductos"})

    def test_menu_cajero(self):
        """El cajero es dinero: POS/caja/gastos y sus bandejas de cobro en cotizaciones y
        órdenes (el hallazgo del e2e: capar la página entera lo dejaba sin su cola)."""
        p = self._usuario(
            "menu_caja", ["l10n_pe_ne_roles.group_l10n_pe_ne_caja"]).l10n_pe_ne_perfil()
        self._assert_menu(p, {"vePos", "veCaja", "veComprobantes", "veCotizaciones",
                              "veOrdenes", "veClientes", "veProductos", "veGastos"})

    def test_menu_operario(self):
        """El operario SOLO ve su cola de órdenes: ni dinero, ni puerta, ni reportes."""
        p = self._usuario(
            "menu_op", ["l10n_pe_ne_roles.group_l10n_pe_ne_taller"]).l10n_pe_ne_perfil()
        self._assert_menu(p, {"veOrdenes"})

    def test_menu_despachador(self):
        """El despachador es mercadería física: guías, órdenes (entrega), compras
        (recepción), frecuentes, consulta de comprobantes/productos — y COTIZACIONES,
        porque su Cola de despacho (CN-01) vive adentro como pestaña (quitársela
        repetiría el hallazgo histórico del cajero, esta vez con él)."""
        p = self._usuario(
            "menu_desp", ["l10n_pe_ne_roles.group_l10n_pe_ne_despacho"]).l10n_pe_ne_perfil()
        self._assert_menu(p, {"veComprobantes", "veCotizaciones", "veOrdenes", "veGuias",
                              "veProductos", "veCompras", "veFrecuentes"})

    def test_menu_contador(self):
        """El contador solo lee: comprobantes y los cuatro reportes. Nada operativo."""
        p = self._usuario(
            "menu_cont", ["l10n_pe_ne_roles.group_l10n_pe_ne_contador"]).l10n_pe_ne_perfil()
        self._assert_menu(p, {"veComprobantes", "veAnalisis", "veLibros",
                              "veVinculadas", "veDescargas"})

    def test_menu_supervisor_y_duenio_ven_todo(self):
        """El supervisor enciende todos los ítems ve* salvo la Venta rápida (el POS es
        cobrar y él aprueba); el dueño hereda lo mismo por implicación."""
        for login, grupo in (("menu_sup", "group_l10n_pe_ne_supervisor"),
                             ("menu_due", "group_l10n_pe_ne_duenio")):
            p = self._usuario(login, ["l10n_pe_ne_roles." + grupo]).l10n_pe_ne_perfil()
            self._assert_menu(p, set(self._VE_SUPERVISOR))

    def test_menu_caja_mas_anulacion_sigue_en_matriz(self):
        """`modal` y cualquier combinación: tener además un grupo NO-matriz (anulación)
        no saca al usuario de la matriz — la unión de sus roles manda."""
        p = self._usuario("menu_caja_anula", [
            "l10n_pe_ne_roles.group_l10n_pe_ne_caja",
            "l10n_pe_ne_biller.group_l10n_pe_ne_anulacion"]).l10n_pe_ne_perfil()
        self.assertTrue(p["vePos"])
        self.assertFalse(p["veEmitir"])

    def test_menu_legacy_emisor_sin_claves(self):
        """RETROCOMPATIBILIDAD (decisión documentada en menu-por-rol.md): NO reciben
        claves ve* — ausente ≠ prohibido, menú operativo completo como pre-roles — el
        legacy solo-`emisor`, el solo-`anulación` (implica emisor) y el usuario sin
        ningún grupo NE (su muro es el backend). Ocultarle el menú a un tenant actual
        sería una regresión de producción."""
        for login, grupos in (
                ("menu_legacy", ["l10n_pe_ne_biller.group_l10n_pe_ne_emisor"]),
                ("menu_anula", ["l10n_pe_ne_biller.group_l10n_pe_ne_anulacion"]),
                ("menu_sin_rol", [])):
            p = self._usuario(login, grupos).l10n_pe_ne_perfil()
            for clave in self._VE_TODAS:
                self.assertNotIn(
                    clave, p, "%s no debía emitirse para %s" % (clave, login))

    def test_menu_admin_plataforma_sin_claves(self):
        """El admin de plataforma (system o erp_manager) queda fuera de la matriz aunque
        acumule roles NE: isAdmin del biller solo cubre system, así que el guard H-3b
        también mira erp_manager (el mismo par que los choke points de H-4)."""
        p = self._usuario("menu_erp", [
            "base.group_erp_manager",
            "l10n_pe_ne_roles.group_l10n_pe_ne_caja"]).l10n_pe_ne_perfil()
        for clave in self._VE_TODAS:
            self.assertNotIn(clave, p, "%s no debía emitirse para un admin" % clave)
