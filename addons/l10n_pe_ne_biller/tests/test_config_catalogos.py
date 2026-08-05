# -*- coding: utf-8 -*-
"""Capa 1.5 · catálogos del negocio: siembra por rubro, validaciones y muro."""
import json

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from .common import L10nPeSeedMixin


@tagged("post_install", "-at_install")
class TestConfigCatalogos(L10nPeSeedMixin, TransactionCase):
    def setUp(self):
        super().setUp()
        self.AM = self.env["account.move"]
        self.env.company.sudo().write({
            "l10n_pe_ne_rubros": "[]", "l10n_pe_ne_modulos_override": "{}",
            "l10n_pe_ne_cfg_catalogos": ""})

    def test_legacy_sin_config(self):
        g = self.AM.l10n_pe_ne_cfg_catalogos_get()
        self.assertIsNone(g["cfg"])                       # sin configurar = catálogo completo
        self.assertIn("GLL", g["maestros"]["unidades"])   # el maestro viaja entero

    def test_siembra_por_rubro_grifo_y_exportador(self):
        # Elegir rubro SIEMBRA los catálogos: el grifo nace con GALÓN/LITRO; sumar
        # exportador agrega USD (multi-rubro = unión también en catálogos).
        self.AM.l10n_pe_ne_set_rubro({"rubros": ["grifo", "exportador"], "overrides": {}})
        cfg = self.env.company._l10n_pe_ne_cfg()
        self.assertIn("GLL", cfg["unidades"]["activas"])
        self.assertIn("LTR", cfg["unidades"]["activas"])
        self.assertIn("USD", cfg["monedas"]["activas"])
        self.assertEqual(cfg["unidades"]["default"], "NIU")
        self.assertEqual(cfg["medios"]["default"], "Efectivo")
        # la siembra queda auditada
        self.assertTrue(self.env["l10n_pe_ne.rubro_auditoria"].search(
            [("company_id", "=", self.env.company.id), ("campo", "=", "catalogos(siembra)")]))

    def test_siembra_no_pisa_config_propia(self):
        self.env.company.sudo().l10n_pe_ne_cfg_catalogos = json.dumps(
            {"unidades": {"activas": ["NIU"], "default": "NIU"},
             "medios": {"lista": ["Efectivo"], "default": "Efectivo"},
             "afectaciones": {"activas": ["1000"], "gratuitas": [], "default": "1000"},
             "monedas": {"activas": ["PEN"]}})
        self.AM.l10n_pe_ne_set_rubro({"rubros": ["grifo"], "overrides": {}})
        cfg = self.env.company._l10n_pe_ne_cfg()
        self.assertNotIn("GLL", cfg["unidades"]["activas"])   # lo del dueño se respeta

    def test_set_valida_y_audita(self):
        estado = self.AM.l10n_pe_ne_cfg_catalogos_set({
            "unidades": {"activas": ["NIU", "KGM", "GLL"], "default": "KGM"},
            "medios": {"lista": ["Yape", "Efectivo", "Agora", "yape"], "default": "Yape"},
            "afectaciones": {"activas": ["1000", "9997"], "gratuitas": ["11"], "default": "1000"},
            "monedas": {"activas": []},
        })
        cfg = estado["cfg"]
        self.assertEqual(cfg["unidades"]["default"], "KGM")
        self.assertEqual(cfg["medios"]["lista"], ["Yape", "Efectivo", "Agora"])   # dedup, orden
        self.assertEqual(cfg["monedas"]["activas"], ["PEN"])   # el sol no se apaga
        self.assertTrue(self.env["l10n_pe_ne.rubro_auditoria"].search(
            [("company_id", "=", self.env.company.id), ("campo", "=", "catalogos")]))

    def test_set_rechaza_default_inactivo_y_vacios(self):
        base = {"medios": {"lista": ["Efectivo"], "default": "Efectivo"},
                "afectaciones": {"activas": ["1000"], "gratuitas": [], "default": "1000"},
                "monedas": {"activas": ["PEN"]}}
        with self.assertRaises(UserError):   # default fuera de activas
            self.AM.l10n_pe_ne_cfg_catalogos_set(
                {**base, "unidades": {"activas": ["NIU"], "default": "KGM"}})
        with self.assertRaises(UserError):   # sin unidades
            self.AM.l10n_pe_ne_cfg_catalogos_set(
                {**base, "unidades": {"activas": [], "default": "NIU"}})
        with self.assertRaises(UserError):   # sin afectación de venta
            self.AM.l10n_pe_ne_cfg_catalogos_set(
                {"unidades": {"activas": ["NIU"], "default": "NIU"},
                 "medios": {"lista": ["Efectivo"], "default": "Efectivo"},
                 "afectaciones": {"activas": ["9996"], "gratuitas": [], "default": "9996"},
                 "monedas": {"activas": ["PEN"]}})

    def test_muro_solo_duenio_supervisor(self):
        user = self.env["res.users"].sudo().create({
            "name": "Cajero Cat", "login": "cajero.cat@test",
            "company_id": self.env.company.id, "company_ids": [(6, 0, [self.env.company.id])],
            "group_ids": [(4, self.env.ref("base.group_user").id),
                          (4, self.env.ref("l10n_pe_ne_biller.group_l10n_pe_ne_emisor").id)],
        })
        with self.assertRaises(UserError):
            self.AM.with_user(user).l10n_pe_ne_cfg_catalogos_set({
                "unidades": {"activas": ["NIU"], "default": "NIU"},
                "medios": {"lista": ["Efectivo"], "default": "Efectivo"},
                "afectaciones": {"activas": ["1000"], "gratuitas": [], "default": "1000"},
                "monedas": {"activas": ["PEN"]}})

    def test_config_expone_catalogos(self):
        self.AM.l10n_pe_ne_set_rubro({"rubros": ["grifo"], "overrides": {}})
        cfg = self.AM.l10n_pe_ne_config()
        self.assertIn("catalogos", cfg)
        self.assertIn("GLL", cfg["catalogos"]["unidades"]["activas"])

    def test_provision_tenant_siembra_catalogos(self):
        self.env["res.company"].l10n_pe_ne_provision_tenant({
            "ruc": "20608888881", "razonSocial": "MADERERA TEST SAC",
            "login": "maderera.cat@test", "password": "S3gura#2026x",
            "rubros": ["maderera"]})
        company = self.env["res.company"].sudo().search([("vat", "=", "20608888881")], limit=1)
        cfg = company._l10n_pe_ne_cfg()
        self.assertIn("MTQ", cfg["unidades"]["activas"])   # nació con m³ activo
        self.assertIn("MTK", cfg["unidades"]["activas"])


@tagged("post_install", "-at_install")
class TestTipoDeNegocio(L10nPeSeedMixin, TransactionCase):
    """P2/P3 · Cambio de tipo de negocio: preview sin efectos y re-siembra con fusión."""

    def setUp(self):
        super().setUp()
        self.AM = self.env["account.move"]
        self.env.company.sudo().write({
            "l10n_pe_ne_rubros": "[]", "l10n_pe_ne_modulos_override": "{}",
            "l10n_pe_ne_cfg_catalogos": ""})

    def test_preview_no_escribe_y_describe_el_cambio(self):
        antes_rubros = self.env.company.l10n_pe_ne_rubros
        p = self.AM.l10n_pe_ne_rubro_preview({"rubros": ["restaurante"]})
        self.assertTrue(p["legacyAntes"])   # la empresa veía todo
        self.assertGreater(len(p["modulos"]["salen"]), 0)   # va a dejar de ver módulos
        self.assertIn("GRM", p["catalogos"]["unidades"]["activas"])   # sugerencia del rubro
        # y NO escribió nada:
        self.assertEqual(self.env.company.l10n_pe_ne_rubros, antes_rubros)
        self.assertFalse(self.env.company._l10n_pe_ne_cfg())

    def test_cambio_de_tipo_resiembra_fusionando(self):
        # Estado inicial: bodega con un medio PERSONALIZADO y una unidad EN USO (producto en
        # galones). Cambiar a restaurante debe re-sembrar PERO conservar ambos.
        self.AM.l10n_pe_ne_set_rubro({"rubros": ["bodega"], "overrides": {}})
        self.AM.l10n_pe_ne_cfg_catalogos_set({
            "unidades": {"activas": ["NIU", "ZZ", "KGM"], "default": "NIU"},
            "medios": {"lista": ["Efectivo", "Yape", "Agora"], "default": "Yape"},
            "afectaciones": {"activas": ["1000", "9997", "9998"], "gratuitas": ["11"], "default": "1000"},
            "monedas": {"activas": ["PEN"]},
        })
        self.env["product.product"].create({
            "name": "Combustible galón", "l10n_pe_ne_unit_code": "GLL",
            "company_id": self.env.company.id})
        estado = self.AM.l10n_pe_ne_set_rubro(
            {"rubros": ["restaurante"], "overrides": {}, "aplicarCatalogos": True})
        cfg = self.env.company._l10n_pe_ne_cfg()
        self.assertIn("GRM", cfg["unidades"]["activas"])    # sugerencia del nuevo rubro
        self.assertIn("GLL", cfg["unidades"]["activas"])    # conservada por EN USO
        self.assertIn("Agora", cfg["medios"]["lista"])      # personalizado conservado
        self.assertEqual(cfg["medios"]["default"], "Yape")  # default vigente respetado
        self.assertIn("GLL", estado["catalogosConservados"]["unidades"])
        self.assertIn("Agora", estado["catalogosConservados"]["medios"])
        # auditoría del resembrado
        self.assertTrue(self.env["l10n_pe_ne.rubro_auditoria"].search(
            [("company_id", "=", self.env.company.id), ("campo", "=", "catalogos(resembrado)")]))

    def test_sin_flag_no_pisa_config(self):
        self.AM.l10n_pe_ne_set_rubro({"rubros": ["bodega"], "overrides": {}})
        self.AM.l10n_pe_ne_cfg_catalogos_set({
            "unidades": {"activas": ["NIU"], "default": "NIU"},
            "medios": {"lista": ["Efectivo"], "default": "Efectivo"},
            "afectaciones": {"activas": ["1000"], "gratuitas": [], "default": "1000"},
            "monedas": {"activas": ["PEN"]},
        })
        self.AM.l10n_pe_ne_set_rubro({"rubros": ["restaurante"], "overrides": {}})   # sin flag
        cfg = self.env.company._l10n_pe_ne_cfg()
        self.assertNotIn("GRM", cfg["unidades"]["activas"])   # la config propia quedó intacta
