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
