# -*- coding: utf-8 -*-
"""Resolución local↔serie en la emisión (S2).

Cubre la cadena completa —nota → payload → serie → caja → domicilio fiscal—, el gate que
rebota una serie ajena al local ANTES de quemar el correlativo, la validación del código
contra el catálogo, la inmutabilidad del local ya emitido y la retrocompatibilidad del tenant
que no configura nada."""
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from .common import EnvioSincronoMixin, L10nPeSeedMixin

_TARGET = "odoo.addons.l10n_pe_ne_biller.models.account_move_biller.requests.post"


@tagged("post_install", "-at_install")
class TestSerieEstablecimiento(L10nPeSeedMixin, EnvioSincronoMixin, TransactionCase):
    def setUp(self):
        super().setUp()   # RUC de la compañía + IGV (self.igv)
        self.company = self.env.company
        self.Move = self.env["account.move"]
        self.Serie = self.env["l10n_pe_ne.serie"]
        self.Estab = self.env["l10n_pe_ne.establecimiento"]
        self.Caja = self.env["l10n_pe_ne.caja.sesion"]
        self.miraflores = self.Estab.create(
            {"codigo": "0002", "ubigeo": "150122", "direccion": "Av. Larco 100, Miraflores"})
        self.san_isidro = self.Estab.create(
            {"codigo": "0003", "ubigeo": "150131", "direccion": "Av. Camino Real 200"})
        ruc_type = self.env["l10n_latam.identification.type"].search(
            [("l10n_pe_vat_code", "=", "6")], limit=1)
        self.partner = self.env["res.partner"].create({
            "name": "CLIENTE SUCURSALES SAC", "vat": "20100070970",
            "l10n_latam_identification_type_id": ruc_type.id})
        self.product = self.env["product.product"].create(
            {"name": "SERVICIO SUCURSAL", "default_code": "SS1"})

    # ------------------------------------------------------------------ utilidades
    def _payload(self, **extra):
        p = {
            "tipoDoc": "01", "moneda": "PEN",
            "cliente": {"tipoDoc": "6", "numDoc": "20100070970",
                        "razonSocial": "CLIENTE SUCURSALES SAC"},
            "lineas": [{"descripcion": "Servicio", "productId": self.product.id,
                        "cantidad": 1, "precioUnitario": 100.0, "taxCode": "1000"}],
        }
        p.update(extra)
        return p

    def _emitir(self, payload):
        ok = type("R", (), {"status_code": 200, "text": '<?xml version="1.0"?><Invoice/>',
                            "headers": {}})()
        with patch(_TARGET, return_value=ok):
            res = self.Move.l10n_pe_ne_quick_emit(payload)
        return self.Move.browse(res["id"])

    def _seq(self, serie):
        return self.env["ir.sequence"].sudo().search(
            [("code", "=", "l10n_pe.ne.cpe.%s" % serie),
             ("company_id", "=", self.company.id)], limit=1)

    def _abrir_caja(self, estab):
        sesion = self.Caja.browse(self.Caja.l10n_pe_ne_abrir_caja({"saldoInicial": 0})["id"])
        sesion.establecimiento_id = estab.id
        return sesion

    # ------------------------------------------------- la cadena, escalón por escalón
    def test_cadena_1_la_nota_hereda_el_local_del_afectado(self):
        """Escalón 1 · La nota NO se emite «desde otro local»: su local es dato derivado del
        comprobante que corrige, y el payload se ignora aunque venga explícito."""
        self.Serie.create({"codigo": "F002", "tipo_doc": "01", "predeterminada": True,
                           "establecimiento_id": self.miraflores.id})
        self.Serie.create({"codigo": "FC02", "tipo_doc": "07", "predeterminada": True,
                           "establecimiento_id": self.miraflores.id})
        factura = self._emitir(self._payload(codEstablecimiento="0002"))
        self.assertEqual(factura.l10n_pe_ne_cod_establecimiento, "0002")

        nota = self._emitir(self._payload(
            tipoDoc="07", motivo="01", docAfectado={"id": factura.id},
            # el payload pide el domicilio fiscal: la herencia manda igual
            codEstablecimiento="0000"))
        self.assertEqual(nota.l10n_pe_ne_cod_establecimiento, "0002")
        self.assertEqual(nota.l10n_pe_ne_serie_emit, "FC02")

    def test_cadena_2_el_payload_explicito_le_gana_a_la_caja(self):
        """Escalón 2 · Elegir el local en la pantalla manda sobre el de la caja abierta: el
        cajero puede facturar por la otra sucursal sin cerrar su turno."""
        self._abrir_caja(self.san_isidro)
        move = self._emitir(self._payload(codEstablecimiento="0002"))
        self.assertEqual(move.l10n_pe_ne_cod_establecimiento, "0002")

    def test_cadena_3_el_local_sale_de_la_serie_pedida(self):
        """Escalón 3 · Pedir F002 ES decir «emito desde Miraflores»: no hace falta repetirlo en
        otro campo (y repetirlo solo da para contradecirse)."""
        self.Serie.create({"codigo": "F002", "tipo_doc": "01",
                           "establecimiento_id": self.miraflores.id})
        self._abrir_caja(self.san_isidro)
        move = self._emitir(self._payload(serie="F002"))
        self.assertEqual(move.l10n_pe_ne_cod_establecimiento, "0002")
        self.assertEqual(move.l10n_pe_ne_serie_emit, "F002")

    def test_cadena_4_el_local_sale_de_la_caja_abierta(self):
        """Escalón 4 · Sin nada en el payload, manda el local del turno: se declara una vez al
        abrir la caja y no una vez por venta."""
        self.Serie.create({"codigo": "F003", "tipo_doc": "01", "predeterminada": True,
                           "establecimiento_id": self.san_isidro.id})
        self._abrir_caja(self.san_isidro)
        move = self._emitir(self._payload())
        self.assertEqual(move.l10n_pe_ne_cod_establecimiento, "0003")
        self.assertEqual(move.l10n_pe_ne_serie_emit, "F003")

    def test_cadena_5_sin_nada_es_el_domicilio_fiscal(self):
        """Escalón 5 · Sin payload, sin serie y sin caja: '0000' y F001, que es lo que hacía
        todo el mundo antes de esta fase."""
        move = self._emitir(self._payload())
        self.assertEqual(move.l10n_pe_ne_cod_establecimiento, "0000")
        self.assertEqual(move.l10n_pe_ne_serie_emit, "F001")

    # --------------------------------------------------------- el local declara y numera
    def test_local_declarado_emite_su_serie_y_la_declara_en_el_xml(self):
        """Criterio 2: el anexo con su serie predeterminada estrena correlativo propio y el XML
        sale con SU codLocalEmisor, no con el del domicilio fiscal."""
        self.Serie.create({"codigo": "F002", "tipo_doc": "01", "predeterminada": True,
                           "establecimiento_id": self.miraflores.id})
        move = self._emitir(self._payload(codEstablecimiento="0002"))
        self.assertEqual(move.l10n_pe_ne_serie_emit, "F002")
        self.assertEqual(move.l10n_pe_ne_corr_emit, "00000001")
        req = move._l10n_pe_build_invoice_request()
        self.assertEqual(req["cabecera"]["codLocalEmisor"], "0002")
        self.assertEqual(req["id"]["serie"], "F002")

    def test_la_predeterminada_gana_a_la_de_menor_codigo(self):
        """Con varias series vivas en el mismo local manda la marcada por defecto; sin ninguna
        marcada, la de menor código —criterio estable, para que la misma configuración numere
        siempre igual y no según el orden en que se dieron de alta—."""
        menor = self.Serie.create({"codigo": "F002", "tipo_doc": "01",
                                   "establecimiento_id": self.miraflores.id})
        pred = self.Serie.create({"codigo": "F009", "tipo_doc": "01", "predeterminada": True,
                                  "establecimiento_id": self.miraflores.id})
        self.assertEqual(
            self._emitir(self._payload(codEstablecimiento="0002")).l10n_pe_ne_serie_emit,
            pred.codigo)
        pred.predeterminada = False
        self.assertEqual(
            self._emitir(self._payload(codEstablecimiento="0002")).l10n_pe_ne_serie_emit,
            menor.codigo)

    def test_series_de_dos_locales_numeran_independientes_y_sin_huecos(self):
        """Criterio 3: alternar entre el domicilio fiscal y el anexo da 1,2,3 en CADA serie. Es
        la prueba de que el correlativo sigue llaveado por serie (D2) y de que, como la serie es
        única por RUC, «por serie» ya es «por local»."""
        self.Serie.create({"codigo": "F002", "tipo_doc": "01", "predeterminada": True,
                           "establecimiento_id": self.miraflores.id})
        corr = {"F001": [], "F002": []}
        for i in range(6):
            local = "0002" if i % 2 else "0000"
            m = self._emitir(self._payload(codEstablecimiento=local))
            corr[m.l10n_pe_ne_serie_emit].append(m.l10n_pe_ne_corr_emit)
        self.assertEqual(corr["F001"], ["00000001", "00000002", "00000003"])
        self.assertEqual(corr["F002"], ["00000001", "00000002", "00000003"])

    def test_ningun_numero_fiscal_repetido(self):
        """Invariante reusable: agrupando por (compañía, serie, correlativo) no puede haber dos.
        Un duplicado solo se corrige con comunicación de baja ante SUNAT."""
        self.Serie.create({"codigo": "F002", "tipo_doc": "01",
                           "establecimiento_id": self.miraflores.id})
        for local in ("0000", "0002", "0000", "0002"):
            self._emitir(self._payload(codEstablecimiento=local))
        grupos = self.Move.sudo()._read_group(
            [("company_id", "=", self.company.id), ("l10n_pe_ne_corr_emit", "!=", False)],
            groupby=["l10n_pe_ne_serie_emit", "l10n_pe_ne_corr_emit"],
            aggregates=["__count"])
        self.assertEqual([g for g in grupos if g[2] > 1], [])

    # ------------------------------------------------------------------------- el gate
    def test_serie_de_otro_local_rebota_sin_quemar_correlativo(self):
        """Criterio 5, el test que evita quemar números fiscales: la serie de Miraflores
        declarando San Isidro se corta ANTES de _l10n_pe_ne_assign_numero, así que el contador
        de F002 no avanza. Un correlativo consumido deja un hueco que hay que justificar."""
        self.Serie.create({"codigo": "F002", "tipo_doc": "01",
                           "establecimiento_id": self.miraflores.id})
        self._emitir(self._payload(codEstablecimiento="0002"))
        proximo = self._seq("F002").number_next_actual
        self.assertEqual(proximo, 2)

        with self.assertRaises(UserError) as ctx:
            with self.env.cr.savepoint():
                self._emitir(self._payload(serie="F002", codEstablecimiento="0003"))
        msg = str(ctx.exception)
        self.assertIn("F002", msg)
        self.assertIn("0002", msg)
        self.assertIn("0003", msg)
        self.assertEqual(self._seq("F002").number_next_actual, proximo)

    def test_serie_sin_local_no_ata_a_nadie(self):
        """La serie declarada SIN local es la del domicilio fiscal, no una cadena: el tenant de
        una sola serie la sigue emitiendo desde su anexo, exactamente como hasta hoy."""
        self.Serie.create({"codigo": "F001", "tipo_doc": "01", "predeterminada": True})
        move = self._emitir(self._payload(codEstablecimiento="0002"))
        self.assertEqual(move.l10n_pe_ne_serie_emit, "F001")
        self.assertEqual(move.l10n_pe_ne_cod_establecimiento, "0002")

    def test_serie_apagada_deja_de_atar(self):
        """Apagar una serie es decir «ya no la uso»: deja de estar habilitada (QA-074) y su
        local deja de vetar nada."""
        serie = self.Serie.create({"codigo": "F002", "tipo_doc": "01",
                                   "establecimiento_id": self.miraflores.id})
        serie.activa = False
        move = self._emitir(self._payload(codEstablecimiento="0003"))
        self.assertEqual(move.l10n_pe_ne_serie_emit, "F001")   # cae al default de siempre
        self.assertEqual(move.l10n_pe_ne_cod_establecimiento, "0003")

    # ---------------------------------------------------- código contra el catálogo
    def test_establecimiento_inexistente_rebota_distinguiendo_el_tramite(self):
        """Criterio 6: un '0009' inventado ya no llega al XML. Y el mensaje separa «no está en
        tu catálogo» de «no está dado de alta ante SUNAT», que es trámite externo."""
        with self.assertRaises(UserError) as ctx:
            with self.env.cr.savepoint():
                self._emitir(self._payload(codEstablecimiento="0009"))
        msg = str(ctx.exception)
        self.assertIn("0009", msg)
        self.assertIn("catálogo", msg)
        self.assertIn("SUNAT", msg)

    def test_establecimiento_archivado_no_emite(self):
        """Archivar el local es cerrar la sucursal: emitir declarando su código seguiría
        diciéndole a SUNAT que sigue abierta."""
        self.Estab.l10n_pe_ne_delete_establecimiento(self.san_isidro.id)
        self.san_isidro.active = False
        with self.assertRaises(UserError):
            with self.env.cr.savepoint():
                self._emitir(self._payload(codEstablecimiento="0003"))

    def test_codigo_con_formato_invalido_rebota(self):
        with self.assertRaises(UserError):
            with self.env.cr.savepoint():
                self._emitir(self._payload(codEstablecimiento="2"))

    # ------------------------------------------------------------------ inmutabilidad
    def test_el_local_es_inmutable_una_vez_numerado(self):
        """El codLocalEmisor viaja dentro del XML firmado: cambiarlo después dejaría al
        comprobante diciendo que salió de un local distinto del que SUNAT recibió."""
        move = self._emitir(self._payload())
        self.assertTrue(move.l10n_pe_ne_corr_emit)
        with self.assertRaises(UserError):
            with self.env.cr.savepoint():
                move.l10n_pe_ne_cod_establecimiento = "0002"
        # mantenimiento/migración sí puede (mismo escape que la caja)
        move.with_context(l10n_pe_ne_bypass_lock=True).write(
            {"l10n_pe_ne_cod_establecimiento": "0002"})
        self.assertEqual(move.l10n_pe_ne_cod_establecimiento, "0002")

    def test_el_local_se_edita_mientras_no_haya_numero(self):
        move = self.Move.create({"move_type": "out_invoice", "partner_id": self.partner.id})
        move.l10n_pe_ne_cod_establecimiento = "0002"
        self.assertEqual(move.l10n_pe_ne_cod_establecimiento, "0002")

    # ------------------------------------------------------------ retrocompatibilidad
    def test_sin_registro_la_emision_se_comporta_igual_que_antes(self):
        """Criterio 1: registro vacío = comportamiento de siempre, sin migración ni
        configuración. Factura F001, boleta B001 y nota de boleta BC01."""
        self.assertFalse(self.Serie.search([("company_id", "=", self.company.id)]))
        self.assertEqual(self.Move._l10n_pe_ne_default_serie("01"), "F001")
        self.assertEqual(self.Move._l10n_pe_ne_default_serie("03"), "B001")

        factura = self._emitir(self._payload())
        self.assertEqual(factura.l10n_pe_ne_serie_emit, "F001")
        self.assertEqual(factura.l10n_pe_ne_cod_establecimiento, "0000")

        boleta = self._emitir(self._payload(
            tipoDoc="03", cliente={"tipoDoc": "1", "numDoc": "45678912",
                                   "razonSocial": "CONSUMIDOR FINAL"}))
        self.assertEqual(boleta.l10n_pe_ne_serie_emit, "B001")
        self.assertEqual(self.Move._l10n_pe_ne_default_serie("07", boleta), "BC01")

        nota = self._emitir(self._payload(
            tipoDoc="07", motivo="01", docAfectado={"id": boleta.id}))
        self.assertEqual(nota.l10n_pe_ne_serie_emit, "BC01")
        self.assertEqual(nota.l10n_pe_ne_cod_establecimiento, "0000")

    def test_sin_registro_el_payload_manda_como_siempre(self):
        """La serie explícita del payload sigue ganando: sin registro no hay nada que la
        contradiga y el gate no tiene a qué agarrarse."""
        move = self._emitir(self._payload(serie="F001", codEstablecimiento="0002"))
        self.assertEqual(move.l10n_pe_ne_serie_emit, "F001")
        self.assertEqual(move.l10n_pe_ne_cod_establecimiento, "0002")
