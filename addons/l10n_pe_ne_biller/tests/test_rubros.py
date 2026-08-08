# -*- coding: utf-8 -*-
"""Capa 1 (rubro → módulos): resolución, muro de emisión, permisos y auditoría."""
import json

from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, tagged

from .common import L10nPeSeedMixin
from ..models.l10n_pe_ne_rubro import MODULOS, NUCLEO, RUBROS


@tagged("post_install", "-at_install")
class TestRubros(L10nPeSeedMixin, TransactionCase):
    def _set(self, rubros=None, overrides=None):
        self.env.company.sudo().write({
            "l10n_pe_ne_rubros": json.dumps(rubros or []),
            "l10n_pe_ne_modulos_override": json.dumps(overrides or {}),
        })

    def _emisor(self):
        """Usuario emisor NORMAL (no admin): el admin de plataforma queda FUERA de la Capa 1
        (regla Super Admin), así que los casos de gating se prueban con un usuario real."""
        return self.env["res.users"].sudo().create({
            "name": "Emisor Rubro", "login": "emisor.rubro@test",
            "company_id": self.env.company.id, "company_ids": [(6, 0, [self.env.company.id])],
            "group_ids": [(4, self.env.ref("base.group_user").id),
                          (4, self.env.ref("l10n_pe_ne_biller.group_l10n_pe_ne_emisor").id)],
        })

    # ------------------------------------------------------------ resolución
    def test_sin_rubro_es_legacy_sin_gating(self):
        self._set()
        self.assertIsNone(self.env.company.l10n_pe_ne_modulos_efectivos())
        # legacy: todo módulo se considera activo (ausente ≠ prohibido)
        self.assertTrue(self.env.company.l10n_pe_ne_modulo_activo("C04"))

    def test_bodega_trae_nucleo_y_defaults_no_detraccion(self):
        self._set(["bodega"])
        efectivos = self.env.company.l10n_pe_ne_modulos_efectivos()
        for cod in NUCLEO:
            self.assertIn(cod, efectivos)
        self.assertIn("V01", efectivos)   # POS
        self.assertIn("I01", efectivos)   # stock perpetuo
        self.assertNotIn("C04", efectivos)   # una bodega no detrae

    def test_multi_rubro_es_union(self):
        self._set(["ferreteria", "alquiler"])
        efectivos = self.env.company.l10n_pe_ne_modulos_efectivos()
        self.assertIn("I05", efectivos)   # ferretería: fraccionamiento
        self.assertIn("C04", efectivos)   # alquiler: detracción
        self.assertIn("V06", efectivos)   # alquiler: crédito con cuotas

    def test_override_agrega_y_quita_pero_nucleo_es_inviolable(self):
        self._set(["bodega"], {"C04": True, "I01": False, "E01": False})
        efectivos = self.env.company.l10n_pe_ne_modulos_efectivos()
        self.assertIn("C04", efectivos)      # opcional activado a mano
        self.assertNotIn("I01", efectivos)   # default del rubro apagado a mano
        self.assertIn("E01", efectivos)      # el núcleo NO se apaga ni a mano

    def test_comodin_otro_solo_nucleo(self):
        self._set(["otro"])
        efectivos = self.env.company.l10n_pe_ne_modulos_efectivos()
        self.assertEqual(efectivos, set(NUCLEO))

    def test_no_disponibles_se_filtran(self):
        # El mecanismo de filtrado, independiente de qué quede por construir: se fuerza un
        # módulo de educación como NO disponible y la resolución debe excluirlo (aunque el
        # rubro lo traiga por defecto y un override intente encenderlo).
        from unittest.mock import patch
        from ..models import l10n_pe_ne_rubro as R
        self._set(["educacion"], {"R10": True})
        with patch.dict(R.MODULOS, {"R10": ("Agenda de citas / turnos", "R", False,
                                            "Agendas al cliente por día y hora.")}), \
                patch.object(R, "_DISPONIBLES",
                             frozenset(c for c, (_n, _c, d, _x) in R.MODULOS.items() if d)):
            efectivos = self.env.company.l10n_pe_ne_modulos_efectivos()
            self.assertNotIn("R10", efectivos)
        # Sin el parche, R10 (ya construido en fase 2) pasa el filtro normalmente.
        efectivos = self.env.company.l10n_pe_ne_modulos_efectivos()
        self.assertIn("R10", efectivos)
        self.assertIn("V11", efectivos)
        self.assertIn("E11", efectivos)
        self.assertIn("V06", efectivos)   # el ajuste experto también disponible

    def test_arrocera_trae_ivap(self):
        # fase 2: C12 (IVAP) disponible — la arrocera lo recibe por defecto.
        self._set(["arrocera"])
        efectivos = self.env.company.l10n_pe_ne_modulos_efectivos()
        self.assertIn("C12", efectivos)
        self.assertIn("E05", efectivos)   # liquidación de compra (compra a productores)

    def test_muro_ivap_rechaza_en_rubro_sin_c12(self):
        # una bodega no vende arroz pilado en primera venta: línea 1016 → rechazo del muro.
        user = self._emisor()
        AM = self.env["account.move"].with_user(user)
        self._set(["bodega"])
        lanzo = False
        try:
            AM._l10n_pe_ne_check_modulo("C12", "IVAP (arroz pilado)")
        except UserError:
            lanzo = True
        self.assertTrue(lanzo)

    def test_catalogo_consistente(self):
        # todo default de rubro y todo código de NUCLEO existen en MODULOS
        for cod in NUCLEO:
            self.assertIn(cod, MODULOS)
        for _cod, (_n, _g, mods) in RUBROS.items():
            for m in mods:
                self.assertIn(m, MODULOS)

    # ---------------------------------------------------------------- API set
    def test_set_rubro_requiere_permiso(self):
        AM = self.env["account.move"]
        user = self.env["res.users"].sudo().create({
            "name": "Cajero Test", "login": "cajero.rubro@test",
            "group_ids": [(4, self.env.ref("base.group_user").id)],
        })
        with self.assertRaises(AccessError):
            AM.with_user(user).l10n_pe_ne_set_rubro({"rubros": ["bodega"]})

    def test_set_rubro_admin_escribe_y_audita(self):
        AM = self.env["account.move"]   # env de tests corre como admin (group_system)
        estado = AM.l10n_pe_ne_set_rubro({"rubros": ["bodega"], "overrides": {"C04": True}})
        self.assertEqual(estado["rubros"], ["bodega"])
        self.assertIn("C04", estado["modulos"])
        filas = self.env["l10n_pe_ne.rubro_auditoria"].search(
            [("company_id", "=", self.env.company.id)])
        self.assertGreaterEqual(len(filas), 2)   # rubros + overrides
        self.assertIn("rubros", filas.mapped("campo"))

    def test_set_rubro_codigo_desconocido(self):
        AM = self.env["account.move"]
        with self.assertRaises(UserError):
            AM.l10n_pe_ne_set_rubro({"rubros": ["marciano"]})
        with self.assertRaises(UserError):
            AM.l10n_pe_ne_set_rubro({"rubros": ["bodega"], "overrides": {"Z99": True}})

    # ----------------------------------------------------------------- perfil
    def test_perfil_expone_modulos_solo_con_rubro(self):
        user = self._emisor()
        self._set()
        self.assertNotIn("modulos", user.with_user(user).l10n_pe_ne_perfil())
        self._set(["bodega"])
        perfil = user.with_user(user).l10n_pe_ne_perfil()
        self.assertIn("modulos", perfil)
        self.assertIn("V01", perfil["modulos"])

    def test_perfil_admin_tambien_ve_la_config_del_negocio(self):
        # Decisión de producto: la VISIBILIDAD sigue la configuración del negocio para
        # TODOS, admin incluido («lo que configuras es lo que ves»). El admin que quiera
        # ver todo elige el tipo «Todos». Su bypass vive en el MURO, no en la UI.
        self._set(["bodega"])
        perfil = self.env.user.l10n_pe_ne_perfil()   # env de tests = admin
        self.assertIn("modulos", perfil)
        self.assertIn("V01", perfil["modulos"])
        self.assertNotIn("C04", perfil["modulos"])
        # config() igual: el admin recibe la lista del negocio.
        cfg = self.env["account.move"].l10n_pe_ne_config()
        self.assertIn("modulos", cfg)

    # ------------------------------------------------- fase 3 · adopción / alta
    def test_adopcion_solo_admin(self):
        self._set(["bodega"], {"C04": True})
        AM = self.env["account.move"]
        cfg = AM.l10n_pe_ne_rubro_config()   # env de tests = admin
        self.assertIn("adopcion", cfg)
        self.assertGreaterEqual(cfg["adopcion"]["empresasPorRubro"].get("bodega", 0), 1)
        self.assertGreaterEqual(cfg["adopcion"]["overridesActivados"].get("C04", 0), 1)
        # el emisor normal NO ve la analítica del servidor
        cfg2 = AM.with_user(self._emisor()).l10n_pe_ne_rubro_config()
        self.assertNotIn("adopcion", cfg2)

    def test_provision_tenant_con_rubro(self):
        res = self.env["res.company"].l10n_pe_ne_provision_tenant({
            "ruc": "20609999991", "razonSocial": "GYM TEST SAC",
            "login": "gym.rubro@test", "password": "S3gura#2026x",
            "rubros": ["gimnasio"]})
        company = self.env["res.company"].sudo().search([("vat", "=", "20609999991")], limit=1)
        self.assertTrue(company)
        efectivos = company.l10n_pe_ne_modulos_efectivos()
        self.assertIsNotNone(efectivos)      # nació CON rubro (no legacy)
        self.assertIn("V11", efectivos)      # membresías, el módulo del gimnasio
        self.assertTrue(res)
        with self.assertRaises(UserError):   # rubro inexistente en el alta
            self.env["res.company"].l10n_pe_ne_provision_tenant({
                "ruc": "20609999992", "razonSocial": "X SAC",
                "login": "x.rubro@test", "password": "S3gura#2026x",
                "rubros": ["marciano"]})

    # ------------------------------------------------- fase 4 · protección en-uso
    def _con_detraccion_en_historia(self):
        """Garantiza que la empresa tenga al menos un comprobante con detracción (C04 en uso)."""
        partner = self.env["res.partner"].create({
            "name": "CLIENTE F4", "vat": "20100070970", "company_id": self.env.company.id})
        self.env["account.move"].create({
            "move_type": "out_invoice", "partner_id": partner.id,
            "company_id": self.env.company.id, "l10n_pe_ne_detraccion": True})

    def test_fase4_en_uso_detecta_detraccion(self):
        self._con_detraccion_en_historia()
        self.assertIn("C04", self.env.company.l10n_pe_ne_modulos_en_uso())

    def test_fase4_elegir_rubro_protege_lo_en_uso(self):
        # La empresa emite detracciones y elige Bodega (que no trae C04): el guardado la
        # protege con un override automático — nada que ya se usa queda oculto (spec f4).
        self._con_detraccion_en_historia()
        AM = self.env["account.move"]
        estado = AM.l10n_pe_ne_set_rubro({"rubros": ["bodega"], "overrides": {}})
        self.assertIn("C04", estado["protegidos"])
        self.assertIn("C04", estado["modulos"])
        self.assertTrue(estado["overrides"].get("C04"))
        self.assertIn("C04", estado["enUso"])

    def test_fase4_apagado_explicito_se_respeta(self):
        # Apagar a sabiendas un módulo en uso es una decisión, no un accidente: el override
        # False explícito del payload NO se pisa.
        self._con_detraccion_en_historia()
        AM = self.env["account.move"]
        estado = AM.l10n_pe_ne_set_rubro({"rubros": ["bodega"], "overrides": {"C04": False}})
        self.assertNotIn("C04", estado["protegidos"])
        self.assertNotIn("C04", estado["modulos"])

    # ------------------------------------------------------------------- muro
    def test_muro_rechaza_y_audita(self):
        user = self._emisor()
        AM = self.env["account.move"].with_user(user)
        self._set(["bodega"])   # bodega no tiene C04
        # try/except (NO assertRaises): el assertRaises de Odoo envuelve en savepoint y su
        # rollback se llevaría la fila de auditoría que justamente queremos verificar.
        lanzo = False
        try:
            AM._l10n_pe_ne_check_modulo("C04", "Detracción (SPOT)")
        except UserError:
            lanzo = True
        self.assertTrue(lanzo)
        rechazo = self.env["l10n_pe_ne.rubro_auditoria"].sudo().search(
            [("company_id", "=", self.env.company.id), ("campo", "=", "rechazo:C04")])
        self.assertTrue(rechazo)

    def test_muro_legacy_pasa(self):
        user = self._emisor()
        AM = self.env["account.move"].with_user(user)
        self._set()   # sin rubro = legacy
        AM._l10n_pe_ne_check_modulo("C04", "Detracción (SPOT)")   # no lanza

    def test_muro_admin_bypass(self):
        # Super Admin opera todo aunque el rubro no tenga el módulo (regla 1 de la spec).
        self._set(["bodega"])
        self.env["account.move"]._l10n_pe_ne_check_modulo("C04", "Detracción (SPOT)")   # no lanza

    # ------------------------------------------------- nivel 2 · salud / historial
    def test_salud_checklist(self):
        self._set()   # estado conocido: sin rubro (el test no depende de la BD que toque)
        s = self.env["account.move"].l10n_pe_ne_salud()
        self.assertEqual(s["total"], 6)
        self.assertIn("pct", s)
        rubro_item = next(i for i in s["items"] if i["clave"] == "rubro")
        self.assertFalse(rubro_item["ok"])          # sin rubro configurado (setUp lo limpia)
        self._set(["bodega"])
        s2 = self.env["account.move"].l10n_pe_ne_salud()
        self.assertTrue(next(i for i in s2["items"] if i["clave"] == "rubro")["ok"])
        self.assertGreaterEqual(s2["hechos"], s["hechos"])

    def test_auditoria_legible_y_gate(self):
        AM = self.env["account.move"]
        AM.l10n_pe_ne_set_rubro({"rubros": ["bodega"], "overrides": {"C04": True}})
        filas = AM.l10n_pe_ne_auditoria_list()
        titulos = [f["titulo"] for f in filas]
        self.assertIn("Cambio de tipo de negocio", titulos)
        cambio = next(f for f in filas if f["titulo"] == "Cambio de tipo de negocio")
        self.assertIn("Bodega", cambio["resumen"])   # nombres legibles, no códigos
        # el emisor raso NO ve el historial
        from odoo.exceptions import AccessError as AE
        with self.assertRaises(AE):
            AM.with_user(self._emisor()).l10n_pe_ne_auditoria_list()

    def test_perfil_trae_rubro_configurado(self):
        user = self._emisor()
        self._set()
        self.assertFalse(user.with_user(user).l10n_pe_ne_perfil()["rubroConfigurado"])
        self._set(["bodega"])
        self.assertTrue(user.with_user(user).l10n_pe_ne_perfil()["rubroConfigurado"])

    # --------------------------------------- robustez · permisos al cliente
    def test_config_expone_puede_editar(self):
        """`puedeEditar` sale del MISMO gate que corta el guardado.

        La SPA reimplementaba la regla con los flags del perfil y en sentido permisivo
        (`puedeSupervisar !== false` es true cuando el campo no viene): un usuario sin
        permiso veía los controles habilitados y solo descubría el muro al guardar."""
        AM = self.env["account.move"]
        self.assertTrue(AM.l10n_pe_ne_rubro_config()["puedeEditar"])   # tests corren como admin

        emisor = self._emisor()
        cfg = AM.with_user(emisor).l10n_pe_ne_rubro_config()
        self.assertFalse(cfg["puedeEditar"], "un emisor raso no configura el rubro")
        # Y el valor coincide con el gate real, no con una heurística paralela.
        self.assertEqual(
            cfg["puedeEditar"],
            AM.with_user(emisor)._l10n_pe_ne_puede_config_rubro())
        # Coherencia dura: si dice que no puede, guardar debe cortar.
        with self.assertRaises(AccessError):
            AM.with_user(emisor).l10n_pe_ne_set_rubro({"rubros": ["bodega"]})

    # ------------------------------------- robustez · fusión de overrides
    def test_cambio_de_rubro_conserva_los_ajustes_manuales(self):
        """El bug que rompía la promesa «lo que ya usas nunca se pierde».

        Antes, aplicar un tipo de negocio mandaba overrides={} y el backend REEMPLAZABA:
        todo lo que el dueño había activado a mano desaparecía en silencio. Ahora, si el
        payload no trae la clave `overrides`, se conservan los guardados."""
        AM = self.env["account.move"]
        self._set(["bodega"], {"I04": True})          # activó Lotes/vencimiento a mano
        estado = AM.l10n_pe_ne_set_rubro({"rubros": ["consultoria"]})   # sin clave overrides
        self.assertTrue(estado["overrides"].get("I04"),
                        "el override manual debe sobrevivir al cambio de rubro")
        self.assertIn("I04", estado["modulos"])

    def test_overrides_presente_es_autoritativo(self):
        """El ajuste fino manda el dict completo: quitar una clave vuelve al default del
        rubro. Un {} explícito sigue significando «sin overrides» (no se fusiona)."""
        AM = self.env["account.move"]
        self._set(["bodega"], {"I04": True})
        estado = AM.l10n_pe_ne_set_rubro({"rubros": ["bodega"], "overrides": {}})
        self.assertNotIn("I04", estado["overrides"])
        self.assertNotIn("I04", estado["modulos"])

    def test_override_sobre_nucleo_no_se_persiste(self):
        """Apagar el núcleo no tiene efecto; antes se guardaba igual y la bitácora lo
        reportaba como «apagó Factura electrónica» — una traza de auditoría mintiendo."""
        AM = self.env["account.move"]
        estado = AM.l10n_pe_ne_set_rubro({"rubros": ["bodega"], "overrides": {"E01": False}})
        self.assertNotIn("E01", estado["overrides"])
        self.assertIn("E01", estado["modulos"])       # sigue activo, como debe

    # ------------------------------------------- robustez · dependencias
    def test_dependencias_se_autocompletan(self):
        """Maderera trae Kardex (I02) pero su preset no lista Stock perpetuo (I01): un
        kardex sin movimientos de stock no es configuración válida, es configuración rota."""
        self._set(["maderera"])
        efectivos = self.env.company.l10n_pe_ne_modulos_efectivos()
        self.assertIn("I02", efectivos)
        self.assertIn("I01", efectivos)

    def test_dependencia_revierte_un_apagado_incoherente(self):
        self._set(["ferreteria"], {"I01": False})     # apagar la base teniendo el kardex
        efectivos = self.env.company.l10n_pe_ne_modulos_efectivos()
        self.assertIn("I01", efectivos, "I02/I05 dependen de I01: no puede quedar apagado")

    def test_dependencias_apuntan_a_modulos_existentes(self):
        from ..models.l10n_pe_ne_rubro import DEPENDENCIAS
        for cod, deps in DEPENDENCIAS.items():
            self.assertIn(cod, MODULOS)
            for d in deps:
                self.assertIn(d, MODULOS)
                self.assertNotEqual(cod, d, "una dependencia sobre sí mismo cuelga el cierre")

    # ------------------------------------------------ robustez · didáctica
    def test_todo_modulo_tiene_descripcion_util(self):
        """La descripción es contrato de producto: es lo único que un dueño de PyME lee
        para decidir si necesita «IVAP». Un módulo sin ella llega mudo a la pantalla."""
        for cod, (nombre, _cat, _disp, desc) in MODULOS.items():
            self.assertTrue(desc and desc.strip(), "%s (%s) sin descripción" % (cod, nombre))
            self.assertGreater(len(desc), 25, "%s: descripción demasiado corta" % cod)
            self.assertNotEqual(desc.strip(), nombre.strip())

    def test_catalogo_modulos_expone_descripcion_y_requiere(self):
        cfg = self.env["account.move"].l10n_pe_ne_rubro_config()
        por_cod = {m["codigo"]: m for m in cfg["catalogoModulos"]}
        self.assertTrue(por_cod["C12"]["descripcion"])
        self.assertEqual(por_cod["I02"]["requiere"], ["I01"])
        self.assertEqual(por_cod["E01"]["requiere"], [])

    # ----------------------------------------------- robustez · salud real
    def test_salud_series_exige_serie_en_cada_local(self):
        """El check se llama «Series declaradas por local»: con dos locales y una sola
        serie daba ✓, y el segundo local descubría que no podía emitir al intentarlo."""
        Est = self.env["l10n_pe_ne.establecimiento"].sudo()
        comp = self.env.company
        e1 = Est.create({"codigo": "0000", "ubigeo": "150101", "direccion": "Av. Uno 123",
                         "company_id": comp.id})
        Est.create({"codigo": "0001", "ubigeo": "150101", "direccion": "Av. Dos 456",
                    "company_id": comp.id})
        self.env["l10n_pe_ne.serie"].sudo().create({
            "codigo": "F001", "tipo_doc": "01", "establecimiento_id": e1.id,
            "activa": True, "company_id": comp.id})
        item = next(i for i in self.env["account.move"].l10n_pe_ne_salud()["items"]
                    if i["clave"] == "series")
        self.assertFalse(item["ok"], "falta la serie del segundo local")
        self.assertIn("0001", item["detalle"])
