# -*- coding: utf-8 -*-
"""C2 · Cierre de caja con tolerancia, justificación y aviso.

El hallazgo: el mismo auditor cerró un turno con +1.00 y otro con -2469.41, y las dos veces el
sistema dijo que sí sin preguntar nada. El arqueo guardaba la cifra pero no la EXPLICACIÓN, así
que al día siguiente no había forma de distinguir «me sobró sencillo» de «falta el efectivo de
media tarde» — y nadie más que el cajero se enteraba.

Lo aprobado (y lo que se prueba aquí):
  * tolerancia por RUC (res.company, nada hardcodeado): por debajo el cierre pasa como siempre;
  * por encima, motivo escrito OBLIGATORIO, congelado en el arqueo junto a quién cerró;
  * aviso al dueño/supervisor por la mensajería del repo (chatter de la sesión);
  * NUNCA se bloquea el cierre esperando una aprobación: en una bodega de tres personas eso es
    un cajero que no se puede ir a su casa (decisión de negocio explícita);
  * el conteo ciego (D-1) sigue intacto: ni el esperado ni la diferencia viajan antes de contar,
    y el mensaje que rebota el cierre tampoco los revela.
"""
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from ..tools.caja_arqueo import descuadre_arqueo


@tagged("post_install", "-at_install")
class TestCajaDescuadre(TransactionCase):
    def setUp(self):
        super().setUp()
        self.Sesion = self.env["l10n_pe_ne.caja.sesion"]
        self.Move = self.env["account.move"]
        self.company = self.env.company
        # El default del RUC es S/ 5.00; se fija explícitamente para que estos tests digan qué
        # tolerancia están probando y no dependan de que nadie toque el default.
        self.company.l10n_pe_ne_cierre_tolerancia = 5.0

    def _abrir(self, saldo=100):
        return self.Sesion.l10n_pe_ne_abrir_caja({"saldoInicial": saldo})

    def _cerrar(self, contado, **extra):
        return self.Sesion.l10n_pe_ne_cerrar_caja(
            dict({"conteos": [{"medio": "Efectivo", "contado": contado}]}, **extra))

    # ═════════════════════════════════════════════ dentro de la tolerancia: sin fricción
    def test_dentro_de_la_tolerancia_cierra_sin_una_sola_pregunta(self):
        """El +1.00 del auditor: es el vuelto de la tarde, no un hallazgo. Pedir un motivo aquí
        haría que el cajero teclee 'ok' doscientas veces y el control moriría de saturación."""
        self._abrir(saldo=100)
        arq = self._cerrar(101)
        self.assertEqual(arq["diferenciaTotal"], 1.0)
        self.assertEqual(arq["descuadre"]["monto"], 1.0)
        self.assertFalse(arq["descuadre"]["sobreTolerancia"])
        self.assertEqual(arq["descuadre"]["motivo"], "")
        self.assertFalse(arq["descuadre"]["avisado"])

    def test_el_descuadre_igual_a_la_tolerancia_todavia_no_exige_nada(self):
        """El tope es «hasta aquí no pasa nada»: se exige explicación cuando se SUPERA, no al
        tocarlo. Un cierre justo en el límite no puede depender del último céntimo del flotante."""
        self._abrir(saldo=100)
        arq = self._cerrar(95)   # diferencia -5.00 == tolerancia
        self.assertEqual(arq["diferenciaTotal"], -5.0)
        self.assertFalse(arq["descuadre"]["sobreTolerancia"])

    def test_el_motivo_se_guarda_aunque_el_arqueo_cuadre(self):
        """Si el cajero se tomó el trabajo de explicar algo, no se tira: mañana es la única
        pista de por qué ese turno tuvo un sobrante de 1 sol tres días seguidos."""
        self._abrir(saldo=100)
        arq = self._cerrar(101, motivoDescuadre="me sobró un sol de vuelto")
        self.assertEqual(arq["descuadre"]["motivo"], "me sobró un sol de vuelto")
        self.assertFalse(arq["descuadre"]["sobreTolerancia"])

    # ═════════════════════════════════════════════ sobre la tolerancia: exige motivo
    def test_sobre_la_tolerancia_el_cierre_exige_motivo(self):
        """EL caso: el -2469.41 que se aceptó en silencio. Ahora rebota y pide que alguien
        escriba qué pasó. El turno NO se cierra: la plata no puede quedar congelada sin una
        línea que la explique."""
        d = self._abrir(saldo=2500)
        with self.assertRaisesRegex(UserError, "descuadra más de lo que tu negocio acepta"):
            self._cerrar(30.59)      # diferencia -2469.41
        sesion = self.Sesion.browse(d["id"])
        self.assertEqual(sesion.estado, "abierta")
        self.assertFalse(sesion.conteos_cierre)

    def test_el_motivo_de_dos_letras_no_es_un_motivo(self):
        """Mismo mínimo que el motivo de un movimiento de caja: si aquí valiera 'ok', el cajero
        aprendería que hay un texto que no cuenta y lo usaría siempre."""
        self._abrir(saldo=100)
        with self.assertRaises(UserError):
            self._cerrar(0, motivoDescuadre="ok")
        # con 3 caracteres ya vale (no se le pide una redacción, se le pide un rastro)
        arq = self._cerrar(0, motivoDescuadre="robo")
        self.assertEqual(arq["descuadre"]["motivo"], "robo")

    def test_el_motivo_queda_congelado_junto_a_quien_cerro(self):
        """D-2: la justificación y el firmante son evidencia. Un motivo reescribible —o un
        cierre al que se le pueda cambiar el firmante— no prueba nada."""
        d = self._abrir(saldo=200)
        arq = self._cerrar(50, motivoDescuadre="falta el efectivo de la tarde, se avisó al dueño")
        sesion = self.Sesion.browse(d["id"])
        self.assertEqual(sesion.descuadre_motivo, "falta el efectivo de la tarde, se avisó al dueño")
        self.assertEqual(sesion.usuario_cierre_id, self.env.user)
        self.assertEqual(arq["descuadre"]["monto"], 150.0)
        self.assertTrue(arq["descuadre"]["sobreTolerancia"])
        self.assertEqual(arq["usuarioCierre"], self.env.user.name)
        for vals in ({"descuadre_motivo": "otra cosa"},
                     {"usuario_cierre_id": self.env.ref("base.user_admin").id},
                     {"descuadre_avisado": False}):
            with self.assertRaisesRegex(UserError, "inmutable"):
                sesion.write(vals)

    def test_dos_descuadres_que_se_compensan_no_pasan_en_silencio(self):
        """+500 en Efectivo y −500 en otro medio suman CERO y son dos descuadres de 500: la
        venta que se cobró por Yape y se registró como efectivo, o algo peor. Mirando solo la
        diferencia neta, ese cierre pasaba sin que nadie escribiera una línea."""
        self._abrir(saldo=500)
        with self.assertRaises(UserError):
            self.Sesion.l10n_pe_ne_cerrar_caja({"conteos": [
                {"medio": "Efectivo", "contado": 0}, {"medio": "Yape", "contado": 500}]})
        arq = self.Sesion.l10n_pe_ne_cerrar_caja({
            "conteos": [{"medio": "Efectivo", "contado": 0}, {"medio": "Yape", "contado": 500}],
            "motivoDescuadre": "la venta de la mañana entró como efectivo y fue por Yape"})
        self.assertEqual(arq["diferenciaTotal"], 0.0)      # la neta cuadra...
        self.assertEqual(arq["descuadre"]["monto"], 500.0)  # ...y aun así hay un descuadre
        self.assertTrue(arq["descuadre"]["sobreTolerancia"])

    def test_la_aritmetica_del_descuadre_es_pura(self):
        """tools/caja_arqueo: el mayor entre la diferencia neta y la mayor de un medio (ninguna
        de las dos domina a la otra). Sin conteo (corte parcial) no hay descuadre que medir."""
        self.assertEqual(descuadre_arqueo([{"diferencia": -3.0}, {"diferencia": -4.0}]), 7.0)
        self.assertEqual(descuadre_arqueo([{"diferencia": 500.0}, {"diferencia": -500.0}]), 500.0)
        self.assertEqual(descuadre_arqueo([{"diferencia": None}]), 0.0)
        self.assertEqual(descuadre_arqueo([]), 0.0)

    # ═════════════════════════════════════════════ aviso al dueño/supervisor
    def test_el_cierre_descuadrado_avisa(self):
        """El aviso usa la mensajería del repo (chatter de la sesión): queda el REGISTRO colgado
        del arqueo —quién, cuánto, por qué— y la notificación a quien supervisa. Sin destinatarios
        (RUC sin roles instalados) el mensaje se postea igual: el registro no depende de que haya
        alguien a quien avisar."""
        d = self._abrir(saldo=200)
        self._cerrar(50, motivoDescuadre="falta el efectivo de la tarde")
        sesion = self.Sesion.browse(d["id"])
        self.assertTrue(sesion.descuadre_avisado)
        cuerpos = " ".join(m.body or "" for m in sesion.message_ids)
        self.assertIn("descuadre", cuerpos)
        self.assertIn("150.00", cuerpos)                       # la magnitud
        self.assertIn("falta el efectivo de la tarde", cuerpos)  # el motivo declarado
        self.assertIn(self.env.user.name, cuerpos)             # quién cerró

    def test_el_cierre_que_cuadra_no_le_avisa_a_nadie(self):
        """Un aviso que llega todos los días deja de leerse. Solo se avisa lo que se sale de la
        tolerancia."""
        d = self._abrir(saldo=100)
        self._cerrar(99)
        sesion = self.Sesion.browse(d["id"])
        self.assertFalse(sesion.descuadre_avisado)
        self.assertFalse(sesion.message_ids.filtered(lambda m: "descuadre" in (m.body or "")))

    def test_el_cierre_nunca_espera_a_un_supervisor(self):
        """Decisión de negocio explícita: con el motivo escrito el cajero cierra AHORA, sin
        aprobación de nadie. Bloquear el cierre en una bodega de tres personas es un problema
        diario, no un control."""
        d = self._abrir(saldo=1000)
        arq = self._cerrar(0, motivoDescuadre="depósito al banco sin registrar el retiro")
        self.assertEqual(arq["estado"], "cerrada")
        self.assertEqual(self.Sesion.browse(d["id"]).estado, "cerrada")

    # ═════════════════════════════════════════════ el parámetro es del RUC
    def test_la_tolerancia_la_decide_el_negocio(self):
        """Nada hardcodeado: la bodega y el local que factura S/ 50 000 al día no tienen la misma
        vara. Con tolerancia alta, el mismo cierre que antes rebotaba pasa sin fricción."""
        self.company.l10n_pe_ne_cierre_tolerancia = 3000.0
        self._abrir(saldo=2500)
        arq = self._cerrar(30.59)     # el -2469.41 del hallazgo
        self.assertFalse(arq["descuadre"]["sobreTolerancia"])
        self.assertEqual(arq["descuadre"]["tolerancia"], 3000.0)

    def test_la_tolerancia_cero_exige_justificar_hasta_el_centimo(self):
        """El otro extremo, también configurable: modo estricto. Es lo contrario del default a
        propósito — se elige, no se hereda."""
        self.company.l10n_pe_ne_cierre_tolerancia = 0.0
        self._abrir(saldo=100)
        with self.assertRaises(UserError):
            self._cerrar(99.9)
        arq = self._cerrar(99.9, motivoDescuadre="10 céntimos de redondeo")
        self.assertTrue(arq["descuadre"]["sobreTolerancia"])

    def test_el_negocio_lee_y_guarda_su_tolerancia(self):
        """Se expone donde el dueño la puede cambiar (pantalla Negocio), no escondida en el ORM."""
        self.assertEqual(self.Move.l10n_pe_ne_negocio()["toleranciaDescuadre"], 5.0)
        n = self.Move.l10n_pe_ne_update_negocio({"toleranciaDescuadre": "12.5"})
        self.assertEqual(n["toleranciaDescuadre"], 12.5)
        self.assertEqual(self.company.l10n_pe_ne_cierre_tolerancia, 12.5)
        # El vacío NO es cero: el formulario manda todos sus campos en cada guardado y un input
        # en blanco por una recarga a medias dejaría al RUC exigiendo justificar cada céntimo.
        self.Move.l10n_pe_ne_update_negocio({"toleranciaDescuadre": ""})
        self.assertEqual(self.company.l10n_pe_ne_cierre_tolerancia, 12.5)
        with self.assertRaises(UserError):
            self.Move.l10n_pe_ne_update_negocio({"toleranciaDescuadre": "-1"})

    # ═════════════════════════════════════════════ el conteo ciego sigue intacto (D-1)
    def test_el_esperado_no_viaja_antes_de_declarar(self):
        """D-1 es sagrado: si el cajero ve el esperado en la misma pantalla donde teclea el
        conteo, el arqueo deja de ser una medición. La tolerancia SÍ viaja —es una política
        publicada, está en Ajustes— para que la pantalla pueda pedir el motivo en el mismo
        formulario en vez de rebotarle el cierre después de contar."""
        d = self._abrir(saldo=100)
        ses = self.Sesion.l10n_pe_ne_caja_actual()["sesion"]
        self.assertNotIn("esperado", ses)
        self.assertNotIn("esperadoTotal", ses)
        self.assertEqual(ses["toleranciaDescuadre"], 5.0)
        arq_abierto = self.Sesion.l10n_pe_ne_caja_arqueo(d["id"])
        self.assertIsNone(arq_abierto["esperadoTotal"])
        self.assertIsNone(arq_abierto["descuadre"]["monto"])
        self.assertIsNone(arq_abierto["descuadre"]["sobreTolerancia"])

    def test_el_rechazo_no_revela_cuanto_falta(self):
        """Un mensaje con la cifra convertiría el rebote en una SONDA: contar 0, leer la
        diferencia y re-declarar un conteo que cuadre. Mismo criterio que el guard del retiro."""
        self._abrir(saldo=1234.56)
        with self.assertRaises(UserError) as ctx:
            self._cerrar(0)
        msg = str(ctx.exception)
        self.assertNotIn("1234.56", msg)
        self.assertNotIn("1,234.56", msg)
        self.assertIn("5.00", msg)      # solo la tolerancia, que es política publicada

    # ═════════════════════════════════════════════ el dueño lo ve en su historial
    def test_el_historial_muestra_que_descuadre_se_explico(self):
        """Es la pantalla donde el dueño mira la semana. Sin el motivo en la fila tendría que
        abrir los cierres uno por uno — o sea, ninguno."""
        d = self._abrir(saldo=200)
        self._cerrar(50, motivoDescuadre="falta el efectivo de la tarde")
        fila = next(f for f in self.Sesion.l10n_pe_ne_list_cajas() if f["id"] == d["id"])
        self.assertEqual(fila["descuadre"]["monto"], 150.0)
        self.assertTrue(fila["descuadre"]["sobreTolerancia"])
        self.assertEqual(fila["descuadre"]["motivo"], "falta el efectivo de la tarde")
        self.assertTrue(fila["descuadre"]["avisado"])

    def test_el_historial_viejo_se_mide_con_la_misma_vara(self):
        """El monto del descuadre se DERIVA del arqueo congelado, no de un campo nuevo: los
        cierres anteriores a esta rebanada —los que nadie explicó— aparecen medidos igual, sin
        migrar nada. Es justo lo que el dueño necesita ver."""
        d = self._abrir(saldo=100)
        sesion = self.Sesion.browse(d["id"])
        # Simula un cierre histórico: snapshot congelado, sin motivo ni aviso (no existían).
        sesion.with_context(l10n_pe_ne_bypass_lock=True).write({
            "estado": "cerrada", "conteos_cierre": [
                {"medio": "Efectivo", "esperado": 100.0, "contado": 0.0, "diferencia": -100.0}]})
        arq = self.Sesion.l10n_pe_ne_caja_arqueo(d["id"])
        self.assertEqual(arq["descuadre"]["monto"], 100.0)
        self.assertTrue(arq["descuadre"]["sobreTolerancia"])
        self.assertEqual(arq["descuadre"]["motivo"], "")
        self.assertFalse(arq["descuadre"]["avisado"])
