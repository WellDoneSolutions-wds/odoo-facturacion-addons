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
