# -*- coding: utf-8 -*-
"""Sesión temporal de importación por lotes.

El archivo (.xlsx) se sube y se parsea UNA sola vez; sus filas quedan guardadas acá (JSON) bajo un
token. Después el front procesa por tandas mandando token+offset, sin re-subir ni re-parsear el
archivo en cada lote. Es un TransientModel: la ORM auto-vacía las sesiones viejas, así que no hay
que limpiar a mano. El token es aleatorio y solo lo lee su creador (aislamiento por usuario).
"""
import json
import secrets

from odoo import api, fields, models
from odoo.exceptions import UserError


class L10nPeNeImportSession(models.TransientModel):
    _name = "l10n_pe_ne.import.session"
    _description = "Sesión temporal de importación por lotes"

    token = fields.Char(index=True, required=True)
    kind = fields.Char(required=True)            # 'productos' | 'clientes'
    rows_json = fields.Text(required=True)       # filas parseadas (incluye la cabecera) en JSON
    total = fields.Integer(default=0)            # filas de datos (sin la cabecera)

    @api.model
    def _crear(self, kind, rows):
        """Guarda las filas ya parseadas y devuelve el token. `rows` incluye la cabecera (rows[0])."""
        token = secrets.token_urlsafe(24)
        self.create({
            "token": token, "kind": kind,
            # default=str: por si openpyxl devuelve datetimes; los códigos/nombres van como texto.
            "rows_json": json.dumps([list(r) for r in rows], default=str),
            "total": max(0, len(rows) - 1),
        })
        return token

    @api.model
    def _rows(self, token):
        """Filas de la sesión (o error si el token no existe / no es del usuario / expiró)."""
        rec = self.search(
            [("token", "=", token or ""), ("create_uid", "=", self.env.uid)], limit=1)
        if not rec:
            raise UserError(
                "La sesión de importación expiró o no existe. Vuelve a subir el archivo.")
        return rec.kind, json.loads(rec.rows_json), rec.total
