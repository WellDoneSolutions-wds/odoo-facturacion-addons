# -*- coding: utf-8 -*-
"""Visibilidad por local (S5): «¿cuánto vendió Miraflores?» es la primera pregunta del dueño
el día que abre el segundo local.

El local ya viajaba al XML, pero no salía por ninguna lectura: la lista de comprobantes no lo
traía ni se podía filtrar por él, y el detalle tampoco lo decía. Sin eso, separar las ventas por
sucursal exigía abrir los XML uno por uno.
"""
from unittest.mock import patch

from odoo.tests import TransactionCase, tagged

from .common import EnvioSincronoMixin, L10nPeSeedMixin

_TARGET = "odoo.addons.l10n_pe_ne_biller.models.account_move_biller.requests.post"


@tagged("post_install", "-at_install")
class TestSerieVisibilidad(L10nPeSeedMixin, EnvioSincronoMixin, TransactionCase):
    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.Move = self.env["account.move"]
        self.Serie = self.env["l10n_pe_ne.serie"]
        self.Estab = self.env["l10n_pe_ne.establecimiento"]
        self.miraflores = self.Estab.create(
            {"codigo": "0002", "ubigeo": "150122", "direccion": "Av. Larco 100, Miraflores"})
        ruc_type = self.env["l10n_latam.identification.type"].search(
            [("l10n_pe_vat_code", "=", "6")], limit=1)
        self.partner = self.env["res.partner"].create({
            "name": "CLIENTE VISIBILIDAD SAC", "vat": "20100070970",
            "l10n_latam_identification_type_id": ruc_type.id})
        self.product = self.env["product.product"].create(
            {"name": "SERVICIO VISIBILIDAD", "default_code": "SV1"})

    # ------------------------------------------------------------------ utilidades
    def _emitir(self, **extra):
        payload = {
            "tipoDoc": "01", "moneda": "PEN",
            "cliente": {"tipoDoc": "6", "numDoc": "20100070970",
                        "razonSocial": "CLIENTE VISIBILIDAD SAC"},
            "lineas": [{"descripcion": "Servicio", "productId": self.product.id,
                        "cantidad": 1, "precioUnitario": 100.0, "taxCode": "1000"}],
        }
        payload.update(extra)
        ok = type("R", (), {"status_code": 200, "text": '<?xml version="1.0"?><Invoice/>',
                            "headers": {}})()
        with patch(_TARGET, return_value=ok):
            res = self.Move.l10n_pe_ne_quick_emit(payload)
        return res, self.Move.browse(res["id"])

    def _listar(self, **kw):
        return {f["id"]: f for f in self.Move.l10n_pe_ne_quick_list(**kw)}

    # ------------------------------------------------------- la columna nueva
    def test_la_lista_dice_de_que_local_es_cada_comprobante(self):
        self.Serie.create({"codigo": "F002", "tipo_doc": "01", "predeterminada": True,
                           "establecimiento_id": self.miraflores.id})
        _, anexo = self._emitir(codEstablecimiento="0002")
        _, domicilio = self._emitir()
        filas = self._listar()
        self.assertEqual(filas[anexo.id]["establecimiento"], "0002")
        self.assertEqual(filas[domicilio.id]["establecimiento"], "0000")

    def test_el_comprobante_sin_codigo_se_lee_como_domicilio_fiscal(self):
        """Todo lo emitido antes de esta fase salió del domicilio fiscal: la celda dice '0000'
        y no queda vacía, que se leería como «no se sabe»."""
        _, move = self._emitir()
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE account_move SET l10n_pe_ne_cod_establecimiento = NULL WHERE id = %s",
            (move.id,))
        move.invalidate_recordset(["l10n_pe_ne_cod_establecimiento"])
        self.assertEqual(self._listar()[move.id]["establecimiento"], "0000")

    # ------------------------------------------------------------- el filtro
    def test_filtrar_por_local_deja_solo_sus_ventas(self):
        self.Serie.create({"codigo": "F002", "tipo_doc": "01", "predeterminada": True,
                           "establecimiento_id": self.miraflores.id})
        _, anexo = self._emitir(codEstablecimiento="0002")
        _, domicilio = self._emitir()
        solo_anexo = self._listar(establecimiento="0002")
        self.assertIn(anexo.id, solo_anexo)
        self.assertNotIn(domicilio.id, solo_anexo)
        solo_domicilio = self._listar(establecimiento="0000")
        self.assertIn(domicilio.id, solo_domicilio)
        self.assertNotIn(anexo.id, solo_domicilio)

    def test_el_filtro_del_domicilio_fiscal_arrastra_la_historia_sin_codigo(self):
        """Si '0000' no incluyera los NULL, el filtro que sirve para cuadrar el mes escondería
        todo lo emitido antes de la fase — justo lo contrario de lo que el dueño pide."""
        _, viejo = self._emitir()
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE account_move SET l10n_pe_ne_cod_establecimiento = NULL WHERE id = %s",
            (viejo.id,))
        viejo.invalidate_recordset(["l10n_pe_ne_cod_establecimiento"])
        self.assertIn(viejo.id, self._listar(establecimiento="0000"))

    def test_el_filtro_acepta_varios_locales(self):
        """Mismo contrato que estado/tipo/serie: CSV o lista → filtra con `in`."""
        self.Serie.create({"codigo": "F002", "tipo_doc": "01", "predeterminada": True,
                           "establecimiento_id": self.miraflores.id})
        _, anexo = self._emitir(codEstablecimiento="0002")
        _, domicilio = self._emitir()
        ambos = self._listar(establecimiento="0000,0002")
        self.assertLessEqual({anexo.id, domicilio.id}, set(ambos))

    def test_sin_filtro_la_lista_no_cambia(self):
        """Aditivo: quien no filtra por local ve exactamente lo de siempre (y sus claves)."""
        _, move = self._emitir()
        fila = self._listar()[move.id]
        self.assertLessEqual(
            {"id", "tipoDoc", "serie", "correlativo", "estado", "total", "moneda", "cliente"},
            set(fila))

    # --------------------------------------------- detalle y resultado del emit
    def test_el_detalle_dice_el_local_y_su_direccion(self):
        """El código solo no le dice nada al dueño: se acompaña de la dirección, que es como
        reconoce su local en la calle."""
        self.Serie.create({"codigo": "F002", "tipo_doc": "01", "predeterminada": True,
                           "establecimiento_id": self.miraflores.id})
        _, anexo = self._emitir(codEstablecimiento="0002")
        d = anexo.l10n_pe_ne_comprobante_detalle()
        self.assertEqual(d["establecimiento"], "0002")
        self.assertEqual(d["establecimientoDireccion"], "Av. Larco 100, Miraflores")

    def test_el_detalle_del_domicilio_fiscal_usa_la_direccion_de_la_empresa(self):
        """'0000' no tiene fila (D3): su dirección es la del partner de la compañía, igual que
        la fila sintética del catálogo."""
        self.company.partner_id.street = "Av. Principal 999"
        _, move = self._emitir()
        d = move.l10n_pe_ne_comprobante_detalle()
        self.assertEqual(d["establecimiento"], "0000")
        self.assertEqual(d["establecimientoDireccion"], "Av. Principal 999")

    def test_el_resultado_del_emit_devuelve_el_local_declarado(self):
        """El POS no manda el local (lo resuelve la caja) y la nota lo hereda: el resultado del
        emit es la única forma de que la pantalla y el ticket digan lo que DE VERDAD se declaró
        en vez de lo que la pantalla creía."""
        self.Serie.create({"codigo": "F002", "tipo_doc": "01", "predeterminada": True,
                           "establecimiento_id": self.miraflores.id})
        res, _move = self._emitir(codEstablecimiento="0002")
        self.assertEqual(res["establecimiento"], "0002")

    def test_la_nota_reporta_el_local_heredado(self):
        """La NC de una venta del anexo se imputa al anexo, en la lista y en el detalle: antes
        todas sumaban al domicilio fiscal y el reporte por local mentía en las devoluciones."""
        self.Serie.create({"codigo": "F002", "tipo_doc": "01", "predeterminada": True,
                           "establecimiento_id": self.miraflores.id})
        self.Serie.create({"codigo": "FC02", "tipo_doc": "07", "predeterminada": True,
                           "establecimiento_id": self.miraflores.id})
        _, factura = self._emitir(codEstablecimiento="0002")
        _, nota = self._emitir(tipoDoc="07", motivo="01", docAfectado={"id": factura.id})
        self.assertEqual(self._listar()[nota.id]["establecimiento"], "0002")
        self.assertIn(nota.id, self._listar(establecimiento="0002"))
