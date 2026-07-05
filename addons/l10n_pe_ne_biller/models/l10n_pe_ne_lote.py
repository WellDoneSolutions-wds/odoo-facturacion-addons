# -*- coding: utf-8 -*-
"""Emisión masiva (NE Express) — carga de ventas desde Excel → N comprobantes.

Modelo propio API-only (patrón l10n_pe_ne.gasto): TODA la lógica (parseo openpyxl,
validación fiscal, emisión vía quick_emit, plantilla/resultados xlsxwriter) vive en el
addon; React solo llama. El estado del lote (reanudable) vive en el servidor. Aislado por
compañía (regla multi-compañía global en security). CERO lógica de emisión nueva: reusa
account.move.l10n_pe_ne_quick_emit / action_l10n_pe_send_to_biller."""
import base64
import hashlib
import io
import json
import os
import re
import unicodedata
from datetime import date, datetime

from odoo import _, api, fields, models, tools
from odoo.exceptions import UserError, ValidationError

# Defaults de los límites (ir.config_parameter con override en caliente, sin data XML).
_MASIVO_DEFAULTS = {
    "masivo_max_filas": 500,
    "masivo_max_comprobantes": 200,
    "masivo_max_chunk": 5,
    "masivo_max_bytes": 2097152,
}
# afectación humana (columna Excel) → taxCode catálogo-05 que consume quick_emit.
_AFECTACION_TAXCODE = {
    "GRAVADO": "1000", "EXONERADO": "9997", "INAFECTO": "9998",
    "EXPORTACION": "9995", "GRATUITO": "9996",
    "1000": "1000", "9997": "9997", "9998": "9998", "9995": "9995", "9996": "9996",
}
# tipo doc cliente humano → código catálogo-06 (l10n_pe_vat_code) que consume quick_partner.
_TIPODOC_CLIENTE = {
    "RUC": "6", "DNI": "1", "CE": "4", "PASAPORTE": "7", "SD": "0",
    "6": "6", "1": "1", "4": "4", "7": "7", "0": "0",
}
# tipo comprobante humano → código catálogo-01 (solo 01/03 en la masiva v1).
_TIPO_COMPROBANTE = {"FACTURA": "01", "BOLETA": "03", "01": "01", "03": "03"}
# l10n_pe_biller_state (que devuelve quick_result como 'estado') → estado de fila del lote.
_ESTADO_MAP = {"enviado": "emitido", "rechazado": "rechazado", "error": "error"}


class L10nPeNeLote(models.Model):
    _name = "l10n_pe_ne.lote"
    _description = "Lote de emisión masiva (NE Express)"
    _order = "id desc"

    name = fields.Char(string="Archivo", required=True)          # filename original
    sha256 = fields.Char(index=True)                             # detección de re-subida
    attachment_id = fields.Many2one("ir.attachment")            # xlsx original (soporte/debug)
    estado = fields.Selection([
        ("con_errores", "Con errores"),   # terminal: solo consulta del reporte
        ("validado", "Validado"),         # listo para procesar
        ("en_proceso", "En proceso"),
        ("terminado", "Terminado"),
        ("cancelado", "Cancelado"),
    ], default="validado", required=True)
    company_id = fields.Many2one("res.company", required=True, index=True,
                                 default=lambda s: s.env.company)
    fila_ids = fields.One2many("l10n_pe_ne.lote.fila", "lote_id")
    # Denormalizados del reporte (para listar sin re-parsear el reporte_json).
    total_filas = fields.Integer(default=0)          # filas de datos del xlsx
    total_comprobantes = fields.Integer(default=0)   # comprobantes agrupados
    reporte_json = fields.Text()                     # {errores:[], advertencias:[], duplicadoDe:int|None}

    # ------------------------------------------------------------- helpers de config
    def _masivo_param(self, key, default):
        """Lee l10n_pe_ne.<key> (int) con default; valor inválido o <=0 → default.
        Patrón de _ttl_hours() en controllers/main.py (aquí int en vez de float)."""
        raw = self.env["ir.config_parameter"].sudo().get_param("l10n_pe_ne.%s" % key, default)
        try:
            v = int(raw)
        except (TypeError, ValueError):
            v = int(default)
        return v if v > 0 else int(default)

    def _masivo_can_commit(self):
        """Commit por fila SOLO fuera de tests (test_enable) y del harness E2E (E2E_NO_COMMIT).
        Así un doc aceptado por SUNAT no se pierde por rollback de una fila posterior, pero los
        unit tests y el shell E2E mantienen el rollback transaccional."""
        return not tools.config["test_enable"] and not os.environ.get("E2E_NO_COMMIT")


class L10nPeNeLoteFila(models.Model):
    _name = "l10n_pe_ne.lote.fila"
    _description = "Comprobante de un lote masivo (NE Express)"
    _order = "lote_id, secuencia"

    lote_id = fields.Many2one("l10n_pe_ne.lote", required=True, index=True, ondelete="cascade")
    company_id = fields.Many2one(related="lote_id.company_id", store=True, index=True)
    secuencia = fields.Integer(required=True)        # nº de comprobante dentro del lote (1..N)
    filas_excel = fields.Char()                      # "5-7": filas de origen en el xlsx
    payload_json = fields.Text(required=True)        # payload EXACTO para l10n_pe_ne_quick_emit
    estado = fields.Selection([
        ("error_validacion", "Error de validación"),
        ("pendiente", "Pendiente"),
        ("emitido", "Emitido"),
        ("rechazado", "Rechazado"),   # SUNAT/biller rechazó (move existe)
        ("error", "Error"),           # error de conexión/infra (move puede existir)
        ("cancelado", "Cancelado"),
    ], default="pendiente", required=True)
    mensaje = fields.Text()
    move_id = fields.Many2one("account.move")        # ancla de idempotencia del reintento
    tipo_doc = fields.Char()
    serie = fields.Char()
    correlativo = fields.Char()
    cliente = fields.Char()
    total = fields.Float()
    moneda = fields.Char(default="PEN")
