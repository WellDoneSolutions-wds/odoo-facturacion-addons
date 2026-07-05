# -*- coding: utf-8 -*-
"""Caja (NE Express) — apertura/cierre/arqueo por medio de pago, estilo POS/bodega.

Dos modelos propios de Odoo: TODA la lógica (CRUD + serialización + amarre de ventas)
vive en el addon; React solo llama. Aislado por compañía (reglas multi-compañía en
security). La aritmética del arqueo se delega a tools/caja_arqueo.py (puro, testeado sin
Odoo). La caja NUNCA bloquea una venta (modo informativo, coherente con stock v1)."""
import calendar

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..tools.caja_arqueo import agrupar_ventas, calcular_arqueo


class L10nPeNeCajaSesion(models.Model):
    _name = "l10n_pe_ne.caja.sesion"
    _description = "Sesión de caja (NE Express)"
    _order = "fecha_apertura desc, id desc"

    estado = fields.Selection(
        [("abierta", "Abierta"), ("cerrada", "Cerrada")],
        default="abierta", required=True, index=True,
    )
    fecha_apertura = fields.Datetime(required=True, default=fields.Datetime.now)
    fecha_cierre = fields.Datetime()
    usuario_apertura_id = fields.Many2one("res.users", required=True, default=lambda s: s.env.user)
    usuario_cierre_id = fields.Many2one("res.users")
    saldo_inicial = fields.Monetary(currency_field="currency_id")  # >= 0, validado en abrir
    nota_apertura = fields.Char()
    nota_cierre = fields.Char()
    # snapshots congelados al cierre:
    conteos_cierre = fields.Json()   # [{'medio','esperado','contado','diferencia'}]
    ventas_cierre = fields.Json()    # {'count','total','sinMedio','countUsd','totalUsd'}
    movimiento_ids = fields.One2many("l10n_pe_ne.caja.movimiento", "sesion_id")
    currency_id = fields.Many2one("res.currency", required=True,
                                  default=lambda s: s.env.company.currency_id)
    company_id = fields.Many2one("res.company", required=True, index=True,
                                 default=lambda s: s.env.company)

    def init(self):
        # Índice único parcial: imposibilita la carrera de doble apertura simultánea
        # (una sola sesión 'abierta' por compañía). La guarda amigable vive en el método
        # l10n_pe_ne_abrir_caja; este índice es la defensa de última línea (race).
        self.env.cr.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS l10n_pe_ne_caja_sesion_unica_abierta
            ON l10n_pe_ne_caja_sesion (company_id) WHERE estado = 'abierta'
        """)


class L10nPeNeCajaMovimiento(models.Model):
    _name = "l10n_pe_ne.caja.movimiento"
    _description = "Movimiento de caja (NE Express)"
    _order = "fecha desc, id desc"

    sesion_id = fields.Many2one("l10n_pe_ne.caja.sesion", required=True, index=True,
                                ondelete="cascade")
    tipo = fields.Selection([("ingreso", "Ingreso"), ("retiro", "Retiro")], required=True)
    motivo = fields.Char(required=True)
    monto = fields.Monetary(currency_field="currency_id")  # > 0, validado en método
    fecha = fields.Datetime(default=fields.Datetime.now)
    usuario_id = fields.Many2one("res.users", default=lambda s: s.env.user)
    currency_id = fields.Many2one("res.currency", default=lambda s: s.env.company.currency_id)
    # company_id PROPIO (no related) para que la ir.rule aplique directa sobre el movimiento.
    company_id = fields.Many2one("res.company", required=True, index=True,
                                 default=lambda s: s.env.company)
