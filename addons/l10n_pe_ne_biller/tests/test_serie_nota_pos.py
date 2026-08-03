# -*- coding: utf-8 -*-
"""Notas y POS: el local deja de mentir (S3).

Dos canales declaraban SIEMPRE el domicilio fiscal aunque la venta fuera de una sucursal:

  * las NC/ND, porque el selector de local se escondía en las notas y nadie heredaba el del
    comprobante afectado — así el reporte por local sumaba todas las devoluciones al '0000';
  * el POS, que nunca envió codEstablecimiento.

Aquí se prueba que la nota hereda local Y serie del afectado, que el POS declara el local de
su turno de caja, y que el tenant que no configura nada sigue viendo '0000' en todo.
"""
from unittest.mock import patch

from odoo.tests import TransactionCase, tagged

from .common import EnvioSincronoMixin, L10nPeSeedMixin

_TARGET = "odoo.addons.l10n_pe_ne_biller.models.account_move_biller.requests.post"


@tagged("post_install", "-at_install")
class TestSerieNotaPos(L10nPeSeedMixin, EnvioSincronoMixin, TransactionCase):
    def setUp(self):
        super().setUp()   # RUC de la compañía + IGV
        self.company = self.env.company
        self.Move = self.env["account.move"]
        self.Serie = self.env["l10n_pe_ne.serie"]
        self.Estab = self.env["l10n_pe_ne.establecimiento"]
        self.Caja = self.env["l10n_pe_ne.caja.sesion"]
        self.miraflores = self.Estab.create(
            {"codigo": "0002", "ubigeo": "150122", "direccion": "Av. Larco 100, Miraflores"})
        ruc_type = self.env["l10n_latam.identification.type"].search(
            [("l10n_pe_vat_code", "=", "6")], limit=1)
        self.partner = self.env["res.partner"].create({
            "name": "CLIENTE NOTAS SAC", "vat": "20100070970",
            "l10n_latam_identification_type_id": ruc_type.id})
        self.product = self.env["product.product"].create(
            {"name": "SERVICIO NOTA", "default_code": "SN1"})

    # ------------------------------------------------------------------ utilidades
    def _payload(self, **extra):
        p = {
            "tipoDoc": "01", "moneda": "PEN",
            "cliente": {"tipoDoc": "6", "numDoc": "20100070970",
                        "razonSocial": "CLIENTE NOTAS SAC"},
            "lineas": [{"descripcion": "Servicio", "productId": self.product.id,
                        "cantidad": 1, "precioUnitario": 100.0, "taxCode": "1000"}],
        }
        p.update(extra)
        return p

    def _payload_pos(self, **extra):
        """Payload TAL CUAL lo arma POS.tsx: sin serie y sin codEstablecimiento. El POS no gana
        selector de local (la doctrina de los 3 toques no admite un paso más por venta): el
        local sale del turno de caja y por eso este payload es el que importa."""
        p = self._payload(
            tipoDoc="03",
            cliente={"tipoDoc": "0", "numDoc": "", "razonSocial": "CLIENTE VARIOS"},
            formaPago={"tipo": "Contado", "medios": [{"medio": "Efectivo", "monto": 118.0}]})
        p.update(extra)
        return p

    def _emitir(self, payload):
        ok = type("R", (), {"status_code": 200, "text": '<?xml version="1.0"?><Invoice/>',
                            "headers": {}})()
        with patch(_TARGET, return_value=ok):
            res = self.Move.l10n_pe_ne_quick_emit(payload)
        return self.Move.browse(res["id"])

    def _abrir_caja(self, estab=None):
        sesion = self.Caja.browse(self.Caja.l10n_pe_ne_abrir_caja({"saldoInicial": 0})["id"])
        if estab:
            sesion.establecimiento_id = estab.id
        return sesion

    def _serie_local(self, codigo, tipo_doc, estab=None):
        return self.Serie.create({
            "codigo": codigo, "tipo_doc": tipo_doc, "predeterminada": True,
            "establecimiento_id": estab.id if estab else False})

    # ---------------------------------------------------------------------- notas
    def test_nota_credito_hereda_local_y_serie_del_afectado(self):
        """Criterio 7: la NC de una factura de Miraflores sale FC02 declarando 0002. Antes salía
        FC01 y '0000' —la devolución se contabilizaba en el domicilio fiscal aunque el cliente
        hubiera comprado en la sucursal—."""
        self._serie_local("F002", "01", self.miraflores)
        self._serie_local("FC02", "07", self.miraflores)
        factura = self._emitir(self._payload(codEstablecimiento="0002"))
        self.assertEqual(factura.l10n_pe_ne_serie_emit, "F002")

        nota = self._emitir(self._payload(
            tipoDoc="07", motivo="01", docAfectado={"id": factura.id}))
        self.assertEqual(nota.l10n_pe_ne_cod_establecimiento, "0002")
        self.assertEqual(nota.l10n_pe_ne_serie_emit, "FC02")
        # y el XML lo declara: es la columna que el contador ve en el RVIE.
        self.assertEqual(
            nota._l10n_pe_build_invoice_request()["cabecera"]["codLocalEmisor"], "0002")

    def test_nota_debito_hereda_igual_que_la_de_credito(self):
        """La ND recorre el mismo escalón 1 del resolver: si no, la mitad de los ajustes de la
        sucursal seguiría declarándose en el domicilio fiscal."""
        self._serie_local("F002", "01", self.miraflores)
        self._serie_local("FD02", "08", self.miraflores)
        factura = self._emitir(self._payload(codEstablecimiento="0002"))

        nota = self._emitir(self._payload(
            tipoDoc="08", motivo="02", docAfectado={"id": factura.id}))
        self.assertEqual(nota.l10n_pe_ne_cod_establecimiento, "0002")
        self.assertEqual(nota.l10n_pe_ne_serie_emit, "FD02")

    def test_nota_de_boleta_hereda_la_familia_y_el_local(self):
        """Una nota de boleta emitida desde la sucursal es BC02, no FC02: la familia la manda el
        afectado y el local también, y elegir mal cualquiera de los dos es rechazo seguro."""
        self._serie_local("B002", "03", self.miraflores)
        self._serie_local("BC02", "07", self.miraflores)
        boleta = self._emitir(self._payload(
            tipoDoc="03", codEstablecimiento="0002",
            cliente={"tipoDoc": "1", "numDoc": "45678912", "razonSocial": "CONSUMIDOR FINAL"}))
        self.assertEqual(boleta.l10n_pe_ne_serie_emit, "B002")

        nota = self._emitir(self._payload(
            tipoDoc="07", motivo="01", docAfectado={"id": boleta.id}))
        self.assertEqual(nota.l10n_pe_ne_cod_establecimiento, "0002")
        self.assertEqual(nota.l10n_pe_ne_serie_emit, "BC02")

    def test_nota_de_factura_del_domicilio_fiscal_sigue_en_0000(self):
        """Retrocompatibilidad: el tenant de un solo local no nota nada. La nota de una factura
        del domicilio fiscal sigue declarando '0000' con FC01, incluso con un anexo dado de alta
        en el catálogo (tener sucursales no cambia dónde se emitió la factura original)."""
        factura = self._emitir(self._payload())
        self.assertEqual(factura.l10n_pe_ne_cod_establecimiento, "0000")

        nota = self._emitir(self._payload(
            tipoDoc="07", motivo="01", docAfectado={"id": factura.id}))
        self.assertEqual(nota.l10n_pe_ne_cod_establecimiento, "0000")
        self.assertEqual(nota.l10n_pe_ne_serie_emit, "FC01")

    def test_la_nota_ignora_la_caja_abierta_en_otro_local(self):
        """El local de la nota es DERIVADO, no elegido: devolver en Miraflores una venta hecha
        en el domicilio fiscal no mueve la nota a Miraflores. Si mandara la caja, el correlativo
        de la nota y el de su factura quedarían en locales distintos."""
        factura = self._emitir(self._payload())
        # C1: esa primera venta, sin caja abierta, abrió la suya sola (o quedaría fuera de todo
        # arqueo). Se cierra para dejar EXACTAMENTE el escenario que este test describe: la única
        # caja abierta es la de Miraflores, y la nota la tiene que ignorar igual.
        # C2: contar 0 contra la venta que abrió ese turno descuadra sobre la tolerancia, así que
        # el cierre exige motivo (aquí el cierre es solo el montaje del escenario).
        self.Caja.l10n_pe_ne_cerrar_caja({"conteos": [{"medio": "Efectivo", "contado": 0}],
                                          "motivoDescuadre": "cierre de prueba sin contar"})
        self._abrir_caja(self.miraflores)
        nota = self._emitir(self._payload(
            tipoDoc="07", motivo="01", docAfectado={"id": factura.id}))
        self.assertEqual(nota.l10n_pe_ne_cod_establecimiento, "0000")

    # ------------------------------------------------------------------------ POS
    def test_pos_declara_el_local_de_su_turno_de_caja(self):
        """El POS no envía codEstablecimiento —no gana un paso más por venta— y aun así la venta
        rápida deja de declararse en el domicilio fiscal: el local sale de la caja que el cajero
        abrió al empezar el turno, y con él la serie del local."""
        self._serie_local("B002", "03", self.miraflores)
        self._abrir_caja(self.miraflores)
        venta = self._emitir(self._payload_pos())
        self.assertEqual(venta.l10n_pe_ne_cod_establecimiento, "0002")
        self.assertEqual(venta.l10n_pe_ne_serie_emit, "B002")
        self.assertEqual(
            venta._l10n_pe_build_invoice_request()["cabecera"]["codLocalEmisor"], "0002")

    def test_pos_sin_caja_sigue_en_el_domicilio_fiscal(self):
        """Sin caja abierta (o con una caja sin local, que es toda caja anterior a esta fase) el
        POS declara '0000' y B001: exactamente lo de siempre."""
        venta = self._emitir(self._payload_pos())
        self.assertEqual(venta.l10n_pe_ne_cod_establecimiento, "0000")
        self.assertEqual(venta.l10n_pe_ne_serie_emit, "B001")

    def test_pos_con_caja_sin_local_sigue_en_el_domicilio_fiscal(self):
        """La caja abierta ANTES de esta fase no tiene local: no debe arrastrar la venta a
        ningún sitio, la cadena sigue hasta el domicilio fiscal."""
        self._abrir_caja()
        venta = self._emitir(self._payload_pos())
        self.assertEqual(venta.l10n_pe_ne_cod_establecimiento, "0000")

    # ------------------------------------------------- lo que la pantalla pinta
    def test_contexto_de_emision_anticipa_lo_que_se_va_a_emitir(self):
        """El chip del POS y el selector de Emitir pintan ESTA respuesta. Si divergiera de lo que
        hace el emit, la pantalla mostraría un local y el XML declararía otro — que es justo el
        problema que S3 viene a cerrar."""
        self._serie_local("B002", "03", self.miraflores)
        self._abrir_caja(self.miraflores)
        ctx = self.Move.l10n_pe_ne_contexto_emision(tipo_doc="03")
        self.assertEqual(ctx["codEstablecimiento"], "0002")
        self.assertEqual(ctx["serie"], "B002")
        self.assertEqual(ctx["establecimiento"], "Av. Larco 100, Miraflores")
        self.assertFalse(ctx["heredado"])

        venta = self._emitir(self._payload_pos())
        self.assertEqual(venta.l10n_pe_ne_cod_establecimiento, ctx["codEstablecimiento"])
        self.assertEqual(venta.l10n_pe_ne_serie_emit, ctx["serie"])

    def test_contexto_de_una_nota_viene_heredado_y_no_es_elegible(self):
        """'heredado' es lo que le dice a la pantalla que pinte el selector BLOQUEADO: ofrecer
        elegir un local que el emit va a ignorar es peor que no ofrecerlo."""
        self._serie_local("F002", "01", self.miraflores)
        self._serie_local("FC02", "07", self.miraflores)
        factura = self._emitir(self._payload(codEstablecimiento="0002"))

        ctx = self.Move.l10n_pe_ne_contexto_emision(
            tipo_doc="07", doc_afectado_id=factura.id)
        self.assertTrue(ctx["heredado"])
        self.assertEqual(ctx["codEstablecimiento"], "0002")
        self.assertEqual(ctx["serie"], "FC02")

    def test_contexto_sin_nada_configurado_es_el_domicilio_fiscal(self):
        """Registro vacío y sin caja: la pantalla muestra '0000' y F001, o sea lo que ya
        mostraba antes de esta fase."""
        ctx = self.Move.l10n_pe_ne_contexto_emision(tipo_doc="01")
        self.assertEqual(ctx["codEstablecimiento"], "0000")
        self.assertEqual(ctx["serie"], "F001")
        self.assertFalse(ctx["heredado"])

    def test_contexto_sigue_a_la_serie_tecleada(self):
        """Teclear F002 en Emitir ES decir «emito desde Miraflores» (escalón 3): el selector
        tiene que moverse con ella, porque desde S3 la pantalla envía SIEMPRE el código y un
        selector quieto en '0000' contradiría a la serie y haría rebotar la emisión."""
        self._serie_local("F002", "01", self.miraflores)
        ctx = self.Move.l10n_pe_ne_contexto_emision(tipo_doc="01", serie="F002")
        self.assertEqual(ctx["codEstablecimiento"], "0002")
        self.assertEqual(ctx["serie"], "F002")
