import base64
import io
import logging
import re
import zipfile

import requests
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


class AccountMove(models.Model):
    _inherit = "account.move"

    l10n_pe_biller_state = fields.Selection(
        selection=[
            ("por_enviar", "Por enviar"),
            ("enviado", "Enviado"),
            ("anulado", "Anulado"),
            ("rechazado", "Rechazado"),
            ("error", "Error"),
        ],
        string="Estado Facturador",
        default="por_enviar",
        copy=False,
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
        dato = {"formaPago": "Contado"}
        if self.l10n_pe_ne_detraccion:
            # Operación al contado con detracción: se declara el neto pendiente de pago.
            dato["mtoNetoPendientePago"] = self._l10n_pe_fmt(self.amount_total)
            dato["tipMonedaMtoNetoPendientePago"] = self.currency_id.name or "PEN"
        return dato

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
        """Código de unidad SUNAT (cat. 03) de la línea: override manual en la UoM, si no el mapeo
        por XMLID de la unidad estándar de Odoo, si no 'NIU'."""
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
                "codProductoSUNAT": "-",
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
        # El ICBPER no entra en el total de tributos ni en el precio de venta; sí en el importe a
        # cobrar (sumImpVenta). amount_tax/amount_total de Odoo incluyen el ICBPER, así que lo restamos.
        icbper = self._l10n_pe_total_icbper()
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
            "sumTotTributos": fmt(self.amount_tax - icbper - anticipo_igv),
            "sumTotValVenta": fmt(self.amount_untaxed - grat_base),
            "sumPrecioVenta": fmt(self.amount_total - icbper - grat_base),
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

    # ------------------------------------------------------------------ acción
    def action_l10n_pe_send_to_biller(self):
        _logger.info("Enviando facturas a Biller: %s", self.ids)
        icp = self.env["ir.config_parameter"].sudo()
        base = icp.get_param("l10n_pe_ne_biller.url", "http://localhost:8090").rstrip(
            "/"
        )
        _logger.info("URL: %s", base)
        timeout = int(icp.get_param("l10n_pe_ne_biller.timeout", "360"))
        _logger.info("Timeout: %s", timeout)
        for move in self:
            _logger.info(
                "Procesando factura: %s (%s)", move.name, move.l10n_pe_biller_state
            )
            if move.l10n_pe_biller_state == "enviado":
                _logger.info("Factura ya enviada: %s", move.name)
                continue
            endpoint, payload = move._l10n_pe_target()
            _logger.info("[[[[[[Enviando %s: %s", endpoint, payload)
            headers = {"X-Api-Key": move.company_id.sudo().l10n_pe_ne_api_key or ""}
            try:
                _logger.info("%%%Enviando %s: %s", endpoint, payload)
                resp = requests.post(
                    base + "/generator/" + endpoint,
                    json=payload,
                    headers=headers,
                    timeout=timeout,
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
            signed = any(
                tag in resp.text for tag in ("<Invoice", "<CreditNote", "<DebitNote")
            )
            if resp.status_code == 200 and signed:
                serie, correlativo = move._l10n_pe_serie_correlativo()
                att = self.env["ir.attachment"].create(
                    {
                        "name": "%s-%s-%s.xml"
                        % (move.company_id.vat, serie, correlativo.zfill(8)),
                        "res_model": "account.move",
                        "res_id": move.id,
                        "mimetype": "application/xml",
                        "raw": resp.text.encode("utf-8"),
                    }
                )
                move.l10n_pe_biller_xml = att.id
                move.l10n_pe_biller_state = "enviado"
                # Congela la identidad emitida para una eventual baja (no recomputar luego del partner/nombre).
                move.l10n_pe_ne_tipo_doc = move._l10n_pe_document_type()
                move.l10n_pe_ne_serie_emit = serie
                move.l10n_pe_ne_corr_emit = correlativo.zfill(8)
                # El biller devuelve el CDR de SUNAT en el header X-Sunat-Cdr (base64 del zip).
                cdr_b64 = resp.headers.get("X-Sunat-Cdr")
                code, desc = move._l10n_pe_store_cdr(cdr_b64) if cdr_b64 else ("", "")
                if code == "0":
                    move.l10n_pe_biller_message = _(
                        "Aceptado por SUNAT — CDR ResponseCode 0. %s"
                    ) % (desc or "")
                elif code:
                    move.l10n_pe_biller_message = _(
                        "CDR de SUNAT (ResponseCode %s). %s"
                    ) % (code, desc or "")
                else:
                    move.l10n_pe_biller_message = _(
                        "Aceptado por el facturador (HTTP 200)."
                    )
            else:
                move.l10n_pe_biller_state = "rechazado"
                move.l10n_pe_biller_message = (resp.text or "")[:2000]
        return True

    # ------------------------------------------------- descargas / PDF (SFS 2.4)
    @staticmethod
    def _l10n_pe_download_url(attachment):
        """Acción de descarga directa del adjunto vía /web/content."""
        return {
            "type": "ir.actions.act_url",
            "url": "/web/content/%s?download=true" % attachment.id,
            "target": "self",
        }

    def _l10n_pe_get_pdf_attachment(self):
        """Devuelve (o genera y cachea) el PDF de la representación impresa pidiéndolo al micro
        (POST /report/pdf con el XML firmado). El micro lo renderiza con las plantillas del SFS 2.4."""
        self.ensure_one()
        if self.l10n_pe_biller_pdf:
            return self.l10n_pe_biller_pdf
        if not self.l10n_pe_biller_xml:
            raise UserError(
                _("El comprobante no tiene XML firmado; envíelo primero a SUNAT.")
            )
        icp = self.env["ir.config_parameter"].sudo()
        base = icp.get_param("l10n_pe_ne_biller.url", "http://localhost:8090").rstrip(
            "/"
        )
        timeout = int(icp.get_param("l10n_pe_ne_biller.timeout", "60"))
        tipo, serie, correlativo = self._l10n_pe_baja_identidad()
        payload = {
            "ruc": self.company_id.vat or "",
            "tipoDoc": tipo,
            "xml": (self.l10n_pe_biller_xml.raw or b"").decode("utf-8"),
        }
        headers = {"X-Api-Key": self.company_id.sudo().l10n_pe_ne_api_key or ""}
        try:
            resp = requests.post(
                base + "/report/pdf", json=payload, headers=headers, timeout=timeout
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
                "name": "%s-%s-%s.pdf"
                % (self.company_id.vat or "", serie, correlativo.zfill(8)),
                "res_model": "account.move",
                "res_id": self.id,
                "mimetype": "application/pdf",
                "raw": resp.content,
            }
        )
        self.l10n_pe_biller_pdf = att.id
        return att

    def action_l10n_pe_download_pdf(self):
        self.ensure_one()
        return self._l10n_pe_download_url(self._l10n_pe_get_pdf_attachment())

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
        timeout = int(icp.get_param("l10n_pe_ne_biller.baja_timeout", "120"))
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
                    base + endpoint, json=payload, headers=headers, timeout=timeout
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
