# -*- coding: utf-8 -*-
"""API HTTP de gestión de equipo (H-4): /ne/api/equipo/*.

Subclasea el controller del biller (L10nPeNeApi) para reusar _identify/_run/_unauth/_body y su
auth por Bearer. Las rutas operan con with_user(uid): el método del modelo comprueba has_group
del dueño (sudo NO cambia env.uid) y hace las escrituras con .sudo() + whitelist.
"""
from odoo import http
from odoo.http import request

from odoo.addons.l10n_pe_ne_biller.controllers.main import L10nPeNeApi, _GET, _POST


class L10nPeNeEquipoApi(L10nPeNeApi):
    def _equipo(self, uid):
        return request.env["res.users"].with_user(uid)

    @http.route("/ne/api/equipo", **_GET)
    def equipo_list(self, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        return self._run(lambda: self._equipo(uid).l10n_pe_ne_duenio_list_equipo())

    @http.route("/ne/api/equipo", **_POST)
    def equipo_alta(self, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        b = self._body() or {}
        return self._run(lambda: self._equipo(uid).l10n_pe_ne_duenio_alta(
            b.get("name"), b.get("login"), b.get("roles"), b.get("email")))

    @http.route("/ne/api/equipo/<int:rec_id>/roles", **_POST)
    def equipo_roles(self, rec_id, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        b = self._body() or {}
        return self._run(lambda: self._equipo(uid).l10n_pe_ne_duenio_set_roles(
            int(rec_id), b.get("roles")))

    @http.route("/ne/api/equipo/<int:rec_id>/activo", **_POST)
    def equipo_activo(self, rec_id, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        b = self._body() or {}
        return self._run(lambda: self._equipo(uid).l10n_pe_ne_duenio_set_activo(
            int(rec_id), b.get("activo")))

    @http.route("/ne/api/equipo/<int:rec_id>/reset-password", **_POST)
    def equipo_reset(self, rec_id, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        return self._run(lambda: self._equipo(uid).l10n_pe_ne_duenio_reset_password(int(rec_id)))

    @http.route("/ne/api/equipo/<int:rec_id>/codueno", **_POST)
    def equipo_codueno(self, rec_id, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        b = self._body() or {}
        return self._run(lambda: self._equipo(uid).l10n_pe_ne_duenio_add_codueno(
            int(rec_id), b.get("password")))

    # ── Políticas de control (gates, iter 4) ──────────────────────────────────
    @http.route("/ne/api/politicas", **_POST)
    def set_politica(self, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        b = self._body() or {}
        return self._run(lambda: request.env["res.company"].with_user(uid).l10n_pe_ne_set_politica(
            b.get("key"), b.get("modo"), b.get("umbral")))

    @http.route("/ne/api/politicas/segregacion", **_POST)
    def set_segregacion(self, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        b = self._body() or {}
        return self._run(lambda: request.env["res.company"].with_user(uid)
                         .l10n_pe_ne_set_exigir_segregacion(b.get("activo")))

    @http.route("/ne/api/politicas/adelanto-facturado", **_POST)
    def set_adelanto_facturado(self, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        b = self._body() or {}
        return self._run(lambda: request.env["res.company"].with_user(uid)
                         .l10n_pe_ne_set_adelanto_facturado(b.get("activo")))

    # ── CN-01: cotización como flujo (transiciones, colas, fold, despacho) ─────
    def _cot(self, uid, rec_id):
        return self._cotizacion(uid).browse(int(rec_id))

    def _paginacion(self, kw):
        page = max(1, int(kw.get("page") or 1))
        size = min(200, max(1, int(kw.get("pageSize") or 10)))
        return (page - 1) * size, size

    @http.route("/ne/api/cotizaciones/<int:rec_id>/enviar", **_POST)
    def cot_enviar(self, rec_id, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        return self._run(lambda: self._cot(uid, rec_id).l10n_pe_ne_enviar())

    @http.route("/ne/api/cotizaciones/<int:rec_id>/aceptar", **_POST)
    def cot_aceptar(self, rec_id, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        return self._run(lambda: self._cot(uid, rec_id).l10n_pe_ne_aceptar())

    @http.route("/ne/api/cotizaciones/<int:rec_id>/rechazar", **_POST)
    def cot_rechazar(self, rec_id, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        b = self._body() or {}
        return self._run(lambda: self._cot(uid, rec_id).l10n_pe_ne_rechazar(b.get("motivo")))

    @http.route("/ne/api/cotizaciones/<int:rec_id>/cobrar-entregar", **_POST)
    def cot_cobrar_entregar(self, rec_id, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        return self._run(lambda: self._cot(uid, rec_id).l10n_pe_ne_cobrar_entregar(self._body() or {}))

    @http.route("/ne/api/cotizaciones/<int:rec_id>/acciones", **_GET)
    def cot_acciones(self, rec_id, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        return self._run(lambda: self._cot(uid, rec_id)._acciones())

    @http.route("/ne/api/cotizaciones/cola-cobro", **_GET)
    def cola_cobro(self, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        off, lim = self._paginacion(kw)
        return self._run(lambda: self._cotizacion(uid).l10n_pe_ne_cola_cobro(off, lim))

    @http.route("/ne/api/despacho/cola", **_GET)
    def cola_despacho(self, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        off, lim = self._paginacion(kw)
        return self._run(lambda: self._cotizacion(uid).l10n_pe_ne_cola_despacho(off, lim))

    @http.route("/ne/api/despacho/<int:rec_id>/entregar", **_POST)
    def despacho_entregar(self, rec_id, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        b = self._body() or {}
        return self._run(lambda: self._cot(uid, rec_id).l10n_pe_ne_entregar(
            b.get("receptorNombre"), b.get("receptorDoc")))

    # ── CN-02: orden de trabajo (taller · adelanto → cola → toma → saldo) ──────
    def _orden_model(self, uid):
        return request.env["l10n_pe_ne.orden.trabajo"].with_user(uid)

    def _orden(self, uid, rec_id):
        return self._orden_model(uid).browse(int(rec_id))

    @http.route("/ne/api/ordenes", **_POST)
    def orden_crear(self, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        return self._run(lambda: self._orden_model(uid).l10n_pe_ne_crear_orden(self._body() or {}))

    @http.route("/ne/api/ordenes/cola", **_GET)
    def orden_cola(self, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        off, lim = self._paginacion(kw)
        return self._run(lambda: self._orden_model(uid).l10n_pe_ne_cola_ordenes(off, lim))

    @http.route("/ne/api/ordenes/cola-adelanto", **_GET)
    def orden_cola_adelanto(self, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        off, lim = self._paginacion(kw)
        return self._run(lambda: self._orden_model(uid).l10n_pe_ne_cola_adelanto(off, lim))

    @http.route("/ne/api/ordenes/cola-saldo", **_GET)
    def orden_cola_saldo(self, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        off, lim = self._paginacion(kw)
        return self._run(lambda: self._orden_model(uid).l10n_pe_ne_cola_saldo(off, lim))

    @http.route("/ne/api/ordenes/<int:rec_id>/adelanto", **_POST)
    def orden_adelanto(self, rec_id, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        b = self._body() or {}
        return self._run(lambda: self._orden(uid, rec_id).l10n_pe_ne_registrar_adelanto(
            b.get("monto"), b.get("medio")))

    @http.route("/ne/api/ordenes/<int:rec_id>/tomar", **_POST)
    def orden_tomar(self, rec_id, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        return self._run(lambda: self._orden(uid, rec_id).l10n_pe_ne_tomar())

    @http.route("/ne/api/ordenes/<int:rec_id>/terminar", **_POST)
    def orden_terminar(self, rec_id, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        return self._run(lambda: self._orden(uid, rec_id).l10n_pe_ne_terminar())

    @http.route("/ne/api/ordenes/<int:rec_id>/cobrar-saldo", **_POST)
    def orden_cobrar_saldo(self, rec_id, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        return self._run(lambda: self._orden(uid, rec_id).l10n_pe_ne_cobrar_saldo(self._body() or {}))

    @http.route("/ne/api/ordenes/<int:rec_id>/anular", **_POST)
    def orden_anular(self, rec_id, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        b = self._body() or {}
        return self._run(lambda: self._orden(uid, rec_id).l10n_pe_ne_anular(b.get("motivo")))

    @http.route("/ne/api/ordenes/<int:rec_id>/acciones", **_GET)
    def orden_acciones(self, rec_id, **kw):
        uid = self._identify()
        if not uid:
            return self._unauth()
        return self._run(lambda: self._orden(uid, rec_id)._acciones())
