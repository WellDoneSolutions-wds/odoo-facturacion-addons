# -*- coding: utf-8 -*-
"""C1 · Integridad de caja: medios normalizados + ninguna venta huérfana.

Los dos problemas tienen el mismo síntoma —el arqueo no refleja el dinero real— y aquí se
prueban por separado:

(a) MEDIOS CASE-INSENSITIVE. El medio es texto libre y llega de cuatro orígenes. Sin
    normalizar, 'Efectivo' y 'efectivo' eran DOS filas del arqueo: el cajero cuenta UN cajón y
    el sistema le pedía contarlo dos veces, así que una de las dos cerraba con diferencia sin
    que faltara un sol. Y el guard del retiro leía la clave EXACTA 'Efectivo', de modo que la
    plata escrita en minúscula era invisible para una regla de negocio: no se podía retirar
    dinero que sí estaba en el cajón.

(b) VENTA HUÉRFANA. El POS emitía con la caja cerrada y esa venta no caía en NINGÚN arqueo (la
    sesión previa está congelada por D-2 y la siguiente arranca después): dinero físico sin
    rastro. Ahora la emisión abre la caja del local con saldo 0 y lo avisa en la respuesta.
"""
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from .common import EnvioSincronoMixin, L10nPeSeedMixin

_TARGET = "odoo.addons.l10n_pe_ne_biller.models.account_move_biller.requests.post"


@tagged("post_install", "-at_install")
class TestCajaIntegridad(L10nPeSeedMixin, EnvioSincronoMixin, TransactionCase):
    def setUp(self):
        super().setUp()   # RUC de la compañía + IGV
        self.company = self.env.company
        self.Sesion = self.env["l10n_pe_ne.caja.sesion"]
        self.Move = self.env["account.move"]
        self.Estab = self.env["l10n_pe_ne.establecimiento"]
        ruc_type = self.env["l10n_latam.identification.type"].search(
            [("l10n_pe_vat_code", "=", "6")], limit=1)
        self.partner = self.env["res.partner"].create({
            "name": "CLIENTE INTEGRIDAD SAC", "vat": "20100070970",
            "l10n_latam_identification_type_id": ruc_type.id})
        self.product = self.env["product.product"].create(
            {"name": "PRODUCTO INTEGRIDAD", "default_code": "C1P"})

    # ---------------------------------------------------------------- utilidades
    def _abrir(self, saldo=0, **datos):
        """Abre la caja y ANCLA fecha_apertura en el pasado: create_date la sella Postgres al
        INICIO de la transacción (constante en toda la TransactionCase) mientras el default de
        fecha_apertura es un now() de Python, así que sin esto la ventana puede dejar fuera las
        ventas por una fracción de segundo (mismo flake que documenta test_caja)."""
        d = self.Sesion.l10n_pe_ne_abrir_caja(dict({"saldoInicial": saldo}, **datos))
        sesion = self.Sesion.browse(d["id"])
        sesion.fecha_apertura = fields.Datetime.now() - timedelta(minutes=5)
        return sesion

    def _venta(self, correlativo, medios=None, precio=100.0, sesion=None):
        """Venta cobrada con su create_date dentro de la ventana de la sesión."""
        move = self.Move.create({
            "move_type": "out_invoice", "partner_id": self.partner.id,
            "invoice_date": "2026-08-02", "l10n_pe_serie": "F001",
            "l10n_pe_correlativo": correlativo, "l10n_pe_ne_forma_pago": "Contado",
            "invoice_line_ids": [(0, 0, {
                "product_id": self.product.id, "quantity": 1.0,
                "price_unit": precio, "tax_ids": [(6, 0, self.igv.ids)]})],
        })
        if medios is not None:
            move.l10n_pe_ne_medios_pago = medios
        move.action_post()
        move.l10n_pe_biller_state = "enviado"
        move.flush_recordset()
        dentro = (sesion.fecha_apertura if sesion else fields.Datetime.now()) + timedelta(seconds=5)
        self.env.cr.execute("UPDATE account_move SET create_date=%s WHERE id=%s",
                            (dentro, move.id))
        move.invalidate_recordset(["create_date"])
        return move

    def _payload(self, **extra):
        p = {
            "tipoDoc": "01", "moneda": "PEN",
            "cliente": {"tipoDoc": "6", "numDoc": "20100070970",
                        "razonSocial": "CLIENTE INTEGRIDAD SAC"},
            "lineas": [{"descripcion": "Servicio", "productId": self.product.id,
                        "cantidad": 1, "precioUnitario": 100.0, "taxCode": "1000"}],
        }
        p.update(extra)
        return p

    def _emitir(self, payload, move=None):
        """`move` permite emitir desde otro recordset (p. ej. con contexto). OJO con el
        `move or self.Move`: un recordset VACÍO es FALSY en Odoo, así que
        `self.Move.with_context(...)` —que es vacío— se descartaba y el emit salía sin el
        contexto. El `is None` es obligatorio, no estilo."""
        ok = type("R", (), {"status_code": 200, "text": '<?xml version="1.0"?><Invoice/>',
                            "headers": {}})()
        with patch(_TARGET, return_value=ok):
            return (self.Move if move is None else move).l10n_pe_ne_quick_emit(payload)

    # ═══════════════════════════════════════════ (a) medios de pago case-insensitive
    def test_dos_grafias_del_medio_son_una_sola_fila_del_arqueo(self):
        """EL caso del cajero: cobra una venta por 'Efectivo' y otra por 'efectivo' (dos
        orígenes distintos escribieron distinto) y al cerrar tiene que contar UN solo cajón,
        con UNA sola fila y el esperado sumado. Antes eran dos filas y una cerraba con
        diferencia sin que faltara un sol."""
        sesion = self._abrir(saldo=50)
        self._venta("2001", medios=[{"medio": "Efectivo", "monto": 118.0}], sesion=sesion)
        self._venta("2002", medios=[{"medio": "efectivo", "monto": 118.0}], sesion=sesion)
        self._venta("2003", medios=[{"medio": "YAPE", "monto": 118.0}], sesion=sesion)
        self._venta("2004", medios=[{"medio": "yape", "monto": 118.0}], sesion=sesion)

        # D-1: con la sesión abierta solo viajan los NOMBRES; ya deben venir consolidados.
        medios = self.Sesion.l10n_pe_ne_caja_actual()["sesion"]["medios"]
        self.assertEqual([m for m in medios if m.lower().startswith("efectivo")], ["Efectivo"])
        self.assertEqual([m for m in medios if m.lower() == "yape"], ["Yape"])

        arq = self.Sesion.l10n_pe_ne_cerrar_caja({"conteos": [
            {"medio": "Efectivo", "contado": 286.0}, {"medio": "Yape", "contado": 236.0}]})
        filas = {f["medio"]: f for f in arq["arqueo"]}
        self.assertEqual(len([m for m in filas if m.lower().startswith("efectivo")]), 1)
        self.assertEqual(filas["Efectivo"]["esperado"], 286.0)   # 50 + 118 + 118
        self.assertEqual(filas["Yape"]["esperado"], 236.0)
        self.assertEqual(arq["diferenciaTotal"], 0.0)

    def test_el_medio_se_guarda_con_su_nombre_canonico(self):
        """Normalización AL ESCRIBIR: entra 'EFECTIVO'/'deposito', se persiste
        'Efectivo'/'Depósito'. El nombre bonito es el que ve el cajero en el arqueo y el que
        sale impreso en el ticket, así que no basta con agrupar al leer."""
        move = self.Move.create({
            "move_type": "out_invoice", "partner_id": self.partner.id,
            "invoice_date": "2026-08-02",
            "l10n_pe_ne_medios_pago": [{"medio": "EFECTIVO", "monto": 10.0}],
        })
        self.assertEqual(move.l10n_pe_ne_medios_pago, [{"medio": "Efectivo", "monto": 10.0}])
        # y por write (el camino que usan la emisión y los flujos de roles)
        move.l10n_pe_ne_medios_pago = [{"medio": "  deposito ", "monto": 5.0},
                                       {"medio": "Rappi", "monto": 1.0}]
        self.assertEqual([m["medio"] for m in move.l10n_pe_ne_medios_pago],
                         ["Depósito", "Rappi"])   # el medio de fuera del catálogo se respeta

    def test_la_emision_normaliza_el_medio_que_manda_el_pos(self):
        """El origen real: quick_emit con el medio en minúscula. Lo que quede en el comprobante
        es lo que el arqueo va a leer mañana."""
        res = self._emitir(self._payload(
            formaPago={"tipo": "Contado", "medios": [{"medio": "yape", "monto": 118.0}]}))
        move = self.Move.browse(res["id"])
        self.assertEqual(move.l10n_pe_ne_medios_pago, [{"medio": "Yape", "monto": 118.0}])

    def test_el_retiro_ve_el_efectivo_escrito_en_minuscula(self):
        """El hallazgo que más duele: el guard del retiro comparaba la clave EXACTA 'Efectivo',
        así que una venta cobrada en 'efectivo' era plata REAL invisible para la regla y el
        cajero no podía retirar dinero que sí estaba en el cajón."""
        sesion = self._abrir(saldo=0)
        self._venta("2010", medios=[{"medio": "efectivo", "monto": 200.0}], sesion=sesion)
        # Antes: disponible = 0 -> UserError. Ahora: disponible = 200.
        d = self.Sesion.l10n_pe_ne_caja_movimiento(
            {"tipo": "retiro", "motivo": "Pago proveedor", "monto": 150.0})
        self.assertEqual(d["retiros"], 150.0)
        # y el tope sigue siendo real: 60 más ya excede los 200 disponibles.
        # (C3 reformuló el mensaje del guard —ahora nombra el bolsillo, porque el egreso ya no es
        # solo de efectivo—. La regla que este test cubre no cambió.)
        with self.assertRaisesRegex(UserError, "excede lo que hay en la caja"):
            self.Sesion.l10n_pe_ne_caja_movimiento(
                {"tipo": "retiro", "motivo": "Otro pago", "monto": 60.0})

    def test_el_conteo_en_minuscula_cuadra_contra_su_fila(self):
        """Cliente viejo (o cajero con el teclado en mayúsculas) mandando 'EFECTIVO' en el
        conteo: tiene que cruzar contra la fila 'Efectivo' y cerrar en 0, no inventar una fila
        contada de la nada con todo el esperado como faltante."""
        sesion = self._abrir(saldo=100)
        self._venta("2020", medios=[{"medio": "Efectivo", "monto": 118.0}], sesion=sesion)
        arq = self.Sesion.l10n_pe_ne_cerrar_caja(
            {"conteos": [{"medio": "EFECTIVO", "contado": 218.0}]})
        self.assertEqual(len(arq["arqueo"]), 1)
        self.assertEqual(arq["arqueo"][0]["medio"], "Efectivo")
        self.assertEqual(arq["diferenciaTotal"], 0.0)

    # ═════════════════════════════════════════ (b) POS sin caja → auto-apertura
    def test_la_venta_sin_caja_abre_la_suya_y_cae_en_su_arqueo(self):
        """EL caso: cobrar por POS sin caja abierta. Antes esa venta no entraba en NINGÚN
        arqueo (la anterior está congelada, la siguiente empieza después). Ahora se abre una
        con saldo 0, la venta CAE dentro (la ventana se ancla al inicio de la transacción, no a
        un now() posterior al comprobante) y el cierre la cuenta."""
        self.assertFalse(self.Sesion.search([("estado", "=", "abierta")]))
        res = self._emitir(self._payload(
            formaPago={"tipo": "Contado", "medios": [{"medio": "Efectivo", "monto": 118.0}]}))

        self.assertTrue(res.get("cajaAbierta"), "la respuesta debe avisar que se abrió la caja")
        sesion = self.Sesion.browse(res["cajaAbierta"]["sesionId"])
        self.assertEqual(sesion.estado, "abierta")
        self.assertEqual(sesion.saldo_inicial, 0.0)
        self.assertTrue(sesion.apertura_automatica)
        # lo que importa: la venta NO quedó huérfana.
        self.assertIn(self.Move.browse(res["id"]), sesion._l10n_pe_ne_ventas_sesion())
        arq = self.Sesion.l10n_pe_ne_cerrar_caja(
            {"conteos": [{"medio": "Efectivo", "contado": 118.0}]})
        self.assertEqual(arq["ventas"]["count"], 1)
        self.assertEqual({f["medio"]: f["esperado"] for f in arq["arqueo"]}["Efectivo"], 118.0)
        self.assertEqual(arq["diferenciaTotal"], 0.0)

    def test_la_caja_ya_abierta_no_se_toca(self):
        """Retrocompatibilidad dura: el turno del cajero (con SU saldo inicial) no se duplica ni
        se reemplaza, y la respuesta no trae aviso de apertura."""
        sesion = self._abrir(saldo=200)
        res = self._emitir(self._payload(
            formaPago={"tipo": "Contado", "medios": [{"medio": "Efectivo", "monto": 118.0}]}))
        self.assertNotIn("cajaAbierta", res)
        self.assertEqual(self.Sesion.search_count([("estado", "=", "abierta")]), 1)
        self.assertEqual(sesion.saldo_inicial, 200.0)
        self.assertFalse(sesion.apertura_automatica)

    def test_la_caja_automatica_se_abre_en_el_local_del_comprobante(self):
        """Convivencia con la caja POR LOCAL: si el comprobante se declara en Miraflores, la
        caja que se abre es la de Miraflores. Abrirla en el domicilio fiscal dejaría la venta
        fuera de su propio arqueo (el filtro por local es parte de la ventana)."""
        miraflores = self.Estab.create(
            {"codigo": "0002", "ubigeo": "150122", "direccion": "Av. Larco 100, Miraflores"})
        res = self._emitir(self._payload(codEstablecimiento="0002"))
        sesion = self.Sesion.browse(res["cajaAbierta"]["sesionId"])
        self.assertEqual(sesion.establecimiento_id, miraflores)
        self.assertFalse(sesion.domicilio_fiscal)
        self.assertEqual(res["cajaAbierta"]["establecimiento"], "0002")
        self.assertIn(self.Move.browse(res["id"]), sesion._l10n_pe_ne_ventas_sesion())

    def test_la_caja_automatica_del_domicilio_fiscal_se_marca_como_tal(self):
        """Sin anexos, la caja nueva es la del '0000' EXPLÍCITO (no la caja «del negocio
        entero» de antes de la fase de sucursales): cuenta lo que el resolver manda al '0000',
        que es exactamente la venta que la abrió."""
        res = self._emitir(self._payload())
        sesion = self.Sesion.browse(res["cajaAbierta"]["sesionId"])
        self.assertFalse(sesion.establecimiento_id)
        self.assertTrue(sesion.domicilio_fiscal)
        self.assertEqual(res["cajaAbierta"]["establecimiento"], "0000")

    def test_la_caja_automatica_no_decide_el_local_de_las_ventas_siguientes(self):
        """Convivencia con la fase de series: la caja que se abrió SOLA no es una declaración de
        nadie —es una inferencia de UN comprobante—, así que no puede fijar el local del resto de
        la emisión. Sin esto, cobrar una vez por Miraflores dejaba TODA la emisión posterior
        saliendo con la serie y el codLocalEmisor de Miraflores, quemando correlativos en el
        local equivocado (que solo se arregla con una nota de crédito)."""
        self.Estab.create({"codigo": "0002", "ubigeo": "150122", "direccion": "Av. Larco 100"})
        primera = self._emitir(self._payload(codEstablecimiento="0002"))
        auto = self.Sesion.browse(primera["cajaAbierta"]["sesionId"])
        self.assertEqual(auto._l10n_pe_ne_cod_local(), "0002")
        # La siguiente NO declara local: debe caer en el domicilio fiscal, como antes de C1.
        segunda = self._emitir(self._payload())
        self.assertEqual(segunda["establecimiento"], "0000")

    def test_la_caja_que_abrio_el_cajero_si_decide_el_local(self):
        """El otro lado de la misma regla: el turno que el cajero abrió declarando su sucursal
        SIGUE siendo el escalón 4 del resolver (la doctrina de los 3 toques: se declara una vez
        por turno, no una vez por venta). C1 no puede haberlo desactivado."""
        miraflores = self.Estab.create(
            {"codigo": "0002", "ubigeo": "150122", "direccion": "Av. Larco 100"})
        self._abrir(saldo=0, establecimientoId=miraflores.id)
        res = self._emitir(self._payload())
        self.assertEqual(res["establecimiento"], "0002")

    def test_la_nota_de_credito_no_abre_caja(self):
        """El arqueo solo mira out_invoice: una NC nunca cae en él, así que abrirle un turno
        sería ruido puro (y un turno que alguien tendría que cerrar)."""
        venta = self._emitir(self._payload())
        # C2: se cierra contando 0 contra una venta de 118, o sea con descuadre sobre la
        # tolerancia; el motivo va porque el cierre ahora lo exige (aquí el cierre es solo
        # atrezzo del escenario: lo que se prueba es que la NC no abre caja).
        self.Sesion.l10n_pe_ne_cerrar_caja({"conteos": [{"medio": "Efectivo", "contado": 0}],
                                            "motivoDescuadre": "cierre de prueba sin contar"})
        res = self._emitir(self._payload(
            tipoDoc="07", motivo="01", docAfectado={"id": venta["id"]}))
        self.assertNotIn("cajaAbierta", res)
        self.assertFalse(self.Sesion.search([("estado", "=", "abierta")]))

    def test_el_lote_masivo_no_abre_caja(self):
        """Subir 200 comprobantes desde la oficina no es un cobro de mostrador: abriría un turno
        que nadie atiende y que al cerrarse pediría contar un cajón inexistente."""
        res = self._emitir(self._payload(),
                           move=self.Move.with_context(l10n_pe_ne_sin_autoapertura=True))
        self.assertNotIn("cajaAbierta", res)
        self.assertFalse(self.Sesion.search([("estado", "=", "abierta")]))

    def test_el_ruc_puede_apagar_la_autoapertura(self):
        """Parámetro de negocio en res.company (nada hardcodeado): apagarlo devuelve EXACTAMENTE
        el comportamiento anterior a esta fase, para el RUC que solo factura desde la oficina."""
        self.company.l10n_pe_ne_caja_autoapertura = False
        res = self._emitir(self._payload())
        self.assertNotIn("cajaAbierta", res)
        self.assertFalse(self.Sesion.search([("estado", "=", "abierta")]))

    def test_el_preflight_no_deja_una_caja_abierta(self):
        """El pre-flight valida y revierte: no debe dejar rastro (ni comprobante ni turno)."""
        self.Move.l10n_pe_ne_preflight(self._payload())
        self.assertFalse(self.Sesion.search([("estado", "=", "abierta")]))

    def test_abrir_caja_a_mano_explica_que_la_automatica_estorba(self):
        """El cajero llega, teclea su sencillo y la apertura rebota. Si el mensaje no dice que
        el turno lo abrió una VENTA, el cajero concluye que el sistema está roto."""
        self._emitir(self._payload())
        with self.assertRaisesRegex(UserError, "se abrió sola al cobrar"):
            self.Sesion.l10n_pe_ne_abrir_caja({"saldoInicial": 200})
