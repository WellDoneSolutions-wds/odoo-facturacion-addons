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
        with patch.dict(R.MODULOS, {"R10": ("Agenda de citas / turnos", "R", False)}), \
                patch.object(R, "_DISPONIBLES",
                             frozenset(c for c, (_n, _c, d) in R.MODULOS.items() if d)):
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

    def test_perfil_admin_queda_fuera_de_la_capa_1(self):
        # Super Admin (plataforma) ve todo: la clave modulos NO viaja aunque haya rubro.
        self._set(["bodega"])
        self.assertNotIn("modulos", self.env.user.l10n_pe_ne_perfil())

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
