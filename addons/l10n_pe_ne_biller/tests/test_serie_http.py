# -*- coding: utf-8 -*-
"""El CRUD de series y establecimientos por HTTP (S5): la vía que usa de verdad la SPA.

`test_serie_registro` ya prueba el muro sobre el método del modelo, que es donde vive la
autoridad. Esto prueba el otro extremo del cable: que la ruta que llama Series.tsx lo respete y
lo traduzca a un 403 —no a un 500, ni a un 200 silencioso—, y que el mismo usuario sin permiso
SÍ pueda leer su numeración (cerrar el GET dejaría sin pantalla a los tenants pre-roles).
"""
import json
from datetime import datetime, timedelta

from odoo.tests import HttpCase, tagged


@tagged("post_install", "-at_install")
class TestSerieCrudHttp(HttpCase):
    def setUp(self):
        super().setUp()
        self.Estab = self.env["l10n_pe_ne.establecimiento"]
        self.miraflores = self.Estab.create(
            {"codigo": "0002", "ubigeo": "150122", "direccion": "Av. Larco 100, Miraflores"})
        # Un cajero: emite todo el día, pero no renumera la empresa.
        self.cajero = self._usuario("cajero_series_http",
                                    "l10n_pe_ne_biller.group_l10n_pe_ne_emisor")
        # Quien sí configura (el supervisor/dueño hereda este grupo por implied_ids).
        self.config = self._usuario("config_series_http",
                                    "l10n_pe_ne_biller.group_l10n_pe_ne_config_series")

    # ------------------------------------------------------------------ utilidades
    def _usuario(self, login, xmlid):
        return self.env["res.users"].create({
            "name": login, "login": login,
            "group_ids": [(4, self.env.ref(xmlid).id)]})

    def _headers(self, user):
        key = self.env["res.users.apikeys"].with_user(user)._generate(
            "l10n_pe_ne", "test-series-%s" % user.login, datetime.now() + timedelta(hours=12))
        return {"Authorization": "Bearer %s" % key, "Content-Type": "application/json"}

    def _call(self, user, method, path, body=None):
        r = self.url_open(
            "/ne/api" + path, headers=self._headers(user), method=method,
            data=json.dumps(body).encode() if body is not None else None)
        return r.status_code, (r.json() if r.content else {})

    # ------------------------------------------------------------- sin permiso
    def test_el_cajero_no_puede_declarar_una_serie(self):
        sc, err = self._call(self.cajero, "POST", "/series",
                             {"serie": "F002", "tipoDoc": "01",
                              "establecimientoId": self.miraflores.id})
        self.assertEqual(sc, 403)
        # el mensaje es el del gate de negocio (español), no el texto técnico del ORM
        self.assertIn("permiso", err["message"])
        self.assertFalse(self.env["l10n_pe_ne.serie"].search([("codigo", "=", "F002")]))

    def test_el_cajero_no_puede_desactivar_una_serie(self):
        serie = self.env["l10n_pe_ne.serie"].create(
            {"codigo": "F002", "tipo_doc": "01", "establecimiento_id": self.miraflores.id})
        sc, _err = self._call(self.cajero, "DELETE", "/series/%s" % serie.id)
        self.assertEqual(sc, 403)
        self.assertTrue(serie.activa)

    def test_el_cajero_no_puede_tocar_los_establecimientos(self):
        """El agujero que cerró esta fase: el alta y la baja de locales no validaban nada, y
        desde que el local determina la serie, eso era cambiar la numeración de la empresa."""
        sc, _err = self._call(self.cajero, "POST", "/establecimientos",
                              {"codigo": "0009", "ubigeo": "150101", "direccion": "Av. Pirata"})
        self.assertEqual(sc, 403)
        sc, _err = self._call(self.cajero, "DELETE",
                              "/establecimientos/%s" % self.miraflores.id)
        self.assertEqual(sc, 403)
        self.assertTrue(self.miraflores.exists())
        self.assertFalse(self.Estab.search([("codigo", "=", "0009")]))

    def test_leer_la_numeracion_sigue_abierto_a_cualquier_emisor(self):
        """El muro está en la escritura: cerrar el GET dejaría sin la pantalla de Series a los
        tenants pre-roles, que hoy la ven con solo el grupo Emisor."""
        sc, filas = self._call(self.cajero, "GET", "/series")
        self.assertEqual(sc, 200)
        self.assertIsInstance(filas, list)

    # ------------------------------------------------------------- con permiso
    def test_el_ciclo_completo_desde_la_pantalla(self):
        """Alta → aparece en el listado como declarada → edición → baja lógica. Es exactamente
        la secuencia de botones de Series.tsx."""
        sc, fila = self._call(self.config, "POST", "/series",
                              {"serie": "F002", "tipoDoc": "01",
                               "establecimientoId": self.miraflores.id,
                               "predeterminada": True})
        self.assertEqual(sc, 200)
        self.assertEqual(fila["establecimiento"], "0002")
        self.assertTrue(fila["predeterminada"])

        sc, filas = self._call(self.config, "GET", "/series")
        f002 = next(f for f in filas if f["serie"] == "F002")
        self.assertEqual(f002["origen"], "config")
        self.assertEqual(f002["establecimiento"], "0002")
        self.assertEqual(f002["proximo"], "00000001")

        # editar: se mueve al domicilio fiscal (establecimientoId nulo = '0000', D3)
        sc, fila = self._call(self.config, "POST", "/series",
                              {"id": fila["id"], "serie": "F002", "tipoDoc": "01",
                               "establecimientoId": None})
        self.assertEqual(sc, 200)
        self.assertEqual(fila["establecimiento"], "0000")

        # baja lógica: desactiva, nunca borra (su correlativo es historia consultable)
        sc, fila = self._call(self.config, "DELETE", "/series/%s" % fila["id"])
        self.assertEqual(sc, 200)
        self.assertFalse(fila["activa"])
        self.assertTrue(self.env["l10n_pe_ne.serie"].browse(fila["id"]).exists())

    def test_la_misma_serie_en_dos_locales_rebota_explicando_la_regla(self):
        """400 con el mensaje de negocio: «quiero F001 en mis dos locales» es la intuición del
        dueño y tiene que salir de la pantalla entendiendo por qué no puede, no con un error
        técnico que acabe en soporte como bug."""
        otro = self.Estab.create(
            {"codigo": "0003", "ubigeo": "150131", "direccion": "Av. Camino Real 200"})
        sc, _f = self._call(self.config, "POST", "/series",
                            {"serie": "F002", "tipoDoc": "01",
                             "establecimientoId": self.miraflores.id})
        self.assertEqual(sc, 200)
        sc, err = self._call(self.config, "POST", "/series",
                             {"serie": "F002", "tipoDoc": "01", "establecimientoId": otro.id})
        self.assertEqual(sc, 400)
        self.assertIn("SUNAT", err["message"])
        self.assertIn("0002", err["message"])
