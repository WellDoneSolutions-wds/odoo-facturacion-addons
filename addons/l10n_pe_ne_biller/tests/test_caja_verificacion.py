# -*- coding: utf-8 -*-
"""Verificación final de la fase de INTEGRIDAD DE CAJA (C1+C2+C3).

Los tests de cada rebanada prueban SU agujero. Estos dos prueban las dos propiedades que la
fase entera no puede romper, y las prueban de forma exhaustiva en vez de campo por campo:

  * D-1 CONTEO CIEGO — con la sesión abierta, NINGUNA superficie que llega al front puede
    llevar el esperado. Los tests de rebanada miran las claves que ya conocen
    (`assertNotIn("esperado", ...)`); este BARRE el payload completo, recursivamente, por
    todas las superficies —incluidas las que la fase estrenó (`locales` del requiereLocal,
    el aviso de auto-apertura) y que nunca se habían auditado—. La diferencia importa: una
    clave nueva metida dentro de una lista anidada pasa desapercibida a un assertNotIn.

  * RETROCOMPAT — el tenant que no configura nada. Los tres parámetros nuevos viven en
    res.company con default, y el default tiene que dejar el sistema comportándose como el
    día anterior al upgrade salvo en los agujeros que la fase vino a tapar.
"""
from datetime import timedelta

from odoo import fields
from odoo.tests import TransactionCase, tagged

from ..tools.caja_arqueo import calcular_arqueo
from .common import L10nPeSeedMixin

# Claves que NUNCA pueden traer valor con la sesión abierta: son el esperado y todo lo que se
# deriva de él. Se buscan por SUBCADENA para que una clave nueva ('esperadoYape', 'contadoUsd')
# quede cubierta el día que alguien la añada sin leer esta doctrina.
_CLAVES_DEL_ARQUEO = ("esperado", "contado", "diferencia", "sobretolerancia")


@tagged("post_install", "-at_install")
class TestCajaConteoCiegoBarrido(L10nPeSeedMixin, TransactionCase):
    """D-1: el barrido exhaustivo del conteo ciego."""

    def setUp(self):
        super().setUp()
        self.Sesion = self.env["l10n_pe_ne.caja.sesion"]
        self.Gasto = self.env["l10n_pe_ne.gasto"]
        self.Estab = self.env["l10n_pe_ne.establecimiento"]
        self.company = self.env.company
        ruc_type = self.env["l10n_latam.identification.type"].search(
            [("l10n_pe_vat_code", "=", "6")], limit=1)
        self.partner = self.env["res.partner"].create({
            "name": "CLIENTE VERIFICACION SAC", "vat": "20100070970",
            "l10n_latam_identification_type_id": ruc_type.id})
        self.product = self.env["product.product"].create(
            {"name": "PRODUCTO VERIFICACION", "default_code": "S4VF"})

    # ---------------------------------------------------------------- utilidades
    def _abrir(self, user=None, **datos):
        Sesion = self.Sesion.with_user(user) if user else self.Sesion
        d = Sesion.l10n_pe_ne_abrir_caja(dict({"saldoInicial": 0}, **datos))
        sesion = self.Sesion.browse(d["id"])
        # Ancla la apertura en el pasado: create_date lo sella Postgres al inicio de la
        # transacción y fecha_apertura es un now() de Python (mismo flake que documenta test_caja).
        sesion.fecha_apertura = fields.Datetime.now() - timedelta(minutes=5)
        return sesion

    def _venta(self, correlativo, medios=None, precio=100.0):
        move = self.env["account.move"].create({
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
        self.env.cr.execute("UPDATE account_move SET create_date=%s WHERE id=%s",
                            (fields.Datetime.now() - timedelta(minutes=1), move.id))
        move.invalidate_recordset(["create_date"])
        return move

    def _fugas(self, nodo, ruta="payload"):
        """Rutas del payload donde un dato del arqueo viaja con valor. Recursivo a propósito:
        el esperado puede esconderse dentro de una lista de dicts (`locales`, `movimientos`,
        `arqueo`), que es justo donde un assertNotIn de primer nivel no mira."""
        fugas = []
        if isinstance(nodo, dict):
            for clave, valor in nodo.items():
                sub = "%s.%s" % (ruta, clave)
                if valor is not None and any(p in clave.lower() for p in _CLAVES_DEL_ARQUEO):
                    fugas.append("%s = %r" % (sub, valor))
                # El bloque `descuadre` se mira aparte: su clave es 'monto', una palabra
                # demasiado común para meterla en la lista de prohibidas (la usa cada
                # movimiento legítimo), pero su valor ES la magnitud del descuadre.
                if clave == "descuadre" and isinstance(valor, dict) and valor.get("monto") is not None:
                    fugas.append("%s.monto = %r" % (sub, valor["monto"]))
                fugas += self._fugas(valor, sub)
        elif isinstance(nodo, (list, tuple)):
            for i, valor in enumerate(nodo):
                fugas += self._fugas(valor, "%s[%s]" % (ruta, i))
        return fugas

    # ═══════════════════════════════════════════ el barrido encuentra fugas de verdad
    def test_el_barrido_no_es_un_test_vacio(self):
        """Control NEGATIVO. Un escáner que no encuentra nada porque no sabe mirar deja pasar
        exactamente lo que dice vigilar. Sobre una sesión CERRADA —donde el esperado SÍ debe
        viajar— el mismo barrido tiene que encontrarlo; si no, el test de abajo no prueba nada."""
        self._abrir(saldoInicial=100.0)
        arq = self.Sesion.l10n_pe_ne_cerrar_caja(
            {"conteos": [{"medio": "Efectivo", "contado": 100.0}]})
        fugas = self._fugas(arq)
        self.assertTrue(fugas, "el barrido no detecta el esperado ni cuando SÍ está: no vigila nada")
        self.assertTrue(any("esperado" in f for f in fugas), fugas)

    # ═══════════════════════════════════════════ ninguna superficie abierta lo revela
    def test_ninguna_superficie_abierta_revela_el_esperado(self):
        """El barrido completo. Una sesión con ventas por varios medios, movimientos por varios
        medios y un gasto del cajón —o sea, todo lo que esta fase estrenó— y NINGUNA de las
        respuestas que la SPA puede pedir con la caja abierta lleva el esperado."""
        sesion = self._abrir(saldoInicial=137.11)
        self._venta("00000001", medios=[{"medio": "Efectivo", "monto": 118.0}])
        self._venta("00000002", medios=[{"medio": "Yape", "monto": 118.0}])
        mov = self.Sesion.l10n_pe_ne_caja_movimiento(
            {"tipo": "ingreso", "motivo": "Sencillo del dueño", "monto": 50.0})
        self.Sesion.l10n_pe_ne_caja_movimiento(
            {"tipo": "retiro", "motivo": "Pago proveedor", "monto": 30.0, "medio": "Yape"})
        self.Gasto.l10n_pe_ne_create_gasto(
            {"descripcion": "Gaseosas", "monto": 20.0, "pagaCaja": True})

        superficies = {
            # GET /ne/api/caja — la pantalla donde el cajero teclea el conteo.
            "caja_actual": self.Sesion.l10n_pe_ne_caja_actual(),
            # GET /ne/api/caja/<id>/arqueo — el "corte parcial" de media jornada.
            "arqueo_parcial": self.Sesion.l10n_pe_ne_caja_arqueo(sesion.id),
            # GET /ne/api/caja/historial — la fila abierta del historial.
            "historial": [f for f in self.Sesion.l10n_pe_ne_list_cajas() if f["id"] == sesion.id],
            # POST /ne/api/caja/movimientos — devuelve la sesión entera.
            "respuesta_movimiento": mov,
            # POST /ne/api/caja/abrir — la respuesta de la apertura.
            "respuesta_apertura": sesion._l10n_pe_ne_sesion_dict(),
            # C1: el aviso «se abrió tu caja» que viaja en la respuesta del cobro.
            "aviso_autoapertura": sesion._l10n_pe_ne_aviso_apertura(),
            # Serie por local: la fila con la que la SPA PREGUNTA en qué local está el cajero.
            "local_dict": sesion._l10n_pe_ne_local_dict(),
        }
        for nombre, payload in superficies.items():
            with self.subTest(superficie=nombre):
                self.assertFalse(self._fugas(payload, nombre),
                                 "el conteo ciego se rompe en %s: %s"
                                 % (nombre, self._fugas(payload, nombre)))

    def test_hasta_donde_llega_el_conteo_ciego_de_verdad(self):
        """LÍMITE CONOCIDO, fijado por escrito para que nadie lo descubra creyendo que es un bug.

        Ninguna clave del payload abierto trae el esperado (lo prueba el barrido de arriba), pero
        el esperado TOTAL se puede reconstruir con una suma de cuarto de primaria a partir de
        cifras que el cajero sí tiene derecho a ver: su propio fondo, sus ventas del turno y sus
        movimientos. O sea: D-1 impide LEER el esperado, no CALCULARLO.

        No es una regresión de esta fase —`saldoInicial`, `ventas.total`, `ingresos` y `retiros`
        ya estaban en el contrato de la sesión abierta desde antes—, y quitarlos vaciaría la
        pantalla del cajero de todo lo que le sirve para trabajar. Lo que sí cambió esta fase es
        que ahora hay una segunda red bajo el agujero: si el conteo se copia del esperado
        calculado, cuadra y no pasa nada; pero si descuadra de verdad, C2 exige el motivo escrito
        y avisa al dueño. El control dejó de depender solo de que el cajero no sepa sumar.

        La identidad vale en el caso simple (todo PEN y todas las ventas cobradas con sus medios
        detallados); con crédito o moneda extranjera de por medio deja de ser exacta."""
        sesion = self._abrir(saldoInicial=137.11)
        self._venta("00000010", medios=[{"medio": "Efectivo", "monto": 118.0}])
        self._venta("00000011", medios=[{"medio": "Yape", "monto": 118.0}])
        self.Sesion.l10n_pe_ne_caja_movimiento(
            {"tipo": "ingreso", "motivo": "Sencillo del dueño", "monto": 50.0})
        self.Sesion.l10n_pe_ne_caja_movimiento(
            {"tipo": "retiro", "motivo": "Pago proveedor", "monto": 30.0})
        d = self.Sesion.l10n_pe_ne_caja_actual()["sesion"]
        derivado = round(d["saldoInicial"] + d["ventas"]["total"] + d["ingresos"] - d["retiros"], 2)
        arq = self.Sesion.l10n_pe_ne_cerrar_caja(
            {"conteos": [{"medio": "Efectivo", "contado": 0.0}, {"medio": "Yape", "contado": 0.0}],
             "motivoDescuadre": "cierre de prueba del limite del conteo ciego"})
        self.assertEqual(derivado, arq["esperadoTotal"],
                         "si esto deja de cuadrar, el límite documentado cambió: revisa el texto")
        self.assertEqual(sesion.estado, "cerrada")

    def test_la_pregunta_por_el_local_tampoco_lo_revela(self):
        """La rama `requiereLocal`: con dos cajas abiertas y ninguna del usuario, el backend
        devuelve la LISTA de locales para que la SPA pregunte. Es una superficie que la fase de
        sucursales estrenó y que nadie había auditado — y va dentro de una lista anidada, que es
        donde un assertNotIn de primer nivel no llega."""
        miraflores = self.Estab.create(
            {"codigo": "0002", "ubigeo": "150122", "direccion": "Av. Larco 100"})
        otro = self.env["res.users"].create({
            "name": "cajero_verif", "login": "cajero_verif",
            "company_id": self.company.id, "company_ids": [(6, 0, [self.company.id])],
            "group_ids": [(4, self.env.ref("l10n_pe_ne_biller.group_l10n_pe_ne_emisor").id)]})
        self._abrir(user=otro, saldoInicial=500.0, codEstablecimiento="0000")
        self._abrir(user=otro, saldoInicial=800.0, establecimientoId=miraflores.id)
        # Un tercer usuario: no abrió ninguna, así que el backend pregunta en vez de adivinar.
        tercero = self.env["res.users"].create({
            "name": "cajero_verif2", "login": "cajero_verif2",
            "company_id": self.company.id, "company_ids": [(6, 0, [self.company.id])],
            "group_ids": [(4, self.env.ref("l10n_pe_ne_biller.group_l10n_pe_ne_emisor").id)]})
        resp = self.Sesion.with_user(tercero).l10n_pe_ne_caja_actual()
        self.assertTrue(resp.get("requiereLocal"))
        self.assertEqual(len(resp["locales"]), 2)
        self.assertFalse(self._fugas(resp, "requiereLocal"), self._fugas(resp, "requiereLocal"))
        # Y tampoco el saldo inicial ajeno: el cajero elige local, no audita la caja del vecino.
        for local in resp["locales"]:
            self.assertNotIn("saldoInicial", local)


@tagged("post_install", "-at_install")
class TestCajaRetrocompat(TransactionCase):
    """El tenant que no configura NADA se comporta como el día anterior al upgrade."""

    def setUp(self):
        super().setUp()
        self.Sesion = self.env["l10n_pe_ne.caja.sesion"]
        self.Gasto = self.env["l10n_pe_ne.gasto"]

    def test_los_defaults_del_ruc_nuevo_son_los_documentados(self):
        """Lo que importa es el DEFAULT del campo —lo que recibe un tenant que nunca entró a
        Ajustes—, no el valor que tenga la compañía de pruebas.

        Se lee con default_get y NO creando una compañía: en esta plataforma cada RUC es su
        propia base (un `company_lock` rechaza la segunda empresa), así que `create` no es una
        forma disponible de preguntar por el default."""
        campos = ["l10n_pe_ne_caja_autoapertura", "l10n_pe_ne_cierre_tolerancia",
                  "l10n_pe_ne_gasto_de_caja"]
        d = self.env["res.company"].default_get(campos)
        # C1: la auto-apertura viene ENCENDIDA — es el agujero que la fase vino a tapar (una
        # venta cobrada sin caja no entraba en ningún arqueo), y taparlo no exige configurar nada.
        self.assertTrue(d.get("l10n_pe_ne_caja_autoapertura"))
        # C2: tolerancia S/ 5.00, nunca 0 — con cero, el céntimo de vuelto de cualquier día
        # obligaría a escribir un motivo y el texto dejaría de significar nada.
        self.assertEqual(d.get("l10n_pe_ne_cierre_tolerancia"), 5.0)
        # C3: los gastos NO salen del cajón por defecto. Es el único default que conserva la
        # conducta anterior: encenderlo haría que gastos que hoy no tocan la caja empezaran a
        # descontarle plata al arqueo el día del upgrade, sin que nadie lo pidiera. Un Boolean
        # sin `default=` ni siquiera aparece en default_get, que es el False que buscamos.
        self.assertFalse(d.get("l10n_pe_ne_gasto_de_caja", False))
        # Y la compañía viva de esta base los tiene igual (nadie los tocó en la instalación).
        self.assertTrue(self.env.company.l10n_pe_ne_caja_autoapertura)
        self.assertFalse(self.env.company.l10n_pe_ne_gasto_de_caja)

    def test_el_gasto_de_siempre_no_toca_la_caja_ni_exige_uma(self):
        """El gasto que el tenant registra hoy —sin marcar nada, sin caja abierta— tiene que
        seguir entrando. Si C3 hubiera enganchado la caja por defecto, este flujo pasaría a
        fallar con «no hay caja abierta» el día del upgrade."""
        self.assertFalse(self.Sesion._l10n_pe_ne_abiertas())
        d = self.Gasto.l10n_pe_ne_create_gasto({"descripcion": "Agua", "monto": 40.0})
        self.assertFalse(d["pagaCaja"])
        self.assertIsNone(d["movimientoCajaId"])
        self.assertFalse(self.Gasto.browse(d["id"]).movimiento_ids)

    def test_el_cierre_de_siempre_no_pregunta_nada(self):
        """El cierre cuadrado (o casi) del 95% de los días: mismo contrato, ninguna pregunta
        nueva. `descuadre` es aditivo — se añade al dict, no reemplaza nada."""
        self.Sesion.l10n_pe_ne_abrir_caja({"saldoInicial": 200.0})
        arq = self.Sesion.l10n_pe_ne_cerrar_caja(
            {"conteos": [{"medio": "Efectivo", "contado": 200.0}]})
        self.assertEqual(arq["estado"], "cerrada")
        self.assertEqual(arq["diferenciaTotal"], 0.0)
        self.assertEqual(arq["descuadre"]["motivo"], "")
        self.assertFalse(arq["descuadre"]["avisado"])
        self.assertFalse(arq["descuadre"]["sobreTolerancia"])

    def test_el_movimiento_sin_medio_sigue_siendo_efectivo_y_los_totales_numeros(self):
        """El cliente viejo (y cualquier integración ya escrita) manda el movimiento SIN medio.
        Tiene que seguir siendo efectivo, y `ingresos`/`retiros` tienen que seguir siendo
        NÚMEROS en el contrato de la sesión: C3 los agrupó por medio puertas adentro, pero el
        resumen que la SPA pinta no cambió de forma."""
        self.Sesion.l10n_pe_ne_abrir_caja({"saldoInicial": 100.0})
        d = self.Sesion.l10n_pe_ne_caja_movimiento(
            {"tipo": "ingreso", "motivo": "Sencillo del dueño", "monto": 50.0})
        self.assertIsInstance(d["ingresos"], float)
        self.assertEqual(d["ingresos"], 50.0)
        self.assertEqual(d["movimientos"][0]["medio"], "Efectivo")
        # Y el esperado sigue cayendo donde caía: 100 + 50 = 150 de efectivo.
        arq = self.Sesion.l10n_pe_ne_cerrar_caja(
            {"conteos": [{"medio": "Efectivo", "contado": 150.0}]})
        self.assertEqual(arq["diferenciaTotal"], 0.0)

    def test_la_aritmetica_conserva_su_contrato_numerico(self):
        """C3 le enseñó a `calcular_arqueo` a recibir ingresos/retiros como dict {medio: monto},
        pero el contrato viejo —dos NÚMEROS, que valen por «todo efectivo»— es el que usan la
        historia ya escrita y cualquier llamador anterior. Las dos formas tienen que dar
        exactamente el mismo arqueo, o la retrocompat es una declaración de intenciones."""
        conteos = [{"medio": "Efectivo", "contado": 170.0}]
        viejo = calcular_arqueo(100.0, {"Efectivo": 50.0}, 40.0, 20.0, conteos)
        nuevo = calcular_arqueo(100.0, {"Efectivo": 50.0},
                                {"Efectivo": 40.0}, {"Efectivo": 20.0}, conteos)
        self.assertEqual(viejo, nuevo)
        filas = viejo[0]
        self.assertEqual(filas[0]["esperado"], 170.0)   # 100 + 50 + 40 - 20
        self.assertEqual(filas[0]["diferencia"], 0.0)
