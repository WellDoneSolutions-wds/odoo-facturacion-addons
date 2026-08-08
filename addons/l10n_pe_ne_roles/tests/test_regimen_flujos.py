# -*- coding: utf-8 -*-
"""Régimen tributario (F1) en los flujos de roles: cotización y orden de trabajo.

Estas dos pantallas DERIVAN el tipo de comprobante del documento del cliente («tiene RUC ⇒
factura») en vez de preguntárselo al cajero: no hay selector de tipo en «Cobrar». Sin gating
por régimen eso está bien; con un NRUS, armaría un tipoDoc '01' que el muro de emisión rechaza
y la venta quedaría INEJECUTABLE por esas pantallas — el cajero solo vería un error, sin
alternativa. Por eso aquí se DEGRADA a boleta (y solo aquí: donde el usuario elige el tipo, el
muro corta y explica).

El invariante de siempre: compañía SIN régimen = legacy = derivación idéntica a la de antes.
"""
from unittest.mock import patch

from odoo.tests import TransactionCase, tagged

from odoo.addons.l10n_pe_ne_biller.tests.common import EnvioSincronoMixin

_EMIT = "odoo.addons.l10n_pe_ne_biller.models.account_move_biller.requests.post"
_OK = type("R", (), {"status_code": 200, "text": '<?xml version="1.0"?><Invoice/>', "headers": {}})()


@tagged("post_install", "-at_install")
class TestRegimenFlujos(EnvioSincronoMixin, TransactionCase):

    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.Cot = self.env["l10n_pe_ne.cotizacion"]
        self.Orden = self.env["l10n_pe_ne.orden.trabajo"]
        self.Sesion = self.env["l10n_pe_ne.caja.sesion"]
        self._set()   # estado conocido: sin régimen
        ruc_type = self.env["l10n_latam.identification.type"].search(
            [("l10n_pe_vat_code", "=", "6")], limit=1)
        # Cliente CON RUC: es el que dispara la derivación a factura.
        self.cliente = self.env["res.partner"].create({
            "name": "CLIENTE REGIMEN SAC", "vat": "20100070970",
            "l10n_latam_identification_type_id": ruc_type.id})
        self.producto = self.env["product.product"].create(
            {"name": "SERVICIO REGF", "default_code": "SREGF"})

    # ------------------------------------------------------------------ helpers
    def _set(self, regimen=False):
        self.env.company.sudo().write({"l10n_pe_ne_regimen": regimen})

    def _user(self, login, grupos):
        return self.env["res.users"].create({
            "name": login, "login": login,
            "company_id": self.company.id, "company_ids": [(6, 0, [self.company.id])],
            "group_ids": [(4, self.env.ref("base.group_user").id)]
                         + [(4, self.env.ref(g).id) for g in grupos],
        })

    def _cot(self, estado="aceptada"):
        cot = self.Cot.create({
            "partner_id": self.cliente.id,
            "line_ids": [(0, 0, {"product_id": self.producto.id, "descripcion": "SERVICIO REGF",
                                 "cantidad": 1.0, "precio_unitario": 118.0, "afecto_igv": True})],
        })
        cot.write({"estado": estado})
        return cot

    def _orden(self):
        return self.Orden.create({
            "partner_id": self.cliente.id,
            "linea_ids": [(0, 0, {"product_id": self.producto.id, "descripcion": "SERVICIO REGF",
                                  "cantidad": 1.0, "precio_unitario": 118.0, "afecto_igv": True})],
        })

    # ======================================================= cotización (H1)
    def test_cotizacion_deriva_factura_sin_regimen(self):
        """Contracara obligatoria: el legacy no cambia. Cliente con RUC ⇒ factura, como siempre."""
        self.assertEqual(self._cot()._l10n_pe_ne_payload_emision()["tipoDoc"], "01")

    def test_cotizacion_deriva_factura_en_rer(self):
        self._set("rer")
        self.assertEqual(self._cot()._l10n_pe_ne_payload_emision()["tipoDoc"], "01")

    def test_cotizacion_degrada_a_boleta_en_nrus(self):
        """El hallazgo: sin esto, cobrar una cotización a un cliente con RUC armaba '01' y el
        muro dejaba la venta sin ninguna vía de cobro."""
        self._set("nrus")
        payload = self._cot()._l10n_pe_ne_payload_emision()
        self.assertEqual(payload["tipoDoc"], "03")
        # El cliente sigue declarándose con su RUC: el NRUS boletea a quien sea, y la boleta
        # lleva el documento REAL del comprador (cat. 06), no un DNI inventado.
        self.assertEqual(payload["cliente"]["tipoDoc"], "6")
        self.assertEqual(payload["cliente"]["numDoc"], "20100070970")

    def test_cotizacion_degradada_pasa_el_muro(self):
        """La prueba de que la venta es EJECUTABLE: el tipo derivado no choca con el muro.
        Con el cajero real (no root), que es quien pulsa «Cobrar»."""
        self._set("nrus")
        cajero = self._user("caj_muro", ["l10n_pe_ne_roles.group_l10n_pe_ne_caja"])
        tipo = self._cot()._l10n_pe_ne_payload_emision()["tipoDoc"]
        self.env["account.move"].with_user(cajero)._l10n_pe_ne_check_regimen(tipo)   # no lanza

    def test_cotizacion_cobrar_emite_boleta_en_nrus(self):
        """La vía real de la pantalla: «Cobrar y emitir» en un NRUS termina en una BOLETA
        emitida, no en un UserError."""
        self._set("nrus")
        cajero = self._user("caj_reg", ["l10n_pe_ne_roles.group_l10n_pe_ne_caja"])
        cot = self._cot()
        with patch(_EMIT, return_value=_OK):
            cot.with_user(cajero).l10n_pe_ne_cobrar_entregar({})
        self.assertEqual(cot.estado, "convertida")
        self.assertEqual(cot.comprobante_id._l10n_pe_document_type(), "03")

    # ================================================ orden de trabajo (H1)
    def test_orden_deriva_factura_sin_regimen(self):
        tipo, _cli = self._orden()._l10n_pe_ne_cliente_emision()
        self.assertEqual(tipo, "01")

    def test_orden_degrada_a_boleta_en_nrus(self):
        self._set("nrus")
        tipo, cli = self._orden()._l10n_pe_ne_cliente_emision()
        self.assertEqual(tipo, "03")
        self.assertEqual(cli["tipoDoc"], "6")

    def test_orden_anticipo_via_a_tambien_degrada(self):
        """El anticipo (Vía A) comparte `_l10n_pe_ne_cliente_emision` con el saldo final: si se
        degradara solo uno, el final referenciaría un anticipo de otra familia."""
        self._set("nrus")
        orden = self._orden()
        self.assertEqual(orden._l10n_pe_ne_payload_anticipo(50.0, "Efectivo")["tipoDoc"], "03")
        self.assertEqual(orden._l10n_pe_ne_payload_emision()["tipoDoc"], "03")

    def test_orden_cobrar_saldo_emite_boleta_en_nrus(self):
        self._set("nrus")
        cajero = self._user("caj_reg_ot", ["l10n_pe_ne_roles.group_l10n_pe_ne_caja"])
        self.Sesion.search([("estado", "=", "abierta")]).write({"estado": "cerrada"})
        self.Sesion.l10n_pe_ne_abrir_caja({"saldoInicial": 0})
        orden = self._orden()
        orden.write({"estado": "terminada"})
        with patch(_EMIT, return_value=_OK):
            res = orden.with_user(cajero).l10n_pe_ne_cobrar_saldo({"medio": "Efectivo"})
        move = self.env["account.move"].browse(res["comprobanteId"])
        self.assertEqual(move._l10n_pe_document_type(), "03")
