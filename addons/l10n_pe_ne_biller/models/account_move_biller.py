import base64
import io
import json
import logging
import re
import zipfile
from datetime import timedelta

import requests

try:  # SQS para el modo asíncrono (l10n_pe_ne_biller.async_enabled); si falta
    import boto3  # boto3, el modo síncrono sigue funcionando igual.
except ImportError:  # pragma: no cover
    boto3 = None

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..tools.amount_to_words import leyenda_monto

_logger = logging.getLogger(__name__)

# Catálogo SUNAT de descripciones de motivo para Nota de Débito (08).
ND_MOTIVO_DESC = {
    "01": "Intereses por mora",
    "02": "Aumento en el valor",
    "03": "Penalidades/otros conceptos",
    "11": "Ajustes de operaciones de exportación",
    "12": "Ajustes afectos al IVAP",
}

# Mapeo del código de tributo de la tax de Odoo (`account.tax.l10n_pe_edi_tax_code`, cat. 05 de
# SUNAT, provisto por la localización community l10n_pe) a la tupla que exige el contrato SFS por
# línea/tributo:  (tipAfeIGV cat.07, codTriIGV cat.05, nomTributo, codTipTributo UN/ECE 5153,
# codCatTributo UN/ECE 5305). Valores tomados de los mocks ya aceptados E2E por el microservicio.
TAX_CODE_MAP = {
    "1000": ("10", "1000", "IGV", "VAT", "S"),  # Gravado - operación onerosa
    "1016": ("17", "1016", "IVAP", "VAT", "S"),  # IVAP (arroz pilado)
    "9997": ("20", "9997", "EXO", "VAT", "E"),  # Exonerado
    "9998": ("30", "9998", "INA", "FRE", "O"),  # Inafecto
    "9995": ("40", "9995", "EXP", "FRE", "G"),  # Exportación
    "9996": (
        "11",
        "9996",
        "GRA",
        "FRE",
        "Z",
    ),  # Gratuita (retiro/transferencia gratuita)
}
DEFAULT_TAX_CODE = "1000"  # Sin tax reconocida -> gravado IGV (caso más común).

# Código de unidad de medida de SUNAT (cat. 03 / UN-ECE Rec. 20) por XMLID de la unidad estándar
# de Odoo. Mapeo replicado de l10n_pe_edi (enterprise). Se resuelve en runtime porque las UoM base
# son `noupdate` y un data file de otro módulo no las actualiza; para unidades personalizadas, el
# usuario fija el override en `uom.uom.l10n_pe_ne_unit_code`.
UOM_CODE_BY_XMLID = {
    "uom.product_uom_unit": "NIU",
    "uom.product_uom_dozen": "DPC",
    "uom.product_uom_kgm": "KGM",
    "uom.product_uom_gram": "GRM",
    "uom.product_uom_day": "DAY",
    "uom.product_uom_hour": "HUR",
    "uom.product_uom_ton": "TNE",
    "uom.product_uom_meter": "MTR",
    "uom.product_uom_km": "KTM",
    "uom.product_uom_cm": "CMT",
    "uom.product_uom_litre": "LTR",
    "uom.product_uom_lb": "LBR",
    "uom.product_uom_oz": "ONZ",
    "uom.product_uom_inch": "INH",
    "uom.product_uom_foot": "FOT",
    "uom.product_uom_mile": "M52",
    "uom.product_uom_floz": "OZ",
    "uom.product_uom_qt": "QTI",
    "uom.product_uom_gal": "GLL",
}
DEFAULT_UNIT_CODE = "NIU"


class AccountMoveLine(models.Model):
    _inherit = "account.move.line"

    # Overrides SUNAT por línea para el flujo rápido (sin depender de la UoM/producto de Odoo,
    # evitando el problema de categorías de unidad de medida).
    l10n_pe_ne_unit_code = fields.Char(
        string="Unidad SUNAT (cat.03)",
        copy=False,
        help="Código de unidad de medida SUNAT de la línea (ej. NIU, KGM, ZZ). "
        "Si está vacío se deriva de la unidad de medida del producto.",
    )
    l10n_pe_ne_cod_producto_sunat = fields.Char(
        string="Cód. producto SUNAT (cat.25)",
        copy=False,
        help="Código de producto SUNAT (UNSPSC, catálogo 25) de la línea, si aplica.",
    )


class AccountMove(models.Model):
    _inherit = "account.move"

    l10n_pe_biller_state = fields.Selection(
        selection=[
            ("por_enviar", "Por enviar"),
            ("en_proceso", "En proceso"),
            ("enviado", "Enviado"),
            ("anulado", "Anulado"),
            ("rechazado", "Rechazado"),
            ("error", "Error"),
        ],
        string="Estado Facturador",
        default="por_enviar",
        copy=False,
        tracking=True,  # cambios visibles en el chatter (que sí refresca en vivo)
    )
    l10n_pe_ne_baja_motivo = fields.Char(
        string="Motivo de baja",
        copy=False,
        help="Motivo de la comunicación de baja (RA) del comprobante ante SUNAT.",
    )
    l10n_pe_ne_baja_correlativo = fields.Char(
        string="Correlativo RA", copy=False, readonly=True
    )
    l10n_pe_ne_baja_fecha = fields.Date(string="Fecha RA", copy=False, readonly=True)
    l10n_pe_ne_baja_doc = fields.Char(
        string="Comunicación de baja",
        compute="_compute_l10n_pe_ne_baja_doc",
        store=True,
        help="Identificador de la comunicación de baja: RA-AAAAMMDD-correlativo.",
    )
    l10n_pe_ne_baja_cdr = fields.Many2one(
        "ir.attachment", string="CDR de baja", copy=False
    )
    # Identidad realmente emitida, congelada al enviar: la baja debe referenciar EXACTAMENTE lo emitido,
    # no recomputar del partner/nombre (que podrían cambiar y anular el comprobante equivocado).
    l10n_pe_ne_tipo_doc = fields.Char(
        string="Tipo doc. emitido", copy=False, readonly=True
    )
    l10n_pe_ne_serie_emit = fields.Char(
        string="Serie emitida", copy=False, readonly=True
    )
    l10n_pe_ne_corr_emit = fields.Char(
        string="Correlativo emitido", copy=False, readonly=True
    )
    l10n_pe_serie = fields.Char(
        string="Serie",
        compute="_compute_l10n_pe_serie",
        store=True,
        readonly=False,
        copy=False,
        help="Serie del comprobante. Por defecto, la del diario (l10n_pe_ne_serie).",
    )
    l10n_pe_correlativo = fields.Char(
        string="Correlativo",
        copy=False,
        help="Correlativo del comprobante. Si se deja vacío, se auto-incrementa del número del "
        "asiento (folio gestionado por Odoo por diario).",
    )

    l10n_pe_ne_detraccion = fields.Boolean(string="Sujeto a detracción", copy=False)
    l10n_pe_ne_detraccion_code = fields.Char(
        string="Código detracción",
        copy=False,
        help="Código del bien/servicio sujeto a detracción (catálogo 54 de SUNAT, ej. 037).",
    )
    l10n_pe_ne_detraccion_rate = fields.Float(
        string="% Detracción", digits=(5, 2), copy=False
    )
    l10n_pe_ne_detraccion_medio_pago = fields.Char(
        string="Medio de pago detracción",
        default="001",
        copy=False,
        help="Catálogo 59 (001 = depósito en cuenta del Banco de la Nación).",
    )
    l10n_pe_ne_percepcion = fields.Boolean(string="Aplica percepción", copy=False)
    l10n_pe_ne_percepcion_rate = fields.Float(
        string="% Percepción", digits=(5, 2), default=2.0, copy=False
    )
    l10n_pe_ne_anticipo_total = fields.Monetary(
        string="Anticipo aplicado (total con IGV)",
        copy=False,
        help="Importe del anticipo (IGV incluido) ya facturado que se regulariza/deduce en esta factura.",
    )
    l10n_pe_ne_anticipo_doc = fields.Char(
        string="Comprobante de anticipo",
        copy=False,
        help="Serie-correlativo de la factura de anticipo (ej. F001-00000100).",
    )
    l10n_pe_ne_anticipo_tipo = fields.Selection(
        [("02", "Factura de anticipo"), ("03", "Boleta de anticipo")],
        string="Tipo doc. anticipo",
        default="02",
        copy=False,
        help="Tipo del comprobante de anticipo que se regulariza (cat. 12).",
    )

    def _l10n_pe_importe_cobrar(self):
        """Importe neto a cobrar = total − anticipo aplicado − bienes gratuitos (lo que el cliente paga)."""
        self.ensure_one()
        ant = self._l10n_pe_anticipo()
        return round(
            self.amount_total
            - (ant[2] if ant else 0.0)
            - self._l10n_pe_gratuito_base(),
            2,
        )

    def _l10n_pe_percepcion_monto(self):
        self.ensure_one()
        # La percepción se calcula sobre el neto a cobrar (descontado el anticipo): así la base de la
        # percepción nunca supera el PayableAmount (evita rechazo SUNAT 2797 al combinar con anticipo).
        return round(
            self._l10n_pe_importe_cobrar()
            * (self.l10n_pe_ne_percepcion_rate or 0.0)
            / 100.0,
            2,
        )

    def _l10n_pe_anticipo_gravado(self):
        """Categoría y tasa de la operación gravada del anticipo: (cod_tri, tasa, motivo_no_soportado).
        El anticipo solo es representable como descuento global código 04 sobre una operación gravada
        homogénea (un único régimen IGV '1000' o IVAP '1016'). Devuelve un motivo si no lo es."""
        self.ensure_one()
        lines = self._l10n_pe_product_lines()
        codes = {self._l10n_pe_tax_info(l)[0][1] for l in lines}
        gravado = codes & {"1000", "1016"}
        if not gravado:
            return None, 0.0, "no_gravado"
        if len(gravado) > 1 or (codes - {"1000", "1016"}):
            return None, 0.0, "mixto"
        cod_tri = next(iter(gravado))
        tasa = next(
            (
                self._l10n_pe_tax_info(l)[1]
                for l in lines
                if self._l10n_pe_tax_info(l)[0][1] == cod_tri
            ),
            18.0,
        )
        return cod_tri, tasa, None

    def _l10n_pe_anticipo(self):
        """(valor, igv, total) del anticipo aplicado, o None. El total trae el impuesto incluido; el
        valor se separa con la tasa real de la operación gravada (1+tasa/100), no asumiendo 18%."""
        self.ensure_one()
        total = self.l10n_pe_ne_anticipo_total or 0.0
        # El anticipo solo se regulariza en factura/boleta de venta, nunca en notas (NC/ND).
        if total <= 0 or self.move_type != "out_invoice" or self.debit_origin_id:
            return None
        _cod, tasa, _motivo = self._l10n_pe_anticipo_gravado()
        valor = round(total / (1.0 + (tasa or 0.0) / 100.0), 2)
        return valor, round(total - valor, 2), round(total, 2)

    def _l10n_pe_check_anticipo(self):
        """Valida que el anticipo sea representable (descuento global código 04) antes de emitir el XML.
        Rechaza con un mensaje claro los casos no soportados, en vez de generar un comprobante inválido."""
        self.ensure_one()
        if not self._l10n_pe_anticipo():
            return
        if not (self.l10n_pe_ne_anticipo_doc or "").strip():
            raise UserError(
                _(
                    "Indique el comprobante de anticipo (serie-correlativo, ej. F001-00000100)."
                )
            )
        if round(self.l10n_pe_ne_anticipo_total, 2) > self.amount_total + 0.01:
            raise UserError(
                _(
                    "El anticipo aplicado (%.2f) no puede exceder el total de la factura (%.2f)."
                )
                % (self.l10n_pe_ne_anticipo_total, self.amount_total)
            )
        _cod, _tasa, motivo = self._l10n_pe_anticipo_gravado()
        if motivo:
            raise UserError(
                _(
                    "El anticipo solo se soporta sobre una operación gravada homogénea (IGV o IVAP). "
                    "No es aplicable a operaciones exoneradas/inafectas/exportación ni a facturas con "
                    "regímenes mixtos: regularice el anticipo en un comprobante separado."
                )
            )

    def _l10n_pe_relacionados(self):
        """Referencia al comprobante de anticipo (indDocRelacionado 2)."""
        ant = self._l10n_pe_anticipo()
        if not ant:
            return []
        return [
            {
                "indDocRelacionado": "2",
                "tipDocRelacionado": self.l10n_pe_ne_anticipo_tipo or "02",
                "numDocRelacionado": self.l10n_pe_ne_anticipo_doc or "",
                "numIdeAnticipo": "1",
                "mtoDocRelacionado": self._l10n_pe_fmt(ant[2]),
                "tipDocEmisor": "6",
                "numDocEmisor": self.company_id.vat or "",
            }
        ]

    def _l10n_pe_variables_globales(self):
        """Variables globales de la factura:
        - código 51: percepción (el agente percibe un % sobre la venta; el cliente paga total + percepción).
        - código 04: descuento global por anticipo (regulariza un anticipo ya facturado; reduce la
          base del IGV en el valor del anticipo). Exigido por SUNAT (regla 3287) cuando hay anticipo."""
        fmt = self._l10n_pe_fmt
        moneda = self.currency_id.name or "PEN"
        out = []
        if self.l10n_pe_ne_percepcion:
            out.append(
                {
                    "tipVariableGlobal": "true",
                    "codTipoVariableGlobal": "51",
                    "porVariableGlobal": "%.2f"
                    % (self.l10n_pe_ne_percepcion_rate / 100.0),
                    "monMontoVariableGlobal": moneda,
                    "mtoVariableGlobal": fmt(self._l10n_pe_percepcion_monto()),
                    "monBaseImponibleVariableGlobal": moneda,
                    # Base de la percepción = neto a cobrar (descontado el anticipo): sin anticipo es el total.
                    "mtoBaseImpVariableGlobal": fmt(self._l10n_pe_importe_cobrar()),
                }
            )
        ant = self._l10n_pe_anticipo()
        if ant:
            valor, _igv, _total = ant
            base = self.amount_untaxed or 0.0
            out.append(
                {
                    "tipVariableGlobal": "false",
                    "codTipoVariableGlobal": "04",
                    "porVariableGlobal": "%.2f" % (valor / base if base else 0.0),
                    "monMontoVariableGlobal": moneda,
                    "mtoVariableGlobal": fmt(valor),
                    "monBaseImponibleVariableGlobal": moneda,
                    "mtoBaseImpVariableGlobal": fmt(base),
                }
            )
        return out

    @api.depends("journal_id")
    def _compute_l10n_pe_serie(self):
        for move in self:
            if not move.l10n_pe_serie:
                move.l10n_pe_serie = move.journal_id.l10n_pe_ne_serie or "F001"

    def _l10n_pe_detraccion_monto(self):
        self.ensure_one()
        return round(
            self.amount_total * (self.l10n_pe_ne_detraccion_rate or 0.0) / 100.0, 2
        )

    def _l10n_pe_adicional_cabecera(self):
        """Bloque adicional de la cabecera: detracción y/o total a cobrar de la percepción."""
        fmt = self._l10n_pe_fmt
        block = {}
        if self.l10n_pe_ne_detraccion:
            block.update(
                {
                    "ctaBancoNacionDetraccion": self.company_id.l10n_pe_ne_cuenta_detraccion
                    or "",
                    "codBienDetraccion": self.l10n_pe_ne_detraccion_code or "",
                    "porDetraccion": fmt(self.l10n_pe_ne_detraccion_rate),
                    "mtoDetraccion": fmt(self._l10n_pe_detraccion_monto()),
                    "codMedioPago": self.l10n_pe_ne_detraccion_medio_pago or "001",
                }
            )
        if self.l10n_pe_ne_percepcion:
            # Total a cobrar = neto a cobrar (descontado el anticipo) + la percepción.
            block["mtoTotPercepcion"] = fmt(
                self._l10n_pe_importe_cobrar() + self._l10n_pe_percepcion_monto()
            )
        return block or None

    def _l10n_pe_dato_pago(self):
        moneda = self.currency_id.name or "PEN"
        if self.l10n_pe_ne_forma_pago == "Credito":
            return {
                "formaPago": "Credito",
                "mtoNetoPendientePago": self._l10n_pe_fmt(
                    self._l10n_pe_credito_pendiente()
                ),
                "tipMonedaMtoNetoPendientePago": moneda,
            }
        dato = {"formaPago": "Contado"}
        if self.l10n_pe_ne_detraccion:
            # Operación al contado con detracción: se declara el neto pendiente de pago.
            dato["mtoNetoPendientePago"] = self._l10n_pe_fmt(self.amount_total)
            dato["tipMonedaMtoNetoPendientePago"] = moneda
        return dato

    def _l10n_pe_credito_pendiente(self):
        """Monto neto pendiente del crédito = suma de cuotas; si no hay, el total."""
        s = sum(float(c.get("monto") or 0) for c in (self.l10n_pe_ne_cuotas or []))
        return s if s > 0 else (self.amount_total or 0.0)

    def _l10n_pe_detalle_pago(self):
        """detallePago (cuotas) para crédito: usa las cuotas guardadas o una = total."""
        moneda = self.currency_id.name or "PEN"
        out = [
            {
                "mtoCuotaPago": self._l10n_pe_fmt(float(c.get("monto") or 0)),
                "fecCuotaPago": c.get("fecha") or "",
                "tipMonedaCuotaPago": moneda,
            }
            for c in (self.l10n_pe_ne_cuotas or [])
            if c.get("fecha") and float(c.get("monto") or 0) > 0
        ]
        if not out:
            fecha = self.invoice_date_due or self.invoice_date
            out = [
                {
                    "mtoCuotaPago": self._l10n_pe_fmt(self.amount_total),
                    "fecCuotaPago": fecha.strftime("%Y-%m-%d") if fecha else "",
                    "tipMonedaCuotaPago": moneda,
                }
            ]
        return out

    l10n_pe_ne_forma_pago = fields.Selection(
        [("Contado", "Contado"), ("Credito", "Crédito")],
        default="Contado",
        copy=False,
        string="Forma de pago",
        help="Forma de pago SUNAT (cac:PaymentTerms). 'Crédito' emite cuotas.",
    )
    l10n_pe_ne_cuotas = fields.Json(
        string="Cuotas de crédito", copy=False
    )  # [{'fecha','monto'}]
    l10n_pe_ne_medios_pago = fields.Json(
        string="Medios de pago (POS)", copy=False
    )  # [{'medio','monto'}]

    l10n_pe_motivo_code = fields.Char(
        string="Cód. motivo NC/ND",
        default="01",
        copy=False,
        help="Código SUNAT del motivo de la nota de crédito (cat. 09) o débito (cat. 10).",
    )
    l10n_pe_biller_xml = fields.Many2one(
        "ir.attachment", string="XML UBL firmado", copy=False
    )
    l10n_pe_biller_cdr = fields.Many2one(
        "ir.attachment", string="CDR SUNAT", copy=False
    )
    l10n_pe_biller_pdf = fields.Many2one(
        "ir.attachment", string="PDF (representación impresa)", copy=False
    )
    l10n_pe_biller_pdf_ticket = fields.Many2one(
        "ir.attachment", string="PDF ticket 80mm (representación impresa)", copy=False
    )
    l10n_pe_biller_message = fields.Text(string="Mensaje Facturador", copy=False)

    # ----------------------------------------------------------------- helpers
    def _l10n_pe_fmt(self, amount):
        return "%.2f" % (amount or 0.0)

    def _l10n_pe_document_type(self):
        """Código SUNAT del comprobante: 01 Factura, 03 Boleta, 07 NC, 08 ND."""
        self.ensure_one()
        if self.move_type == "out_refund":
            return "07"
        if self.move_type == "out_invoice" and self.debit_origin_id:
            return "08"
        # Una exportación es siempre Factura (01), aunque el adquirente extranjero no tenga RUC
        # (si fuese Boleta 03 con serie F, el validador de factura la rechaza por tipo/serie).
        if self.move_type == "out_invoice" and self._l10n_pe_tipo_operacion() == "0200":
            return "01"
        vat_code = (
            self.partner_id.l10n_latam_identification_type_id.l10n_pe_vat_code or ""
        )
        return "01" if vat_code == "6" else "03"

    def _l10n_pe_product_lines(self):
        return self.invoice_line_ids.filtered(
            lambda l: not l.display_type or l.display_type == "product"
        )

    def _l10n_pe_tax_info(self, line):
        """Afectación IGV de la línea según la tax de Odoo. Devuelve
        ((tipAfeIGV, codTriIGV, nomTributo, codTipTributo, codCatTributo), porcentaje_igv).
        Lee `account.tax.l10n_pe_edi_tax_code` (cat. 05) de la localización l10n_pe; si la línea no
        trae una tax reconocida, asume gravado (IGV)."""
        for tax in line.tax_ids:
            if tax.l10n_pe_edi_tax_code in TAX_CODE_MAP:
                return TAX_CODE_MAP[tax.l10n_pe_edi_tax_code], tax.amount
        return TAX_CODE_MAP[DEFAULT_TAX_CODE], 0.0

    def _l10n_pe_icbper_tax(self, line):
        """La tax ICBPER (impuesto a las bolsas, cat. 05 = 7152) de la línea, si la trae. Es una
        tax de monto fijo (amount_type='fixed') = soles por bolsa."""
        return line.tax_ids.filtered(lambda t: t.l10n_pe_edi_tax_code == "7152")[:1]

    def _l10n_pe_isc_tax(self, line):
        """La tax ISC (Impuesto Selectivo al Consumo, cat. 05 = 2000) de la línea, si la trae.
        Debe estar marcada 'Afecta la base de los impuestos posteriores' para que el IGV se compute
        sobre valor+ISC."""
        return line.tax_ids.filtered(lambda t: t.l10n_pe_edi_tax_code == "2000")[:1]

    def _l10n_pe_line_amounts(self, line):
        """Descompone los tributos de la línea: (base, igv, isc, icbper).

        price_total - price_subtotal incluye los tres. El ICBPER = nº bolsas × monto fijo. El ISC
        'al valor' (sis. 01) = base × tasa; 'monto fijo' (02) = cantidad × monto. El IGV es el resto
        (Odoo ya lo computa sobre valor+ISC si la tax ISC afecta la base)."""
        base = line.price_subtotal
        total_tax = line.price_total - line.price_subtotal
        icbper_tax = self._l10n_pe_icbper_tax(line)
        icbper = (
            round(int(round(line.quantity or 0)) * icbper_tax.amount, 2)
            if icbper_tax
            else 0.0
        )
        isc_tax = self._l10n_pe_isc_tax(line)
        if isc_tax:
            if isc_tax.amount_type == "fixed":
                isc = round((line.quantity or 0.0) * isc_tax.amount, 2)
            else:
                isc = round(base * isc_tax.amount / 100.0, 2)
        else:
            isc = 0.0
        return base, total_tax - isc - icbper, isc, icbper

    def _l10n_pe_total_icbper(self):
        return sum(
            self._l10n_pe_line_amounts(l)[3] for l in self._l10n_pe_product_lines()
        )

    def _l10n_pe_unit_code(self, line):
        """Código de unidad SUNAT (cat. 03) de la línea: override por línea, luego override manual en
        la UoM, si no el mapeo por XMLID de la unidad estándar de Odoo, si no 'NIU'."""
        if line.l10n_pe_ne_unit_code:
            return line.l10n_pe_ne_unit_code
        uom = line.product_uom_id
        if not uom:
            return DEFAULT_UNIT_CODE
        if uom.l10n_pe_ne_unit_code:
            return uom.l10n_pe_ne_unit_code
        xmlid = uom.get_external_id().get(uom.id, "")
        return UOM_CODE_BY_XMLID.get(xmlid, DEFAULT_UNIT_CODE)

    def _l10n_pe_detalle(self):
        fmt = self._l10n_pe_fmt
        detalle = []
        for line in self._l10n_pe_product_lines():
            (tip_afe, cod_tri, nom_trib, cod_tip_trib, _cod_cat), por_igv = (
                self._l10n_pe_tax_info(line)
            )
            qty = line.quantity or 1.0
            base, igv, isc, icbper = self._l10n_pe_line_amounts(line)
            # Valor unitario BRUTO (antes del descuento): regla SUNAT 3271 exige
            # mtoValorVentaItem = mtoValorUnitario*cantidad - descuento. El descuento sale aparte
            # en adicionalDetalle; mtoValorVentaItem (LineExtensionAmount) queda neto.
            disc = (
                round(line.price_unit * line.quantity - base, 2)
                if line.discount
                else 0.0
            )
            gross = base + disc
            item = {
                "tipAfeIGV": tip_afe,
                "codProducto": line.product_id.default_code or "-",
                "codProductoSUNAT": line.l10n_pe_ne_cod_producto_sunat or "-",
                "codUnidadMedida": self._l10n_pe_unit_code(line),
                "ctdUnidadItem": fmt(qty),
                "desItem": line.name or line.product_id.display_name or "",
                "mtoValorUnitario": fmt(gross / qty if qty else 0.0),
                "mtoValorVentaItem": fmt(base),
                # Precio de venta unitario = (valor venta + ISC + IGV) / cantidad; NO incluye el ICBPER.
                "mtoPrecioVentaUnitario": fmt((base + isc + igv) / qty if qty else 0.0),
                "mtoValorReferencialUnitario": "0.00",
                "porIgvItem": fmt(por_igv),
                # La base del IGV incluye el ISC (el IGV se computa sobre valor venta + ISC).
                "mtoBaseIgvItem": fmt(base + isc),
                "mtoIgvItem": fmt(igv),
                "sumTotTributosItem": fmt(igv + isc + icbper),
                "codTriIGV": cod_tri,
                "nomTributoIgvItem": nom_trib,
                "codTipTributoIgvItem": cod_tip_trib,
            }
            # Operación gratuita (cat. 05 = 9996). Estructura SUNAT (ref: enterprise invoice_free.xml):
            # Price/PriceAmount=0; valor de mercado en mtoValorReferencialUnitario (PricingReference 02);
            # LineExtensionAmount(mtoValorVentaItem)=valor de mercado; TaxSubtotal 9996 con base y el IGV
            # teórico 18% (mtoBaseIgvItem/mtoIgvItem); pero el TaxTotal/TaxAmount de la LÍNEA
            # (sumTotTributosItem) = 0 — el IGV gratuito NO se cobra (clave del fault 3272).
            if cod_tri == "9996":
                igv_grat = round(base * 0.18, 2)
                item.update(
                    {
                        "mtoValorUnitario": "0.00",
                        "mtoValorVentaItem": fmt(base),
                        "mtoPrecioVentaUnitario": "0.00",
                        "mtoValorReferencialUnitario": fmt(gross / qty if qty else 0.0),
                        "porIgvItem": "18.00",
                        "mtoBaseIgvItem": fmt(base),
                        "mtoIgvItem": fmt(igv_grat),
                        "sumTotTributosItem": "0.00",
                    }
                )
            isc_tax = self._l10n_pe_isc_tax(line)
            if isc_tax:
                por_isc = (
                    isc_tax.amount
                    if isc_tax.amount_type != "fixed"
                    else (isc / base * 100.0 if base else 0.0)
                )
                item.update(
                    {
                        "codTriISC": "2000",
                        "nomTributoIscItem": "ISC",
                        "codTipTributoIscItem": "EXC",
                        "tipSisISC": isc_tax.l10n_pe_edi_isc_type or "01",
                        "mtoBaseIscItem": fmt(base),
                        "mtoIscItem": fmt(isc),
                        "porIscItem": fmt(por_isc),
                    }
                )
            icbper_tax = self._l10n_pe_icbper_tax(line)
            if icbper_tax:
                item.update(
                    {
                        "codTriIcbper": "7152",
                        "nomTributoIcbperItem": "ICBPER",
                        "codTipTributoIcbperItem": "OTH",
                        "ctdBolsasTriIcbperItem": str(int(round(qty))),
                        "mtoTriIcbperUnidad": fmt(icbper_tax.amount),
                        "mtoTriIcbperItem": fmt(icbper),
                    }
                )
            detalle.append(item)
        return detalle

    def _l10n_pe_tributos(self):
        """Un tributo por categoría presente (IGV/EXO/INA/EXP/GRA/IVAP), con la base y el monto
        sumados de las líneas de esa categoría."""
        fmt = self._l10n_pe_fmt
        grupos = {}  # codTriIGV -> [base, monto, (nomTributo, codTipTributo, codCatTributo)]
        isc_base = isc_total = 0.0
        for line in self._l10n_pe_product_lines():
            (_tip, cod_tri, nom_trib, cod_tip_trib, cod_cat), _por = (
                self._l10n_pe_tax_info(line)
            )
            base, igv, isc, _icbper = self._l10n_pe_line_amounts(line)
            # Base del IGV de cabecera = valor venta (no incluye el ISC, a diferencia de la línea).
            g = grupos.setdefault(
                cod_tri, [0.0, 0.0, (nom_trib, cod_tip_trib, cod_cat)]
            )
            g[0] += base
            # Gratuito (9996): el IGV teórico (18% del valor de mercado) va en el tributo de cabecera
            # aunque no se cobre. En las demás categorías es el IGV real (el grupo no incluye ICBPER).
            g[1] += round(base * 0.18, 2) if cod_tri == "9996" else igv
            if isc:
                isc_base += base
                isc_total += isc
        # Anticipo: el descuento global código 04 reduce la base y el impuesto de cabecera del grupo
        # gravado (no las líneas, que declaran la operación completa). El validador computa el impuesto
        # sobre la base ya reducida. Se reduce el régimen real (IGV '1000' o IVAP '1016').
        ant = self._l10n_pe_anticipo()
        if ant:
            cod_tri, _tasa, _motivo = self._l10n_pe_anticipo_gravado()
            if cod_tri and cod_tri in grupos:
                valor, igv, _total = ant
                grupos[cod_tri][0] -= valor
                grupos[cod_tri][1] -= igv
        tributos = [
            {
                "ideTributo": cod_tri,
                "nomTributo": meta[0],
                "codTipTributo": meta[1],
                "codCatTributo": meta[2],
                "mtoBaseImponible": fmt(b),
                "mtoTributo": fmt(m),
            }
            for cod_tri, (b, m, meta) in grupos.items()
        ]
        if isc_total:
            tributos.append(
                {
                    "ideTributo": "2000",
                    "nomTributo": "ISC",
                    "codTipTributo": "EXC",
                    "codCatTributo": "S",
                    "mtoBaseImponible": fmt(isc_base),
                    "mtoTributo": fmt(isc_total),
                }
            )
        # ICBPER (7152): TaxSubtotal de cabecera SIN TaxableAmount (el FTL lo omite para 7152), solo el
        # monto. Necesario para que TaxInclusive = LineExt + TaxTotal (regla SUNAT 3279).
        icbper_total = self._l10n_pe_total_icbper()
        if icbper_total:
            tributos.append(
                {
                    "ideTributo": "7152",
                    "nomTributo": "ICBPER",
                    "codTipTributo": "OTH",
                    "codCatTributo": "S",
                    "mtoBaseImponible": "0.00",
                    "mtoTributo": fmt(icbper_total),
                }
            )
        return tributos

    def _l10n_pe_leyendas(self):
        # El monto en letras corresponde al importe a cobrar (total − anticipo aplicado).
        leyendas = [
            {
                "codLeyenda": "1000",
                "desLeyenda": leyenda_monto(self._l10n_pe_importe_cobrar()),
            }
        ]
        if self.l10n_pe_ne_detraccion:
            leyendas.append(
                {"codLeyenda": "2006", "desLeyenda": "Operacion sujeta a detraccion"}
            )
        if self._l10n_pe_gratuito_base() > 0:
            leyendas.append(
                {"codLeyenda": "1002", "desLeyenda": "TRANSFERENCIA GRATUITA"}
            )
        return leyendas

    def _l10n_pe_gratuito_base(self):
        """Suma de las bases (valor de mercado) de las líneas gratuitas (cat. 05 = 9996)."""
        self.ensure_one()
        total = 0.0
        for line in self._l10n_pe_product_lines():
            if self._l10n_pe_tax_info(line)[0][1] == "9996":
                total += self._l10n_pe_line_amounts(line)[0]
        return round(total, 2)

    def _l10n_pe_tipo_operacion(self):
        """1001 detracción, 2001 percepción, 0200 exportación; si no, 0101 (venta interna)."""
        if self.l10n_pe_ne_detraccion:
            return "1001"
        if self.l10n_pe_ne_percepcion:
            return "2001"
        lineas = self._l10n_pe_product_lines()
        afectaciones = {self._l10n_pe_tax_info(l)[0][0] for l in lineas}
        return "0200" if afectaciones == {"40"} else "0101"

    def _l10n_pe_cliente_doc(self):
        """(tipDocUsuario, numDocUsuario) del cliente. Consumidor final sin documento → ('0','00000000');
        si trae número pero no tipo, se infiere (11 dígitos→RUC '6', si no DNI '1')."""
        self.ensure_one()
        p = self.partner_id
        vat = (p.vat or "").strip()
        cod = p.l10n_latam_identification_type_id.l10n_pe_vat_code or ""
        if not vat:
            return "0", "00000000"
        if not cod:
            cod = "6" if (len(vat) == 11 and vat.isdigit()) else "1"
        return cod, vat

    def _l10n_pe_cabecera(self):
        fmt = self._l10n_pe_fmt
        partner = self.partner_id
        # El ICBPER (cat. 05 = 7152) SÍ entra en el total de tributos (sumTotTributos), en el precio de
        # venta (TaxInclusiveAmount) y en el importe a cobrar — regla SUNAT 3279/3280 (ref. enterprise:
        # ICBPER es tributo 'OTH', no allowance-charge). Ademas se emite como su propio TaxSubtotal de
        # cabecera (ver _l10n_pe_tributos). amount_tax/amount_total de Odoo ya lo incluyen.
        # Anticipo aplicado: el IGV de cabecera se reduce por el IGV del anticipo; el importe a cobrar
        # (PayableAmount) = precio de venta completo − total del anticipo (que va como PrepaidAmount).
        ant = self._l10n_pe_anticipo()
        anticipo_total = ant[2] if ant else 0.0
        anticipo_igv = ant[1] if ant else 0.0
        # Operación gratuita: el valor de los bienes regalados NO se cobra → se excluye de valor venta,
        # precio, importe Y del total de tributos de cabecera. El IGV teórico (18%) solo vive en la
        # TaxSubtotal 9996 (línea y cabecera); el cbc:TaxAmount de cabecera (sumTotTributos) NO lo
        # incluye: la regla 4301 suma únicamente los tributos 1000/1016/7152/9999/2000 (no el 9996),
        # y la referencia SUNAT aceptada consigna sumTotTributos = IGV real, sin el 18% gratuito.
        grat_base = self._l10n_pe_gratuito_base()
        cabecera = {
            "tipOperacion": self._l10n_pe_tipo_operacion(),
            "fecEmision": self.invoice_date.strftime("%Y-%m-%d")
            if self.invoice_date
            else "",
            "horEmision": fields.Datetime.now().strftime("%H:%M:%S"),
            "fecVencimiento": self.invoice_date_due.strftime("%Y-%m-%d")
            if self.invoice_date_due
            else "",
            "codLocalEmisor": "0000",
            "tipDocUsuario": self._l10n_pe_cliente_doc()[0],
            "numDocUsuario": self._l10n_pe_cliente_doc()[1],
            "rznSocialUsuario": partner.name or "",
            "tipMoneda": self.currency_id.name or "PEN",
            # El IGV teórico del gratuito NO se cobra: NO entra en el total de tributos de cabecera
            # (regla 4301: el TaxAmount de cabecera excluye el 9996). El 9996 va solo como TaxSubtotal.
            "sumTotTributos": fmt(self.amount_tax - anticipo_igv),
            "sumTotValVenta": fmt(self.amount_untaxed - grat_base),
            # TaxInclusiveAmount: INCLUYE el ICBPER (igual que la ref. enterprise: PayableAmount =
            # TaxInclusive − anticipo, ambos con el ICBPER). Excluirlo de aquí pero incluirlo en
            # sumImpVenta desbalancea el comprobante → SUNAT Client.3280.
            "sumPrecioVenta": fmt(self.amount_total - grat_base),
            "sumImpVenta": fmt(self.amount_total - anticipo_total - grat_base),
            "sumDescTotal": "0.00",
            "sumOtrosCargos": "0.00",
            "sumTotalAnticipos": fmt(anticipo_total),
            "ublVersionId": "2.1",
            "customizationId": "2.0",
        }
        if grat_base:
            cabecera["sumValVentaGratuito"] = fmt(grat_base)
        adicional = self._l10n_pe_adicional_cabecera()
        if adicional:
            cabecera["adicionalCabecera"] = adicional
        return cabecera

    def _l10n_pe_serie_correlativo(self):
        """Serie y correlativo del comprobante. La serie viene del campo del move (por defecto la del
        diario). El correlativo: el manual si se fijó; si no, el folio (parte numérica final) del
        número del asiento, que Odoo auto-incrementa por diario; si no hay, '1'."""
        self.ensure_one()
        name = (self.name or "").replace(" ", "")
        matches = list(re.finditer(r"\d+", name))
        folio = matches[-1].group() if matches else None
        serie = self.l10n_pe_serie or self.journal_id.l10n_pe_ne_serie or "F001"
        correlativo = self.l10n_pe_correlativo or folio or "1"
        return serie, correlativo

    def _l10n_pe_id_block(self, with_document_type=True):
        serie, correlativo = self._l10n_pe_serie_correlativo()
        block = {
            "ruc": self.company_id.vat or "",
            "serie": serie,
            "correlativo": correlativo.zfill(8),
        }
        if with_document_type:
            block["documentType"] = self._l10n_pe_document_type()
        return block

    # ----------------------------------------------------------- constructores
    def _l10n_pe_emisor(self):
        """Datos de empresa del emisor (desde res.company) para el request. Las credenciales y el
        certificado de firma quedan en el servidor indexados por RUC; aquí solo van datos NO secretos.
        El microservicio prefiere estos sobre su registro por RUC, campo a campo."""
        self.ensure_one()
        company = self.company_id
        partner = company.partner_id
        emisor = {
            "razonSocial": company.name or "",
            "nombreComercial": company.name or "",
        }
        # Dirección todo-o-nada: solo se envía si el distrito (ubigeo) está configurado, para no mezclar
        # datos reales con los del registro del micro campo a campo (coalesce).
        distrito = partner.l10n_pe_district
        if distrito:
            emisor["direccion"] = {
                "ubigeo": distrito.code or "",
                "direccion": partner.street or "",
                "departamento": partner.state_id.name or "",
                "provincia": (distrito.city_id.name or partner.city or ""),
                "distrito": distrito.name or "",
                "urbanizacion": partner.street2 or "",
            }
        return emisor

    def _l10n_pe_build_invoice_request(self):
        """Factura (01) / Boleta (03) — endpoint /generator/factura."""
        _logger.info(
            "Invoice request: %s %s %s",
            self._l10n_pe_id_block(with_document_type=True),
            self._l10n_pe_emisor(),
            self._l10n_pe_cabecera(),
        )
        self.ensure_one()
        self._l10n_pe_check_anticipo()
        # Boleta > S/700 exige el documento de identidad del cliente (SUNAT la rechaza sin él en prod).
        _logger.info("Product lines: %s", len(self._l10n_pe_product_lines()))
        if (
            self._l10n_pe_document_type() == "03"
            and (self.amount_total or 0.0) > 700
            and not (self.partner_id.vat or "").strip()
        ):
            raise UserError(
                _(
                    "Una boleta mayor a S/ 700 requiere el documento de identidad del cliente."
                )
            )
        req = {
            "id": self._l10n_pe_id_block(with_document_type=True),
            "emisor": self._l10n_pe_emisor(),
            "cabecera": self._l10n_pe_cabecera(),
            "datoPago": self._l10n_pe_dato_pago(),
            "tributos": self._l10n_pe_tributos(),
            "detalle": self._l10n_pe_detalle(),
            "adicionalDetalle": self._l10n_pe_adicional_detalle(),
            "variablesGlobales": self._l10n_pe_variables_globales(),
            "leyendas": self._l10n_pe_leyendas(),
        }
        if self.l10n_pe_ne_forma_pago == "Credito":
            req["detallePago"] = self._l10n_pe_detalle_pago()
        relacionados = self._l10n_pe_relacionados()
        if relacionados:
            req["relacionados"] = relacionados
        return req

    def _l10n_pe_adicional_detalle(self):
        """Descuentos por ítem (cat. 53 código 00, que afecta la base del IGV) — hace explícito en
        el comprobante el descuento de cada línea con `discount` > 0. La línea ya va por su valor
        neto (IGV sobre el neto); este bloque solo lo muestra, no cambia los totales."""
        fmt = self._l10n_pe_fmt
        moneda = self.currency_id.name or "PEN"
        out = []
        idx = 0
        for line in self._l10n_pe_product_lines():
            idx += 1
            if not line.discount:
                continue
            gross = round(line.price_unit * line.quantity, 2)
            disc = round(gross - line.price_subtotal, 2)
            out.append(
                {
                    "idLinea": str(idx),
                    # "-" en las propiedades para que la plantilla salte el bloque AdditionalItemProperty
                    # (la misma lista sirve para descuentos y propiedades; sin esto el render falla).
                    "nomPropiedad": "-",
                    "codBienPropiedad": "-",
                    "tipVariable": "false",
                    "codTipoVariable": "00",
                    "porVariable": "%.2f" % (line.discount / 100.0),
                    "monMontoVariable": moneda,
                    "mtoVariable": fmt(disc),
                    "monBaseImponibleVariable": moneda,
                    "mtoBaseImpVariable": fmt(gross),
                }
            )
        return out

    def _l10n_pe_build_note_request(self):
        """Nota de Crédito (07) / Débito (08) — referencia al documento afectado."""
        self.ensure_one()
        dt = self._l10n_pe_document_type()
        origin = self.reversed_entry_id if dt == "07" else self.debit_origin_id
        cabecera = self._l10n_pe_cabecera()
        if origin:
            o_serie, o_corr = origin._l10n_pe_serie_correlativo()
            cabecera["numDocAfectado"] = "%s-%s" % (o_serie, o_corr.zfill(8))
            cabecera["tipDocAfectado"] = origin._l10n_pe_document_type()
        else:
            cabecera["numDocAfectado"] = ""
            cabecera["tipDocAfectado"] = "01"
        cabecera["codMotivo"] = self.l10n_pe_motivo_code or (
            "01" if dt == "07" else "02"
        )
        if dt == "08":
            cabecera["desMotivo"] = ND_MOTIVO_DESC.get(
                cabecera["codMotivo"], "Aumento en el valor"
            )
        req = {
            "id": self._l10n_pe_id_block(with_document_type=False),
            "emisor": self._l10n_pe_emisor(),
            "cabecera": cabecera,
            "tributos": self._l10n_pe_tributos(),
            "detalle": self._l10n_pe_detalle(),
            "leyendas": self._l10n_pe_leyendas(),
        }
        # Nota de Crédito (07): el CreditNoteMapper del biller exige forma de pago y
        # fuerza el <cbc:Amount> de PaymentTerms. "Contado" rebota (errorCode 2071/3246)
        # y omitirlo rebota (3245). El único patrón que valida en el SFS es "Credito"
        # con una cuota = total (campos válidos del contrato SFS, no se toca el biller).
        # La ND (08) valida sin datoPago, así que no se le agrega.
        if dt == "07":
            total = self._l10n_pe_fmt(self.amount_total)
            fecha = self.invoice_date.strftime("%Y-%m-%d") if self.invoice_date else ""
            moneda = self.currency_id.name or "PEN"
            req["datoPago"] = {
                "formaPago": "Credito",
                "mtoNetoPendientePago": total,
                "tipMonedaMtoNetoPendientePago": moneda,
            }
            req["detallePago"] = [
                {
                    "mtoCuotaPago": total,
                    "fecCuotaPago": fecha,
                    "tipMonedaCuotaPago": moneda,
                }
            ]
        return req

    def _l10n_pe_target(self):
        """(endpoint, payload) según el tipo de comprobante."""
        dt = self._l10n_pe_document_type()
        if dt == "07":
            return ("notaCredito", self._l10n_pe_build_note_request())
        if dt == "08":
            return ("notaDebito", self._l10n_pe_build_note_request())
        return ("factura", self._l10n_pe_build_invoice_request())

    def _l10n_pe_store_cdr(self, cdr_b64):
        """Guarda el CDR de SUNAT (zip en base64, del header X-Sunat-Cdr) como adjunto en
        l10n_pe_biller_cdr y devuelve (responseCode, description) del ApplicationResponse."""
        self.ensure_one()
        try:
            cdr_bytes = base64.b64decode(cdr_b64)
        except Exception:
            return "", ""
        serie, correlativo = self._l10n_pe_serie_correlativo()
        name = "R%s-%s-%s.zip" % (
            self.company_id.vat or "",
            serie,
            correlativo.zfill(8),
        )
        att = self.env["ir.attachment"].create(
            {
                "name": name,
                "res_model": "account.move",
                "res_id": self.id,
                "mimetype": "application/zip",
                "raw": cdr_bytes,
            }
        )
        self.l10n_pe_biller_cdr = att.id
        return self._l10n_pe_parse_cdr_codes(cdr_bytes)

    def _l10n_pe_parse_cdr_codes(self, cdr_bytes):
        """(responseCode, description) del ApplicationResponse dentro del zip CDR."""
        code = desc = ""
        try:
            with zipfile.ZipFile(io.BytesIO(cdr_bytes)) as zf:
                xml_name = next(
                    (n for n in zf.namelist() if n.lower().endswith(".xml")), None
                )
                content = zf.read(xml_name) if xml_name else b""
            m = re.search(rb"<cbc:ResponseCode>([^<]*)</cbc:ResponseCode>", content)
            code = m.group(1).decode() if m else ""
            m = re.search(rb"<cbc:Description>([^<]*)</cbc:Description>", content)
            desc = m.group(1).decode("utf-8", "replace") if m else ""
        except Exception:
            pass
        return code, desc

    def _l10n_pe_apply_emission_response(self, ok, body_text, cdr_b64):
        """Aplica al move el resultado de una emisión — mismo tratamiento para el
        flujo síncrono (respuesta HTTP directa) y el asíncrono (cron que recoge
        XML/CDR desde S3 vía el worker): adjunta el XML firmado, guarda el CDR,
        congela la identidad emitida y fija estado + mensaje."""
        self.ensure_one()
        signed = ok and any(
            tag in (body_text or "")
            for tag in ("<Invoice", "<CreditNote", "<DebitNote")
        )
        if not signed:
            self.l10n_pe_biller_state = "rechazado"
            self.l10n_pe_biller_message = (body_text or "")[:2000]
            return
        serie, correlativo = self._l10n_pe_serie_correlativo()
        att = self.env["ir.attachment"].create(
            {
                "name": "%s-%s-%s.xml"
                % (self.company_id.vat, serie, correlativo.zfill(8)),
                "res_model": "account.move",
                "res_id": self.id,
                "mimetype": "application/xml",
                "raw": body_text.encode("utf-8"),
            }
        )
        self.l10n_pe_biller_xml = att.id
        self.l10n_pe_biller_state = "enviado"
        # Congela la identidad emitida para una eventual baja (no recomputar luego del partner/nombre).
        self.l10n_pe_ne_tipo_doc = self._l10n_pe_document_type()
        self.l10n_pe_ne_serie_emit = serie
        self.l10n_pe_ne_corr_emit = correlativo.zfill(8)
        code, desc = self._l10n_pe_store_cdr(cdr_b64) if cdr_b64 else ("", "")
        if code == "0":
            self.l10n_pe_biller_message = _(
                "Aceptado por SUNAT — CDR ResponseCode 0. %s"
            ) % (desc or "")
        elif code:
            self.l10n_pe_biller_message = _(
                "CDR de SUNAT (ResponseCode %s). %s"
            ) % (code, desc or "")
        else:
            self.l10n_pe_biller_message = _("Aceptado por el facturador (HTTP 200).")

    # -------------------------------------------------------- emisión asíncrona
    # Toggle: ir.config_parameter `l10n_pe_ne_biller.async_enabled` = "1".
    # Odoo encola en SQS (rol IAM del EC2, patrón del sibling partner_lookup) y
    # responde al instante; el Lambda facturas-worker procesa contra biller-core
    # con idempotencia (DynamoDB) y deja XML/CDR en S3; el cron de abajo recoge.

    def _l10n_pe_enqueue_emission(self, icp):
        self.ensure_one()
        queue_url = icp.get_param("l10n_pe_ne_biller.sqs_queue_url", "")
        region = icp.get_param("l10n_pe_ne_biller.aws_region", "us-east-1")
        if not boto3 or not queue_url:
            self.l10n_pe_biller_state = "error"
            self.l10n_pe_biller_message = _(
                "Modo asíncrono activo pero falta boto3 o el parámetro "
                "l10n_pe_ne_biller.sqs_queue_url."
            )
            return
        endpoint, payload = self._l10n_pe_target()
        serie, correlativo = self._l10n_pe_serie_correlativo()
        msg = {
            "ruc": self.company_id.vat or "",
            "serie_correlativo": "%s-%s" % (serie, correlativo.zfill(8)),
            "db": self.env.cr.dbname,
            "move_id": self.id,
            "path": "/generator/" + endpoint,
            "api_key": self.company_id.sudo().l10n_pe_ne_api_key or "",
            # tipoDoc (01/03/07/08) para que el worker pre-genere el PDF
            "doc_type": self._l10n_pe_document_type(),
            "payload": payload,
        }
        # Reintento tras un rechazo: borra el resultado viejo ANTES de encolar,
        # para que el cron no aplique el resultado obsoleto mientras el worker
        # procesa el intento nuevo (best-effort: si no existe, no pasa nada).
        table = icp.get_param("l10n_pe_ne_biller.results_table", "")
        if table:
            try:
                boto3.client("dynamodb", region_name=region).delete_item(
                    TableName=table,
                    Key={
                        "ruc_emisor": {"S": msg["ruc"]},
                        "serie_correlativo": {"S": msg["serie_correlativo"]},
                    },
                )
            except Exception as exc:  # noqa: BLE001
                _logger.warning("async biller: no se pudo limpiar resultado previo: %s", exc)
        try:
            boto3.client("sqs", region_name=region).send_message(
                QueueUrl=queue_url,
                MessageBody=json.dumps(msg, ensure_ascii=False),
            )
        except Exception as exc:  # noqa: BLE001
            self.l10n_pe_biller_state = "error"
            self.l10n_pe_biller_message = _("No se pudo encolar la emisión: %s") % exc
            return
        self.l10n_pe_biller_state = "en_proceso"
        self.l10n_pe_biller_message = _(
            "Encolado para envío a SUNAT — el resultado llega en unos minutos "
            "(aparece en el chatter; recargá la vista para ver el estado final)."
        )
        self._l10n_pe_trigger_poll_async(seconds=20)

    @api.model
    def _l10n_pe_trigger_poll_async(self, seconds=20):
        """Adelanta el próximo run del cron de recogida: sin esto el resultado
        espera el beat base de 2 min aunque el worker ya lo haya dejado en
        DynamoDB. Ojo con la expectativa: el scheduler de Odoo duerme beats
        fijos de ~60s y un trigger futuro NO lo despierta a call_at (ni con
        ODOO_NOTIFY_CRON_CHANGES: ese NOTIFY sale al commit, cuando el trigger
        aún no venció) — el pickup real es el primer beat posterior a call_at,
        o sea hasta ~60-70s después. Best-effort: si falla, el beat base sigue."""
        try:
            self.env.ref(
                "l10n_pe_ne_biller.ir_cron_l10n_pe_ne_poll_async"
            ).sudo()._trigger(at=fields.Datetime.now() + timedelta(seconds=seconds))
        except Exception as exc:  # noqa: BLE001
            _logger.warning("async biller: no se pudo adelantar el cron: %s", exc)

    def _l10n_pe_attach_async_pdf(self, s3c, bucket, item):
        """Adjunta el PDF pre-generado por el worker (pdf_s3_key del item), si
        ya existe y el move no tiene uno. Best-effort: si falta, el botón
        Descargar PDF cae al camino síncrono de siempre."""
        self.ensure_one()
        pdf_s3 = (item.get("pdf_s3_key") or {}).get("S", "")
        if not pdf_s3 or self.l10n_pe_biller_pdf:
            return
        try:
            pdf_bytes = s3c.get_object(Bucket=bucket, Key=pdf_s3)["Body"].read()
            serie = self.l10n_pe_ne_serie_emit
            corr = self.l10n_pe_ne_corr_emit
            if not serie or not corr:
                serie, corr = self._l10n_pe_serie_correlativo()
                corr = corr.zfill(8)
            att = self.env["ir.attachment"].create(
                {
                    "name": "%s-%s-%s.pdf"
                    % (self.company_id.vat or "", serie, corr),
                    "res_model": "account.move",
                    "res_id": self.id,
                    "mimetype": "application/pdf",
                    "raw": pdf_bytes,
                }
            )
            self.l10n_pe_biller_pdf = att.id
        except Exception as exc:  # noqa: BLE001 — PDF es best-effort
            _logger.warning(
                "async biller: PDF no adjuntado en %s: %s", self.name, exc
            )

    @api.model
    def _l10n_pe_cron_poll_async(self):
        """Recoge resultados de emisiones asíncronas: lee el item del worker en
        DynamoDB (PK ruc_emisor / SK serie_correlativo) y, si terminó, baja el
        XML/CDR de S3 y lo aplica con el mismo código del flujo síncrono."""
        icp = self.env["ir.config_parameter"].sudo()
        if icp.get_param("l10n_pe_ne_biller.async_enabled", "").strip().lower() not in ("1", "true"):
            return
        table = icp.get_param("l10n_pe_ne_biller.results_table", "")
        bucket = icp.get_param("l10n_pe_ne_biller.results_bucket", "")
        region = icp.get_param("l10n_pe_ne_biller.aws_region", "us-east-1")
        if not boto3 or not table or not bucket:
            _logger.warning(
                "async biller: faltan parámetros results_table/results_bucket o boto3"
            )
            return
        ddb = boto3.client("dynamodb", region_name=region)
        s3c = boto3.client("s3", region_name=region)
        moves = self.search([("l10n_pe_biller_state", "=", "en_proceso")], limit=25)
        for move in moves:
            try:
                serie, correlativo = move._l10n_pe_serie_correlativo()
                key = {
                    "ruc_emisor": {"S": move.company_id.vat or ""},
                    "serie_correlativo": {"S": "%s-%s" % (serie, correlativo.zfill(8))},
                }
                item = ddb.get_item(TableName=table, Key=key).get("Item")
                if not item:  # aún en cola o procesándose
                    continue
                status = item["status"]["S"]
                if status == "enviado":
                    xml_key = (item.get("xml_s3_key") or {}).get("S", "")
                    body = (
                        s3c.get_object(Bucket=bucket, Key=xml_key)["Body"]
                        .read()
                        .decode("iso-8859-1")
                    )
                    cdr_b64 = ""
                    cdr_key = (item.get("cdr_s3_key") or {}).get("S", "")
                    if cdr_key:
                        cdr_b64 = base64.b64encode(
                            s3c.get_object(Bucket=bucket, Key=cdr_key)["Body"].read()
                        ).decode()
                    move._l10n_pe_apply_emission_response(True, body, cdr_b64)
                    # PDF pre-generado por el worker: el botón "Descargar PDF"
                    # lo sirve cacheado, sin llamada síncrona al facturador.
                    move._l10n_pe_attach_async_pdf(s3c, bucket, item)
                elif status in ("rechazado", "error"):
                    move.l10n_pe_biller_state = status
                    move.l10n_pe_biller_message = (
                        (item.get("message") or {}).get("S") or ""
                    )[:2000]
                # El form no refresca solo cuando escribe un cron: el chatter sí.
                move.message_post(
                    body=_("Facturador (async): %s — %s")
                    % (
                        dict(
                            move._fields["l10n_pe_biller_state"].selection
                        ).get(move.l10n_pe_biller_state, move.l10n_pe_biller_state),
                        (move.l10n_pe_biller_message or "")[:500],
                    )
                )
                # ...y el statusbar en vivo va por el bus (websocket): el JS
                # biller_live_statusbar recarga el form abierto al recibir esto.
                self.env["bus.bus"]._sendone(
                    "l10n_pe_biller_updates",
                    "l10n_pe_biller_update",
                    {"move_id": move.id, "state": move.l10n_pe_biller_state},
                )
            except Exception as exc:  # noqa: BLE001 — un move malo no frena al resto
                _logger.warning("async biller: error procesando %s: %s", move.name, exc)
        # Segundo pase — PDFs rezagados: el worker publica "enviado" ANTES de
        # generar el PDF, así que el pase de arriba suele aplicar el resultado
        # cuando pdf_s3_key aún no existe; se re-lee el item hasta que aparezca
        # (ventana corta: biller-pdf tarda segundos, ~2 min en cold start).
        sin_pdf = self.search(
            [
                ("l10n_pe_biller_state", "=", "enviado"),
                ("l10n_pe_biller_pdf", "=", False),
                ("write_date", ">=", fields.Datetime.now() - timedelta(minutes=15)),
            ],
            limit=25,
        )
        for move in sin_pdf:
            try:
                serie = move.l10n_pe_ne_serie_emit
                corr = move.l10n_pe_ne_corr_emit
                if not serie or not corr:
                    serie, corr = move._l10n_pe_serie_correlativo()
                    corr = corr.zfill(8)
                item = ddb.get_item(
                    TableName=table,
                    Key={
                        "ruc_emisor": {"S": move.company_id.vat or ""},
                        "serie_correlativo": {"S": "%s-%s" % (serie, corr)},
                    },
                ).get("Item")
                if item:
                    move._l10n_pe_attach_async_pdf(s3c, bucket, item)
            except Exception as exc:  # noqa: BLE001
                _logger.warning(
                    "async biller: reconciliación PDF %s: %s", move.name, exc
                )
        # Re-poll corto mientras quede trabajo FRESCO (emisiones en curso o
        # PDFs por reconciliar). Acotado por edad: si el worker nunca escribió
        # el item (p.ej. mensaje muerto en la DLQ), el move zombi vuelve al
        # beat base de 2 min en vez de re-disparar el cron para siempre.
        limite = fields.Datetime.now() - timedelta(minutes=30)
        pendientes = moves.filtered(
            lambda m: m.l10n_pe_biller_state == "en_proceso"
            and m.write_date
            and m.write_date >= limite
        )
        if pendientes or sin_pdf.filtered(lambda m: not m.l10n_pe_biller_pdf):
            self._l10n_pe_trigger_poll_async(seconds=30)

    # ------------------------------------------------------------------ acción
    def action_l10n_pe_send_to_biller(self):
        _logger.info("Enviando facturas a Biller: %s", self.ids)
        icp = self.env["ir.config_parameter"].sudo()
        base = icp.get_param("l10n_pe_ne_biller.url", "http://localhost:8090").rstrip(
            "/"
        )
        _logger.info("URL: %s", base)
        # >240 es inalcanzable: limit_time_real=240 mata el worker de Odoo
        # antes (SIGKILL con rollback), con el POST quizá ya aceptado en SUNAT.
        timeout = int(icp.get_param("l10n_pe_ne_biller.timeout", "240"))
        _logger.info("Timeout: %s", timeout)
        use_async = icp.get_param(
            "l10n_pe_ne_biller.async_enabled", ""
        ).strip().lower() in ("1", "true")
        for move in self:
            _logger.info(
                "Procesando factura: %s (%s)", move.name, move.l10n_pe_biller_state
            )
            if move.l10n_pe_biller_state in ("enviado", "en_proceso"):
                _logger.info("Factura ya enviada o en proceso: %s", move.name)
                continue
            if use_async:
                move._l10n_pe_enqueue_emission(icp)
                continue
            endpoint, payload = move._l10n_pe_target()
            _logger.info("AAAEnviando %s: %s", endpoint, payload)
            headers = {"X-Api-Key": move.company_id.sudo().l10n_pe_ne_api_key or ""}
            try:
                _logger.info("EEEEnviando %s: %s", endpoint, payload)
                resp = requests.post(
                    base + "/generator/" + endpoint,
                    json=payload,
                    headers=headers,
                    # connect corto aparte: un endpoint inalcanzable (SG, DNS)
                    # falla en 5s en vez de colgar el worker hasta el read.
                    timeout=(5, timeout),
                )
                _logger.info(
                    "RESP %s -> POST %s/generator/%s -> HTTP %s | %s",
                    move.name,
                    base,
                    endpoint,
                    resp.status_code,
                    resp.text[:500],
                )
                _logger.info("Respuesta: %s", resp.text)
            except requests.RequestException as exc:
                move.l10n_pe_biller_state = "error"
                _logger.error("Error: %s", exc)
                move.l10n_pe_biller_message = (
                    _("Error de conexión con el facturador: %s") % exc
                )
                continue
            # El biller devuelve el XML firmado como body y el CDR de SUNAT en
            # el header X-Sunat-Cdr (base64 del zip).
            move._l10n_pe_apply_emission_response(
                resp.status_code == 200, resp.text, resp.headers.get("X-Sunat-Cdr")
            )
        return True

    # ------------------------------------------- API ligera (BFF NE Express, /json/2)
    @api.model
    def l10n_pe_ne_quick_emit(self, payload):
        """Emite un comprobante desde un payload PLANO (sin contexto contable previo): crea/halla el
        cliente, arma el account.move con sus líneas (impuesto por código cat-05), lo postea y lo envía a
        SUNAT vía el facturador. Devuelve el resultado. Lo consume el BFF stateless por /json/2 — así la
        lógica de negocio queda en Odoo (fuente única) y el dato vive en Odoo (upgrade sin migración)."""
        company = self.env.company
        journal = self.env["account.journal"].search(
            [("type", "=", "sale"), ("company_id", "=", company.id)], limit=1
        )
        if not journal:
            raise UserError(_("No hay diario de ventas configurado para la compañía."))
        tipo = payload.get("tipoDoc") or "01"
        # NC (07) / ND (08): resuelven el documento afectado (mismo cliente, serie derivada del original).
        origin = None
        if tipo in ("07", "08"):
            origin = self._l10n_pe_ne_quick_origin(
                payload.get("docAfectado") or payload.get("afectado")
            )
        if origin is not None:
            partner = origin.partner_id
        else:
            partner = self._l10n_pe_ne_quick_partner(payload.get("cliente") or {})
        # Descuento global (% sobre toda la operación): se prorratea a cada línea como descuento que
        # afecta la base (cat. 53 código 00), combinándose con el descuento propio de la línea. Produce
        # los mismos totales que un descuento global y reusa la emisión de descuento por ítem ya validada.
        g = float(payload.get("descuentoGlobal") or 0)
        lines = []
        for ln in payload.get("lineas") or []:
            tax = self._l10n_pe_ne_tax_by_code(ln.get("taxCode"))
            taxes = tax
            if ln.get("icbper"):
                # Bolsa plástica: el ICBPER (monto fijo por unidad) se SUMA al IGV de la línea.
                taxes = tax + self._l10n_pe_ne_ensure_icbper_tax()
            isc_rate = float(ln.get("isc") or 0)
            if isc_rate > 0:
                # ISC (ad-valorem): se agrega a la línea; el IGV se recalcula sobre valor + ISC.
                taxes = taxes + self._l10n_pe_ne_ensure_isc_tax(isc_rate)
            prod = self._l10n_pe_ne_quick_product(ln, tax)
            d = float(ln.get("descuento") or 0)
            disc = round(100.0 * (1 - (1 - d / 100.0) * (1 - g / 100.0)), 6) if g else d
            lvals = {
                "name": ln.get("descripcion") or (prod.name if prod else "ITEM"),
                "quantity": float(ln.get("cantidad") or 1),
                "price_unit": float(ln.get("precioUnitario") or 0),
                "discount": disc,
                "tax_ids": [(6, 0, taxes.ids if taxes else [])],
            }
            if prod:
                lvals["product_id"] = prod.id
            if ln.get("unidad"):
                lvals["l10n_pe_ne_unit_code"] = ln["unidad"]
            if ln.get("codSunat"):
                lvals["l10n_pe_ne_cod_producto_sunat"] = ln["codSunat"]
            lines.append((0, 0, lvals))
        # Otros cargos (que afectan la base imponible): se agregan como una línea gravada adicional, así
        # suben gravada/IGV/total con la maquinaria de líneas ya validada (no se prorratea el desc. global).
        oc = float(payload.get("otrosCargos") or 0)
        if oc > 0:
            lines.append(
                (
                    0,
                    0,
                    {
                        "name": payload.get("otrosCargosDesc") or "OTROS CARGOS",
                        "quantity": 1,
                        "price_unit": oc,
                        "tax_ids": [(6, 0, self._l10n_pe_ne_tax_by_code("1000").ids)],
                    },
                )
            )
        vals = {
            "move_type": "out_refund" if tipo == "07" else "out_invoice",
            "partner_id": partner.id,
            "journal_id": journal.id,
            "invoice_date": payload.get("fechaEmision")
            or fields.Date.context_today(self),
            "l10n_pe_serie": payload.get("serie")
            or self._l10n_pe_ne_default_serie(tipo, origin),
            "invoice_line_ids": lines,
        }
        moneda = self._l10n_pe_ne_quick_currency(payload.get("moneda"))
        if moneda:
            vals["currency_id"] = moneda.id
        if origin is not None:
            vals["l10n_pe_motivo_code"] = str(
                payload.get("motivo") or ("01" if tipo == "07" else "02")
            )
            if tipo == "07":
                vals["reversed_entry_id"] = origin.id
            else:
                vals["debit_origin_id"] = origin.id
        if payload.get("correlativo"):
            vals["l10n_pe_correlativo"] = str(payload["correlativo"])
            # Con correlativo MANUAL no aplica la unicidad de la secuencia por diario:
            # dos emisiones forzadas comparten serie+correlativo fiscal pero tienen
            # 'name' internos distintos, así que account_move_unique_name_latam no las
            # detecta. Verificamos el número fiscal (serie_emit+corr_emit) contra los ya
            # emitidos/anulados de la compañía antes de crear y mandar a SUNAT.
            self._l10n_pe_ne_check_numero_libre(
                vals["l10n_pe_serie"], str(payload["correlativo"])
            )
        move = self.env["account.move"].create(vals)
        self._l10n_pe_ne_quick_flags(move, payload)
        move.action_post()
        # Si la emisión vino de "Convertir a comprobante", vincula el comprobante
        # recién posteado a la cotización de origen y la marca como 'convertida'.
        cotid = payload.get("cotizacionId")
        if cotid:
            cot = self.env["l10n_pe_ne.cotizacion"].browse(int(cotid)).exists()
            if cot:
                cot.l10n_pe_ne_vincular_comprobante(move.id)
        move.action_l10n_pe_send_to_biller()
        return move.l10n_pe_ne_quick_result()

    def _l10n_pe_ne_check_numero_libre(self, serie, correlativo):
        """Impide reutilizar un número fiscal (serie+correlativo) ya emitido/anulado en
        la compañía. Necesario solo con correlativo manual: la unicidad de la secuencia
        del diario no cubre este caso (ver quick_emit)."""
        corr = (correlativo or "").strip().zfill(8)
        dup = self.env["account.move"].sudo().search(
            [
                ("company_id", "=", self.env.company.id),
                ("l10n_pe_ne_serie_emit", "=", serie),
                ("l10n_pe_ne_corr_emit", "=", corr),
                ("l10n_pe_biller_state", "in", ("enviado", "anulado")),
            ],
            limit=1,
        )
        if dup:
            raise UserError(
                _(
                    "Ya existe un comprobante con ese número para ese cliente "
                    "(número duplicado)."
                )
            )

    def _l10n_pe_ne_default_serie(self, tipo, origin=None):
        """Serie por defecto: F001/B001 para factura/boleta; FC01/FD01 (o BC01/BD01 si el afectado es
        boleta) para NC/ND, derivando la familia del documento original."""
        if tipo == "03":
            return "B001"
        if tipo in ("07", "08"):
            base = (
                "B"
                if origin is not None
                and (origin.l10n_pe_serie or "F")[:1].upper() == "B"
                else "F"
            )
            return base + ("C01" if tipo == "07" else "D01")
        return "F001"

    def _l10n_pe_ne_quick_currency(self, moneda):
        """Moneda del comprobante: PEN por defecto; USD si el payload lo pide
        (USD/DOLARES/$). Activa la moneda si está inactiva. El builder ya emite
        tipMoneda desde currency_id."""
        code = (moneda or "PEN").strip().upper()
        code = (
            "USD"
            if code in ("USD", "DOLARES", "DÓLARES", "DOLAR", "US$", "$")
            else "PEN"
        )
        cur = (
            self.env["res.currency"]
            .with_context(active_test=False)
            .search([("name", "=", code)], limit=1)
        )
        if cur and not cur.active:
            cur.sudo().active = True
        return cur

    def _l10n_pe_ne_quick_origin(self, ref):
        """Resuelve el account.move afectado por una NC/ND: por id (lo natural, el emit devuelve 'id') o
        por serie+correlativo. Lanza si no lo encuentra."""
        ref = ref or {}
        Move = self.env["account.move"]
        if ref.get("id"):
            m = Move.browse(int(ref["id"])).exists()
            if m:
                return m
        serie = (ref.get("serie") or "").strip()
        corr = str(ref.get("correlativo") or "").strip().lstrip("0")
        if serie and corr:
            cands = Move.search(
                [
                    ("l10n_pe_serie", "=", serie),
                    ("move_type", "in", ("out_invoice", "out_refund")),
                ],
                order="id desc",
                limit=300,
            )
            for m in cands:
                _s, c = m._l10n_pe_serie_correlativo()
                if (c or "").lstrip("0") == corr:
                    return m
        raise UserError(
            _(
                "No se encontró el documento afectado (envía docAfectado.id o serie+correlativo)."
            )
        )

    @api.model
    def l10n_pe_ne_quick_anular(self, payload):
        """Anula un comprobante ya emitido a SUNAT: boletas por Resumen Diario (RC, tipEstado 3),
        facturas/NC/ND por Comunicación de Baja (RA). payload: {id | serie+correlativo, motivo}.
        Lo consume el BFF por /json/2."""
        payload = payload or {}
        move = self._l10n_pe_ne_quick_origin(payload.get("comprobante") or payload)
        move.l10n_pe_ne_baja_motivo = (
            payload.get("motivo") or ""
        ).strip() or "Anulacion de la operacion"
        move.action_l10n_pe_send_baja()
        return move._l10n_pe_ne_anular_result()

    def _l10n_pe_ne_anular_result(self):
        self.ensure_one()
        tipo, serie, corr = self._l10n_pe_baja_identidad()
        msg = self.l10n_pe_biller_message or ""
        m = re.search(r"ResponseCode (\d+)", msg)
        anulado = self.l10n_pe_biller_state == "anulado"
        return {
            "id": self.id,
            "tipoAnulacion": "RC" if tipo == "03" else "RA",
            "docAnulacion": self.l10n_pe_ne_baja_doc or "",
            "comprobante": "%s-%s" % (serie, (corr or "").zfill(8)),
            "estado": self.l10n_pe_biller_state,
            "anulado": anulado,
            "responseCode": m.group(1) if m else ("0" if anulado else ""),
            "mensaje": msg,
        }

    def l10n_pe_ne_get_baja_files(self, kind=None):
        """{cdr} base64 de la anulación (RA/RC), para que el BFF lo sirva.

        Acepta e ignora ``kind`` (una baja no tiene ticket): la ruta
        ``/ne/api/anulacion/<id>/cdr`` invoca este método vía
        ``_serve_file`` con ``kind='cdr'`` — simétrico con
        ``l10n_pe_ne_get_files``.
        """
        self.ensure_one()
        out = {}
        att = self.l10n_pe_ne_baja_cdr
        if att:
            v = att.datas
            out["cdr"] = (
                v.decode("ascii") if isinstance(v, (bytes, bytearray)) else (v or "")
            )
        return out

    def _l10n_pe_ne_quick_partner(self, c):
        num = (c.get("numDoc") or "").strip()
        Partner = self.env["res.partner"]
        if num:
            found = Partner.search([("vat", "=", num)], limit=1)
            if found:
                return found
        # company_id del emisor actual: aísla el cliente por RUC (multi-tenant). Sin
        # esto quedaría company_id=False = visible/editable por TODOS los tenants.
        vals = {
            "name": c.get("razonSocial") or "CONSUMIDOR FINAL",
            "customer_rank": 1,
            "company_id": self.env.company.id,
        }
        if num:
            vals["vat"] = num
            t = self.env["l10n_latam.identification.type"].search(
                [("l10n_pe_vat_code", "=", c.get("tipoDoc") or "6")], limit=1
            )
            if t:
                vals["l10n_latam_identification_type_id"] = t.id
        return Partner.create(vals)

    def _l10n_pe_ne_tax_by_code(self, code):
        """account.tax de venta por código cat-05 (l10n_pe_edi_tax_code); default 1000 (IGV gravado)."""
        tax = self.env["account.tax"].search(
            [
                ("company_id", "=", self.env.company.id),
                ("type_tax_use", "=", "sale"),
                ("l10n_pe_edi_tax_code", "=", code or "1000"),
            ],
            limit=1,
        )
        return self._l10n_pe_ne_normalize_tax_excluded(tax)

    @api.model
    def _l10n_pe_ne_normalize_tax_excluded(self, tax):
        """Garantiza que la IGV/IVAP de venta trate el precio como VALOR (sin IGV).

        Contrato del app: `precioUnitario` es el valor unitario SIN IGV — el front
        (Emitir) lo muestra como `Gravada` y suma el IGV 18% por encima. Pero la base
        que emitimos sale de `line.price_subtotal`, que respeta el flag `price_include`
        de la tax: si en la BD la IGV quedó como "precio incluye impuesto"
        (`price_include_override='tax_included'`, o por el default de la compañía),
        Odoo descompone la base dividiendo por 1+tasa (100 -> 84.75) y el comprobante
        emitido NO coincide con el preview (que mostraba 118). Para que preview==emitido
        sin depender de la config ambiente, fijamos tax-excluded en la IGV/IVAP de venta
        de forma idempotente (solo escribe si hace falta; se autocorrige en el 1er emit)."""
        if (
            tax
            and tax.l10n_pe_edi_tax_code in ("1000", "1016")
            and tax.price_include_override != "tax_excluded"
        ):
            tax.sudo().write({"price_include_override": "tax_excluded"})
        return tax

    def _l10n_pe_ne_ensure_icbper_tax(self):
        """Tax ICBPER (cat-05 7152): monto FIJO por unidad (S/ 0.50 vigente desde 2023). Se crea en Odoo
        si no existe — el dato y la lógica viven en Odoo, no en el orquestador."""
        Tax = self.env["account.tax"].sudo()
        company = self.env.company
        tax = Tax.search(
            [
                ("company_id", "=", company.id),
                ("type_tax_use", "=", "sale"),
                ("l10n_pe_edi_tax_code", "=", "7152"),
            ],
            limit=1,
        )
        if tax:
            return tax
        return Tax.create(
            {
                "name": "ICBPER",
                "amount_type": "fixed",
                "amount": 0.50,
                "type_tax_use": "sale",
                "l10n_pe_edi_tax_code": "7152",
                "company_id": company.id,
                "description": "ICBPER",
            }
        )

    def _l10n_pe_ne_ensure_isc_tax(self, rate):
        """Tax ISC (Impuesto Selectivo al Consumo, cat-05 2000) — Sistema al Valor (ad-valorem %).
        Se crea/reusa por tasa. include_base_amount=True y secuencia ANTES del IGV → el IGV se
        computa sobre (valor venta + ISC), como exige SUNAT (mtoBaseIgvItem = base + ISC)."""
        Tax = self.env["account.tax"].sudo()
        company = self.env.company
        rate = round(float(rate or 0), 4)
        tax = Tax.search(
            [
                ("company_id", "=", company.id),
                ("type_tax_use", "=", "sale"),
                ("l10n_pe_edi_tax_code", "=", "2000"),
                ("amount_type", "=", "percent"),
                ("amount", "=", rate),
            ],
            limit=1,
        )
        if tax:
            return tax
        igv = self._l10n_pe_ne_tax_by_code("1000")
        return Tax.create(
            {
                "name": "ISC %g%%" % rate,
                "amount_type": "percent",
                "amount": rate,
                "type_tax_use": "sale",
                "l10n_pe_edi_tax_code": "2000",
                "include_base_amount": True,   # el IGV se calcula sobre valor + ISC
                "sequence": (igv.sequence - 1) if igv else 1,   # ISC se aplica antes que el IGV
                "company_id": company.id,
                "description": "ISC",
            }
        )

    @api.model
    def l10n_pe_ne_config(self):
        """Parámetros que React debe leer DESDE Odoo (no hardcodear): tasa IGV y monto ICBPER por unidad."""
        return {
            "igv": 18.0,
            "icbperRate": self._l10n_pe_ne_ensure_icbper_tax().amount,
        }

    @api.model
    def l10n_pe_ne_series(self, limit=None, offset=None):
        """Series realmente en uso, agregadas desde los comprobantes emitidos (la serie la
        fija el emisor al emitir; el correlativo lo autoincrementa Odoo por diario). Por serie:
        tipo, cuántos emitidos, último correlativo y el próximo a emitir. Incluye las series de
        retención/percepción (account.payment). Aislado por RUC vía el contexto de compañía."""
        TIPO = {
            "01": "Factura",
            "03": "Boleta",
            "07": "Nota de crédito",
            "08": "Nota de débito",
            "20": "Retención",
            "40": "Percepción",
        }
        agg = {}

        def add(serie, tipo, corr):
            # Solo cuenta CPE realmente emitidos: con correlativo asignado (n>=1). Un
            # account.payment lleva R001 y P001 por defecto, pero solo se emite uno; el
            # otro queda 'por_enviar' con correlativo vacío y no debe contarse.
            n = int(corr) if (corr or "").strip().isdigit() else 0
            if not serie or n < 1:
                return
            cur = agg.setdefault(
                serie, {"serie": serie, "tipoDoc": tipo, "emitidos": 0, "ultimo": 0}
            )
            cur["emitidos"] += 1
            if n > cur["ultimo"]:
                cur["ultimo"] = n

        for m in self.search([("l10n_pe_ne_serie_emit", "!=", False)]):
            add(
                m.l10n_pe_ne_serie_emit,
                m.l10n_pe_ne_tipo_doc or m._l10n_pe_document_type(),
                m.l10n_pe_ne_corr_emit,
            )
        for p in self.env["account.payment"].search(
            [("company_id", "=", self.env.company.id)]
        ):
            add(p.l10n_pe_ret_serie, "20", p.l10n_pe_ret_correlativo)
            add(p.l10n_pe_per_serie, "40", p.l10n_pe_per_correlativo)

        filas = [
            {
                "serie": s["serie"],
                "tipoDoc": s["tipoDoc"],
                "tipo": TIPO.get(s["tipoDoc"], s["tipoDoc"]),
                "emitidos": s["emitidos"],
                "ultimo": str(s["ultimo"]).zfill(8) if s["ultimo"] else "—",
                "proximo": str(s["ultimo"] + 1).zfill(8),
            }
            for s in sorted(agg.values(), key=lambda x: x["serie"])
        ]
        # Paginación opt-in sobre el agregado ya construido (no hay search directo).
        if offset is None:
            return filas
        return {"items": filas[offset:offset + limit] if limit else filas[offset:],
                "total": len(filas)}

    # ============================================================ datos negocio
    @api.model
    def l10n_pe_ne_negocio(self):
        """Datos del emisor (negocio) que alimentan el bloque `emisor` del XML, leídos desde
        res.company + su partner. El RUC es de solo lectura (identidad del emisor, indexa el
        certificado de firma en el servidor)."""
        company = self.env.company
        p = company.partner_id
        d = p.l10n_pe_district
        return {
            "ruc": p.vat or "",
            "razonSocial": company.name or "",
            "direccion": p.street or "",
            "urbanizacion": p.street2 or "",
            "telefono": p.phone or "",
            "email": p.email or "",
            "distritoId": d.id if d else None,
            "distrito": d.name if d else "",
            "ubigeo": d.code if d else "",
            "provincia": (d.city_id.name if d and d.city_id else (p.city or "")),
            "departamento": p.state_id.name or "",
        }

    @api.model
    def l10n_pe_ne_buscar_distrito(self, q=None, limit=20):
        """Busca distritos (ubigeo) por nombre o código para el selector de dirección."""
        q = (q or "").strip()
        dom = ["|", ("name", "ilike", q), ("code", "ilike", q)] if q else []
        recs = self.env["l10n_pe.res.city.district"].search(dom, limit=limit)
        return [
            {
                "id": r.id,
                "code": r.code or "",
                "name": r.name or "",
                "provincia": r.city_id.name or "",
                "departamento": r.city_id.state_id.name or "",
            }
            for r in recs
        ]

    @api.model
    def l10n_pe_ne_update_negocio(self, vals):
        """Actualiza los datos editables del emisor (razón social, dirección, contacto y
        distrito). El RUC nunca se toca. Al fijar un distrito se sincronizan también provincia
        (city) y departamento (state) para que el bloque `emisor` quede consistente. Los cambios
        fluyen al PRÓXIMO XML emitido vía _l10n_pe_emisor."""
        # env.company lo fija el servidor desde el usuario (with_company), así que estas
        # escrituras SIEMPRE recaen sobre la empresa del propio emisor. res.company solo es
        # escribible por "Access Rights" (que el emisor no tiene); usamos sudo acotado a su
        # propia empresa para no exigirle ese rol global.
        company = self.env.company.sudo()
        p = company.partner_id
        razon = (vals.get("razonSocial") or "").strip()
        if "razonSocial" in vals and razon:
            company.name = razon
        pvals = {}
        for key, field in (
            ("direccion", "street"),
            ("urbanizacion", "street2"),
            ("telefono", "phone"),
            ("email", "email"),
        ):
            if key in vals:
                pvals[field] = (vals.get(key) or "").strip() or False
        did = vals.get("distritoId")
        if did:
            d = self.env["l10n_pe.res.city.district"].sudo().browse(int(did)).exists()
            if d:
                pvals["l10n_pe_district"] = d.id
                if d.city_id:
                    pvals["city"] = d.city_id.name
                    if d.city_id.state_id:
                        pvals["state_id"] = d.city_id.state_id.id
                    if d.city_id.country_id:
                        pvals["country_id"] = d.city_id.country_id.id
        if pvals:
            p.write(pvals)
        return self.l10n_pe_ne_negocio()

    # ============================================================ resumen estado
    @api.model
    def l10n_pe_ne_resumen(self):
        """Resumen de estado del emisor, calculado en Odoo (no en React): actividad emitida
        hoy y en el mes en curso —separando PEN/USD para no mezclar monedas— y el desglose por
        estado SUNAT de todos los comprobantes de venta. Aislado por RUC vía la compañía."""
        today = fields.Date.context_today(self)
        mes0 = today.replace(day=1)
        sales = [
            ("move_type", "in", ("out_invoice", "out_refund")),
            ("company_id", "=", self.env.company.id),
        ]
        emitidos = sales + [("l10n_pe_biller_state", "in", ("enviado", "anulado"))]

        def bucket(moves):
            pen = usd = 0.0
            for m in moves:
                if (m.currency_id.name or "PEN") == "USD":
                    usd += m.amount_total or 0.0
                else:
                    pen += m.amount_total or 0.0
            return {"count": len(moves), "pen": round(pen, 2), "usd": round(usd, 2)}

        hoy = self.search(emitidos + [("invoice_date", "=", today)])
        mes = self.search(
            emitidos + [("invoice_date", ">=", mes0), ("invoice_date", "<=", today)]
        )

        # Desglose por estado SUNAT (toda la historia de ventas de la compañía).
        estados = {
            "aceptado": 0,
            "anulado": 0,
            "rechazado": 0,
            "pendiente": 0,
            "error": 0,
        }
        MAP = {
            "enviado": "aceptado",
            "anulado": "anulado",
            "rechazado": "rechazado",
            "por_enviar": "pendiente",
            "error": "error",
        }
        for m in self.search(sales):
            k = MAP.get(m.l10n_pe_biller_state)
            if k:
                estados[k] += 1

        return {
            "hoy": bucket(hoy),
            "mes": dict(bucket(mes), periodo=today.strftime("%Y%m")),
            "estados": estados,
            "porAtender": estados["rechazado"] + estados["error"],
        }

    # ============================================================== PLE 14.1
    # Registro de Ventas e Ingresos Electrónico (PLE, formato 14.1). Estructura
    # oficial SUNAT (Anexo RS 286-2009 y modif.): campos 1-34, separador '|',
    # palote final, líneas CRLF, codificación ISO-8859-1. El archivo que el
    # CONTADOR sube al PLE de SUNAT, generado desde los comprobantes emitidos.

    @staticmethod
    def _l10n_pe_ne_ple_num(v):
        return "%.2f" % (v or 0.0)

    def _l10n_pe_ne_ple_breakdown(self):
        """Desglose por afectación para el PLE (cuadra con el XML emitido)."""
        self.ensure_one()
        gravado = exonerado = inafecto = exportacion = igv = icbper = 0.0
        for ln in self.invoice_line_ids:
            codes = ln.tax_ids.mapped("l10n_pe_edi_tax_code")
            base = ln.price_subtotal or 0.0
            if "9997" in codes:
                exonerado += base
            elif "9998" in codes:
                inafecto += base
            elif "9995" in codes:
                exportacion += base
            elif any(c in codes for c in ("1000", "1016")):
                gravado += base
        for tl in self.line_ids.filtered(lambda l: l.tax_line_id):
            code = tl.tax_line_id.l10n_pe_edi_tax_code or ""
            amt = abs(tl.amount_currency or 0.0)
            if code == "7152":
                icbper += amt
            elif code in ("1000", "1016"):
                igv += amt
        return {
            "gravado": gravado,
            "exonerado": exonerado,
            "inafecto": inafecto,
            "exportacion": exportacion,
            "igv": igv,
            "icbper": icbper,
            "total": self.amount_total or 0.0,
        }

    def _l10n_pe_ne_doc_id(self):
        """(serie, número) del comprobante para el PLE: prefiere el correlativo
        EMITIDO; si no, el folio (parte numérica final) del `name`, zfill 8. NO usa
        l10n_pe_correlativo (datos antiguos lo tienen con basura)."""
        self.ensure_one()
        serie = (
            self.l10n_pe_ne_serie_emit
            or self.l10n_pe_serie
            or self.journal_id.l10n_pe_ne_serie
            or "F001"
        )
        numero = (self.l10n_pe_ne_corr_emit or "").strip()
        if not numero:
            folios = re.findall(r"\d+", (self.name or "").replace(" ", ""))
            numero = (folios[-1] if folios else "1").zfill(8)
        return serie, numero

    def _l10n_pe_ne_ple_origen(self):
        """(fecha, tipo, serie, numero) del comprobante que se modifica (NC/ND)."""
        orig = self.reversed_entry_id or getattr(self, "debit_origin_id", False)
        if not orig:
            return "", "", "", ""
        fecha = orig.invoice_date.strftime("%d/%m/%Y") if orig.invoice_date else ""
        tipo = orig.l10n_pe_ne_tipo_doc or orig._l10n_pe_document_type()
        serie, num = orig._l10n_pe_ne_doc_id()
        return fecha, tipo, serie, num

    def _l10n_pe_ne_ple_linea(self, periodo8, cuo):
        """Una línea del PLE 14.1 (campos 1-34, '|' separador + palote final)."""
        self.ensure_one()
        num = self._l10n_pe_ne_ple_num
        b = self._l10n_pe_ne_ple_breakdown()
        tipo = self.l10n_pe_ne_tipo_doc or self._l10n_pe_document_type()
        serie, corr = self._l10n_pe_ne_doc_id()
        tdoc, ndoc = self._l10n_pe_cliente_doc()
        con_doc = bool(ndoc) and ndoc != "00000000"
        fecha = self.invoice_date.strftime("%d/%m/%Y") if self.invoice_date else ""
        estado = "2" if self.l10n_pe_biller_state == "anulado" else "1"
        of, ot, os_, on = self._l10n_pe_ne_ple_origen()
        moneda = self.currency_id.name or "PEN"
        campos = [
            periodo8,  # 1 Periodo (AAAAMM00)
            str(self.id),  # 2 CUO (único)
            "",  # 3 Nro correlativo (solo estado 8/9)
            fecha,  # 4 Fecha emisión
            "",  # 5 Fecha vencimiento
            tipo,  # 6 Tipo comprobante (tabla 10)
            serie,  # 7 Serie
            corr,  # 8 Número
            "",  # 9 Número final (consolidado)
            tdoc if con_doc else "",  # 10 Tipo doc cliente (tabla 2)
            ndoc if con_doc else "",  # 11 Nro doc cliente
            (self.partner_id.name or "").upper(),  # 12 Razón social
            num(b["exportacion"]),  # 13 Valor exportación
            num(b["gravado"]),  # 14 Base imponible gravada
            "0.00",  # 15 Descuento base
            num(b["igv"]),  # 16 IGV / IPM
            "0.00",  # 17 Descuento IGV
            num(b["exonerado"]),  # 18 Exonerado
            num(b["inafecto"]),  # 19 Inafecto
            "0.00",  # 20 ISC
            "0.00",  # 21 Base IVAP
            "0.00",  # 22 IVAP
            num(b["icbper"]),  # 23 Otros tributos (ICBPER)
            num(b["total"]),  # 24 Importe total
            moneda,  # 25 Moneda (tabla 4)
            "1.000"
            if moneda == "PEN"
            else "%.3f"
            % (
                1.0
                / (self.currency_id.with_context(date=self.invoice_date).rate or 1.0)
            ),  # 26 Tipo cambio
            of,  # 27 Fecha doc modificado
            ot,  # 28 Tipo doc modificado
            os_,  # 29 Serie doc modificado
            on,  # 30 Nro doc modificado
            "",  # 31 Contrato/proyecto
            "",  # 32 Error tipo 1
            "",  # 33 Indicador medio de pago
            estado,  # 34 Estado
        ]
        return "|".join(campos) + "|"

    @api.model
    def _l10n_pe_ne_ventas_periodo(self, periodo):
        """Comprobantes de venta válidos (01/03/07/08) del periodo YYYYMM, ordenados.
        Excluye borradores/rechazados; aislado por compañía. Compartido PLE + SIRE."""
        import calendar

        periodo = (periodo or "").strip()
        if len(periodo) != 6 or not periodo.isdigit():
            raise UserError(_("Periodo inválido. Usa YYYYMM (p.ej. 202606)."))
        year, month = int(periodo[:4]), int(periodo[4:6])
        if not (1 <= month <= 12):
            raise UserError(_("Mes inválido en el periodo."))
        last = calendar.monthrange(year, month)[1]
        d0 = fields.Date.to_date("%04d-%02d-01" % (year, month))
        d1 = fields.Date.to_date("%04d-%02d-%02d" % (year, month, last))
        return self.search(
            [
                ("move_type", "in", ("out_invoice", "out_refund")),
                ("state", "=", "posted"),
                ("invoice_date", ">=", d0),
                ("invoice_date", "<=", d1),
                ("l10n_pe_biller_state", "not in", ("por_enviar", "rechazado", False)),
            ],
            order="invoice_date, id",
        )

    @api.model
    def l10n_pe_ne_ple_ventas(self, periodo):
        """Genera el PLE 14.1 (Registro de Ventas) del periodo YYYYMM desde los
        comprobantes emitidos (01/03/07/08) de la compañía actual. Devuelve
        {filename, contentB64, count, periodo, total}. contentB64 = base64 del
        txt en ISO-8859-1 (lo que sube el contador al PLE de SUNAT)."""
        import base64

        periodo = (periodo or "").strip()
        moves = self._l10n_pe_ne_ventas_periodo(periodo)
        periodo8 = periodo + "00"
        lines = [m._l10n_pe_ne_ple_linea(periodo8, i) for i, m in enumerate(moves, 1)]
        content = ("\r\n".join(lines) + "\r\n") if lines else ""
        ruc = (self.env.company.vat or "").strip()
        ind_cont = "1" if lines else "0"  # contenido: con/sin información
        # LE + RUC + AAAAMM + DD(00) + 140100 + indOper(1) + indCont + moneda(1=PEN) + libro(1)
        filename = "LE%s%s00140100%s11.txt" % (ruc, periodo, "1" + ind_cont)
        return {
            "filename": filename,
            "contentB64": base64.b64encode(content.encode("latin-1", "replace")).decode(
                "ascii"
            ),
            "count": len(lines),
            "periodo": periodo,
            "total": sum(moves.mapped("amount_total")),
        }

    @api.model
    def l10n_pe_ne_dashboard(self, periodo=None):
        """Datos del dashboard de ventas del periodo (YYYYMM, default mes actual):
        serie diaria (para el gráfico), desglose por tipo de comprobante y KPIs.
        Reusa el filtro de ventas; aislado por compañía."""
        import calendar

        if not periodo:
            periodo = fields.Date.context_today(self).strftime("%Y%m")
        moves = self._l10n_pe_ne_ventas_periodo(periodo)
        year, month = int(periodo[:4]), int(periodo[4:6])
        por_dia, por_tipo = {}, {}
        total = anulados = 0.0
        for m in moves:
            key = m.invoice_date.strftime("%Y-%m-%d") if m.invoice_date else ""
            por_dia[key] = por_dia.get(key, 0.0) + (m.amount_total or 0.0)
            t = m.l10n_pe_ne_tipo_doc or m._l10n_pe_document_type()
            agg = por_tipo.setdefault(t, {"count": 0, "total": 0.0})
            agg["count"] += 1
            agg["total"] += m.amount_total or 0.0
            total += m.amount_total or 0.0
            if m.l10n_pe_biller_state == "anulado":
                anulados += 1
        ndays = calendar.monthrange(year, month)[1]
        serie = [
            {
                "dia": d,
                "total": round(
                    por_dia.get("%04d-%02d-%02d" % (year, month, d), 0.0), 2
                ),
            }
            for d in range(1, ndays + 1)
        ]
        tipos = [
            {
                "tipoDoc": t,
                "count": v["count"],
                "total": round(v["total"], 2),
            }
            for t, v in sorted(por_tipo.items())
        ]
        gastos = self.env["l10n_pe_ne.gasto"].l10n_pe_ne_total_gastos(periodo)
        return {
            "periodo": periodo,
            "total": round(total, 2),
            "count": len(moves),
            "anulados": int(anulados),
            "gastos": gastos,
            "neto": round(total - gastos, 2),
            "porDia": serie,
            "porTipo": tipos,
        }

    @api.model
    def l10n_pe_ne_reporte_ventas(self, periodo=None):
        """Reportes de ventas del periodo (YYYYMM, default mes actual): resumen,
        ventas de hoy, top por producto y top por cliente. Reusa el filtro de
        ventas; aislado por compañía. (Suma amount_total en su moneda; mezclar
        PEN/USD es aproximado para el MVP.)"""
        if not periodo:
            periodo = fields.Date.context_today(self).strftime("%Y%m")
        moves = self._l10n_pe_ne_ventas_periodo(periodo)
        today = fields.Date.context_today(self)
        prod, cli = {}, {}
        hoy_count, hoy_total = 0, 0.0
        for m in moves:
            for ln in m.invoice_line_ids:
                key = (
                    ln.product_id.display_name if ln.product_id else (ln.name or "ITEM")
                )
                a = prod.setdefault(key, {"cantidad": 0.0, "total": 0.0})
                a["cantidad"] += ln.quantity or 0.0
                a["total"] += ln.price_total or 0.0
            kc = (m.partner_id.name or "—", m.partner_id.vat or "")
            c = cli.setdefault(kc, {"count": 0, "total": 0.0})
            c["count"] += 1
            c["total"] += m.amount_total or 0.0
            if m.invoice_date == today:
                hoy_count += 1
                hoy_total += m.amount_total or 0.0
        por_producto = sorted(
            (
                {
                    "producto": k,
                    "cantidad": round(v["cantidad"], 2),
                    "total": round(v["total"], 2),
                }
                for k, v in prod.items()
            ),
            key=lambda x: -x["total"],
        )[:50]
        por_cliente = sorted(
            (
                {
                    "cliente": k[0],
                    "ruc": k[1],
                    "count": v["count"],
                    "total": round(v["total"], 2),
                }
                for k, v in cli.items()
            ),
            key=lambda x: -x["total"],
        )[:50]
        return {
            "periodo": periodo,
            "resumen": {
                "count": len(moves),
                "total": round(sum(moves.mapped("amount_total")), 2),
            },
            "hoy": {"count": hoy_count, "total": round(hoy_total, 2)},
            "porProducto": por_producto,
            "porCliente": por_cliente,
        }

    @api.model
    def l10n_pe_ne_export(self, tipo, periodo=None):
        """Centro de descargas: exporta a XLSX. tipo = ventas|productos|clientes
        (ventas usa el periodo). Devuelve {filename, contentB64, count}."""
        import base64
        import io

        import xlsxwriter

        tipo = (tipo or "ventas").strip().lower()
        if tipo == "ventas":
            if not periodo:
                periodo = fields.Date.context_today(self).strftime("%Y%m")
            moves = self._l10n_pe_ne_ventas_periodo(periodo)
            headers = [
                "Serie",
                "Número",
                "Tipo",
                "Fecha",
                "Cliente",
                "Doc. cliente",
                "Gravada",
                "Exonerada",
                "Inafecta",
                "IGV",
                "ICBPER",
                "Total",
                "Moneda",
                "Estado",
            ]
            rows = []
            for m in moves:
                b = m._l10n_pe_ne_ple_breakdown()
                serie, num = m._l10n_pe_ne_doc_id()
                rows.append(
                    [
                        serie,
                        num,
                        m.l10n_pe_ne_tipo_doc or m._l10n_pe_document_type(),
                        m.invoice_date.strftime("%d/%m/%Y") if m.invoice_date else "",
                        m.partner_id.name or "",
                        m.partner_id.vat or "",
                        round(b["gravado"], 2),
                        round(b["exonerado"], 2),
                        round(b["inafecto"], 2),
                        round(b["igv"], 2),
                        round(b["icbper"], 2),
                        round(b["total"], 2),
                        m.currency_id.name or "PEN",
                        m.l10n_pe_biller_state or "",
                    ]
                )
            sheet, base = "Ventas", "ventas-%s" % periodo
        elif tipo == "productos":
            prods = self.l10n_pe_ne_list_productos(limit=10000)
            headers = ["Código", "Descripción", "Precio", "Afectación"]
            rows = [
                [
                    p.get("codigo", ""),
                    p.get("descripcion", ""),
                    p.get("precio", 0),
                    p.get("taxCode", ""),
                ]
                for p in prods
            ]
            sheet, base = "Productos", "productos"
        elif tipo == "clientes":
            clis = self.l10n_pe_ne_list_clientes(limit=10000)
            headers = [
                "Razón social",
                "Tipo doc",
                "Número",
                "Email",
                "Teléfono",
                "Dirección",
            ]
            rows = [
                [
                    c.get("razonSocial", ""),
                    c.get("tipoDocNombre", ""),
                    c.get("numDoc", ""),
                    c.get("email", ""),
                    c.get("telefono", ""),
                    c.get("direccion", ""),
                ]
                for c in clis
            ]
            sheet, base = "Clientes", "clientes"
        else:
            raise UserError(
                _("Tipo de exporte no soportado (ventas|productos|clientes).")
            )
        buf = io.BytesIO()
        wb = xlsxwriter.Workbook(buf, {"in_memory": True})
        ws = wb.add_worksheet(sheet)
        head = wb.add_format(
            {"bold": True, "bg_color": "#2563eb", "font_color": "white", "border": 1}
        )
        for c, h in enumerate(headers):
            ws.write(0, c, h, head)
            ws.set_column(c, c, max(12, len(h) + 2))
        for r, row in enumerate(rows, 1):
            for c, val in enumerate(row):
                ws.write(r, c, val)
        ws.autofilter(0, 0, max(1, len(rows)), len(headers) - 1)
        ws.freeze_panes(1, 0)
        wb.close()
        ruc = (self.env.company.vat or "").strip()
        return {
            "filename": "%s-%s.xlsx" % (base, ruc),
            "count": len(rows),
            "contentB64": base64.b64encode(buf.getvalue()).decode("ascii"),
        }

    @api.model
    def l10n_pe_ne_rvie_reemplazo(self, periodo):
        """SIRE — archivo de REEMPLAZO de la propuesta del RVIE (Registro de Ventas)
        del periodo YYYYMM, empaquetado en ZIP, para que el contador reemplace la
        propuesta de SUNAT. Reusa el motor de líneas del PLE (mismo desglose).

        Nombre SIRE (35 chars): LE + RUC + AAAA + MM + 00 + 140000(libro RVIE) +
        02(reemplazo) + O(operaciones) + I(contenido) + M(moneda) + 2(fijo) + NN(secuencia).
        Devuelve {zipFilename, txtFilename, contentB64 (zip base64), count, total}.
        OJO: el layout EXACTO de campos del Anexo 3 se valida/ajusta contra el PVSIRE."""
        import base64
        import io
        import zipfile

        periodo = (periodo or "").strip()
        moves = self._l10n_pe_ne_ventas_periodo(periodo)
        periodo8 = periodo + "00"
        lines = [m._l10n_pe_ne_ple_linea(periodo8, i) for i, m in enumerate(moves, 1)]
        content = ("\r\n".join(lines) + "\r\n") if lines else ""
        ruc = (self.env.company.vat or "").strip()
        cont = "1" if lines else "0"  # I: con/sin información
        # LE+RUC+AAAAMM+00 + 140000(libro RVIE) + 02(reemplazo) + O=1 + I=cont + M=1(soles) + 2 + NN=00
        txt_name = "LE%s%s00140000021%s1200.txt" % (ruc, periodo, cont)
        zip_name = txt_name[:-4] + ".zip"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(txt_name, content.encode("latin-1", "replace"))
        return {
            "zipFilename": zip_name,
            "txtFilename": txt_name,
            "contentB64": base64.b64encode(buf.getvalue()).decode("ascii"),
            "count": len(lines),
            "periodo": periodo,
            "total": sum(moves.mapped("amount_total")),
        }

    def _l10n_pe_ne_quick_product(self, ln, tax=None):
        """Resuelve el product.product de una línea para que el documento USE un registro de Odoo:
        busca por id, por código (default_code) o por nombre exacto; si no existe y hay datos, lo
        CREA simplificado y lo enlaza (igual que el cliente por vat). Devuelve recordset vacío si la
        línea no aporta nada por lo que crear (queda como texto libre, compatible hacia atrás)."""
        Product = self.env["product.product"]
        pid = ln.get("productId")
        if pid:
            prod = Product.browse(int(pid)).exists()
            if prod:
                return prod
        cod = (ln.get("productCod") or ln.get("codProducto") or "").strip()
        if cod:
            found = Product.search([("default_code", "=", cod)], limit=1)
            if found:
                return found
        desc = (ln.get("descripcion") or "").strip()
        if desc:
            found = Product.search([("name", "=", desc)], limit=1)
            if found:
                return found
        if not (cod or desc):
            return Product.browse()
        vals = {
            "name": desc or cod or "PRODUCTO",
            "type": "service",
            "sale_ok": True,
            "list_price": float(ln.get("precioUnitario") or 0),
            # company_id del emisor: aísla el producto por RUC (igual que el cliente).
            "company_id": self.env.company.id,
        }
        if cod:
            vals["default_code"] = cod
        bc = (ln.get("barcode") or "").strip()
        if bc:
            vals["barcode"] = bc
        if tax:
            vals["taxes_id"] = [(6, 0, tax.ids)]
        return Product.create(vals)

    def _l10n_pe_ne_product_dict(self, p):
        tax = p.taxes_id.filtered(lambda t: t.type_tax_use == "sale")[:1]
        return {
            "id": p.id,
            "descripcion": p.name or "",
            "codigo": p.default_code or "",
            "barcode": p.barcode or "",
            "precio": p.list_price,
            "taxCode": (tax.l10n_pe_edi_tax_code or "1000") if tax else "1000",
        }

    def _l10n_pe_ne_partner_dict(self, p):
        return {
            "id": p.id,
            "razonSocial": p.name or "",
            "numDoc": p.vat or "",
            "tipoDoc": p.l10n_latam_identification_type_id.l10n_pe_vat_code or "",
            "tipoDocNombre": p.l10n_latam_identification_type_id.name or "",
            "email": p.email or "",
            "telefono": p.phone or "",
            "direccion": p.street or "",
        }

    def _l10n_pe_ne_ident_type(self, tipoDoc):
        return self.env["l10n_latam.identification.type"].search(
            [("l10n_pe_vat_code", "=", tipoDoc or "6")], limit=1
        )

    def _l10n_pe_ne_partner_apply(self, p, c):
        """Aplica los campos simplificados (los del caso común de facturación) a un res.partner."""
        vals = {}
        if c.get("razonSocial"):
            vals["name"] = c["razonSocial"]
        if c.get("numDoc") is not None:
            vals["vat"] = (c.get("numDoc") or "").strip() or False
        if c.get("tipoDoc"):
            t = self._l10n_pe_ne_ident_type(c["tipoDoc"])
            if t:
                vals["l10n_latam_identification_type_id"] = t.id
        for key, field in (
            ("email", "email"),
            ("telefono", "phone"),
            ("direccion", "street"),
        ):
            if key in c:
                vals[field] = c.get(key) or False
        if vals:
            p.write(vals)
        return p

    @api.model
    def l10n_pe_ne_list_clientes(self, query=None, limit=50, offset=None):
        """Clientes de Odoo para que React liste/autocomplete (no reinventa el padrón).

        Paginación opt-in: con `offset` (aunque sea 0) devuelve el envelope
        {items, total}; sin `offset` (None) devuelve la lista plana de siempre
        —así el autocomplete del POS/Emitir sigue recibiendo un array."""
        domain = [("customer_rank", ">", 0)]
        if query:
            domain = [
                "&",
                ("customer_rank", ">", 0),
                "|",
                ("name", "ilike", query),
                ("vat", "ilike", query),
            ]
        Partner = self.env["res.partner"]
        parts = Partner.search(domain, order="name", limit=limit, offset=offset or 0)
        items = [self._l10n_pe_ne_partner_dict(p) for p in parts]
        if offset is None:
            return items
        return {"items": items, "total": Partner.search_count(domain)}

    @api.model
    def l10n_pe_ne_create_cliente(self, cliente):
        """Crea (o reusa por vat) un cliente con los campos PE correctos; lo guarda EN Odoo."""
        cliente = cliente or {}
        p = self._l10n_pe_ne_quick_partner(cliente)
        self._l10n_pe_ne_partner_apply(p, cliente)
        if not p.customer_rank:
            p.customer_rank = 1
        return self._l10n_pe_ne_partner_dict(p)

    @api.model
    def l10n_pe_ne_update_cliente(self, cliente):
        """Actualiza un cliente existente (por id) con los campos simplificados."""
        cliente = cliente or {}
        p = self.env["res.partner"].browse(int(cliente.get("id") or 0)).exists()
        if not p:
            raise UserError(_("Cliente no encontrado."))
        self._l10n_pe_ne_partner_apply(p, cliente)
        return self._l10n_pe_ne_partner_dict(p)

    @api.model
    def l10n_pe_ne_delete_cliente(self, rec_id):
        """Elimina el cliente; si está referenciado (comprobantes), lo archiva en su lugar."""
        p = self.env["res.partner"].browse(int(rec_id or 0)).exists()
        if not p:
            return {"ok": True, "modo": "inexistente"}
        try:
            p.unlink()
            return {"ok": True, "modo": "eliminado"}
        except Exception:
            p.active = False
            return {"ok": True, "modo": "archivado"}

    @api.model
    def l10n_pe_ne_list_productos(self, query=None, limit=50, offset=None):
        """Productos de Odoo para que React liste/autocomplete y los documentos los referencien.
        Busca por nombre, código interno (default_code) o código de barras (barcode).

        Paginación opt-in: con `offset` devuelve {items, total}; sin él, lista plana."""
        domain = [("sale_ok", "=", True)]
        if query:
            domain = [
                "&",
                ("sale_ok", "=", True),
                "|",
                "|",
                ("name", "ilike", query),
                ("default_code", "ilike", query),
                ("barcode", "ilike", query),
            ]
        Product = self.env["product.product"]
        prods = Product.search(domain, order="name", limit=limit, offset=offset or 0)
        items = [self._l10n_pe_ne_product_dict(p) for p in prods]
        if offset is None:
            return items
        return {"items": items, "total": Product.search_count(domain)}

    @api.model
    def l10n_pe_ne_producto_por_barcode(self, code):
        """Resuelve UN producto por código de barras exacto (para el escaneo en el POS).
        Devuelve el dict del producto o None si no hay coincidencia. Aislado por compañía."""
        code = (code or "").strip()
        if not code:
            return None
        p = self.env["product.product"].search(
            [("sale_ok", "=", True), ("barcode", "=", code)], limit=1
        )
        return self._l10n_pe_ne_product_dict(p) if p else None

    @api.model
    def l10n_pe_ne_create_producto(self, producto):
        """Crea (o reusa por código/nombre) un producto simplificado; lo guarda EN Odoo."""
        _logger.info("l10n_pe_ne_create_producto: %s", producto)
        producto = producto or {}
        desc = producto.get("descripcion") or producto.get("nombre")
        _logger.info("desc: %s", desc)
        if not desc and not producto.get("codigo"):
            raise UserError(
                _("El producto necesita al menos una descripción o un código.")
            )
        tax = self._l10n_pe_ne_tax_by_code(producto.get("taxCode") or "1000")
        _logger.info("tax: %s", tax)
        p = self._l10n_pe_ne_quick_product(
            {
                "descripcion": desc,
                "productCod": producto.get("codigo"),
                "barcode": producto.get("barcode"),
                "precioUnitario": producto.get("precio"),
            },
            tax,
        )
        _logger.info("p: %s", p)
        return self._l10n_pe_ne_product_dict(p)

    @api.model
    def l10n_pe_ne_update_producto(self, producto):
        """Actualiza un producto (por id): descripción, código, precio e impuesto (afectación)."""
        producto = producto or {}
        p = self.env["product.product"].browse(int(producto.get("id") or 0)).exists()
        if not p:
            raise UserError(_("Producto no encontrado."))
        vals = {}
        if producto.get("descripcion"):
            vals["name"] = producto["descripcion"]
        if "codigo" in producto:
            vals["default_code"] = (producto.get("codigo") or "").strip() or False
        if "barcode" in producto:
            vals["barcode"] = (producto.get("barcode") or "").strip() or False
        if producto.get("precio") is not None:
            vals["list_price"] = float(producto.get("precio") or 0)
        if producto.get("taxCode"):
            tax = self._l10n_pe_ne_tax_by_code(producto["taxCode"])
            vals["taxes_id"] = [(6, 0, tax.ids if tax else [])]
        if vals:
            p.write(vals)
        return self._l10n_pe_ne_product_dict(p)

    @api.model
    def l10n_pe_ne_delete_producto(self, rec_id):
        """Elimina el producto; si está referenciado (en comprobantes), lo archiva en su lugar."""
        p = self.env["product.product"].browse(int(rec_id or 0)).exists()
        if not p:
            return {"ok": True, "modo": "inexistente"}
        try:
            p.unlink()
            return {"ok": True, "modo": "eliminado"}
        except Exception:
            p.active = False
            return {"ok": True, "modo": "archivado"}

    # ----------------------------------------------------------------- compras
    # Compra = factura de proveedor (account.move in_invoice). TODA la lógica en
    # Odoo; reusa el patrón de in_invoice de retención (campos l10n_latam que PE
    # exige). Aislado por compañía vía reglas multi-compañía nativas de account.move.
    def _l10n_pe_ne_compra_dict(self):
        self.ensure_one()
        return {
            "id": self.id,
            "fecha": self.invoice_date.strftime("%Y-%m-%d")
            if self.invoice_date
            else "",
            "documento": self.l10n_latam_document_number or self.ref or "",
            "tipoComprobante": self.l10n_latam_document_type_id.code
            if self.l10n_latam_document_type_id
            else "",
            "proveedor": self.partner_id.name or "",
            "ruc": self.partner_id.vat or "",
            "total": self.amount_total or 0.0,
            "moneda": self.currency_id.name or "PEN",
            "estado": self.state,
            # Descripción = nombre de la línea (para prefill al editar; la línea de
            # detalle simple lleva la descripción original o "COMPRA").
            "descripcion": (self.invoice_line_ids[:1].name or "")
            if self.invoice_line_ids
            else "",
        }

    @api.model
    def l10n_pe_ne_list_compras(self, query=None, periodo=None, limit=200, offset=None):
        """Lista de compras (facturas de proveedor) — opcional por texto o periodo.

        Paginación opt-in: con `offset` devuelve {items, total}; sin él, lista plana."""
        import calendar

        domain = [("move_type", "=", "in_invoice")]
        if query:
            domain += [
                "|",
                ("partner_id.name", "ilike", query),
                ("l10n_latam_document_number", "ilike", query),
            ]
        if periodo and len(str(periodo)) == 6 and str(periodo).isdigit():
            y, m = int(periodo[:4]), int(periodo[4:6])
            last = calendar.monthrange(y, m)[1]
            domain += [
                ("invoice_date", ">=", "%04d-%02d-01" % (y, m)),
                ("invoice_date", "<=", "%04d-%02d-%02d" % (y, m, last)),
            ]
        recs = self.search(
            domain, order="invoice_date desc, id desc", limit=limit, offset=offset or 0
        )
        items = [m._l10n_pe_ne_compra_dict() for m in recs]
        if offset is None:
            return items
        return {"items": items, "total": self.search_count(domain)}

    @api.model
    def l10n_pe_ne_create_compra(self, compra):
        """Registra una compra (factura de proveedor). payload: {proveedor:{numDoc,
        razonSocial,tipoDoc}, tipoComprobante(cat.10), serie, numero, fecha, total,
        descripcion, moneda}. Registro simple (línea = total); el IGV/crédito fiscal
        detallado queda para una iteración posterior."""
        compra = compra or {}
        prov = self._l10n_pe_ne_quick_partner(compra.get("proveedor") or {})
        if not prov.supplier_rank:
            prov.supplier_rank = 1
        journal = self.env["account.journal"].search(
            [("type", "=", "purchase"), ("company_id", "=", self.env.company.id)],
            limit=1,
        )
        if not journal:
            raise UserError(_("No hay diario de compras configurado para la compañía."))
        serie = (compra.get("serie") or "").strip()
        numero = (compra.get("numero") or "").strip()
        doc_num = ("%s-%s" % (serie, numero)) if serie and numero else (numero or serie)
        if not doc_num:
            raise UserError(_("Indica el número del documento del proveedor."))
        total = float(compra.get("total") or 0)
        if total <= 0:
            raise UserError(_("Indica el monto total de la compra."))
        vals = {
            "move_type": "in_invoice",
            "partner_id": prov.id,
            "journal_id": journal.id,
            "invoice_date": compra.get("fecha") or fields.Date.context_today(self),
            "ref": doc_num,
            "l10n_latam_document_number": doc_num,
            "invoice_line_ids": [
                (
                    0,
                    0,
                    {
                        "name": compra.get("descripcion") or "COMPRA",
                        "quantity": 1,
                        "price_unit": total,
                        "tax_ids": [(6, 0, [])],
                    },
                )
            ],
        }
        moneda = self._l10n_pe_ne_quick_currency(compra.get("moneda"))
        if moneda:
            vals["currency_id"] = moneda.id
        dt = self.env["l10n_latam.document.type"].search(
            [
                ("code", "=", compra.get("tipoComprobante") or "01"),
                ("country_id.code", "=", "PE"),
            ],
            limit=1,
        )
        if dt:
            vals["l10n_latam_document_type_id"] = dt.id
        move = self.create(vals)
        move.action_post()
        return move._l10n_pe_ne_compra_dict()

    @api.model
    def l10n_pe_ne_update_compra(self, rec_id, compra):
        """Actualiza una compra existente: la pasa a borrador, reescribe cabecera y
        la línea única, y la vuelve a postear. Mismas validaciones que el alta."""
        m = self.browse(int(rec_id or 0)).exists()
        if not m or m.move_type != "in_invoice":
            raise UserError(_("Compra no encontrada."))
        compra = compra or {}
        prov = self._l10n_pe_ne_quick_partner(compra.get("proveedor") or {})
        if not prov.supplier_rank:
            prov.supplier_rank = 1
        serie = (compra.get("serie") or "").strip()
        numero = (compra.get("numero") or "").strip()
        doc_num = ("%s-%s" % (serie, numero)) if serie and numero else (numero or serie)
        if not doc_num:
            raise UserError(_("Indica el número del documento del proveedor."))
        total = float(compra.get("total") or 0)
        if total <= 0:
            raise UserError(_("Indica el monto total de la compra."))
        if m.state == "posted":
            m.button_draft()
        vals = {
            "partner_id": prov.id,
            "invoice_date": compra.get("fecha") or m.invoice_date,
            "ref": doc_num,
            "l10n_latam_document_number": doc_num,
            "invoice_line_ids": [
                (5, 0, 0),
                (
                    0,
                    0,
                    {
                        "name": compra.get("descripcion") or "COMPRA",
                        "quantity": 1,
                        "price_unit": total,
                        "tax_ids": [(6, 0, [])],
                    },
                ),
            ],
        }
        moneda = self._l10n_pe_ne_quick_currency(compra.get("moneda"))
        if moneda:
            vals["currency_id"] = moneda.id
        dt = self.env["l10n_latam.document.type"].search(
            [
                ("code", "=", compra.get("tipoComprobante") or "01"),
                ("country_id.code", "=", "PE"),
            ],
            limit=1,
        )
        if dt:
            vals["l10n_latam_document_type_id"] = dt.id
        m.write(vals)
        m.action_post()
        return m._l10n_pe_ne_compra_dict()

    @api.model
    def l10n_pe_ne_delete_compra(self, rec_id):
        """Elimina la compra; si está posteada, la pasa a borrador y elimina; si no
        se puede, la anula (cancel)."""
        m = self.browse(int(rec_id or 0)).exists()
        if not m or m.move_type != "in_invoice":
            return {"ok": True, "modo": "inexistente"}
        try:
            if m.state == "posted":
                m.button_draft()
            m.unlink()
            return {"ok": True, "modo": "eliminado"}
        except Exception:
            m.button_cancel()
            return {"ok": True, "modo": "anulado"}

    def _l10n_pe_ne_quick_flags(self, move, payload):
        d = payload.get("detraccion")
        if d:
            move.l10n_pe_ne_detraccion = True
            move.l10n_pe_ne_detraccion_code = d.get("codBien") or "037"
            move.l10n_pe_ne_detraccion_rate = float(d.get("tasa") or 12)
            if d.get("medioPago"):
                move.l10n_pe_ne_detraccion_medio_pago = d["medioPago"]
            if d.get("cuentaBN") and not move.company_id.l10n_pe_ne_cuenta_detraccion:
                move.company_id.sudo().l10n_pe_ne_cuenta_detraccion = d["cuentaBN"]
        p = payload.get("percepcion")
        if p:
            move.l10n_pe_ne_percepcion = True
            move.l10n_pe_ne_percepcion_rate = float(p.get("tasa") or 2)
        a = payload.get("anticipo")
        if a:
            move.l10n_pe_ne_anticipo_total = float(a.get("total") or 0)
            move.l10n_pe_ne_anticipo_doc = a.get("doc") or ""
            if a.get("tipo"):
                move.l10n_pe_ne_anticipo_tipo = a["tipo"]
        # Forma de pago: Crédito (con cuotas) emite cac:PaymentTerms; medios de pago
        # (efectivo/Yape/…) se guardan como dato interno del POS (no van al XML SUNAT).
        fp = payload.get("formaPago") or {}
        if fp.get("tipo") == "Credito" or fp.get("cuotas"):
            move.l10n_pe_ne_forma_pago = "Credito"
            move.l10n_pe_ne_cuotas = fp.get("cuotas") or []
            venc = (fp.get("cuotas") or [{}])[-1].get("fecha")
            if venc:
                move.invoice_date_due = venc
        if fp.get("medios"):
            move.l10n_pe_ne_medios_pago = fp.get("medios")

    def l10n_pe_ne_quick_result(self):
        self.ensure_one()
        serie, corr = self._l10n_pe_serie_correlativo()
        m = re.search(r"ResponseCode (\d+)", self.l10n_pe_biller_message or "")
        return {
            "id": self.id,
            "tipoDoc": self.l10n_pe_ne_tipo_doc or self._l10n_pe_document_type(),
            "serie": self.l10n_pe_ne_serie_emit or serie,
            "correlativo": (self.l10n_pe_ne_corr_emit or corr).zfill(8),
            "estado": self.l10n_pe_biller_state,
            "responseCode": m.group(1) if m else "",
            "mensaje": self.l10n_pe_biller_message or "",
            "total": self.amount_total,
            "cliente": self.partner_id.name or "",
            "fechaEmision": self.invoice_date.strftime("%Y-%m-%d")
            if self.invoice_date
            else "",
        }

    @api.model
    def l10n_pe_ne_quick_list(self, query=None, desde=None, hasta=None, estado=None, tipo=None,
                              forma_pago=None, monto_min=None, monto_max=None, limit=100, offset=None):
        """Lista de comprobantes emitidos (sin los blobs), para la UI. Filtros
        opcionales: query (cliente/RUC/correlativo), rango de fechas (desde/hasta),
        estado del facturador (por_enviar/en_proceso/enviado/anulado/rechazado/error),
        tipo de comprobante (01/03/07/08), forma de pago (Contado/Credito) y rango de
        monto total (monto_min/monto_max). `estado` y `tipo` aceptan varios valores
        (lista o CSV "a,b") → filtran con `in` (multiselect en la UI).

        Paginación opt-in: con `offset` devuelve {items, total} (total vía
        search_count sobre el mismo dominio); sin él, la lista plana de siempre."""
        def _as_list(v):
            if not v:
                return None
            vals = [x for x in v.split(",") if x] if isinstance(v, str) else list(v)
            return vals or None

        def _num(v):
            try:
                return float(v) if v not in (None, "") else None
            except (TypeError, ValueError):
                return None
        estados = _as_list(estado)
        tipos = _as_list(tipo)
        mmin, mmax = _num(monto_min), _num(monto_max)
        # Se incluyen los 'por_enviar' (pendientes de envío) para que sean visibles y
        # reenviables desde la UI; antes se excluían y quedaban sin dónde verse.
        domain = [("l10n_pe_biller_state", "!=", False)]
        if estados:
            domain.append(("l10n_pe_biller_state", "in", estados))
        if tipos:
            domain.append(("l10n_pe_ne_tipo_doc", "in", tipos))
        if forma_pago:
            domain.append(("l10n_pe_ne_forma_pago", "=", forma_pago))
        if mmin is not None:
            domain.append(("amount_total", ">=", mmin))
        if mmax is not None:
            domain.append(("amount_total", "<=", mmax))
        if desde:
            domain.append(("invoice_date", ">=", desde))
        if hasta:
            domain.append(("invoice_date", "<=", hasta))
        if query:
            q = query.strip()
            domain += [
                "|",
                "|",
                ("partner_id.name", "ilike", q),
                ("partner_id.vat", "ilike", q),
                ("l10n_pe_ne_corr_emit", "ilike", q),
            ]
        moves = self.search(domain, order="id desc", limit=limit, offset=offset or 0)
        items = [
            {
                "id": m.id,
                "tipoDoc": m.l10n_pe_ne_tipo_doc or m._l10n_pe_document_type(),
                "serie": m.l10n_pe_ne_serie_emit or m.l10n_pe_serie or "",
                "correlativo": m.l10n_pe_ne_corr_emit or "",
                "estado": m.l10n_pe_biller_state,
                "total": m.amount_total,
                "moneda": m.currency_id.name or "PEN",
                "cliente": m.partner_id.name or "",
                "fechaEmision": m.invoice_date.strftime("%Y-%m-%d")
                if m.invoice_date
                else "",
                # Hora de creación del comprobante (≈ emisión), en tz local (Lima).
                "hora": fields.Datetime.context_timestamp(m, m.create_date).strftime("%H:%M")
                if m.create_date
                else "",
                "mensaje": m.l10n_pe_biller_message or "",
            }
            for m in moves
        ]
        if offset is None:
            return items
        return {"items": items, "total": self.search_count(domain)}

    def l10n_pe_ne_comprobante_detalle(self):
        """Detalle completo de un comprobante para la vista de detalle (cabecera +
        líneas + totales por afectación + estado SUNAT). Todo calculado en Odoo."""
        self.ensure_one()
        b = self._l10n_pe_ne_ple_breakdown()
        serie, num = self._l10n_pe_ne_doc_id()
        lineas = []
        for ln in self.invoice_line_ids:
            codes = ln.tax_ids.mapped("l10n_pe_edi_tax_code")
            afect = next(
                (c for c in ("9997", "9998", "9995", "9996") if c in codes), "1000"
            )
            lineas.append(
                {
                    "descripcion": ln.name or "",
                    "cantidad": ln.quantity or 0.0,
                    "precio": ln.price_unit or 0.0,
                    "descuento": ln.discount or 0.0,
                    "afectacion": afect,
                    "subtotal": ln.price_subtotal or 0.0,
                }
            )
        of, ot, os_, on = self._l10n_pe_ne_ple_origen()
        return {
            "id": self.id,
            "tipoDoc": self.l10n_pe_ne_tipo_doc or self._l10n_pe_document_type(),
            "serie": serie,
            "correlativo": num,
            "fecha": self.invoice_date.strftime("%Y-%m-%d")
            if self.invoice_date
            else "",
            "cliente": self.partner_id.name or "",
            "clienteDoc": self.partner_id.vat or "",
            "moneda": self.currency_id.name or "PEN",
            "estado": self.l10n_pe_biller_state or "",
            "mensaje": self.l10n_pe_biller_message or "",
            "formaPago": self.l10n_pe_ne_forma_pago or "Contado",
            "docOrigen": ("%s %s-%s" % (ot, os_, on)) if on else "",
            "lineas": lineas,
            "totales": {
                "gravada": round(b["gravado"], 2),
                "exonerada": round(b["exonerado"], 2),
                "inafecta": round(b["inafecto"], 2),
                "igv": round(b["igv"], 2),
                "icbper": round(b["icbper"], 2),
                "total": round(b["total"], 2),
            },
        }

    def l10n_pe_ne_get_files(self, kind=None):
        """Devuelve {xml, cdr, pdf[, ticket]} en base64 del comprobante, para que el BFF los sirva
        (sin /web/content). `ticket` (80mm) solo se incluye cuando kind == 'ticket' — así una descarga
        normal no dispara el render del ticket."""
        self.ensure_one()

        def b64(att):
            v = att.datas
            return v.decode("ascii") if isinstance(v, (bytes, bytearray)) else (v or "")

        out = {}
        if self.l10n_pe_biller_xml:
            out["xml"] = b64(self.l10n_pe_biller_xml)
        if self.l10n_pe_biller_cdr:
            out["cdr"] = b64(self.l10n_pe_biller_cdr)
        try:
            pdf_att = (
                self._l10n_pe_get_pdf_attachment() if self.l10n_pe_biller_xml else False
            )
            if pdf_att:
                out["pdf"] = b64(pdf_att)
        except Exception:
            pass
        if kind == "ticket":
            try:
                t = self._l10n_pe_get_pdf_attachment(formato="TICKET") if self.l10n_pe_biller_xml else False
                if t:
                    out["ticket"] = b64(t)
            except Exception:
                pass
        return out

    # ------------------------------------------------- descargas / PDF (SFS 2.4)
    @staticmethod
    def _l10n_pe_download_url(attachment):
        """Acción de descarga directa del adjunto vía /web/content."""
        return {
            "type": "ir.actions.act_url",
            "url": "/web/content/%s?download=true" % attachment.id,
            "target": "self",
        }

    def _l10n_pe_get_pdf_attachment(self, formato="A4"):
        """Devuelve (o genera y cachea) el PDF de la representación impresa pidiéndolo al micro
        (POST /report/pdf con el XML firmado). El micro lo renderiza con las plantillas del SFS 2.4.
        formato: 'A4' (SFS 2.4) o 'TICKET' (80mm, solo 01/03; otros tipos caen al A4)."""
        self.ensure_one()
        tipo, serie, correlativo = self._l10n_pe_baja_identidad()
        es_ticket = formato == "TICKET" and tipo in ("01", "03")
        if formato == "TICKET" and not es_ticket:
            return self._l10n_pe_get_pdf_attachment()  # fallback A4 (NC/ND/retención…)
        cache_field = "l10n_pe_biller_pdf_ticket" if es_ticket else "l10n_pe_biller_pdf"
        if self[cache_field]:
            return self[cache_field]
        if not self.l10n_pe_biller_xml:
            raise UserError(
                _("El comprobante no tiene XML firmado; envíelo primero a SUNAT.")
            )
        icp = self.env["ir.config_parameter"].sudo()
        base = icp.get_param("l10n_pe_ne_biller.url", "http://localhost:8090").rstrip(
            "/"
        )
        # Clave propia: reusar l10n_pe_ne_biller.timeout hacía que subir el
        # timeout de emisión (240s) arrastrara también la espera de un PDF.
        timeout = int(icp.get_param("l10n_pe_ne_biller.pdf_timeout", "60"))
        payload = {
            "ruc": self.company_id.vat or "",
            "tipoDoc": tipo,
            "xml": (self.l10n_pe_biller_xml.raw or b"").decode("utf-8"),
        }
        if es_ticket:
            payload["formato"] = "TICKET"
        headers = {"X-Api-Key": self.company_id.sudo().l10n_pe_ne_api_key or ""}
        try:
            resp = requests.post(
                base + "/report/pdf",
                json=payload,
                headers=headers,
                timeout=(5, timeout),
            )
        except requests.RequestException as exc:
            raise UserError(_("Error de conexión con el facturador: %s") % exc)
        if resp.status_code != 200 or not resp.content.startswith(b"%PDF"):
            raise UserError(
                _("El facturador no devolvió un PDF (HTTP %s): %s")
                % (resp.status_code, (resp.text or "")[:500])
            )
        att = self.env["ir.attachment"].create(
            {
                "name": "%s-%s-%s%s.pdf"
                % (
                    self.company_id.vat or "",
                    serie,
                    correlativo.zfill(8),
                    "-ticket" if es_ticket else "",
                ),
                "res_model": "account.move",
                "res_id": self.id,
                "mimetype": "application/pdf",
                "raw": resp.content,
            }
        )
        self[cache_field] = att.id
        return att

    def _l10n_pe_ne_is_aceptado(self):
        """True solo si el comprobante fue aceptado por SUNAT: estado 'enviado',
        con CDR guardado y ResponseCode 0. Re-parsea el CDR (no confía en el texto
        de l10n_pe_biller_message)."""
        self.ensure_one()
        if self.l10n_pe_biller_state != "enviado" or not self.l10n_pe_biller_cdr:
            return False
        code, _desc = self._l10n_pe_parse_cdr_codes(self.l10n_pe_biller_cdr.raw or b"")
        return code == "0"

    def l10n_pe_ne_email_comprobante(self, to=None, cc=None):
        """Envía el comprobante aceptado al cliente por correo, adjuntando el PDF
        (representación impresa SFS) y el XML firmado, vía la plantilla
        l10n_pe_ne_biller.mail_template_comprobante."""
        self.ensure_one()
        if not self._l10n_pe_ne_is_aceptado():
            raise UserError(
                _("El comprobante no está aceptado por SUNAT; no se puede enviar.")
            )
        to = (to or self.partner_id.email or "").strip()
        if not to:
            raise UserError(
                _("El cliente no tiene correo configurado; indica un destinatario.")
            )
        pdf = self._l10n_pe_get_pdf_attachment()
        xml = self.l10n_pe_biller_xml
        template = self.env.ref("l10n_pe_ne_biller.mail_template_comprobante")
        template.send_mail(
            self.id,
            force_send=True,
            raise_exception=True,
            email_values={
                "email_to": to,
                "email_cc": cc or "",
                "attachment_ids": [(6, 0, [pdf.id, xml.id])],
            },
        )
        self.message_post(body=_("Comprobante enviado por correo a %s") % to)
        return {"ok": True, "to": to}

    def action_l10n_pe_download_pdf(self):
        self.ensure_one()
        return self._l10n_pe_download_url(self._l10n_pe_get_pdf_attachment())

    def action_l10n_pe_download_ticket(self):
        self.ensure_one()
        return self._l10n_pe_download_url(self._l10n_pe_get_pdf_attachment(formato="TICKET"))

    def action_l10n_pe_download_xml(self):
        self.ensure_one()
        if not self.l10n_pe_biller_xml:
            raise UserError(_("El comprobante no tiene XML firmado."))
        return self._l10n_pe_download_url(self.l10n_pe_biller_xml)

    def action_l10n_pe_download_cdr(self):
        self.ensure_one()
        if not self.l10n_pe_biller_cdr:
            raise UserError(_("El comprobante no tiene CDR de SUNAT."))
        return self._l10n_pe_download_url(self.l10n_pe_biller_cdr)

    def action_l10n_pe_download_zip(self):
        """Empaqueta en un ZIP el XML firmado, el CDR y el PDF de los comprobantes seleccionados.
        El PDF se genera al vuelo (best-effort) si aún no existe."""
        buf = io.BytesIO()
        incluidos = 0
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for move in self:
                if move.l10n_pe_biller_xml:
                    zf.writestr(
                        move.l10n_pe_biller_xml.name, move.l10n_pe_biller_xml.raw or b""
                    )
                    incluidos += 1
                if move.l10n_pe_biller_cdr:
                    zf.writestr(
                        move.l10n_pe_biller_cdr.name, move.l10n_pe_biller_cdr.raw or b""
                    )
                    incluidos += 1
                try:
                    pdf = (
                        move._l10n_pe_get_pdf_attachment()
                        if move.l10n_pe_biller_xml
                        else False
                    )
                    if pdf:
                        zf.writestr(pdf.name, pdf.raw or b"")
                        incluidos += 1
                except UserError as exc:
                    # PDF best-effort: si el micro falla, el ZIP igual lleva XML + CDR; se registra el motivo.
                    _logger.warning(
                        "No se pudo generar el PDF de %s para el ZIP: %s",
                        move.name,
                        exc,
                    )
        if not incluidos:
            raise UserError(
                _("Los comprobantes seleccionados no tienen archivos para descargar.")
            )
        att = self.env["ir.attachment"].create(
            {
                "name": "comprobantes_sunat.zip",
                "mimetype": "application/zip",
                "raw": buf.getvalue(),
            }
        )
        return self._l10n_pe_download_url(att)

    # ---------------------------------------------- comunicación de baja (RA)
    @api.depends(
        "l10n_pe_ne_baja_fecha", "l10n_pe_ne_baja_correlativo", "l10n_pe_ne_tipo_doc"
    )
    def _compute_l10n_pe_ne_baja_doc(self):
        for m in self:
            if m.l10n_pe_ne_baja_fecha and m.l10n_pe_ne_baja_correlativo:
                # Boletas (03) se anulan por Resumen Diario (RC); el resto por Comunicación de Baja (RA).
                prefijo = "RC" if m.l10n_pe_ne_tipo_doc == "03" else "RA"
                m.l10n_pe_ne_baja_doc = "%s-%s-%s" % (
                    prefijo,
                    m.l10n_pe_ne_baja_fecha.strftime("%Y%m%d"),
                    m.l10n_pe_ne_baja_correlativo,
                )
            else:
                m.l10n_pe_ne_baja_doc = False

    def _l10n_pe_baja_identidad(self):
        """(tipo, serie, correlativo) realmente emitidos. Usa lo congelado al enviar; si falta (comprobante
        enviado por una versión previa), recae en el cálculo del momento."""
        self.ensure_one()
        tipo = self.l10n_pe_ne_tipo_doc or self._l10n_pe_document_type()
        serie = self.l10n_pe_ne_serie_emit or self._l10n_pe_serie_correlativo()[0]
        correlativo = self.l10n_pe_ne_corr_emit or self._l10n_pe_serie_correlativo()[1]
        return tipo, serie, correlativo

    def _l10n_pe_check_baja(self):
        """Guardas de la comunicación de baja: solo factura/NC/ND ya enviadas, con serie válida, motivo,
        fecha y (para facturas) dentro del plazo de 7 días."""
        self.ensure_one()
        if self.l10n_pe_biller_state not in ("enviado", "anulado"):
            raise UserError(
                _(
                    "Solo puede comunicarse la baja de un comprobante ya enviado a SUNAT."
                )
            )
        tipo, serie, _corr = self._l10n_pe_baja_identidad()
        if tipo not in ("01", "03", "07", "08"):
            raise UserError(
                _(
                    "La anulación aplica a factura, boleta, nota de crédito y nota de débito."
                )
            )
        # Serie con prefijo B (boleta) / F / S, o numérica: refleja el formato del comprobante emitido.
        if not re.match(r"^([BFS][A-Z0-9]{3}|\d{1,4})$", serie or ""):
            raise UserError(
                _(
                    "La serie del comprobante (%s) no tiene un formato válido para la anulación."
                )
                % serie
            )
        if not (self.l10n_pe_ne_baja_motivo or "").strip():
            raise UserError(_("Indique el motivo de la baja."))
        if not self.invoice_date:
            raise UserError(_("El comprobante no tiene fecha de emisión."))
        # Plazo de 7 días calendario para anular una FACTURA por baja (las NC/ND no tienen este límite).
        if tipo == "01":
            limite = int(
                self.env["ir.config_parameter"]
                .sudo()
                .get_param("l10n_pe_ne_biller.baja_plazo_dias", "7")
            )
            dias = (fields.Date.context_today(self) - self.invoice_date).days
            if dias > limite:
                raise UserError(
                    _(
                        "Fuera del plazo de baja: han pasado %s días desde la emisión de la factura "
                        "(máximo %s días calendario). Para anularla, emita una nota de crédito."
                    )
                    % (dias, limite)
                )
        # Boleta mayor a S/ 700 exige el documento de identidad del adquirente (igual que en su emisión).
        if (
            tipo == "03"
            and (self.amount_total or 0.0) > 700
            and not (self.partner_id.vat or "").strip()
        ):
            raise UserError(
                _(
                    "Una boleta mayor a S/ 700 requiere el documento de identidad del cliente para anularse "
                    "por resumen diario."
                )
            )

    def _l10n_pe_build_baja_request(self):
        """Comunicación de Baja (RA) — endpoint /generator/resumenBaja. Da de baja este comprobante."""
        self.ensure_one()
        tipo, serie, correlativo = self._l10n_pe_baja_identidad()
        return {
            "id": {
                "ruc": self.company_id.vat or "",
                # El DTO del facturador deserializa estas fechas con patrón yyyyMMdd (sin guiones).
                "fechaGeneracion": self.l10n_pe_ne_baja_fecha.strftime("%Y%m%d"),
                "correlativo": self.l10n_pe_ne_baja_correlativo or "1",
            },
            "emisor": self._l10n_pe_emisor(),
            # ReferenceDate = fecha de emisión del comprobante que se anula; IssueDate = fecha de la baja.
            "fecGeneracion": self.invoice_date.strftime("%Y%m%d"),
            "fecComunicacion": self.l10n_pe_ne_baja_fecha.strftime("%Y%m%d"),
            "resumenBajas": [
                {
                    "tipDocBaja": tipo,
                    "numDocBaja": "%s-%s" % (serie, correlativo.zfill(8)),
                    "desMotivoBaja": self.l10n_pe_ne_baja_motivo or "",
                }
            ],
        }

    # --- Resumen Diario de Boletas (RC): anula boletas (tipEstado 3) ---
    _RC_CATEGORIA = {
        "1000": "gravado",
        "1016": "gravado",
        "9997": "exonerado",
        "9998": "inafecto",
        "9995": "exportado",
        "9996": "gratuito",
    }

    def _l10n_pe_rc_totales(self):
        """Totales de valor por categoría (gravado/exonerado/inafecto/exportado/gratuito) para el RC."""
        self.ensure_one()
        cats = dict.fromkeys(
            ("gravado", "exonerado", "inafecto", "exportado", "gratuito"), 0.0
        )
        for line in self._l10n_pe_product_lines():
            (_tip, cod_tri, *_), _por = self._l10n_pe_tax_info(line)
            base, _igv, _isc, _icb = self._l10n_pe_line_amounts(line)
            cats[self._RC_CATEGORIA.get(cod_tri, "gravado")] += base
        return {k: round(v, 2) for k, v in cats.items()}

    def _l10n_pe_build_rc_request(self):
        """Resumen Diario de Boletas (RC) — endpoint /generator/resumenBoleta. Anula esta boleta (tipEstado 3)."""
        self.ensure_one()
        fmt = self._l10n_pe_fmt
        _tipo, serie, correlativo = self._l10n_pe_baja_identidad()
        partner = self.partner_id
        cats = self._l10n_pe_rc_totales()
        tributos = [
            {
                "idLineaRd": "1",
                "ideTributoRd": t["ideTributo"],
                "nomTributoRd": t["nomTributo"],
                "codTipTributoRd": t["codTipTributo"],
                "mtoBaseImponibleRd": t["mtoBaseImponible"],
                "mtoTributoRd": t["mtoTributo"],
            }
            for t in self._l10n_pe_tributos()
        ]
        # ICBPER: va como tributo 7152 en el RC para que la suma de componentes cuadre con totImpCpe
        # (que incluye el ICBPER), evitando la observación 4027 del validador.
        icbper = self._l10n_pe_total_icbper()
        if icbper:
            tributos.append(
                {
                    "idLineaRd": "1",
                    "ideTributoRd": "7152",
                    "nomTributoRd": "ICBPER",
                    "codTipTributoRd": "OTH",
                    "mtoBaseImponibleRd": "0.00",
                    "mtoTributoRd": fmt(icbper),
                }
            )
        # El validador exige que CADA línea del RC tenga el tributo IGV '1000'. Si la boleta no es
        # gravada (exo/inafecto/exportación/gratuita/IVAP), se agrega uno en cero (regla 2278).
        if not any(t["ideTributoRd"] == "1000" for t in tributos):
            tributos.append(
                {
                    "idLineaRd": "1",
                    "ideTributoRd": "1000",
                    "nomTributoRd": "IGV",
                    "codTipTributoRd": "VAT",
                    "mtoBaseImponibleRd": "0.00",
                    "mtoTributoRd": "0.00",
                }
            )
        # Adquirente: consumidor final sin documento → tipo "0" y número "00000000" (catálogo SUNAT).
        vat = (partner.vat or "").strip()
        cod_doc = partner.l10n_latam_identification_type_id.l10n_pe_vat_code or ""
        if not vat:
            cod_doc, vat = "0", "00000000"
        elif not cod_doc:
            cod_doc = "6" if (len(vat) == 11 and vat.isdigit()) else "1"
        return {
            "id": {
                "ruc": self.company_id.vat or "",
                "fechaGeneracion": self.l10n_pe_ne_baja_fecha.strftime("%Y%m%d"),
                "correlativo": self.l10n_pe_ne_baja_correlativo or "1",
            },
            "emisor": self._l10n_pe_emisor(),
            "resumenDiario": [
                {
                    # ReferenceDate = emisión de la boleta; IssueDate = fecha del resumen (ISO en el XML).
                    "fecEmision": self.invoice_date.strftime("%Y-%m-%d"),
                    "fecResumen": self.l10n_pe_ne_baja_fecha.strftime("%Y-%m-%d"),
                    "tipDocResumen": "03",
                    "idDocResumen": "%s-%s" % (serie, correlativo.zfill(8)),
                    "tipDocUsuario": cod_doc,
                    "numDocUsuario": vat,
                    "tipMoneda": self.currency_id.name or "PEN",
                    "totValGrabado": fmt(cats["gravado"]),
                    "totValExoneado": fmt(cats["exonerado"]),
                    "totValInafecto": fmt(cats["inafecto"]),
                    "totValExportado": fmt(cats["exportado"]),
                    "totValGratuito": fmt(cats["gratuito"]),
                    "totOtroCargo": "0.00",
                    "totImpCpe": fmt(self.amount_total),
                    "tipDocModifico": "",
                    "serDocModifico": "",
                    "numDocModifico": "",
                    "tipRegPercepcion": "",
                    "porPercepcion": "",
                    "monBasePercepcion": "",
                    "monPercepcion": "",
                    "monTotIncPercepcion": "",
                    "tipEstado": "3",  # 3 = anulación/baja de la boleta
                    "tributosDocResumen": tributos,
                }
            ],
        }

    def _l10n_pe_store_baja_cdr(self, cdr_b64):
        """Guarda el CDR de la baja en un adjunto propio (no pisa el CDR original) y devuelve (code, desc)."""
        self.ensure_one()
        try:
            cdr_bytes = base64.b64decode(cdr_b64)
        except Exception:
            return "", ""
        att = self.env["ir.attachment"].create(
            {
                "name": "R%s-%s.zip"
                % (self.company_id.vat or "", self.l10n_pe_ne_baja_doc or "RA"),
                "res_model": "account.move",
                "res_id": self.id,
                "mimetype": "application/zip",
                "raw": cdr_bytes,
            }
        )
        self.l10n_pe_ne_baja_cdr = att.id
        return self._l10n_pe_parse_cdr_codes(cdr_bytes)

    def action_l10n_pe_send_baja(self):
        """Anula en SUNAT cada comprobante seleccionado: boletas por Resumen Diario (RC, tipEstado 3),
        facturas/NC/ND por Comunicación de Baja (RA)."""
        icp = self.env["ir.config_parameter"].sudo()
        base = icp.get_param("l10n_pe_ne_biller.url", "http://localhost:8090").rstrip(
            "/"
        )
        # En producción el biller está tras API Gateway (tope duro 30s): esperar
        # 120s solo alargaba el error. 40 = margen sobre el 504 del gateway; el
        # cron/reintento resuelve las bajas que SUNAT termina aceptando después.
        timeout = int(icp.get_param("l10n_pe_ne_biller.baja_timeout", "40"))
        for move in self:
            if move.l10n_pe_biller_state == "anulado":
                continue  # ya dado de baja: no reenviar el mismo RA (SUNAT lo rechaza por duplicado)
            move._l10n_pe_check_baja()
            es_boleta = move._l10n_pe_baja_identidad()[0] == "03"
            # Boletas → Resumen Diario (RC, tipEstado 3); el resto → Comunicación de Baja (RA).
            seq, endpoint, root = (
                ("l10n_pe.ne.rc", "/generator/resumenBoleta", "<SummaryDocuments")
                if es_boleta
                else ("l10n_pe.ne.ra", "/generator/resumenBaja", "<VoidedDocuments")
            )
            # Correlativo del resumen asignado una sola vez; un reintento limpio reusa el mismo.
            if not move.l10n_pe_ne_baja_correlativo:
                move.l10n_pe_ne_baja_correlativo = (
                    self.env["ir.sequence"].next_by_code(seq) or "1"
                )
                move.l10n_pe_ne_baja_fecha = fields.Date.context_today(move)
            ra = move.l10n_pe_ne_baja_doc
            payload = (
                move._l10n_pe_build_rc_request()
                if es_boleta
                else move._l10n_pe_build_baja_request()
            )
            headers = {"X-Api-Key": move.company_id.sudo().l10n_pe_ne_api_key or ""}
            try:
                resp = requests.post(
                    base + endpoint,
                    json=payload,
                    headers=headers,
                    timeout=(5, timeout),
                )
            except requests.RequestException as exc:
                # Nunca llegó a SUNAT: libera el resumen para reintentar con uno fresco.
                move._l10n_pe_baja_liberar()
                move.l10n_pe_biller_message = _(
                    "Error de conexión con el facturador (anulación %s): %s"
                ) % (ra, exc)
                continue
            if resp.status_code == 200 and root in resp.text:
                cdr_b64 = resp.headers.get("X-Sunat-Cdr")
                code, desc = (
                    move._l10n_pe_store_baja_cdr(cdr_b64) if cdr_b64 else ("", "")
                )
                if code == "0":
                    move.l10n_pe_biller_state = "anulado"
                    move.l10n_pe_biller_message = _(
                        "Anulación %s aceptada por SUNAT (ResponseCode 0). %s"
                    ) % (ra, desc or "")
                elif code:
                    # SUNAT lo recibió y rechazó: libera el resumen; el usuario corrige y reintenta con uno nuevo.
                    move._l10n_pe_baja_liberar()
                    move.l10n_pe_biller_message = _(
                        "Anulación %s rechazada por SUNAT (ResponseCode %s). %s"
                    ) % (ra, code, desc or "")
                else:
                    # 200 + XML firmado pero sin CDR legible: resultado indeterminado; no marcar anulado ni
                    # liberar el resumen (SUNAT pudo haberlo aceptado). El usuario verifica antes de reintentar.
                    move.l10n_pe_biller_message = (
                        _(
                            "Anulación %s enviada; respuesta de SUNAT indeterminada (sin CDR legible). Verifique en SUNAT."
                        )
                        % ra
                    )
            else:
                move._l10n_pe_baja_liberar()
                move.l10n_pe_biller_message = _(
                    "Anulación %s rechazada por el facturador: %s"
                ) % (ra, (resp.text or "")[:1200])
        return True

    def _l10n_pe_baja_liberar(self):
        """Libera el correlativo/fecha del resumen tras un fallo, para un reintento limpio."""
        self.l10n_pe_ne_baja_correlativo = False
        self.l10n_pe_ne_baja_fecha = False
