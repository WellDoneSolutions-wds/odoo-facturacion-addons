# -*- coding: utf-8 -*-
"""C2 · A quién le llega el aviso de un cierre descuadrado, y quién puede mover la vara.

La matriz de roles promete «supervisor: aprueba descuadres» y ese flujo no existía: el cajero
cerraba con cualquier diferencia y nadie se enteraba. La rebanada NO trae la aprobación
bloqueante (decisión de negocio: en una bodega de tres personas un cierre que espera a un
supervisor es un problema diario), trae el aviso — que es el eje de DETECCIÓN del repo.

Estas pruebas viven en el addon de roles porque los grupos dueño/supervisor son suyos: el
biller resuelve esos xmlid con raise_if_not_found=False y funciona igual sin este módulo.
"""
from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestCajaDescuadreAviso(TransactionCase):
    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.company.l10n_pe_ne_cierre_tolerancia = 5.0
        self.Sesion = self.env["l10n_pe_ne.caja.sesion"]
        self.cajero = self._user("cajero_c2", "caja")
        self.supervisor = self._user("supervisor_c2", "supervisor")
        self.duenio = self._user("duenio_c2", "duenio")
        self.vendedor = self._user("vendedor_c2", "ventas")

    def _user(self, login, rol):
        return self.env["res.users"].create({
            "name": login, "login": login, "email": "%s@ejemplo.pe" % login,
            "company_id": self.company.id, "company_ids": [(6, 0, [self.company.id])],
            "group_ids": [(4, self.env.ref("l10n_pe_ne_roles.group_l10n_pe_ne_" + rol).id)],
        })

    def test_el_aviso_le_llega_al_dueno_y_al_supervisor(self):
        """Quien se tiene que enterar de un faltante es quien responde por la plata. El vendedor
        no: un aviso que le llega a todo el mundo deja de leerse en una semana."""
        caja = self.Sesion.with_user(self.cajero)
        caja.l10n_pe_ne_abrir_caja({"saldoInicial": 500})
        arq = caja.l10n_pe_ne_cerrar_caja({
            "conteos": [{"medio": "Efectivo", "contado": 100}],
            "motivoDescuadre": "falta el efectivo de la tarde"})
        self.assertTrue(arq["descuadre"]["avisado"])
        sesion = self.Sesion.browse(arq["id"])
        msg = sesion.message_ids.filtered(lambda m: "descuadre" in (m.body or ""))
        self.assertTrue(msg, "el cierre descuadrado tiene que dejar su aviso")
        destinatarios = msg.partner_ids
        self.assertIn(self.supervisor.partner_id, destinatarios)
        self.assertIn(self.duenio.partner_id, destinatarios)
        self.assertNotIn(self.vendedor.partner_id, destinatarios)
        # El autor es el cajero que cerró (el aviso se postea en sudo, que cambia permisos, no
        # identidad): un aviso firmado por «el sistema» no le sirve a nadie.
        self.assertEqual(msg[0].author_id, self.cajero.partner_id)

    def test_el_cierre_que_cuadra_no_despierta_a_nadie(self):
        caja = self.Sesion.with_user(self.cajero)
        caja.l10n_pe_ne_abrir_caja({"saldoInicial": 500})
        arq = caja.l10n_pe_ne_cerrar_caja({"conteos": [{"medio": "Efectivo", "contado": 499}]})
        self.assertFalse(arq["descuadre"]["avisado"])
        self.assertFalse(self.Sesion.browse(arq["id"]).message_ids
                         .filtered(lambda m: "descuadre" in (m.body or "")))

    def test_el_cajero_no_se_puede_subir_la_tolerancia(self):
        """El agujero obvio: subirla a S/ 999 999, cerrar sin escribir nada y sin que se avise.
        Cambiar la regla que a uno le aplica no es operar la caja, es supervisarla."""
        Move = self.env["account.move"]
        with self.assertRaises(AccessError):
            Move.with_user(self.cajero).l10n_pe_ne_update_negocio({"toleranciaDescuadre": "999999"})
        self.assertEqual(self.company.l10n_pe_ne_cierre_tolerancia, 5.0)
        # el supervisor sí (y el dueño lo tiene por implicación)
        Move.with_user(self.supervisor).l10n_pe_ne_update_negocio({"toleranciaDescuadre": "50"})
        self.assertEqual(self.company.l10n_pe_ne_cierre_tolerancia, 50.0)

    def test_guardar_el_negocio_sin_tocar_la_tolerancia_no_se_gatea(self):
        """El muro es sobre el CAMBIO del parámetro de control, no sobre el endpoint entero:
        endurecer todo /ne/api/negocio es otra decisión y no un efecto colateral de esta
        rebanada. Reenviar el mismo valor (lo que hace un formulario al guardar) no rebota."""
        self.env["account.move"].with_user(self.cajero).l10n_pe_ne_update_negocio(
            {"toleranciaDescuadre": "5"})
        self.assertEqual(self.company.l10n_pe_ne_cierre_tolerancia, 5.0)
