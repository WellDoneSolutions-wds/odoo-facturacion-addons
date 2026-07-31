import base64
import io
import json
import logging
import re
import zipfile
from datetime import timedelta

import pytz
import requests

try:  # SQS para el modo asíncrono (l10n_pe_ne_biller.async_enabled); si falta
    import boto3  # boto3, el modo síncrono sigue funcionando igual.
except ImportError:  # pragma: no cover
    boto3 = None

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.tools import float_round, html2plaintext

from ..tools.amount_to_words import leyenda_monto

_logger = logging.getLogger(__name__)

# Cache de clientes boto3 por (service, region) — ver _l10n_pe_boto_client.
# Guarda (módulo_boto3, cliente) para invalidarse solo si boto3 fue parcheado.
_BOTO_CLIENTS = {}

# Descuento global que NO afecta la base imponible del IGV (AllowanceChargeReasonCode, cat. 53).
# CONFIRMADO contra el validador SUNAT (ValidaExprRegFactura-2.0.1.xsl) y beta (spike 2026-07-21):
# el código "03" es el que el validador cuenta en `descuentosGlobalesNOAfectaBI` (línea 335) y NO
# resta de la base del IGV; el "02" cae en `MontoDescuentoAfectoBI` (SÍ afecta la base) → daba
# error 3291 (esperaba el IGV sobre la base ya descontada). Con "03" el IGV queda sobre el precio
# lleno y baja solo el MtoImpVenta.
DESC_GLOBAL_NO_AFECTA_COD = "03"

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

# Tasas OFICIALES de detracción (SPOT) por código de bien/servicio (cat. 54). Guard autoritativo
# del backend: el front (detracciones.ts) las valida al capturar, pero esto cubre masiva/API/bypass.
# Ej.: contratos de CONSTRUCCIÓN = 030 (4%), NO 037 (12% "demás servicios"). Código 028 (transporte
# de pasajeros) no lleva tasa fija → se omite. Regulatorio: cambia por resolución SUNAT (mantener en
# sync con el front). Un código fuera de esta tabla no dispara el aviso.
DETRACCION_TASAS = {
    # Servicios (Anexo 3)
    "012": 12.0, "019": 10.0, "020": 12.0, "021": 10.0, "022": 12.0, "024": 10.0,
    "025": 10.0, "026": 10.0, "027": 4.0, "030": 4.0, "037": 12.0, "099": 8.0,
    # Bienes (Anexo 2)
    "001": 10.0, "002": 4.0, "003": 4.0, "004": 4.0, "005": 4.0, "007": 10.0,
    "008": 4.0, "009": 10.0, "010": 15.0, "011": 10.0, "014": 4.0, "016": 10.0,
    "017": 4.0, "023": 4.0, "031": 10.0, "032": 10.0, "034": 10.0, "035": 1.0,
    "036": 1.0, "039": 10.0, "040": 4.0, "041": 15.0,
}

# Código de unidad de medida de SUNAT (cat. 03 / UN-ECE Rec. 20) por XMLID de la unidad estándar
# de Odoo. Mapeo replicado de l10n_pe_edi (enterprise). Se resuelve en runtime porque las UoM base
# son `noupdate` y un data file de otro módulo no las actualiza; para unidades personalizadas, el
# usuario fija el override en `uom.uom.l10n_pe_ne_unit_code`.
UOM_CODE_BY_XMLID = {
    "uom.product_uom_unit": "NIU",
    "uom.product_uom_dozen": "DZN",
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

# Importación de productos por Excel: mapeo tolerante de TEXTO en español (o el propio código
# cat.03) → código SUNAT cat.03. Clave = texto normalizado (minúsculas, sin tildes). Espejo del
# catálogo del front (lib/unidades.ts) más sinónimos comunes de ferretería/bodega.
UNIDAD_IMPORT = {
    "unidad": "NIU", "unidades": "NIU", "und": "NIU", "unid": "NIU", "niu": "NIU", "u": "NIU",
    "servicio": "ZZ", "servicios": "ZZ", "serv": "ZZ", "zz": "ZZ",
    "kilogramo": "KGM", "kilogramos": "KGM", "kilo": "KGM", "kilos": "KGM", "kg": "KGM", "kgm": "KGM",
    "gramo": "GRM", "gramos": "GRM", "gr": "GRM", "grm": "GRM",
    "libra": "LBR", "libras": "LBR", "lb": "LBR", "lbr": "LBR",
    "tonelada": "TNE", "toneladas": "TNE", "tonelada metrica": "TNE", "ton": "TNE", "tne": "TNE",
    "litro": "LTR", "litros": "LTR", "lt": "LTR", "ltr": "LTR",
    "galon": "GLL", "galones": "GLL", "gln": "GLL", "gll": "GLL",
    "barril": "BLL", "barriles": "BLL", "bll": "BLL",
    "lata": "CA", "latas": "CA", "ca": "CA",
    "caja": "BX", "cajas": "BX", "bx": "BX",
    "millar": "MLL", "millares": "MLL", "mll": "MLL",
    "metro": "MTR", "metros": "MTR", "mt": "MTR", "mtr": "MTR", "m": "MTR",
    "centimetro": "CMT", "centimetros": "CMT", "cm": "CMT", "cmt": "CMT",
    "metro cuadrado": "MTK", "m2": "MTK", "mtk": "MTK",
    "metro cubico": "MTQ", "m3": "MTQ", "mtq": "MTQ",
    "dia": "DAY", "dias": "DAY", "day": "DAY",
    "hora": "HUR", "horas": "HUR", "hr": "HUR", "hur": "HUR",
    "juego": "SET", "juegos": "SET", "set": "SET",
    # Docena = DZN ("dozen" en cat.03), igual que el front (QA-021). DPC ("dozen piece") también
    # es válido en cat.03 y se respeta si el usuario lo teclea explícito.
    "docena": "DZN", "docenas": "DZN", "dzn": "DZN", "dpc": "DPC",
    "onza": "ONZ", "onzas": "ONZ", "onz": "ONZ",
}
# Afectación IGV: texto (cat.07 humano) → código cat.07 que espera el producto.
AFECT_IMPORT = {
    "gravado": "1000", "gravada": "1000",
    "exonerado": "9997", "exonerada": "9997",
    "inafecto": "9998", "inafecta": "9998",
    "exportacion": "9995",
    "gratuito": "9996", "gratuita": "9996",
}
# Bien/servicio (tipo Odoo → stock) desde el Excel. Vacío = deducir de la unidad (ZZ → servicio).
TIPO_IMPORT = {
    "bien": "bien", "bienes": "bien", "producto": "bien", "b": "bien",
    "servicio": "servicio", "servicios": "servicio", "serv": "servicio", "s": "servicio",
}
# Códigos cat.03 válidos (para aceptar el código directo en el Excel, ej. "KGM").
_UNIDAD_CODES = set(UNIDAD_IMPORT.values())


def _percep_float(v):
    """float() de percepTasa que no revienta con un 500 críptico: tolera coma decimal (igual
    que el import masivo) y, si no es numérico, da un UserError legible en vez de un
    ValueError sin traducir. Vacío/None/False → 0.0 (limpia el campo, mismo criterio de
    siempre)."""
    if v in (None, "", False):
        return 0.0
    try:
        return float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        raise UserError(_("La percepción sugerida debe ser un número (ej. 2 o 1.5)."))


class AccountMoveLine(models.Model):
    _inherit = "account.move.line"

    # Cantidad con 3 decimales (SUNAT admite hasta 10 en ctdUnidadItem). Por defecto la precisión
    # de UoM de Odoo es 2 y truncaba la venta al peso de balanza (18.375 kg -> 18.38). Ver QA-020.
    quantity = fields.Float(digits=(16, 3))

    # Overrides SUNAT por línea para el flujo rápido (sin depender de la UoM/producto de Odoo,
    # evitando el problema de categorías de unidad de medida).
    l10n_pe_ne_unit_code = fields.Char(
        string="Unidad SUNAT (cat.03)",
        copy=False,
        help="Código de unidad de medida SUNAT de la línea (ej. NIU, KGM, ZZ). "
        "Si está vacío se deriva de la unidad de medida del producto.",
    )
    l10n_pe_ne_fraccionado = fields.Boolean(
        string="Vendido fraccionado", copy=False,
        help="Farma: esta línea se vende por la sub-unidad del producto (fraccionamiento). La "
        "cantidad va en sub-unidades y el stock descuenta cantidad/unidades_por_empaque del empaque.")
    l10n_pe_ne_cod_producto_sunat = fields.Char(
        string="Cód. producto SUNAT (cat.25)",
        copy=False,
        help="Código de producto SUNAT (UNSPSC, catálogo 25) de la línea, si aplica.",
    )
    # Lote/serie de una línea de COMPRA. El lote entra con la mercadería, así que se captura
    # en la compra y viaja con la línea hasta que _l10n_pe_ne_mover_stock_compra crea el
    # movimiento. En la VENTA no se pide: Odoo reserva y asigna el lote solo, por su
    # estrategia de salida (lo que vence antes sale primero). Verificado.
    l10n_pe_ne_lote = fields.Char(
        string="Lote / serie",
        copy=False,
        help="Número de lote o serie de la mercadería que ingresa por esta línea.",
    )
    l10n_pe_ne_vence = fields.Date(
        string="Vencimiento del lote",
        copy=False,
        help="Fecha de vencimiento del lote que ingresa por esta línea.",
    )
    # Sub-tipo de operación gratuita (cat. 07 SUNAT). Solo aplica a líneas gratuitas (9996):
    # afina el genérico "11" al motivo real (retiro, bonificación, donación…). Vacío = 11.
    l10n_pe_ne_afectacion_gratuita = fields.Selection(
        [
            ("11", "Retiro por premio"),
            ("12", "Retiro por donación"),
            ("13", "Retiro de bienes"),
            ("14", "Retiro por publicidad"),
            ("15", "Bonificación"),
            ("16", "Retiro por entrega a trabajadores"),
        ],
        string="Tipo de operación gratuita",
        copy=False,
        help="Solo para líneas gratuitas: precisa el motivo (catálogo 07 de SUNAT). "
        "Si se deja vacío se usa 'Retiro por premio' (11).",
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
    # Liquidación de compra (comprobante tipo 04): la emite el COMPRADOR (con RUC) cuando le
    # compra a un productor/vendedor SIN RUC (agropecuario, recolección, artesanía). Es una
    # COMPRA (in_invoice): la mercadería ENTRA al stock y va al PLE de compras; pero además se
    # emite electrónicamente a SUNAT como SelfBilledInvoice. El flag marca ese cruce.
    l10n_pe_ne_liquidacion = fields.Boolean(
        string="Liquidación de compra", copy=False,
        help="Marca esta compra como Liquidación de compra electrónica (tipo 04): se emite a "
             "SUNAT como comprobante, con el proveedor sin RUC (DNI) como vendedor.")
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
        help="Serie del comprobante. Por defecto, la del diario (l10n_pe_ne_serie) con la "
        "letra ajustada a la familia del comprobante (F factura / B boleta).",
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
    l10n_pe_ne_detraccion_cuenta = fields.Char(
        string="Cuenta de detracción (Banco de la Nación)",
        copy=False,
        help="Cuenta del Banco de la Nación de ESTE comprobante. Si se deja vacía, "
        "se usa la cuenta de detracciones configurada en la empresa.",
    )
    l10n_pe_ne_percepcion = fields.Boolean(string="Aplica percepción", copy=False)
    l10n_pe_ne_percepcion_rate = fields.Float(
        string="% Percepción", digits=(5, 2), default=2.0, copy=False
    )
    l10n_pe_ne_anticipos = fields.Json(
        string="Anticipos regularizados",
        copy=False,
        help="Lista de anticipos (doc. A) que ESTA venta final regulariza/deduce. Cada elemento: "
        "{doc: serie-correlativo, monto: importe con IGV, tipo: '02'/'03' (cat. 12), "
        "origenId: id del anticipo local enlazado o null si se emitió por fuera}. SUNAT permite "
        "varios (pagos escalonados): se emiten como N documentos relacionados con numIdeAnticipo 1..N.",
    )
    l10n_pe_ne_es_anticipo = fields.Boolean(
        string="Es pago anticipado",
        copy=False,
        help="Marca que ESTE comprobante se emite por un pago anticipado (doc. A del ciclo de "
        "anticipos, equivale al 'Sí' de SEE-SOL). Es una venta interna normal (0101) por el monto "
        "anticipado; su descripción lleva 'PAGO ANTICIPADO' y queda disponible para regularizarse "
        "luego en la factura de venta final. No confundir con el anticipo aplicado, que descuenta "
        "un anticipo ya emitido (doc. B).",
    )
    l10n_pe_ne_anticipo_aplicado = fields.Monetary(
        string="Anticipo ya aplicado",
        compute="_compute_l10n_pe_ne_anticipo_saldo",
        help="Suma de las regularizaciones vivas que apuntan a este anticipo (doc. A).",
    )
    l10n_pe_ne_anticipo_saldo = fields.Monetary(
        string="Saldo del anticipo",
        compute="_compute_l10n_pe_ne_anticipo_saldo",
        help="Importe del anticipo aún disponible para regularizar = total − aplicado. Solo "
        "aplica a comprobantes marcados como pago anticipado (doc. A).",
    )
    l10n_pe_ne_desc_no_afecta = fields.Monetary(
        string="Descuento que no afecta el IGV",
        copy=False,
        help="Descuento (S/, CON IGV incluido en el sentido de que baja el total a pagar) que NO "
        "afecta la base imponible del IGV (cat. 53 'no afecta'): la gravada y el IGV se calculan "
        "sobre el precio lleno; este importe solo reduce el total (MtoImpVenta). Agrega el "
        "descuento por ítem 'no afecta' y el descuento global 'no afecta' del comprobante.",
    )

    @api.depends("l10n_pe_ne_es_anticipo", "amount_total")
    def _compute_l10n_pe_ne_anticipo_saldo(self):
        """Saldo por anticipo (doc. A) = total − regularizaciones vivas que lo aplican. Mismo
        criterio de 'vivo' que las NC (posteadas y no rechazadas/anuladas/con error; las en cola
        cuentan, para que dos regularizaciones simultáneas no consuman más que el total).
        El dato vive en una lista JSON (`l10n_pe_ne_anticipos`): no se puede agrupar por
        contenido JSON con `_read_group`, así que se busca las regularizaciones vivas y se
        agrega en Python sumando el `monto` de cada anticipo cuyo `origenId` matchee."""
        anticipos = self.filtered("l10n_pe_ne_es_anticipo")
        aplicado = {a.id: 0.0 for a in anticipos}
        if anticipos:
            # Nota de escala: search + loop Python sobre TODAS las regularizaciones vivas del
            # sistema; para volúmenes altos se podría filtrar por partner/JSONB, pero YAGNI.
            regs = self.env["account.move"].search([
                ("l10n_pe_ne_anticipos", "!=", False),
                ("state", "=", "posted"),
                ("l10n_pe_biller_state", "not in", ("rechazado", "error", "anulado")),
            ])
            for reg in regs:
                for a in reg._l10n_pe_ne_anticipos_list():
                    oid = a["origenId"]
                    if oid in aplicado:
                        aplicado[oid] += a["monto"]
        for move in self:
            ap = round(aplicado.get(move.id, 0.0), 2) if move.l10n_pe_ne_es_anticipo else 0.0
            move.l10n_pe_ne_anticipo_aplicado = ap
            move.l10n_pe_ne_anticipo_saldo = (
                round(move.amount_total - ap, 2) if move.l10n_pe_ne_es_anticipo else 0.0
            )

    def l10n_pe_ne_anticipos_pendientes(self, ruc=None, partner_id=None, moneda=None):
        """Anticipos (doc. A) ACEPTADOS por SUNAT y con saldo pendiente, para autocompletar la
        regularización en la venta final. Filtra por cliente (id o RUC/DNI) y, opcionalmente,
        moneda. Devuelve serie-correlativo, tipo (cat. 12: 02 factura / 03 boleta) y saldo."""
        domain = [
            ("l10n_pe_ne_es_anticipo", "=", True),
            ("l10n_pe_biller_state", "=", "enviado"),
            ("move_type", "=", "out_invoice"),
        ]
        if partner_id:
            domain.append(("partner_id", "=", int(partner_id)))
        elif ruc:
            domain.append(("partner_id.vat", "=", (ruc or "").strip()))
        if moneda:
            domain.append(("currency_id.name", "=", moneda))
        out = []
        for m in self.search(domain, order="id desc", limit=100):
            if round(m.l10n_pe_ne_anticipo_saldo, 2) <= 0:
                continue
            out.append(
                {
                    "id": m.id,
                    "doc": "%s-%s" % (m.l10n_pe_ne_serie_emit or "", m.l10n_pe_ne_corr_emit or ""),
                    "tipo": "02" if (m.l10n_pe_ne_tipo_doc or "01") == "01" else "03",
                    "total": m.amount_total,
                    "aplicado": m.l10n_pe_ne_anticipo_aplicado,
                    "saldo": m.l10n_pe_ne_anticipo_saldo,
                    "moneda": m.currency_id.name or "PEN",
                    "cliente": m.partner_id.name or "",
                    "fechaEmision": m.invoice_date.strftime("%Y-%m-%d") if m.invoice_date else "",
                }
            )
        return out

    # ==================================================== MODELO DE DINERO (L3)
    # Cuatro magnitudes, una definición autoritativa cada una. Confundirlas fue la raíz del
    # rechazo 3265 (el neto pendiente incluía gratuitos). Las invariantes se fijan por test
    # en test_modelo_dinero.py.
    #
    #   amount_total            total contable (Odoo). INCLUYE los bienes gratuitos.
    #   _l10n_pe_importe_cobrar total − anticipo − desc. que NO afecta IGV − gratuitos.
    #                           = lo que el cliente PAGA = mtoImpVenta (PayableAmount).
    #   _l10n_pe_detraccion_base total − desc. que NO afecta IGV. Importe de la OPERACIÓN para
    #                           el SPOT (incluye gratuitos y NO resta anticipo; distinto criterio).
    #   _l10n_pe_neto_pendiente importe_cobrar − detracción − inicial al contado − retención de
    #                           garantía − amortización de adelanto − penalidad − monto cubierto.
    #                           = saldo a CRÉDITO (lo que suman las cuotas) / copago del paciente.
    #
    # Invariantes (SUNAT + negocio):  neto_pendiente ≤ importe_cobrar ≤ amount_total ;
    #   importe_cobrar ≥ 0 ; detracción ≥ 0 ; sum(cuotas) == neto pendiente del crédito.
    def _l10n_pe_importe_cobrar(self):
        """Importe neto a cobrar = total − anticipo aplicado − descuento que no afecta el IGV −
        bienes gratuitos (lo que el cliente paga). Ver «MODELO DE DINERO» arriba."""
        self.ensure_one()
        ant = self._l10n_pe_anticipo()
        return round(
            self.amount_total
            - (ant[2] if ant else 0.0)
            - self._l10n_pe_desc_no_afecta()
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

    def _l10n_pe_ne_anticipos_list(self):
        """Lista normalizada de anticipos de esta factura: [{doc, monto, tipo, origenId}]. Vacía
        si no aplica (no out_invoice, NC/ND o sin anticipos).
        `origenId` se coerciona con seguridad (nunca explota con basura tipo "abc"): esta lista
        la recorre también `_compute_l10n_pe_ne_anticipo_saldo` para TODAS las regularizaciones
        vivas del sistema, así que una fila envenenada en OTRA factura no debe romper el
        saldo/pendientes de todas las demás — se trata como sin origen local (None)."""
        self.ensure_one()
        if self.move_type != "out_invoice" or self.debit_origin_id:
            return []
        out = []
        for a in (self.l10n_pe_ne_anticipos or []):
            monto = round(float(a.get("monto") or 0.0), 2)
            if monto <= 0:
                continue
            origen_raw = a.get("origenId")
            try:
                origen_id = int(origen_raw) if origen_raw not in (None, "", False) else None
            except (TypeError, ValueError):
                origen_id = None
            out.append({
                "doc": (a.get("doc") or "").strip(),
                "monto": monto,
                "tipo": a.get("tipo") or "02",
                "origenId": origen_id,
            })
        return out

    def _l10n_pe_anticipos_montos(self):
        """(valor, igv, total) AGREGADO de los anticipos: (0.0, 0.0, 0.0) si no aplica (no
        out_invoice, NC/ND o sin anticipos). El valor de CADA anticipo se separa con la tasa
        gravada homogénea real de la factura (no asume 18%) y el agregado es la SUMA de los
        valores/igv por anticipo (no el total dividido una sola vez), así que con montos que no
        son fracción redonda de la base el agregado no arrastra un desvío de redondeo distinto al
        que vería cada `AdditionalDocumentReference` individual — de ahí que el loop por ítem se
        mantenga aunque solo se devuelva el agregado (nadie más consume el desglose por ítem)."""
        self.ensure_one()
        lst = self._l10n_pe_ne_anticipos_list()
        if not lst:
            return (0.0, 0.0, 0.0)
        _cod, tasa, _m = self._l10n_pe_anticipo_gravado()
        vt = it = tt = 0.0
        for a in lst:
            total = round(a["monto"], 2)
            valor = round(total / (1.0 + (tasa or 0.0) / 100.0), 2)
            igv = round(total - valor, 2)
            vt += valor
            it += igv
            tt += total
        return round(vt, 2), round(it, 2), round(tt, 2)

    def _l10n_pe_anticipo(self):
        """(valor, igv, total) AGREGADO de los anticipos, o None si no hay ninguno. Wrapper de
        `_l10n_pe_anticipos_montos()` para no romper a los llamadores previos (percepción, importe
        a cobrar, cabecera, tributos, variable global 04) que solo necesitan el agregado."""
        self.ensure_one()
        v, i, t = self._l10n_pe_anticipos_montos()
        return (v, i, t) if t > 0 else None

    def _l10n_pe_desc_no_afecta(self):
        """Monto del descuento global que NO afecta la base del IGV, topeado para no dejar el total
        negativo. Es un ajuste SOLO de emisión (como el anticipo): no agrega una línea a Odoo, así
        que la gravada/IGV quedan sobre el precio lleno; solo baja el MtoImpVenta (total a pagar).
        Devuelve 0.0 si no aplica (notas, o sin descuento)."""
        self.ensure_one()
        monto = self.l10n_pe_ne_desc_no_afecta or 0.0
        if monto <= 0 or self.move_type not in ("out_invoice",) or self.debit_origin_id:
            return 0.0
        # Tope: no puede superar el total menos lo ya deducido por anticipo (dejaría MtoImpVenta < 0).
        ant = self._l10n_pe_anticipo()
        tope = round((self.amount_total or 0.0) - (ant[2] if ant else 0.0), 2)
        return round(min(monto, max(0.0, tope)), 2)

    def _l10n_pe_check_anticipo(self):
        """Valida que los anticipos sean representables (N documentos relacionados + un descuento
        global código 04 AGREGADO) antes de emitir el XML. Rechaza con un mensaje claro los casos no
        soportados, en vez de generar un comprobante inválido. Cada anticipo de la lista se valida
        individualmente (doc, origen, partner, moneda y saldo propio); la SUMA se valida contra el
        total de la factura."""
        self.ensure_one()
        if self.l10n_pe_ne_es_anticipo and self._l10n_pe_anticipo():
            raise UserError(
                _(
                    "Un comprobante que se emite por un pago anticipado no puede a la vez regularizar "
                    "otro anticipo. Desmarque una de las dos opciones."
                )
            )
        if not self._l10n_pe_anticipo():
            return
        lst = self._l10n_pe_ne_anticipos_list()
        total_aplicado = round(sum(a["monto"] for a in lst), 2)
        if total_aplicado > self.amount_total + 0.01:
            raise UserError(
                _(
                    "El total de anticipos (%.2f) no puede exceder el total de la factura (%.2f)."
                )
                % (total_aplicado, self.amount_total)
            )
        # Otras regularizaciones vivas que ya consumen anticipos (excluye esta factura). El dato vive
        # en la lista JSON: se busca ampliamente UNA sola vez y se filtra/agrega en Python por origen
        # (mismo patrón que `_compute_l10n_pe_ne_anticipo_saldo`).
        otras = self.env["account.move"].search([
            ("id", "!=", self.id),
            ("l10n_pe_ne_anticipos", "!=", False),
            ("state", "=", "posted"),
            ("l10n_pe_biller_state", "not in", ("rechazado", "error", "anulado")),
        ])
        aplicado_en_esta = {}  # origenId -> suma de monto ya visto en ESTA factura (mismo origen 2x).
        for idx, a in enumerate(lst, start=1):
            if a["tipo"] not in ("02", "03"):
                raise UserError(
                    _(
                        "Tipo de documento de anticipo inválido (debe ser 02 factura o 03 boleta)."
                    )
                )
            if not a["doc"]:
                raise UserError(
                    _(
                        "Indique el comprobante del anticipo #%d (serie-correlativo, ej. F001-00000100)."
                    )
                    % idx
                )
            # Si la regularización enlaza un anticipo local (doc. A), valida moneda y saldo
            # disponible: el importe aplicado no puede exceder lo que le queda al anticipo (evita
            # doble consumo), sea desde otra factura o desde otra línea de esta misma lista.
            origen = (
                self.env["account.move"].browse(a["origenId"])
                if a["origenId"]
                else self.env["account.move"]
            )
            if not origen:
                continue
            if not origen.l10n_pe_ne_es_anticipo:
                raise UserError(
                    _("El documento enlazado (%s) no está marcado como pago anticipado.")
                    % origen.display_name
                )
            if origen.partner_id != self.partner_id:
                raise UserError(
                    _("El anticipo (%s) pertenece a otro cliente: solo puede regularizarlo su titular.")
                    % origen.display_name
                )
            if origen.currency_id != self.currency_id:
                raise UserError(
                    _(
                        "El anticipo (%s) y la factura deben estar en la misma moneda: "
                        "regularice el anticipo en un comprobante de su misma moneda."
                    )
                    % origen.display_name
                )
            aplicado_otras = round(
                sum(
                    oa["monto"]
                    for m in otras
                    for oa in m._l10n_pe_ne_anticipos_list()
                    if oa["origenId"] == origen.id
                ),
                2,
            )
            aplicado_en_esta[origen.id] = round(
                aplicado_en_esta.get(origen.id, 0.0) + a["monto"], 2
            )
            disponible = round(origen.amount_total - aplicado_otras, 2)
            if aplicado_en_esta[origen.id] > disponible + 0.01:
                raise UserError(
                    _(
                        "El anticipo %s ya no tiene saldo suficiente: disponible %.2f, "
                        "intentas aplicar %.2f."
                    )
                    % (origen.display_name, disponible, aplicado_en_esta[origen.id])
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

    def _l10n_pe_check_lineas_impuesto(self):
        """Ninguna línea con importe llega al XML sin su tax cat-05: `_l10n_pe_tax_info`
        la clasificaría con el default 'gravado (1000)' a tasa 0 y SUNAT rechaza con 3111
        (TaxableAmount>0 + TaxAmount=0.00), un mensaje críptico que además llega recién
        del validador. Se corta aquí, con el dato que el usuario sí puede arreglar.
        Las líneas de importe 0 (p.ej. NC de corrección de texto) no tienen base imponible
        —no hay 3111 posible— y pasan."""
        self.ensure_one()
        for line in self._l10n_pe_product_lines():
            if not line.price_subtotal:
                continue
            if not any(t.l10n_pe_edi_tax_code in TAX_CODE_MAP for t in line.tax_ids):
                raise UserError(
                    _(
                        "La línea «%s» no tiene impuesto SUNAT asignado (IGV, exonerado, "
                        "inafecto…). Asigna la afectación IGV en el producto o en la línea "
                        "y vuelve a emitir."
                    )
                    % (line.name or line.product_id.display_name or "?")
                )

    # ==================================================== L1 · validación pre-emisión
    # Motor de reglas SUNAT: valida el comprobante ANTES de enviarlo y devuelve findings
    # accionables (nivel 'error' | 'aviso'). Reemplaza el faultCode críptico (p.ej. 3265)
    # por un mensaje que el emisor sí puede arreglar. Fuente única para (a) el guard duro de
    # la emisión y (b) un futuro pre-flight de la SPA. Cada regla es un método _regla_*; sumar
    # una regla = agregarla a la tupla de _l10n_pe_ne_validaciones.
    def _l10n_pe_ne_validaciones(self):
        """[{'code','campo','nivel','mensaje'}]. 'error' bloquea la emisión; 'aviso' informa
        (lo consume el pre-flight de la SPA) y no bloquea."""
        self.ensure_one()
        findings = []
        for regla in (
            self._l10n_pe_ne_regla_neto_pendiente,      # SUNAT 3265
            self._l10n_pe_ne_regla_cuotas_suma,
            self._l10n_pe_ne_regla_deducciones_exceden, # neto a cobrar no puede ser negativo
            self._l10n_pe_ne_regla_estado_grupo,        # SUNAT 3146-3149
            self._l10n_pe_ne_regla_estado_conformidad,  # venta al Estado: acta de recepción
            self._l10n_pe_ne_regla_vinculada_valor_mercado,  # vinculadas: recordar valor de mercado
            self._l10n_pe_ne_regla_detraccion_cuenta,   # SPOT: cta. Banco de la Nación
            self._l10n_pe_ne_regla_detraccion_monto,    # SPOT: mtoDetraccion > 0
            self._l10n_pe_ne_regla_detraccion_tasa,     # SPOT: tasa oficial del código
            self._l10n_pe_ne_regla_exportacion_pais,    # 0200: país del no domiciliado
            self._l10n_pe_ne_regla_exportacion_ruc,     # 0200: adquirente no domiciliado (aviso si RUC)
            self._l10n_pe_ne_regla_boleta_doc,          # boleta > S/700 con documento
            self._l10n_pe_ne_regla_vencidos,            # farma/perecibles: lote vencido
            self._l10n_pe_ne_regla_convenio_cubierto,   # convenio: cubierto ≤ importe a cobrar
            self._l10n_pe_ne_regla_controlado_receta,   # farma: controlado exige receta retenida
            self._l10n_pe_ne_regla_linea_valor_cero,    # SUNAT 2028: línea onerosa con importe 0
        ):
            findings += regla() or []
        return findings

    def _l10n_pe_ne_regla_neto_pendiente(self):
        """SUNAT 3265: el neto pendiente de pago a crédito no puede superar el importe a cobrar
        del comprobante (que ya excluye gratuitos, anticipo y descuento que no afecta el IGV).
        Invariante del modelo de dinero: si se viola, SUNAT rechaza con 3265."""
        if self.l10n_pe_ne_forma_pago != "Credito":
            return []
        neto = self._l10n_pe_credito_pendiente()
        cobrar = self._l10n_pe_importe_cobrar()
        if neto > cobrar + 0.005:
            return [{
                "code": "3265", "campo": "datoPago/mtoNetoPendientePago", "nivel": "error",
                "mensaje": _(
                    "El monto neto pendiente de pago a crédito (S/ %(neto).2f) supera el "
                    "importe a cobrar del comprobante (S/ %(cobrar).2f). Revisa las cuotas, la "
                    "inicial al contado o los ítems gratuitos."
                ) % {"neto": neto, "cobrar": cobrar},
            }]
        return []

    def _l10n_pe_ne_regla_cuotas_suma(self):
        """Aviso: las cuotas tecleadas no suman el neto a cobrar; se ajustarán a este al emitir."""
        if self.l10n_pe_ne_forma_pago != "Credito":
            return []
        cuotas = [c for c in (self.l10n_pe_ne_cuotas or []) if (c or {}).get("monto")]
        if not cuotas:
            return []
        suma = round(sum(float(c["monto"]) for c in cuotas), 2)
        neto = self._l10n_pe_credito_pendiente()
        if abs(suma - neto) > 0.01:
            return [{
                "code": "cuotas-suma", "campo": "cuotas", "nivel": "aviso",
                "mensaje": _(
                    "Las cuotas suman S/ %(suma).2f pero el neto a cobrar es S/ %(neto).2f; se "
                    "ajustarán a este último al emitir."
                ) % {"suma": suma, "neto": neto},
            }]
        return []

    def _l10n_pe_ne_regla_estado_grupo(self):
        """SUNAT 3146-3149: los 4 datos de Ventas al Estado (cat. 55) van como GRUPO. Si están
        algunos pero no los 4, la emisión los OMITE todos → aviso para no perder el dato en
        silencio."""
        datos = [
            self.l10n_pe_ne_estado_expediente, self.l10n_pe_ne_estado_unidad_ejecutora,
            self.l10n_pe_ne_estado_proceso_seleccion, self.l10n_pe_ne_estado_contrato,
        ]
        llenos = [bool((v or "").strip()) for v in datos]
        if any(llenos) and not all(llenos):
            return [{
                "code": "3146", "campo": "AdditionalItemProperty (Estado)", "nivel": "aviso",
                "mensaje": _(
                    "Ventas al Estado: llenaste %(n)d de 4 datos del proceso (expediente, "
                    "unidad ejecutora, proceso de selección y contrato). SUNAT los exige como "
                    "grupo, así que se omitirán TODOS. Complétalos o déjalos vacíos."
                ) % {"n": sum(llenos)},
            }]
        return []

    def _l10n_pe_ne_regla_deducciones_exceden(self):
        """Las deducciones contractuales (retención de garantía, amortización de adelanto,
        penalidad) + la detracción + la inicial + el monto cubierto por convenio no pueden dejar
        el neto a cobrar en negativo: significaría que el comprobante 'devuelve' dinero. Bloquea."""
        if self._l10n_pe_neto_pendiente() < -0.005:
            return [{
                "code": "deducciones-exceden", "campo": "neto a cobrar", "nivel": "error",
                "mensaje": _(
                    "Las deducciones del comprobante (retención de garantía, amortización de "
                    "adelanto, penalidad, detracción, inicial y convenio) superan el importe a "
                    "cobrar (S/ %(cobrar).2f): el neto a cobrar quedaría negativo. Revísalas."
                ) % {"cobrar": self._l10n_pe_importe_cobrar()},
            }]
        return []

    def _l10n_pe_ne_regla_estado_conformidad(self):
        """Venta al Estado: la entidad exige un acta de conformidad/recepción como requisito previo
        a facturar. Si los 4 datos del proceso están completos pero falta la conformidad, avisa
        (no bloquea: hay casos —adelantos, valorizaciones a cuenta— sin acta todavía)."""
        datos = [
            self.l10n_pe_ne_estado_expediente, self.l10n_pe_ne_estado_unidad_ejecutora,
            self.l10n_pe_ne_estado_proceso_seleccion, self.l10n_pe_ne_estado_contrato,
        ]
        if all((v or "").strip() for v in datos) and not (self.l10n_pe_ne_conformidad or "").strip():
            return [{
                "code": "estado-conformidad", "campo": "conformidad", "nivel": "aviso",
                "mensaje": _(
                    "Venta al Estado sin acta de conformidad/recepción. La entidad suele exigirla "
                    "como sustento antes de facturar; regístrala si ya la tienes."
                ),
            }]
        return []

    def _l10n_pe_ne_regla_vinculada_valor_mercado(self):
        """Precios de transferencia (V2): si el comprobante va a una parte vinculada, avisa para
        que el emisor confirme que el precio pactado es de mercado (art. 32-A LIR). No bloquea —no
        hay fuente de valor de mercado en el sistema—: es un recordatorio para el sustento de la DJ.
        Con parte no domiciliada, la operación entra a precios de transferencia sin umbral."""
        p = self.partner_id
        if not p or not p.l10n_pe_ne_parte_vinculada:
            return []
        extra = _(" (no domiciliada: entra a precios de transferencia sin umbral de país)") \
            if p.l10n_pe_ne_no_domiciliada else ""
        return [{
            "code": "vinculada-valor-mercado", "campo": "cliente/parteVinculada", "nivel": "aviso",
            "mensaje": _(
                "Operación con parte vinculada «%(nombre)s»%(extra)s. Verifica que el precio sea de "
                "valor de mercado (art. 32-A LIR) y guarda el sustento para la DJ de precios de "
                "transferencia."
            ) % {"nombre": p.name or "", "extra": extra},
        }]

    def _l10n_pe_ne_regla_detraccion_cuenta(self):
        """SPOT: si el comprobante está sujeto a detracción, la cuenta del Banco de la Nación es
        obligatoria (cbc:ID de cac:PaymentMeans → ctaBancoNacionDetraccion). Va la del comprobante
        o, si no, la de la compañía; vacía = SUNAT rechaza el depósito de detracción."""
        if not self.l10n_pe_ne_detraccion:
            return []
        cuenta = (
            self.l10n_pe_ne_detraccion_cuenta
            or self.company_id.l10n_pe_ne_cuenta_detraccion
            or ""
        ).strip()
        if not cuenta:
            return [{
                "code": "detraccion-cuenta", "campo": "ctaBancoNacionDetraccion", "nivel": "error",
                "mensaje": _(
                    "La operación está sujeta a detracción pero no tiene número de cuenta del "
                    "Banco de la Nación. Cárgala en el comprobante o en los datos de la empresa."
                ),
            }]
        return []

    def _l10n_pe_ne_regla_detraccion_monto(self):
        """SPOT: el monto de la detracción debe ser mayor a 0. Si la tasa es 0 (o el código no
        lleva tasa, p.ej. transporte de pasajeros 028) o el importe es tan chico que redondea a 0,
        el mtoDetraccion sale en 0 y SUNAT rechaza."""
        if not self.l10n_pe_ne_detraccion:
            return []
        if self._l10n_pe_detraccion_monto() <= 0:
            return [{
                "code": "detraccion-monto", "campo": "mtoDetraccion", "nivel": "error",
                "mensaje": _(
                    "La detracción da un monto de S/ 0.00. Revisa la tasa (%(tasa)s%%) o el "
                    "importe de la operación: el monto de la detracción debe ser mayor a 0."
                ) % {"tasa": self._l10n_pe_fmt(self.l10n_pe_ne_detraccion_rate or 0.0)},
            }]
        return []

    def _l10n_pe_ne_regla_detraccion_tasa(self):
        """SPOT: avisa si la tasa de detracción no coincide con la OFICIAL del código (cat. 54).
        Ej.: contratos de construcción (030) = 4%, no 12%. Es un AVISO —la tabla cambia por
        resolución SUNAT y el contador confirma la tasa—; un código fuera de la tabla no dispara."""
        if not self.l10n_pe_ne_detraccion:
            return []
        code = (self.l10n_pe_ne_detraccion_code or "").strip()
        oficial = DETRACCION_TASAS.get(code)
        if oficial is None:
            return []
        if abs((self.l10n_pe_ne_detraccion_rate or 0.0) - oficial) > 0.01:
            return [{
                "code": "detraccion-tasa", "campo": "porDetraccion", "nivel": "aviso",
                "mensaje": _(
                    "La tasa de detracción (%(tasa)s%%) no coincide con la oficial del código "
                    "%(code)s (%(of)s%%). Verifícala antes de emitir."
                ) % {"tasa": self._l10n_pe_fmt(self.l10n_pe_ne_detraccion_rate or 0.0),
                     "code": code, "of": self._l10n_pe_fmt(oficial)},
            }]
        return []

    def _l10n_pe_ne_regla_exportacion_pais(self):
        """Exportación (tipOperacion 0200 = todas las líneas con afectación 9995): SUNAT exige el
        país del adquirente NO DOMICILIADO (codPaisCliente del AdditionalHeader). Sin país en el
        cliente el dato se omite del XML y la exportación se rechaza/observa."""
        if self._l10n_pe_tipo_operacion() != "0200":
            return []
        if not (self.partner_id.country_id.code or "").strip():
            return [{
                "code": "exportacion-pais", "campo": "codPaisCliente", "nivel": "error",
                "mensaje": _(
                    "Es una operación de exportación pero el cliente no tiene país. SUNAT exige "
                    "el país del adquirente no domiciliado: edítalo en el cliente y vuelve a "
                    "emitir."
                ),
            }]
        return []

    def _l10n_pe_ne_regla_exportacion_ruc(self):
        """Exportación (0200): el adquirente es un sujeto NO DOMICILIADO, que por definición no
        tiene RUC peruano — SUNAT espera identificarlo con carné de extranjería (4), pasaporte
        (7) o doc. no domiciliado sin RUC (0). Un RUC en una 0200 es sospechoso; se AVISA (no
        bloquea: el emisor puede tener un caso legítimo, y así la regla no rompe emisiones que
        SUNAT sí acepta)."""
        if self._l10n_pe_tipo_operacion() != "0200":
            return []
        if (self._l10n_pe_cliente_doc()[0] or "") != "6":
            return []
        return [{
            "code": "exportacion-ruc", "campo": "tipDocUsuario", "nivel": "aviso",
            "mensaje": _(
                "En una exportación el adquirente suele ser no domiciliado y no tener RUC. "
                "Verifica el tipo de documento del cliente (carné de extranjería, pasaporte o "
                "sin RUC): SUNAT puede observar una operación de exportación con RUC."
            ),
        }]

    def _l10n_pe_ne_regla_boleta_doc(self):
        """Boleta (03) mayor a S/ 700: SUNAT (Rgto. de Comprobantes de Pago, art. 8) exige
        identificar al adquirente con su documento cuando el importe SUPERA los S/ 700. Sin
        documento (consumidor final) la boleta se rechaza. Acepta cualquier documento válido —
        DNI/RUC/CE/pasaporte viajan en `vat`."""
        if self._l10n_pe_document_type() != "03":
            return []
        if (self.amount_total or 0.0) > 700 and not (self.partner_id.vat or "").strip():
            return [{
                "code": "boleta-700-doc", "campo": "cliente/numDoc", "nivel": "error",
                "mensaje": _(
                    "Una boleta mayor a S/ 700 requiere el documento de identidad del cliente "
                    "(DNI, RUC, carné de extranjería o pasaporte)."
                ),
            }]
        return []

    def _l10n_pe_ne_regla_vencidos(self, hoy=None):
        """Farma / perecibles: avisa si la venta despachó un lote VENCIDO. Lee el lote que la
        salida de stock reservó (FEFO: el que caduca antes sale primero); si ya venció, el
        negocio está entregando producto caducado. Es un AVISO —control de negocio/DIGEMID, no
        una regla de SUNAT—: no bloquea la emisión, pero salta en el pre-flight para que quien
        despacha lo vea antes de entregar. Solo aplica a ventas (out_invoice)."""
        if self.move_type != "out_invoice":
            return []
        hoy = hoy or self._l10n_pe_ne_today_lima()
        smls = self.env["stock.move.line"].search(
            [("move_id.l10n_pe_ne_move_id", "=", self.id)]
        )
        vencidos = []
        for sml in smls:
            venc = sml.lot_id.expiration_date
            if venc and venc.date() < hoy:
                vencidos.append(
                    "%s (lote %s, venció %s)"
                    % (sml.product_id.display_name, sml.lot_id.name, venc.date())
                )
        if vencidos:
            return [{
                "code": "vencido", "campo": "stock.lot", "nivel": "aviso",
                "mensaje": _(
                    "Se está despachando producto VENCIDO: %s. Revisa el lote antes de entregar."
                ) % "; ".join(vencidos),
            }]
        return []

    def _l10n_pe_ne_regla_convenio_cubierto(self):
        """Convenio/tercero pagador: el monto cubierto por el tercero no puede superar el importe a
        cobrar del comprobante (dejaría el copago del paciente en negativo)."""
        cubierto = self.l10n_pe_ne_monto_cubierto or 0.0
        if cubierto <= 0:
            return []
        cobrar = self._l10n_pe_importe_cobrar()
        if cubierto > cobrar + 0.005:
            return [{
                "code": "convenio-cubierto", "campo": "montoCubierto", "nivel": "error",
                "mensaje": _(
                    "El monto cubierto por el convenio (S/ %(cub).2f) supera el importe a cobrar "
                    "del comprobante (S/ %(cob).2f). El copago del paciente no puede ser negativo."
                ) % {"cub": cubierto, "cob": cobrar},
            }]
        return []

    def _l10n_pe_ne_tiene_controlado(self):
        """True si alguna línea de producto es una sustancia controlada (DIGEMID)."""
        return any(l.product_id.l10n_pe_ne_controlado for l in self._l10n_pe_product_lines())

    def _l10n_pe_ne_regla_controlado_receta(self):
        """Farma: la venta de un producto CONTROLADO (psicotrópico/estupefaciente) exige receta
        retenida — número de receta + colegiatura (CMP) del médico. Sin esos datos se bloquea."""
        if not self._l10n_pe_ne_tiene_controlado():
            return []
        if not (self.l10n_pe_ne_receta_numero or "").strip() or \
           not (self.l10n_pe_ne_receta_colegiatura or "").strip():
            return [{
                "code": "controlado-receta", "campo": "receta", "nivel": "error",
                "mensaje": _(
                    "La venta incluye un producto controlado: se requiere la receta retenida "
                    "(número de receta y colegiatura CMP del médico)."
                ),
            }]
        return []

    def _l10n_pe_ne_regla_linea_valor_cero(self):
        """SUNAT 2028: una línea de operación ONEROSA (gravada 1000, exonerada 9997, inafecta 9998,
        exportación 9995, IVAP 1016) no puede tener importe 0 — el valor de venta queda vacío y SUNAT
        rechaza con 'errorCode 2028 (nodo: /)'. Solo la línea GRATUITA (9996) admite valor 0 (su
        importe es referencial). Convierte el 2028 críptico en un mensaje accionable: poné precio o
        marcá la línea como gratuita."""
        # La NC de corrección por error en la descripción (motivo 03) lleva sus líneas a valor 0 por
        # diseño —solo corrige texto, no montos— y SUNAT la acepta: esta regla no aplica.
        if (self.l10n_pe_motivo_code or "").strip() == "03":
            return []
        malas = []
        for line in self._l10n_pe_product_lines():
            (_tip_afe, cod_tri, _nt, _ct, _cc), _por = self._l10n_pe_tax_info(line)
            if cod_tri == "9996":  # gratuito: el valor 0 es válido (precio referencial aparte)
                continue
            base, _igv, _isc, _icb = self._l10n_pe_line_amounts(line)
            if base <= 0.005:
                malas.append(line.product_id.display_name or line.name or _("(ítem sin nombre)"))
        if malas:
            return [{
                "code": "2028", "campo": "detalle/mtoValorVentaItem", "nivel": "error",
                "mensaje": _(
                    "Estas líneas están gravadas/afectas pero su importe es S/ 0.00, y SUNAT las "
                    "rechaza (error 2028): %(items)s. Ponles precio, o si no se cobran, márcalas "
                    "como gratuitas (bonificación)."
                ) % {"items": ", ".join(malas)},
            }]
        return []

    def _l10n_pe_ne_asegurar_valido(self):
        """Guard de emisión: corta con los errores accionables ANTES de enviar a SUNAT. Los
        avisos no bloquean (los consume el pre-flight de la SPA)."""
        self.ensure_one()
        errores = [f for f in self._l10n_pe_ne_validaciones() if f["nivel"] == "error"]
        if errores:
            detalle = "\n".join("• [%s] %s" % (e["code"], e["mensaje"]) for e in errores)
            raise UserError(
                _("El comprobante no cumple una regla de SUNAT:\n%s") % detalle
            )

    def _l10n_pe_relacionados(self):
        """Documentos relacionados de la factura: guía de remisión (indDocRelacionado 1,
        DespatchDocumentReference) y/o comprobante de anticipo (indDocRelacionado 2)."""
        rels = []
        # Orden de compra (indDocRelacionado 3 → cac:OrderReference). VA PRIMERO: en el UBL Invoice
        # el OrderReference precede a DespatchDocumentReference/AdditionalDocumentReference (orden de
        # elementos que el XSD de SUNAT exige), y el FTL emite los relacionados en el orden de la lista.
        oc = (self.l10n_pe_ne_orden_compra or "").strip()
        if oc:
            rels.append(
                {
                    "indDocRelacionado": "3",
                    "numDocRelacionado": oc,
                    "tipDocEmisor": "6",
                    "numDocEmisor": self.company_id.vat or "",
                }
            )
        guia = (self.l10n_pe_ne_guia_ref or "").strip()
        if guia:
            rels.append(
                {
                    "indDocRelacionado": "1",
                    "tipDocRelacionado": self.l10n_pe_ne_guia_tipo or "09",
                    "numDocRelacionado": guia,
                    "tipDocEmisor": "6",
                    "numDocEmisor": self.company_id.vat or "",
                }
            )
        # N AdditionalDocumentReference (uno por anticipo), numIdeAnticipo correlativo 1..N en el
        # orden de la lista — así SUNAT liga cada PrepaidPayment con su propio documento relacionado.
        lst = self._l10n_pe_ne_anticipos_list()
        for idx, a in enumerate(lst, start=1):
            rels.append(
                {
                    "indDocRelacionado": "2",
                    "tipDocRelacionado": a["tipo"] or "02",
                    "numDocRelacionado": a["doc"],
                    "numIdeAnticipo": str(idx),
                    "mtoDocRelacionado": self._l10n_pe_fmt(a["monto"]),
                    "tipDocEmisor": "6",
                    "numDocEmisor": self.company_id.vat or "",
                }
            )
        return rels

    def _l10n_pe_variables_globales(self):
        """Variables globales de la factura:
        - código 51: percepción (el agente percibe un % sobre la venta; el cliente paga total + percepción).
        - código 04: descuento global por anticipo (regulariza uno o más anticipos ya facturados;
          reduce la base del IGV en el valor AGREGADO de todos los anticipos). Exigido por SUNAT
          (regla 3287) cuando hay anticipo. Con N>1 anticipos se emite UN solo 04 con la suma —no uno
          por anticipo—, en línea con los N documentos relacionados (`_l10n_pe_relacionados`) que sí
          van uno por cada `AdditionalDocumentReference`/`numIdeAnticipo`."""
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
            # Descuento 04 con FACTOR UNITARIO: base = el propio valor del anticipo, factor 1.00000,
            # monto = valor. Así base × factor = monto EXACTO para cualquier importe, y la regla SUNAT
            # 4322 (|monto − base × factor| ≤ 1) pasa siempre. Antes se emitía base = base completa de la
            # operación con el factor a 5 decimales (valor/base): en operaciones de base alta (≳ S/ 200.000)
            # el redondeo del factor multiplicado por la base se desviaba > 1 sol y SUNAT rechazaba con
            # 4322. El IGV/base de cabecera NO cambian: SUNAT reduce la base gravada con el `Amount`
            # (mtoVariableGlobal = valor), no con el BaseAmount de este descuento.
            out.append(
                {
                    "tipVariableGlobal": "false",
                    "codTipoVariableGlobal": "04",
                    "porVariableGlobal": "1.00000",
                    "monMontoVariableGlobal": moneda,
                    "mtoVariableGlobal": fmt(valor),
                    "monBaseImponibleVariableGlobal": moneda,
                    "mtoBaseImpVariableGlobal": fmt(valor),
                }
            )
        # Descuento global que NO afecta la base del IGV (código del facturador en
        # DESC_GLOBAL_NO_AFECTA_COD, pendiente de confirmar contra beta). La base es el precio de
        # venta con IGV (amount_total): el descuento NO reduce gravada/IGV, solo el MtoImpVenta.
        desc_na = self._l10n_pe_desc_no_afecta()
        if desc_na > 0:
            # FACTOR UNITARIO (igual que el anticipo 04): base = el propio monto del descuento,
            # factor 1.00000, monto = base. Así base × factor = monto EXACTO para cualquier importe
            # y la regla SUNAT 4322 (|monto − base × factor| ≤ 1) pasa siempre. Antes se emitía
            # base = amount_total con el factor a 5 decimales (desc/base): en operaciones de base
            # alta (≳ S/ 200.000) el redondeo del factor × base se desviaba > 1 sol → rechazo 4322
            # (mismo bug que ya se corrigió en el anticipo). El XSL de SUNAT solo suma el `Amount`
            # (mtoVariableGlobal) de este código, nunca su BaseAmount → achicar la base no cambia nada.
            out.append(
                {
                    "tipVariableGlobal": "false",
                    "codTipoVariableGlobal": DESC_GLOBAL_NO_AFECTA_COD,
                    "porVariableGlobal": "1.00000",
                    "monMontoVariableGlobal": moneda,
                    "mtoVariableGlobal": fmt(desc_na),
                    "monBaseImponibleVariableGlobal": moneda,
                    "mtoBaseImpVariableGlobal": fmt(desc_na),
                }
            )
        return out

    @api.depends("journal_id", "partner_id", "move_type", "debit_origin_id",
                 "reversed_entry_id", "l10n_latam_document_type_id")
    def _compute_l10n_pe_serie(self):
        for move in self:
            serie = move.l10n_pe_serie or move.journal_id.l10n_pe_ne_serie or "F001"
            # La letra de la serie la manda la familia del comprobante (F factura / B boleta),
            # no el diario: con un solo diario de ventas la serie del diario es de una familia
            # y la boleta (cliente sin RUC) necesita la otra.
            if (
                move.state == "draft"
                and move.move_type in ("out_invoice", "out_refund")
                and move.partner_id
                and serie[:1].upper() in ("F", "B")
            ):
                prefix = move._l10n_pe_serie_prefix()
                if serie[:1].upper() != prefix:
                    serie = prefix + serie[1:]
            move.l10n_pe_serie = serie

    def _l10n_pe_detraccion_base(self):
        """Base de la detracción (SPOT) = importe de la operación ONEROSA = total − líneas
        gratuitas (9996) − descuento que NO afecta la base del IGV (cat. 53 cód. 03). El
        descuento no-afecta reduce el MtoImpVenta que paga el adquirente; las gratuitas no son
        operación onerosa sujeta al SPOT (amount_total las incluye vía grat_base). Sin excluir
        ambos se detrae de más y la base no coincide ni con el importe a cobrar (sumImpVenta) ni
        con lo que muestra el front. NO se descuenta el anticipo: la base es la de la operación."""
        self.ensure_one()
        return round((self.amount_total or 0.0) - self._l10n_pe_gratuito_base()
                     - self._l10n_pe_desc_no_afecta(), 2)

    def _l10n_pe_detraccion_monto(self):
        self.ensure_one()
        # SUNAT (SPOT): el monto de la detracción se redondea al ENTERO más próximo
        # (sin decimales), medio hacia arriba. Ej.: 12% de 25 386.52 = 3046.38 -> 3046.
        return float_round(
            self._l10n_pe_detraccion_base() * (self.l10n_pe_ne_detraccion_rate or 0.0) / 100.0,
            precision_digits=0,
            rounding_method="HALF-UP",
        )

    def _l10n_pe_neto_pendiente(self):
        """Neto pendiente de pago = lo que el cliente REALMENTE paga a crédito. Parte del importe
        a cobrar (que ya excluye los bienes GRATUITOS, el anticipo aplicado y el descuento que no
        afecta el IGV), menos la detracción (va al Banco de la Nación) y menos la inicial ya pagada
        al contado. Base ≠ base de detracción: aquella es el importe de la operación (con gratuitos
        y sin restar anticipo); usarla aquí hacía mtoNetoPendientePago > mtoImpVenta cuando había una
        línea gratuita (p.ej. total 2950 con gratuito 790 → neto 2950 > payable 2160) → rechazo SUNAT
        3265 ('El Monto neto pendiente de pago debe ser menor o igual al Importe total del comprobante')."""
        self.ensure_one()
        det = self._l10n_pe_detraccion_monto() if self.l10n_pe_ne_detraccion else 0.0
        # Venta con inicial al contado: el saldo a crédito (lo que suman las cuotas) es el importe
        # a cobrar menos la detracción, la inicial ya pagada y la retención de garantía de obra
        # (el cliente la retiene y la libera al final del contrato; se cobra menos AHORA).
        inicial = self.l10n_pe_ne_inicial_contado or 0.0
        return round(
            self._l10n_pe_importe_cobrar() - det - inicial
            - self._l10n_pe_ne_retencion_garantia_monto()
            - (self.l10n_pe_ne_amortizacion_adelanto or 0.0)
            - (self.l10n_pe_ne_penalidad or 0.0)
            - (self.l10n_pe_ne_monto_cubierto or 0.0), 2)

    def _l10n_pe_ne_retencion_garantia_monto(self):
        """Monto de la retención de garantía (obra) = % sobre el importe a cobrar. 0 si no aplica.
        No toca el total ni el IGV del comprobante; solo reduce el neto a cobrar de la valorización."""
        self.ensure_one()
        rate = self.l10n_pe_ne_retencion_garantia_rate or 0.0
        return round(self._l10n_pe_importe_cobrar() * rate / 100.0, 2) if rate else 0.0

    def _l10n_pe_adicional_cabecera(self):
        """Bloque adicional de la cabecera: detracción y/o total a cobrar de la percepción."""
        fmt = self._l10n_pe_fmt
        block = {}
        if self.l10n_pe_ne_detraccion:
            block.update(
                {
                    "ctaBancoNacionDetraccion": self.l10n_pe_ne_detraccion_cuenta
                    or self.company_id.l10n_pe_ne_cuenta_detraccion
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
        # Exportación (tipOperacion 0200): el adquirente es no domiciliado. SUNAT pide el país del
        # cliente (cat. país, ISO 3166 alpha-2 = el mismo code de res.country). El biller lo mapea a
        # codPaisCliente del AdditionalHeader. Se omite si el partner no tiene país (evita "" inútil).
        if self._l10n_pe_tipo_operacion() == "0200":
            pais = (self.partner_id.country_id.code or "").strip().upper()
            if pais:
                block["codPaisCliente"] = pais
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
            # Operación al contado con detracción: el neto pendiente es total − detracción
            # (lo que el cliente paga; la detracción va al Banco de la Nación).
            dato["mtoNetoPendientePago"] = self._l10n_pe_fmt(
                self._l10n_pe_neto_pendiente()
            )
            dato["tipMonedaMtoNetoPendientePago"] = moneda
        return dato

    def _l10n_pe_cuotas_netas(self):
        """Cuotas guardadas AJUSTADAS al neto pendiente. Con detracción, las cuotas pueden
        venir sobre el TOTAL (front antiguo, emisión masiva, API); se escalan al neto para
        que sumen exactamente el pendiente — la última absorbe el redondeo. Sin detracción
        el neto == total, así que no cambian. Garantiza sum(cuotas) == mtoNetoPendientePago
        pase lo que pase (SUNAT lo exige) y que el cliente no pague la parte detraída."""
        cuotas = [
            c
            for c in (self.l10n_pe_ne_cuotas or [])
            if c.get("fecha") and float(c.get("monto") or 0) > 0
        ]
        if not cuotas:
            return []
        neto = self._l10n_pe_neto_pendiente()
        suma = sum(float(c["monto"]) for c in cuotas)
        if suma <= 0 or abs(suma - neto) < 0.01:
            return [{"fecha": c["fecha"], "monto": round(float(c["monto"]), 2)} for c in cuotas]
        factor = neto / suma
        out, acc = [], 0.0
        for i, c in enumerate(cuotas):
            if i < len(cuotas) - 1:
                monto = round(float(c["monto"]) * factor, 2)
                acc += monto
            else:  # la última cuota cuadra el total al neto exacto
                monto = round(neto - acc, 2)
            out.append({"fecha": c["fecha"], "monto": monto})
        return out

    def _l10n_pe_credito_pendiente(self):
        """Monto neto pendiente del crédito = suma de las cuotas (ya ajustadas al neto);
        si no hay cuotas, el neto (total − detracción)."""
        netas = self._l10n_pe_cuotas_netas()
        return sum(c["monto"] for c in netas) if netas else self._l10n_pe_neto_pendiente()

    def _l10n_pe_detalle_pago(self):
        """detallePago (cuotas) para crédito: cuotas ajustadas al neto, o una = neto."""
        moneda = self.currency_id.name or "PEN"
        out = [
            {
                "mtoCuotaPago": self._l10n_pe_fmt(c["monto"]),
                "fecCuotaPago": c["fecha"],
                "tipMonedaCuotaPago": moneda,
            }
            for c in self._l10n_pe_cuotas_netas()
        ]
        if not out:
            fecha = self.invoice_date_due or self.invoice_date
            out = [
                {
                    "mtoCuotaPago": self._l10n_pe_fmt(self._l10n_pe_neto_pendiente()),
                    "fecCuotaPago": fecha.strftime("%Y-%m-%d") if fecha else "",
                    "tipMonedaCuotaPago": moneda,
                }
            ]
        return out

    @api.depends("l10n_pe_ne_cuotas")
    def _compute_l10n_pe_ne_cuotas_display(self):
        """Texto legible de las cuotas para el form de Odoo (fields.Json no tiene widget)."""
        for m in self:
            cuotas = m.l10n_pe_ne_cuotas or []
            m.l10n_pe_ne_cuotas_display = " · ".join(
                "%s @ %s" % (c.get("monto"), c.get("fecha")) for c in cuotas
            ) or False

    # Establecimiento anexo emisor (código SUNAT de 4 dígitos). Va como codLocalEmisor en el XML;
    # "0000" = domicilio fiscal. Para negocios con sucursales, cada comprobante declara su local.
    l10n_pe_ne_cod_establecimiento = fields.Char(
        string="Establecimiento emisor",
        default="0000",
        copy=False,
        help="Código de establecimiento anexo SUNAT (4 dígitos). '0000' = domicilio fiscal.",
    )
    # Guía de remisión que sustenta el traslado: va como cac:DespatchDocumentReference en el XML
    # de la factura (indDocRelacionado 1). QA-031.
    l10n_pe_ne_guia_ref = fields.Char(
        string="Guía de remisión referenciada",
        copy=False,
        help="Serie-número de la GRE que sustenta el traslado (ej. T001-00000123).",
    )
    l10n_pe_ne_guia_tipo = fields.Selection(
        [("09", "Guía de remisión remitente"), ("31", "Guía de remisión transportista")],
        string="Tipo de guía referenciada",
        default="09",
    )
    l10n_pe_ne_orden_compra = fields.Char(
        string="Orden de compra",
        copy=False,
        help="Número de orden de compra del cliente (opcional). Se emite como "
        "cac:OrderReference/cbc:ID (documento relacionado ind. 3), típico en ventas B2B.",
    )
    # DUA/DAM de exportación (QA-023). NO va al XML de la factura: la Declaración Aduanera de
    # Mercancías la genera ADUANAS *después* del comprobante comercial (por eso la exportación se
    # emite sin ella — QA-024) y el XSD SUNAT de la factura de exportación no tiene un campo para
    # el número de DUA. Se guarda como dato del ERP (data-of-record) para el archivo/reporte del
    # exportador y para poder asociarla luego. Es un Char informativo (sin efecto contable), así que
    # queda editable aun con el comprobante ya emitido/posteado — es lo que pide QA-024.
    l10n_pe_ne_dua = fields.Char(
        string="N° DUA/DAM (exportación)",
        copy=False,
        help="Número de la Declaración Aduanera de Mercancías (DUA/DAM) de la exportación. "
        "Opcional y editable después de emitir: aduanas la numera tras el comprobante. No se "
        "envía a SUNAT en el XML de la factura; queda como referencia en el ERP.",
    )
    l10n_pe_ne_placa = fields.Char(
        string="Placa del vehículo",
        copy=False,
        help="Solo factura de combustible: número de placa del vehículo. Se emite como "
        "cac:AdditionalItemProperty (catálogo 55, código 7000 «Gastos Art. 37 Renta: Número de "
        "Placa») en cada línea, para sustentar la deducción del gasto.")
    l10n_pe_ne_cliente_nombre = fields.Char(
        string="Nombre del cliente en el comprobante",
        copy=False,
        help="Override por-comprobante de la razón social del cliente (solo boleta ≤700: constancia "
        "institucional). Si está seteado, se emite en rznSocialUsuario en vez del nombre del partner, "
        "sin renombrar el partner del DNI.")
    # Ventas al Estado (proveedor del Estado): datos del proceso de contratación pública que
    # SUNAT exige como cac:AdditionalItemProperty (catálogo 55, códigos 5000-5003) en CADA línea.
    # Las reglas SUNAT 3146-3149 los validan como GRUPO: van los 4 juntos o ninguno.
    l10n_pe_ne_estado_expediente = fields.Char(
        string="N° de expediente (Estado)", copy=False,
        help="Ventas al Estado: número de expediente (cat. 55 cód. 5000).")
    l10n_pe_ne_estado_unidad_ejecutora = fields.Char(
        string="Código de unidad ejecutora (Estado)", copy=False,
        help="Ventas al Estado: código de unidad ejecutora (cat. 55 cód. 5001).")
    l10n_pe_ne_estado_proceso_seleccion = fields.Char(
        string="N° de proceso de selección (Estado)", copy=False,
        help="Ventas al Estado: número de proceso de selección/licitación (cat. 55 cód. 5002).")
    l10n_pe_ne_estado_contrato = fields.Char(
        string="N° de contrato (Estado)", copy=False,
        help="Ventas al Estado: número de contrato (cat. 55 cód. 5003).")
    # Proyecto/contrato (facturación por avance de obra): controla que la suma de las
    # valorizaciones no supere el valor total del contrato (QA-039).
    l10n_pe_ne_proyecto_id = fields.Many2one(
        "l10n_pe_ne.proyecto", string="Proyecto / contrato", copy=False,
        help="Contrato al que pertenece esta valorización. El total facturado no puede superar "
        "el valor del contrato.",
    )
    # N° de valorización dentro del contrato (1ª, 2ª, …). Se fija al emitir desde la valorización;
    # 0 = el comprobante no es una valorización de obra.
    l10n_pe_ne_valorizacion_nro = fields.Integer(
        string="N° de valorización", copy=False, default=0,
        help="Orden de esta valorización dentro del contrato (facturación por avance de obra).")
    l10n_pe_ne_retencion_garantia_rate = fields.Float(
        string="Retención de garantía %", copy=False,
        help="Retención de fiel cumplimiento (obra): % que el cliente retiene de la valorización "
        "y libera al final del contrato. NO es tributo ni descuento —no cambia el total ni el "
        "IGV del comprobante—: solo reduce el neto a cobrar de esta valorización.")
    l10n_pe_ne_amortizacion_adelanto = fields.Monetary(
        string="Amortización de adelanto", copy=False, currency_field="currency_id",
        help="Obra: parte del adelanto (directo/de materiales) que la entidad ya pagó y recupera "
        "en ESTA valorización. NO es el anticipo SUNAT (doc A/B): es una deducción contractual "
        "que no cambia el total ni el IGV, solo reduce el neto a cobrar y amortiza el adelanto.")
    # Penalidad del contrato (venta al Estado / obra): descuento fijo (S/) que la entidad aplica por
    # incumplimiento (plazos, calidad). Como la retención y la amortización, es una deducción
    # CONTRACTUAL: reduce el neto a cobrar de esta valorización/comprobante, no el total ni el IGV.
    l10n_pe_ne_penalidad = fields.Monetary(
        string="Penalidad del contrato", copy=False, currency_field="currency_id",
        help="Venta al Estado / obra: penalidad (S/) que la entidad descuenta por incumplimiento. "
        "Deducción contractual: reduce el neto a cobrar, no el total ni el IGV del comprobante.")
    # Conformidad / acta de recepción (venta al Estado): número o referencia del acta que la entidad
    # emite como requisito previo a facturar. Dato de registro del ERP (como la DUA): NO va al XML
    # firmado —el UBL no tiene campo— y queda editable aun con el comprobante emitido.
    l10n_pe_ne_conformidad = fields.Char(
        string="Conformidad / acta de recepción (Estado)", copy=False,
        help="Venta al Estado: N° o referencia del acta de conformidad/recepción previa a facturar. "
        "Dato del ERP para el sustento del expediente; no se envía a SUNAT en el XML.")
    # Convenio / tercero pagador (farma: SIS, aseguradora). El comprobante va al PACIENTE por el
    # total; la parte cubierta por el tercero reduce el neto que paga el paciente (copago) y queda
    # como cuenta por cobrar al tercero. No cambia el total ni el IGV del comprobante.
    l10n_pe_ne_tercero_pagador = fields.Char(
        string="Tercero pagador (convenio)", copy=False,
        help="Nombre del tercero que cubre parte de la venta (SIS, aseguradora, convenio).")
    l10n_pe_ne_monto_cubierto = fields.Monetary(
        string="Monto cubierto por el tercero", copy=False, currency_field="currency_id",
        help="Parte del importe a cobrar que paga el tercero (convenio). Reduce el neto del "
        "paciente (copago); no cambia el total ni el IGV.")
    # Receta retenida (farma): obligatoria cuando el comprobante vende un producto controlado.
    l10n_pe_ne_receta_numero = fields.Char(
        string="N° de receta", copy=False,
        help="Número de la receta retenida (venta de productos controlados).")
    l10n_pe_ne_receta_colegiatura = fields.Char(
        string="Colegiatura del médico (CMP)", copy=False,
        help="N° de colegiatura (CMP) del médico que prescribe (venta de productos controlados).")
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
    # Versión legible de las cuotas para el form de Odoo: fields.Json no tiene un widget de
    # form limpio, así que el contador ve las cuotas como texto "monto @ fecha" (solo lectura).
    l10n_pe_ne_cuotas_display = fields.Char(
        string="Cuotas de crédito",
        compute="_compute_l10n_pe_ne_cuotas_display",
    )
    # Forma de pago MIXTA: parte pagada al contado (inicial) + saldo a crédito en cuotas. El neto
    # pendiente (y por ende las cuotas y el mtoNetoPendientePago SUNAT) se reduce en esta inicial.
    l10n_pe_ne_inicial_contado = fields.Monetary(
        string="Inicial al contado",
        copy=False,
        help="Parte del total pagada al contado al emitir (venta con inicial + saldo a crédito). "
        "El saldo a crédito = total − detracción − inicial y es lo que suman las cuotas.",
    )
    l10n_pe_ne_medios_pago = fields.Json(
        string="Medios de pago (POS)", copy=False
    )  # [{'medio','monto'}]
    l10n_pe_ne_bancarizacion = fields.Selection(
        [('no_aplica', 'No aplica'), ('pendiente', 'Pendiente'), ('bancarizado', 'Bancarizado')],
        string="Bancarización (Ley 28194)", default='no_aplica', copy=False,
        help="Seguimiento del uso de medio de pago para operaciones ≥ S/2,000 o US$500.")
    l10n_pe_ne_bancarizacion_constancia = fields.Char(string="Constancia de bancarización", copy=False)
    l10n_pe_ne_bancarizacion_fecha = fields.Date(string="Fecha de bancarización", copy=False)
    l10n_pe_ne_bancarizacion_medio = fields.Char(string="Medio de bancarización", copy=False)
    l10n_pe_ne_bancarizacion_doc = fields.Binary(
        string="Documento de bancarización", attachment=True, copy=False,
        help="Voucher/constancia del banco que sustenta la bancarización (Ley 28194).")
    l10n_pe_ne_bancarizacion_doc_name = fields.Char(string="Nombre del documento", copy=False)
    # Redondeo de efectivo (Ley 29571 + retiro de monedas < S/ 0.10): ajuste ≤ 0 a favor del
    # consumidor sobre el total a cobrar EN EFECTIVO. NO va al XML/comprobante (amount_total sigue
    # exacto); es un dato de caja: el arqueo espera 'amount_total + redondeo' de efectivo, y el
    # ticket muestra 'A pagar efectivo'. Ver _l10n_pe_ne_ticket_adicional y l10n_pe_ne_caja.
    l10n_pe_ne_redondeo = fields.Monetary(
        string="Redondeo efectivo",
        copy=False,
        help="Ajuste (≤ 0) del importe cobrado en efectivo por redondeo al décimo. No altera el "
        "comprobante ni las bases/IGV; solo el efectivo cobrado y el arqueo de caja.",
    )

    l10n_pe_motivo_code = fields.Char(
        string="Cód. motivo NC/ND",
        default="01",
        copy=False,
        help="Código SUNAT del motivo de la nota de crédito (cat. 09) o débito (cat. 10).",
    )
    l10n_pe_motivo_desc = fields.Char(
        string="Motivo/sustento NC/ND",
        copy=False,
        help="Motivo o sustento (texto libre) de la nota. Si se omite, se usa la "
             "descripción del catálogo correspondiente al código de motivo.",
    )
    l10n_pe_biller_xml = fields.Many2one(
        "ir.attachment", string="XML UBL firmado", copy=False
    )
    l10n_pe_biller_cdr = fields.Many2one(
        "ir.attachment", string="CDR SUNAT", copy=False
    )
    # Modo instantáneo: tras FIRMAR se guarda el ZIP de ENVI + el filename/canal para que el
    # cron envíe a SUNAT en 2º plano. Se limpian al recibir el CDR (ya no hay nada pendiente).
    l10n_pe_ne_envi_zip = fields.Text(
        string="ZIP ENVI pendiente (base64)", copy=False,
        help="ZIP de ENVI firmado, aún no enviado a SUNAT. El cron lo envía y lo limpia al aceptarse.")
    l10n_pe_ne_biller_filename = fields.Char(string="Nombre de archivo del facturador", copy=False)
    l10n_pe_ne_biller_canal = fields.Char(string="Canal SUNAT (GEM/OTROS_CPE)", copy=False)
    l10n_pe_ne_envio_intentos = fields.Integer(string="Intentos de envío a SUNAT", default=0, copy=False)
    l10n_pe_ne_stock_aviso = fields.Char(
        string="Aviso de inventario",
        copy=False,
        readonly=True,
        help="Por qué no se pudo mover el inventario de este documento. El comprobante es "
        "válido igual: el stock nunca lo tumba. Vacío = el movimiento se hizo.",
    )

    # Resumen Diario de boletas (RC) idempotente: al enviar se guarda el TICKET; el poll usa el
    # ticket (no re-envía → no duplica). Correlativo/fecha del RC al que pertenece la boleta.
    l10n_pe_ne_rc_ticket = fields.Char(string="Ticket del Resumen Diario", copy=False)
    l10n_pe_ne_rc_correlativo = fields.Char(string="Correlativo del Resumen Diario", copy=False)
    l10n_pe_ne_rc_fecha = fields.Date(string="Fecha del Resumen Diario", copy=False)
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

    def _l10n_pe_fmt_unit(self, amount):
        # Valores UNITARIOS (valor/precio unitario): SUNAT admite hasta 10 decimales. A 2 decimales,
        # `mtoValorUnitario × cantidad` se desviaba de `mtoValorVentaItem` en líneas de alta cantidad
        # (qty ≳ 200 con valor sin-IGV no terminante, p.ej. 10/1.18 → > 1 sol → rechazo 3271/4288).
        # Se mantiene "%.2f" cuando el valor YA es exacto a 2 decimales (compat con los tests y la
        # referencia SUNAT) y se amplía a 8 decimales SOLO cuando hace falta para reconciliar.
        amount = amount or 0.0
        r2 = round(amount, 2)
        if abs(amount - r2) < 1e-9:
            return "%.2f" % r2
        return ("%.8f" % amount).rstrip("0")

    def _l10n_pe_fmt_cant(self, qty):
        """Cantidad para SUNAT (ctdUnidadItem): hasta 3 decimales, sin ceros de relleno más allá
        de 2. Conserva la venta al peso de balanza (18.375) sin ensuciar los conteos (2 -> 2.00).
        SUNAT admite hasta 10 decimales; `_l10n_pe_fmt` (2 dec) es solo para montos."""
        entero, _p, dec = ("%.3f" % (qty or 0.0)).partition(".")
        dec = dec.rstrip("0")
        if len(dec) < 2:
            dec = (dec + "00")[:2]
        return "%s.%s" % (entero, dec)

    def _l10n_pe_ne_bancarizacion_estado(self):
        """Estado de bancarización derivado del total, moneda y medios (efectivo no bancariza).
        Solo factura (01) en PEN/USD; boleta/NC/ND/otra moneda → no_aplica."""
        self.ensure_one()
        UMBRAL = {"PEN": 2000.0, "USD": 500.0}
        umbral = UMBRAL.get(self.currency_id.name or "PEN")
        tipo = self.l10n_pe_ne_tipo_doc or self._l10n_pe_document_type()
        if self.move_type != "out_invoice" or self.debit_origin_id or tipo != "01" or umbral is None:
            return "no_aplica"
        if (self.amount_total or 0.0) < umbral:
            return "no_aplica"
        medios = self.l10n_pe_ne_medios_pago or []
        bancariza = any(m.get("medio") != "Efectivo" and float(m.get("monto") or 0) > 0 for m in medios)
        return "bancarizado" if bancariza else "pendiente"

    def l10n_pe_ne_marcar_bancarizado(self, payload=None):
        """Marca la factura como bancarizada + guarda la constancia (texto/fecha/medio) y,
        opcional, el documento de respaldo (PDF/JPG/PNG ≤ 5MB). Re-llamarla con otro doc lo
        reemplaza; sin doc, el documento existente se conserva."""
        self.ensure_one()
        payload = payload or {}
        doc = payload.get("doc")
        if doc:
            try:
                raw = base64.b64decode(doc, validate=True)
            except Exception:
                raise UserError(_("El documento de bancarización no es un archivo válido."))
            if len(raw) > 5 * 1024 * 1024:
                raise UserError(_("El documento de bancarización no puede superar los 5 MB."))
            # Magic-number: PDF %PDF, JPEG \xff\xd8, PNG \x89PNG. La extensión sola no basta.
            es_pdf = raw[:5] == b"%PDF-"
            es_jpg = raw[:3] == b"\xff\xd8\xff"
            es_png = raw[:8] == b"\x89PNG\r\n\x1a\n"
            if not (es_pdf or es_jpg or es_png):
                raise UserError(_("El documento debe ser PDF, JPG o PNG."))
            self.l10n_pe_ne_bancarizacion_doc = doc.encode() if isinstance(doc, str) else doc
            nombre = (payload.get("docName") or "").strip() or ("voucher.pdf" if es_pdf else "voucher.png" if es_png else "voucher.jpg")
            self.l10n_pe_ne_bancarizacion_doc_name = nombre
        self.l10n_pe_ne_bancarizacion = "bancarizado"
        if payload.get("constancia"):
            self.l10n_pe_ne_bancarizacion_constancia = payload["constancia"]
        if payload.get("fecha"):
            self.l10n_pe_ne_bancarizacion_fecha = payload["fecha"]
        if payload.get("medio"):
            self.l10n_pe_ne_bancarizacion_medio = payload["medio"]
        return {"ok": True, "bancarizacion": self.l10n_pe_ne_bancarizacion}

    def _l10n_pe_ne_bancarizacion_doc_bytes(self):
        """(raw, filename, content_type) del documento de bancarización, o None si no hay."""
        self.ensure_one()
        if not self.l10n_pe_ne_bancarizacion_doc:
            return None
        raw = base64.b64decode(self.l10n_pe_ne_bancarizacion_doc)
        name = self.l10n_pe_ne_bancarizacion_doc_name or "bancarizacion"
        ext = (name.rsplit(".", 1)[-1] or "").lower()
        ct = {"pdf": "application/pdf", "png": "image/png",
              "jpg": "image/jpeg", "jpeg": "image/jpeg"}.get(ext, "application/octet-stream")
        return raw, name, ct

    def _l10n_pe_document_type(self):
        """Código SUNAT del comprobante: 01 Factura, 03 Boleta, 04 Liquidación de compra, 07 NC,
        08 ND."""
        self.ensure_one()
        # Liquidación de compra: es un in_invoice (compra) que igual se emite a SUNAT como 04.
        # Va primero porque para un in_invoice el resto del método devolvería '03' por defecto.
        if self.l10n_pe_ne_liquidacion:
            return "04"
        if self.move_type == "out_refund":
            return "07"
        if self.move_type == "out_invoice" and self.debit_origin_id:
            return "08"
        # Una exportación es siempre Factura (01), aunque el adquirente extranjero no tenga RUC
        # (si fuese Boleta 03 con serie F, el validador de factura la rechaza por tipo/serie).
        if self.move_type == "out_invoice" and self._l10n_pe_tipo_operacion() == "0200":
            return "01"
        # El tipo elegido en el comprobante manda: a un cliente con RUC se le puede emitir
        # Boleta (compra como consumidor final). El documento de identidad solo decide
        # cuando no hay tipo elegido (diario sin documentos latam, flujos por código).
        if self.move_type == "out_invoice":
            code = self.l10n_latam_document_type_id.code
            if code in ("01", "03"):
                return code
        vat_code = (
            self.partner_id.l10n_latam_identification_type_id.l10n_pe_vat_code or ""
        )
        return "01" if vat_code == "6" else "03"

    def _l10n_pe_serie_prefix(self):
        """Letra que SUNAT exige en la serie: F para Factura (01) y sus notas, B para Boleta (03)
        y las suyas, E para Liquidación de compra (04). En NC/ND manda la familia del documento
        afectado, no el partner."""
        self.ensure_one()
        # La liquidación de compra electrónica usa serie que empieza con 'E' (4 posiciones).
        if self.l10n_pe_ne_liquidacion:
            return "E"
        origin = self.reversed_entry_id or self.debit_origin_id
        if origin:
            tipo = origin.l10n_pe_ne_tipo_doc or origin._l10n_pe_document_type()
        else:
            tipo = self._l10n_pe_document_type()
        if tipo not in ("01", "03"):  # NC/ND sin documento afectado: decide el cliente
            vat_code = (
                self.partner_id.l10n_latam_identification_type_id.l10n_pe_vat_code or ""
            )
            tipo = "01" if vat_code == "6" else "03"
        return "B" if tipo == "03" else "F"

    def _l10n_pe_check_serie(self):
        """Serie de familia equivocada (p.ej. F001 en una boleta) es rechazo seguro de SUNAT;
        se corta aquí antes de enviar/encolar."""
        self.ensure_one()
        serie, _corr = self._l10n_pe_serie_correlativo()
        prefix = self._l10n_pe_serie_prefix()
        if (serie or "")[:1].upper() != prefix:
            docname = {
                "01": _("Factura"),
                "03": _("Boleta"),
                "07": _("Nota de Crédito"),
                "08": _("Nota de Débito"),
            }.get(self._l10n_pe_document_type(), "")
            raise UserError(
                _(
                    "La serie '%(serie)s' no corresponde al tipo de comprobante: una %(doc)s "
                    "debe usar una serie que empiece con '%(prefix)s' (p.ej. %(prefix)s001)."
                )
                % {"serie": serie, "doc": docname, "prefix": prefix}
            )
        # QA-074: la serie debe estar HABILITADA para el emisor. Una serie inventada (p.ej. F099
        # tecleada a mano) la acepta la beta de SUNAT pero en producción se rechaza; se corta aquí.
        habilitadas = self._l10n_pe_ne_series_habilitadas()
        if (serie or "").upper() not in habilitadas:
            raise UserError(
                _(
                    "La serie '%(serie)s' no está habilitada para %(ruc)s. Configúrala en un "
                    "diario de venta (campo Serie) o usa una serie registrada: %(lista)s."
                )
                % {
                    "serie": serie,
                    "ruc": self.company_id.vat or self.company_id.display_name or "",
                    "lista": ", ".join(sorted(habilitadas)),
                }
            )

    def _l10n_pe_ne_series_habilitadas(self):
        """Series válidas del emisor (QA-074): las configuradas en sus diarios de venta
        (l10n_pe_ne_serie) con su variante de familia (F↔B), más los defaults que genera el
        sistema (F001/B001 y las notas FC01/FD01/BC01/BD01). No se usa el histórico de series
        ya emitidas a propósito: una serie inventada usada por error no debe volverse 'válida'."""
        self.ensure_one()
        # E001 = serie por defecto de la liquidación de compra electrónica (tipo 04).
        validas = {"F001", "B001", "FC01", "FD01", "BC01", "BD01", "E001"}
        journals = self.env["account.journal"].sudo().search(
            [
                ("company_id", "=", self.company_id.id),
                ("type", "in", ("sale", "purchase")),
                ("l10n_pe_ne_serie", "!=", False),
            ]
        )
        for j in journals:
            base = (j.l10n_pe_ne_serie or "").upper().strip()
            if len(base) >= 2 and base[0] in ("F", "B"):
                validas.add("F" + base[1:])
                validas.add("B" + base[1:])
            elif len(base) >= 2 and base[0] == "E":
                validas.add(base)
        return validas

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

    @staticmethod
    def _l10n_pe_ne_bolsas(qty):
        """Nº de bolsas para el ICBPER. SUNAT cuenta la bolsa como unidad DISCRETA
        (ctdBolsasTriIcbperItem es entero; no existe fracción de bolsa), así que la cantidad
        se lleva al entero. Redondeo comercial (mitad hacia arriba) para coincidir con el
        front (Math.round) y que el total del carrito == el total emitido. Fuente ÚNICA del
        conteo de bolsas: base, IGV, ICBPER por ítem y ctdBolsas salen todos de aquí."""
        n = float(qty or 0.0)
        return int(n + 0.5) if n >= 0 else -int(-n + 0.5)

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
            round(self._l10n_pe_ne_bolsas(line.quantity) * icbper_tax.amount, 2)
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
        """Código de unidad SUNAT (cat. 03) de la línea: si es venta fraccionada, la sub-unidad del
        producto; luego override por línea, el guardado en el producto (POS/masiva no mandan unidad
        por línea), override manual en la UoM, mapeo por XMLID de la unidad estándar de Odoo, si no
        'NIU'."""
        if line.l10n_pe_ne_fraccionado:
            return line.product_id.l10n_pe_ne_unidad_fraccion or DEFAULT_UNIT_CODE
        if line.l10n_pe_ne_unit_code:
            return line.l10n_pe_ne_unit_code
        if line.product_id.l10n_pe_ne_unit_code:
            return line.product_id.l10n_pe_ne_unit_code
        uom = line.product_uom_id
        if not uom:
            return DEFAULT_UNIT_CODE
        if uom.l10n_pe_ne_unit_code:
            return uom.l10n_pe_ne_unit_code
        xmlid = uom.get_external_id().get(uom.id, "")
        return UOM_CODE_BY_XMLID.get(xmlid, DEFAULT_UNIT_CODE)

    _L10N_PE_ANTICIPO_PREFIX = "PAGO ANTICIPADO"

    def _l10n_pe_ne_lotes_linea(self, line):
        """(nombre, vencimiento) de los lotes que la salida de stock reservó para el producto de
        la línea, SOLO si el producto rastrea vencimiento (farma/perecibles). Vacío si no aplica.
        Sirve para anotar lote y caducidad en la descripción del ítem (trazabilidad y canje)."""
        prod = line.product_id
        if not prod or not prod.use_expiration_date:
            return []
        smls = self.env["stock.move.line"].search([
            ("move_id.l10n_pe_ne_move_id", "=", self.id),
            ("product_id", "=", prod.id),
        ])
        return [(sml.lot_id.name, sml.lot_id.expiration_date) for sml in smls if sml.lot_id]

    def _l10n_pe_des_item(self, line):
        """Descripción del ítem para el XML. En un comprobante marcado como pago anticipado
        (doc. A) antepone 'PAGO ANTICIPADO' para que el documento identifique la operación sin
        depender de una leyenda cat. 52 (que no existe para anticipos). En productos que rastrean
        vencimiento (farma/perecibles) anexa el lote y la caducidad despachados, para que queden
        en el comprobante (XML y PDF) sin campos nuevos ni cambios en el micro/plantilla."""
        desc = line.name or line.product_id.display_name or ""
        if self.l10n_pe_ne_es_anticipo and not desc.startswith(self._L10N_PE_ANTICIPO_PREFIX):
            desc = ("%s - %s" % (self._L10N_PE_ANTICIPO_PREFIX, desc)).strip(" -")
        lotes = self._l10n_pe_ne_lotes_linea(line)
        if lotes:
            etqs = []
            for nombre, venc in lotes:
                etq = "Lote %s" % nombre
                if venc:
                    etq += " Vence %s" % venc.date().strftime("%d/%m/%Y")
                etqs.append(etq)
            desc = "%s | %s" % (desc, " · ".join(etqs))
        reg = (line.product_id.l10n_pe_ne_registro_sanitario or "").strip()
        if reg:
            desc = "%s · Reg. San. %s" % (desc, reg)
        if line.product_id.l10n_pe_ne_controlado and (self.l10n_pe_ne_receta_numero or "").strip():
            desc = "%s · Receta %s (CMP %s)" % (
                desc, self.l10n_pe_ne_receta_numero.strip(),
                (self.l10n_pe_ne_receta_colegiatura or "").strip())
        return desc

    def _l10n_pe_detalle(self):
        fmt = self._l10n_pe_fmt
        detalle = []
        for line in self._l10n_pe_product_lines():
            (tip_afe, cod_tri, nom_trib, cod_tip_trib, _cod_cat), por_igv = (
                self._l10n_pe_tax_info(line)
            )
            # Gratuita: si la línea precisa el sub-tipo (retiro 13, bonificación 15, …) se usa ese
            # código de catálogo 07 en vez del genérico 11. La estructura UBL gratuita es idéntica.
            if cod_tri == "9996" and line.l10n_pe_ne_afectacion_gratuita:
                tip_afe = line.l10n_pe_ne_afectacion_gratuita
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
                "ctdUnidadItem": self._l10n_pe_fmt_cant(qty),
                "desItem": self._l10n_pe_des_item(line),
                "mtoValorUnitario": self._l10n_pe_fmt_unit(gross / qty if qty else 0.0),
                "mtoValorVentaItem": fmt(base),
                # Precio de venta unitario = (valor venta + ISC + IGV) / cantidad; NO incluye el ICBPER.
                "mtoPrecioVentaUnitario": self._l10n_pe_fmt_unit((base + isc + igv) / qty if qty else 0.0),
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
                        "mtoValorReferencialUnitario": self._l10n_pe_fmt_unit(gross / qty if qty else 0.0),
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
                        "ctdBolsasTriIcbperItem": str(self._l10n_pe_ne_bolsas(qty)),
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

    @api.model
    def _l10n_pe_ne_today_lima(self):
        """Fecha de HOY en hora local de Perú (América/Lima, UTC-5).

        Evita el descuadre de zona horaria: `fields.Date.context_today` cae a UTC
        cuando el usuario no tiene tz configurada, así que de noche (después de las
        7pm Lima = medianoche UTC) devuelve el día SIGUIENTE. Eso hacía que fecEmision
        saltara un día respecto a horEmision (que sí fuerza América/Lima)."""
        return (
            pytz.utc.localize(fields.Datetime.now())
            .astimezone(pytz.timezone("America/Lima"))
            .date()
        )

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
        # Descuento global que NO afecta la base del IGV: baja el importe a cobrar (MtoImpVenta) y va
        # como AllowanceCharge global (sumDescTotal), SIN tocar la base gravada ni el IGV. Mismo estilo
        # de ajuste solo-de-emisión que el anticipo (no agrega línea a Odoo).
        desc_no_afecta = self._l10n_pe_desc_no_afecta()
        cabecera = {
            "tipOperacion": self._l10n_pe_tipo_operacion(),
            "fecEmision": self.invoice_date.strftime("%Y-%m-%d")
            if self.invoice_date
            else "",
            # Hora de emisión en hora local de Perú (América/Lima, UTC-5). `fields.Datetime.now()`
            # es UTC-naive: sin convertir, el comprobante salía +5h (bug de zona horaria).
            "horEmision": pytz.utc.localize(fields.Datetime.now())
            .astimezone(pytz.timezone("America/Lima"))
            .strftime("%H:%M:%S"),
            # Vencimiento AUTOMÁTICO (no editable), siempre presente:
            #  - Crédito → la última cuota (invoice_date_due la fija quick_flags desde las cuotas).
            #  - Contado → la propia fecha de EMISIÓN (pago inmediato: vence el mismo día).
            # Se usa invoice_date explícito para el contado porque Odoo autopobla invoice_date_due con
            # la fecha contable/HOY, que no siempre coincide con la de emisión (facturas con fecha atrás).
            "fecVencimiento": (
                self.invoice_date_due
                if (self.l10n_pe_ne_forma_pago == "Credito" and self.invoice_date_due)
                else self.invoice_date
            ).strftime("%Y-%m-%d")
            if self.invoice_date
            else "",
            "codLocalEmisor": (self.l10n_pe_ne_cod_establecimiento or "0000"),
            "tipDocUsuario": self._l10n_pe_cliente_doc()[0],
            "numDocUsuario": self._l10n_pe_cliente_doc()[1],
            "rznSocialUsuario": self.l10n_pe_ne_cliente_nombre or partner.name or "",
            "tipMoneda": self.currency_id.name or "PEN",
            # El IGV teórico del gratuito NO se cobra: NO entra en el total de tributos de cabecera
            # (regla 4301: el TaxAmount de cabecera excluye el 9996). El 9996 va solo como TaxSubtotal.
            "sumTotTributos": fmt(self.amount_tax - anticipo_igv),
            "sumTotValVenta": fmt(self.amount_untaxed - grat_base),
            # TaxInclusiveAmount: INCLUYE el ICBPER (igual que la ref. enterprise: PayableAmount =
            # TaxInclusive − anticipo, ambos con el ICBPER). Excluirlo de aquí pero incluirlo en
            # sumImpVenta desbalancea el comprobante → SUNAT Client.3280.
            "sumPrecioVenta": fmt(self.amount_total - grat_base),
            "sumImpVenta": fmt(
                self.amount_total - anticipo_total - grat_base - desc_no_afecta
            ),
            "sumDescTotal": fmt(desc_no_afecta),
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
        """Serie y correlativo del comprobante. Una vez emitido, la identidad fiscal es
        inmutable: se devuelve la serie/correlativo CONGELADOS (l10n_pe_ne_serie/corr_emit),
        que ahora salen de una secuencia POR SERIE (ver _l10n_pe_ne_assign_numero). Para un
        move aún no emitido (previsualización) se cae al comportamiento anterior: el manual si
        se fijó; si no, el folio (parte numérica final) del número del asiento; si no hay, '1'."""
        self.ensure_one()
        # Retrocompatible: en los comprobantes históricos corr_emit == folio, así que esto
        # devuelve el mismo valor de antes; solo las emisiones nuevas usan la secuencia por serie.
        if self.l10n_pe_ne_serie_emit and self.l10n_pe_ne_corr_emit:
            try:
                return self.l10n_pe_ne_serie_emit, str(int(self.l10n_pe_ne_corr_emit))
            except (TypeError, ValueError):
                return self.l10n_pe_ne_serie_emit, self.l10n_pe_ne_corr_emit
        name = (self.name or "").replace(" ", "")
        matches = list(re.finditer(r"\d+", name))
        folio = matches[-1].group() if matches else None
        serie = self.l10n_pe_serie or self.journal_id.l10n_pe_ne_serie or "F001"
        correlativo = self.l10n_pe_correlativo or folio or "1"
        return serie, correlativo

    def _l10n_pe_ne_next_correlativo(self, company, serie):
        """Correlativo por (compañía, serie): SUNAT exige numeración correlativa POR SERIE y por
        RUC. Con un contador global (el folio del diario) la serie F001 se saltaba números cuando
        una boleta B001 o una nota FC01 tomaban el correlativo intermedio (hueco por serie → riesgo
        de observación en el RVIE). Crea una ir.sequence 'no_gap' al primer uso, sembrada tras el
        correlativo más alto ya emitido en esa serie (migración transparente desde el folio global).
        Mismo patrón, ya probado, que las Guías de Remisión (l10n_pe_ne_guia_remision)."""
        code = "l10n_pe.ne.cpe.%s" % serie
        # Lock consultivo: serializa el primer uso de una (serie, compañía) para no crear la
        # secuencia dos veces en concurrencia; después la unicidad la garantiza 'no_gap' (que
        # bloquea la fila de ir_sequence en cada next_by_id → dos cajas no obtienen el mismo nº).
        self.env.cr.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            ("%s/%s" % (code, company.id),),
        )
        Seq = self.env["ir.sequence"].sudo()
        seq = Seq.search(
            [("code", "=", code), ("company_id", "=", company.id)], limit=1
        )
        if not seq:
            ultimo = 0
            for m in self.sudo().search(
                [
                    ("company_id", "=", company.id),
                    ("l10n_pe_ne_serie_emit", "=", serie),
                    ("l10n_pe_ne_corr_emit", "!=", False),
                ]
            ):
                try:
                    ultimo = max(ultimo, int(m.l10n_pe_ne_corr_emit or 0))
                except (TypeError, ValueError):
                    pass
            seq = Seq.create(
                {
                    "name": "CPE %s (%s)" % (serie, company.display_name),
                    "code": code,
                    "company_id": company.id,
                    "padding": 1,
                    "number_increment": 1,
                    "implementation": "no_gap",
                    "number_next": ultimo + 1,
                }
            )
        return str(seq.next_by_id())

    def _l10n_pe_ne_assign_numero(self):
        """Fija (una sola vez) la serie+correlativo FISCAL antes de construir el payload/firmar.
        Idempotente: si ya está asignado no hace nada. Respeta un correlativo manual si se fijó;
        si no, lo toma de la secuencia POR SERIE. A partir de aquí _l10n_pe_serie_correlativo()
        devuelve estos valores congelados en todo el flujo (payload, XML, QR, PDF, baja)."""
        self.ensure_one()
        if self.l10n_pe_ne_corr_emit:
            return
        serie = self.l10n_pe_serie or self.journal_id.l10n_pe_ne_serie or "F001"
        if self.l10n_pe_correlativo:
            corr = str(self.l10n_pe_correlativo).strip()
        else:
            corr = self._l10n_pe_ne_next_correlativo(self.company_id, serie)
        self.l10n_pe_ne_serie_emit = serie
        self.l10n_pe_ne_corr_emit = corr.zfill(8)

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
        _logger.info("---------------------------------------- Invoice request ------------------------------------------------")
        _logger.info(
            "%s %s %s",
            self._l10n_pe_id_block(with_document_type=True),
            self._l10n_pe_emisor(),
            self._l10n_pe_cabecera(),
        )
        _logger.info("---------------------------------------- Invoice request ------------------------------------------------")
        self.ensure_one()
        self._l10n_pe_check_lineas_impuesto()
        self._l10n_pe_check_anticipo()
        self._l10n_pe_ne_asegurar_valido()   # L1: reglas SUNAT (3265, boleta>700, detracción, …)
        _logger.info("Product lines: %s", len(self._l10n_pe_product_lines()))
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
                    # Factor con 5 decimales: SUNAT valida mtoVariable ≈ base × porVariable (error 3290,
                    # "cargo/descuento por ítem difiere"). Con 2 decimales, un descuento en monto fijo
                    # (p.ej. S/50 sobre 470 → 10.6383% → 0.11) descuadra y se rechaza; 5 decimales
                    # reconstruyen el monto dentro de la tolerancia.
                    "porVariable": "%.5f" % (line.discount / 100.0),
                    "monMontoVariable": moneda,
                    "mtoVariable": fmt(disc),
                    "monBaseImponibleVariable": moneda,
                    "mtoBaseImpVariable": fmt(gross),
                }
            )
        # Ventas al Estado: 4 propiedades del proceso de contratación pública (cat. 55) por CADA
        # línea, como cac:AdditionalItemProperty. SUNAT (reglas 3146-3149) las valida como GRUPO:
        # van las 4 juntas o ninguna → solo se emiten si están las 4 completas.
        estado = [
            ("5000", "Numero de Expediente", self.l10n_pe_ne_estado_expediente),
            ("5001", "Codigo de Unidad Ejecutora", self.l10n_pe_ne_estado_unidad_ejecutora),
            ("5002", "Numero de Proceso de Seleccion", self.l10n_pe_ne_estado_proceso_seleccion),
            ("5003", "Numero de Contrato", self.l10n_pe_ne_estado_contrato),
        ]
        if all((v or "").strip() for _c, _n, v in estado):
            for li in range(1, idx + 1):  # idx = nº de líneas de producto contadas arriba
                for cod, nom, val in estado:
                    out.append(
                        {
                            "idLinea": str(li),
                            # no es descuento/cargo: "-" salta el loop de AllowanceCharge por ítem
                            "codTipoVariable": "-",
                            # dispara el bloque cac:AdditionalItemProperty en el FTL
                            "nomPropiedad": nom,
                            "codPropiedad": cod,
                            "valPropiedad": val.strip(),
                            "codBienPropiedad": "-",
                            "fecInicioPropiedad": "-",
                            "horInicioPropiedad": "-",
                            "fecFinPropiedad": "-",
                            "numDiasPropiedad": "-",
                        }
                    )
        # Placa del vehículo (factura de combustible): cac:AdditionalItemProperty cat-55 código 7000
        # (Gastos Art. 37 Renta) en CADA línea. Solo factura (la deducción Art. 37 es factura-only).
        # l10n_pe_ne_tipo_doc recién se congela al emitir (_l10n_pe_apply_emission_response /
        # _l10n_pe_apply_signed): en la primera emisión, mientras se arma este payload, todavía
        # está vacío. Usar `or "01"` aquí lo hacía SIEMPRE factura y filtraba la placa también
        # en boletas. El idioma correcto (igual que en el resto del archivo) es
        # `l10n_pe_ne_tipo_doc or _l10n_pe_document_type()`.
        if self.l10n_pe_ne_placa and (self.l10n_pe_ne_tipo_doc or self._l10n_pe_document_type()) == "01":
            for li in range(1, idx + 1):
                out.append({
                    "idLinea": str(li),
                    "codTipoVariable": "-",
                    "nomPropiedad": "Numero de Placa",
                    "codPropiedad": "7000",
                    "valPropiedad": self.l10n_pe_ne_placa.strip(),
                    "codBienPropiedad": "-",
                    "fecInicioPropiedad": "-",
                    "horInicioPropiedad": "-",
                    "fecFinPropiedad": "-",
                    "numDiasPropiedad": "-",
                })
        return out

    def _l10n_pe_build_note_request(self):
        """Nota de Crédito (07) / Débito (08) — referencia al documento afectado."""
        self.ensure_one()
        self._l10n_pe_check_lineas_impuesto()
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
            # Sustento libre si el usuario lo escribió; si no, descripción del catálogo.
            cabecera["desMotivo"] = (self.l10n_pe_motivo_desc or "").strip() or ND_MOTIVO_DESC.get(
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
        # EXCEPCIÓN: una NC de importe 0 (motivo 03, corrección de descripción) NO puede
        # llevar el Amount de la cuota Crédito (SUNAT rechaza cac:PaymentTerms/cbc:Amount
        # "0.00"), y omitir la FormaPago rebota con errorCode 3245. El patrón válido es
        # "Contado" SIN <cbc:Amount>. El mapper del biller (GenericBillingMapper) defaultea
        # el monto a "0.00" y la moneda a "" salvo que se le mande el sentinel "-", que le
        # dice que NO setee esos campos → el FTL entonces omite el <cbc:Amount>.
        if dt == "07":
            if self.amount_total:
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
            else:
                # NC de importe 0 (motivo 03): SUNAT exige FormaPago=Credito con Amount>0
                # (Contado→3246, omitir→3245, Amount 0.00→2071). Se referencia el total del
                # comprobante afectado como monto de la cuota (el documento en sí va en 0).
                ref = self._l10n_pe_fmt((origin.amount_total if origin else 0) or 0)
                fecha = self.invoice_date.strftime("%Y-%m-%d") if self.invoice_date else ""
                moneda = self.currency_id.name or "PEN"
                req["datoPago"] = {
                    "formaPago": "Credito",
                    "mtoNetoPendientePago": ref,
                    "tipMonedaMtoNetoPendientePago": moneda,
                }
                req["detallePago"] = [
                    {"mtoCuotaPago": ref, "fecCuotaPago": fecha, "tipMonedaCuotaPago": moneda}
                ]
        return req

    def _l10n_pe_target(self):
        """(endpoint, payload) según el tipo de comprobante."""
        self._l10n_pe_check_serie()
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
        # Si el XML ya se adjuntó estando en_proceso (firma del modo instant o
        # item "firmado" del worker async), reemplazarlo: sin esto quedaban DOS
        # adjuntos idénticos colgados del move (el viejo huérfano en el panel).
        if self.l10n_pe_biller_xml:
            # Contenido distinto = re-emisión con XML corregido: los PDFs
            # cacheados renderizan el XML viejo y quedarían servidos por
            # siempre (el cache pdfver no detecta cambios de contenido).
            if (self.l10n_pe_biller_xml.raw or b"") != body_text.encode("utf-8"):
                self._l10n_pe_invalidar_pdfs()
            self.l10n_pe_biller_xml.unlink()
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
            # Automatización (opt-in): al aceptarse, enviar el comprobante (XML + PDF + CDR) al
            # correo del cliente. Gateado por config para no mandar correos sin querer; nunca
            # rompe la emisión (un fallo de correo se loguea y sigue).
            if self.env["ir.config_parameter"].sudo().get_param(
                "l10n_pe_ne_biller.email_on_accept", ""
            ).strip().lower() in ("1", "true"):
                try:
                    self._l10n_pe_ne_email_comprobante()
                except Exception as e:  # noqa: BLE001
                    _logger.warning("email comprobante %s: %s", self.name, e)
        elif code:
            self.l10n_pe_biller_message = _(
                "CDR de SUNAT (ResponseCode %s). %s"
            ) % (code, desc or "")
        else:
            self.l10n_pe_biller_message = _("Aceptado por el facturador (HTTP 200).")

    def _l10n_pe_ne_email_comprobante(self):
        """Envía el comprobante aceptado (XML firmado + PDF A4 + CDR) al correo del cliente.
        Automatiza la entrega manual. No-op si el cliente no tiene correo; nunca lanza (el
        llamador lo envuelve, pero igual usamos send sin excepción)."""
        self.ensure_one()
        email = (self.partner_id.email or "").strip()
        if not email:
            _logger.info("email comprobante %s: cliente sin correo, se omite", self.name)
            return False
        atts = self.env["ir.attachment"]
        if self.l10n_pe_biller_xml:
            atts |= self.l10n_pe_biller_xml
        try:
            pdf = self._l10n_pe_get_pdf_attachment(formato="A4")
            if pdf:
                atts |= pdf
        except Exception:  # noqa: BLE001 — el PDF es deseable pero no bloquea el correo
            pass
        if self.l10n_pe_biller_cdr:
            atts |= self.l10n_pe_biller_cdr
        serie, corr = self._l10n_pe_serie_correlativo()
        num = "%s-%s" % (serie, corr)
        subject = _("Comprobante electrónico %s") % num
        body = _(
            "<p>Estimado cliente,</p>"
            "<p>Adjuntamos su comprobante electrónico <b>%(num)s</b> emitido por "
            "<b>%(emisor)s</b> y aceptado por SUNAT.</p>"
            "<p>Se incluyen el XML firmado, la representación impresa (PDF) y el CDR.</p>"
        ) % {"num": num, "emisor": self.company_id.name or ""}
        mail = self.env["mail.mail"].sudo().create({
            "subject": subject,
            "body_html": body,
            "email_to": email,
            "email_from": self.company_id.email or self.env.user.email_formatted,
            "attachment_ids": [(6, 0, atts.ids)],
            "auto_delete": False,
        })
        mail.send(raise_exception=False)
        _logger.info("email comprobante %s enviado a %s (%d adjuntos)", num, email, len(atts))
        return True

    def _l10n_pe_apply_signed(self, firma):
        """Modo instantáneo: aplica el resultado de la FIRMA (sin enviar a SUNAT). Adjunta el
        XML firmado (con eso el ticket/PDF ya funcionan), congela la identidad, guarda el ZIP
        de ENVI + filename/canal para el envío en 2º plano y deja el estado en 'en_proceso'."""
        self.ensure_one()
        firma = firma or {}
        xml = firma.get("xmlFirmado") or ""
        if not any(tag in xml for tag in ("<Invoice", "<CreditNote", "<DebitNote")):
            self.l10n_pe_biller_state = "error"
            self.l10n_pe_biller_message = _("La firma no devolvió un XML válido.")
            return False
        serie, correlativo = self._l10n_pe_serie_correlativo()
        # Re-firma (re-emisión tras rechazo/error en modo instant): reemplaza el
        # XML anterior (evita el adjunto huérfano) e invalida los PDFs cacheados
        # del intento previo antes de pre-generar los nuevos.
        if self.l10n_pe_biller_xml:
            if (self.l10n_pe_biller_xml.raw or b"") != xml.encode("utf-8"):
                self._l10n_pe_invalidar_pdfs()
            self.l10n_pe_biller_xml.unlink()
        att = self.env["ir.attachment"].create(
            {
                "name": "%s-%s-%s.xml" % (self.company_id.vat, serie, correlativo.zfill(8)),
                "res_model": "account.move",
                "res_id": self.id,
                "mimetype": "application/xml",
                "raw": xml.encode("utf-8"),
            }
        )
        self.l10n_pe_biller_xml = att.id
        self.l10n_pe_ne_tipo_doc = self._l10n_pe_document_type()
        self.l10n_pe_ne_serie_emit = serie
        self.l10n_pe_ne_corr_emit = correlativo.zfill(8)
        self.l10n_pe_ne_envi_zip = firma.get("enviZip") or ""
        self.l10n_pe_ne_biller_filename = firma.get("filename") or ""
        self.l10n_pe_ne_biller_canal = firma.get("canal") or "GEM"
        self.l10n_pe_ne_envio_intentos = 0
        self.l10n_pe_biller_state = "en_proceso"
        self.l10n_pe_biller_message = _("Firmado — ticket listo. Pendiente de envío a SUNAT.")
        # Pre-generar la representación impresa YA (con el XML firmado) para que la
        # descarga sea instantánea: así el adjunto existe cuando el usuario da clic y
        # no depende de un cold-start del micro en ese momento (que llegaba a expirar y
        # dejaba la sensación de "no se puede descargar mientras procesa"). No es fatal:
        # si el micro falla aquí, queda como fallback la generación on-demand.
        try:
            self._l10n_pe_get_pdf_attachment()  # A4
            if self.l10n_pe_ne_tipo_doc in ("01", "03"):
                self._l10n_pe_get_pdf_attachment(formato="TICKET")  # 80mm
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "No se pudo pre-generar el PDF tras firmar %s: %s",
                self.name or self.id, exc,
            )
        return True

    @api.model
    def _l10n_pe_cron_enviar_pendientes(self):
        """Modo instantáneo: envía a SUNAT (2º plano) los comprobantes ya FIRMADOS que quedaron
        en 'en_proceso' con ZIP pendiente. Al recibir el CDR pasa a aceptado/rechazado y limpia
        el ZIP. Reintentable: un fallo de red deja el move en en_proceso para la próxima corrida
        (con tope de intentos para no reintentar por siempre un rechazo)."""
        icp = self.env["ir.config_parameter"].sudo()
        if icp.get_param("l10n_pe_ne_biller.instant_enabled", "").strip().lower() not in ("1", "true"):
            return
        base = icp.get_param("l10n_pe_ne_biller.url", "http://localhost:8090").rstrip("/")
        timeout = int(icp.get_param("l10n_pe_ne_biller.timeout", "240"))
        max_intentos = int(icp.get_param("l10n_pe_ne_biller.max_intentos_envio", "30"))
        domain = [("l10n_pe_biller_state", "=", "en_proceso"), ("l10n_pe_ne_envi_zip", "!=", False)]
        # Si las boletas van por Resumen Diario, se excluyen del envío individual (las manda el RC).
        if icp.get_param("l10n_pe_ne_biller.boletas_resumen", "").strip().lower() in ("1", "true"):
            domain.append(("l10n_pe_ne_tipo_doc", "!=", "03"))
        pend = self.search(domain, limit=50)
        for move in pend:
            headers = {"X-Api-Key": move.company_id.sudo().l10n_pe_ne_api_key or ""}
            signed_xml = (move.l10n_pe_biller_xml.raw or b"").decode("utf-8") if move.l10n_pe_biller_xml else ""
            body = {
                "ruc": move.company_id.vat or "",
                "filename": move.l10n_pe_ne_biller_filename or "",
                "canal": move.l10n_pe_ne_biller_canal or "GEM",
                "enviZip": move.l10n_pe_ne_envi_zip or "",
                "signedXml": signed_xml,
            }
            ok = False
            try:
                resp = requests.post(base + "/generator/enviar", json=body, headers=headers, timeout=(5, timeout))
                if resp.status_code == 200:
                    data = resp.json() or {}
                    if data.get("rechazado"):
                        # SUNAT rechazó (regla de negocio) → estado final, NO reintentar.
                        move.l10n_pe_biller_state = "rechazado"
                        move.l10n_pe_biller_message = (_("Rechazado por SUNAT: %s") % (data.get("motivo") or ""))[:2000]
                        move.l10n_pe_ne_envi_zip = False
                    else:
                        move._l10n_pe_apply_emission_response(True, signed_xml, data.get("cdr") or "")
                        move.l10n_pe_ne_envi_zip = False  # enviado; nada pendiente
                    ok = True
                else:
                    move.l10n_pe_biller_message = ("Envío HTTP %s: %s" % (resp.status_code, resp.text))[:2000]
            except Exception as e:  # noqa: BLE001 — red/SUNAT: reintentar
                _logger.warning("enviar pendiente %s: %s (reintenta)", move.name, e)
                move.l10n_pe_biller_message = ("Reintentando envío: %s" % e)[:2000]
            if not ok:
                move.l10n_pe_ne_envio_intentos = (move.l10n_pe_ne_envio_intentos or 0) + 1
                if move.l10n_pe_ne_envio_intentos >= max_intentos:
                    move.l10n_pe_biller_state = "error"
            self.env["bus.bus"]._sendone(
                "l10n_pe_biller_updates",
                "l10n_pe_biller_update",
                {"move_id": move.id, "state": move.l10n_pe_biller_state},
            )
            self.env.cr.commit()

    # -------------------------------------------------------- emisión asíncrona
    # Toggle: ir.config_parameter `l10n_pe_ne_biller.async_enabled` = "1".
    # Odoo encola en SQS (rol IAM del EC2, patrón del sibling partner_lookup) y
    # responde al instante; el Lambda facturas-worker procesa contra biller-core
    # con idempotencia (DynamoDB) y deja XML/CDR en S3; el cron de abajo recoge.

    @api.model
    def _l10n_pe_boto_client(self, service, region):
        """Cliente boto3 memoizado por (service, region). Crear un cliente
        cuesta 100-400ms de CPU (carga los modelos JSON del servicio) y se
        pagaba dos veces POR EMISIÓN (dynamodb + sqs). El cache vive por
        worker de Odoo (prefork: se puebla post-fork, sin estado compartido
        entre procesos; los clientes boto3 son thread-safe para invocar).
        Se reconstruye si el módulo boto3 cambió (tests que lo parchean)."""
        key = (service, region)
        cached = _BOTO_CLIENTS.get(key)
        if cached is not None and cached[0] is boto3:
            return cached[1]
        client = boto3.client(service, region_name=region)
        _BOTO_CLIENTS[key] = (boto3, client)
        return client

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
                self._l10n_pe_boto_client("dynamodb", region).delete_item(
                    TableName=table,
                    Key={
                        "ruc_emisor": {"S": msg["ruc"]},
                        "serie_correlativo": {"S": msg["serie_correlativo"]},
                    },
                )
            except Exception as exc:  # noqa: BLE001
                _logger.warning("async biller: no se pudo limpiar resultado previo: %s", exc)
        try:
            self._l10n_pe_boto_client("sqs", region).send_message(
                QueueUrl=queue_url,
                MessageBody=json.dumps(msg, ensure_ascii=False),
            )
        except Exception as exc:  # noqa: BLE001
            self.l10n_pe_biller_state = "error"
            self.l10n_pe_biller_message = _("No se pudo encolar la emisión: %s") % exc
            return
        # Re-emisión tras rechazado/error: el XML firmado y los PDFs del intento
        # anterior quedan obsoletos (el worker firmará uno nuevo). Sin esto, el
        # cache pdfver serviría la representación vieja para siempre y el PDF
        # nuevo del worker jamás se adjuntaría (guard "ya hay PDF" del attach).
        if self.l10n_pe_biller_xml:
            self.l10n_pe_biller_xml.unlink()
        self._l10n_pe_invalidar_pdfs()
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

    def _l10n_pe_pdf_ver(self):
        """Etiqueta de versión del template de PDF (`description` del adjunto):
        _l10n_pe_get_pdf_attachment solo sirve el cache si coincide con el
        `pdf_ver` vigente; cualquier PDF que se adjunte debe llevarla."""
        return "pdfver:" + self.env["ir.config_parameter"].sudo().get_param(
            "l10n_pe_ne_biller.pdf_ver", "1"
        )

    def _l10n_pe_invalidar_pdfs(self):
        """Descarta los PDFs cacheados (A4 y ticket). Debe llamarse siempre que
        el XML firmado cambie (re-emisión tras rechazo/error): la representación
        impresa de un XML anterior no debe sobrevivir — el cache por `pdfver`
        solo detecta cambios de template, no de contenido."""
        self.ensure_one()
        for campo in ("l10n_pe_biller_pdf", "l10n_pe_biller_pdf_ticket"):
            att = self[campo]
            if att:
                try:
                    att.sudo().unlink()
                except Exception as exc:  # noqa: BLE001 — best-effort
                    _logger.warning(
                        "no se pudo descartar el PDF cacheado de %s: %s",
                        self.name, exc,
                    )

    def _l10n_pe_attach_async_pdf(self, s3c, bucket, item):
        """Adjunta el PDF pre-generado por el worker (pdf_s3_key del item), si
        ya existe y el move no tiene uno. Best-effort: si falta, el botón
        Descargar PDF cae al camino síncrono de siempre."""
        self.ensure_one()
        # El worker pre-genera el A4 SIN logo del emisor ni dirección del cliente (el mensaje
        # de la cola no los lleva, ver _l10n_pe_enqueue_emission). Si el emisor tiene logo o el
        # cliente tiene dirección, ese PDF saldría incompleto: NO lo adjuntamos y dejamos que la
        # descarga lo regenere por la ruta síncrona (_l10n_pe_get_pdf_attachment), que sí los
        # incluye. Si no hay nada que agregar, reusamos el del worker (más rápido, sin diferencia).
        if self.company_id.logo or self.partner_id.street or self.partner_id.street2:
            return
        pdf_s3 = (item.get("pdf_s3_key") or {}).get("S", "")
        if not pdf_s3 or self.l10n_pe_biller_pdf:
            return
        try:
            pdf_bytes = s3c.get_object(Bucket=bucket, Key=pdf_s3)["Body"].read()
            if not pdf_bytes.startswith(b"%PDF"):
                _logger.warning(
                    "async biller: pdf_s3_key de %s no es un PDF; se ignora",
                    self.name,
                )
                return
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
                    # Sin la etiqueta, la primera descarga vía API lo descartaba
                    # (cache-busting) y re-renderizaba contra el micro (~hasta 60s).
                    "description": self._l10n_pe_pdf_ver(),
                }
            )
            self.l10n_pe_biller_pdf = att.id
        except Exception as exc:  # noqa: BLE001 — PDF es best-effort
            _logger.warning(
                "async biller: PDF no adjuntado en %s: %s", self.name, exc
            )

    def _l10n_pe_async_attach_firmado(self, s3c, bucket, item):
        """Modo async: cuando el worker publica un item intermedio (status no
        terminal, p.ej. "firmado") con `xml_s3_key`, adjunta el XML firmado a
        `l10n_pe_biller_xml` y toma el PDF del worker si ya está (`pdf_s3_key`).
        Con el XML adjunto, la descarga funciona estando en_proceso aunque el PDF
        aún no llegue (el botón cae al camino on-demand de siempre). NO cambia el
        estado (sigue en_proceso) y NO genera el PDF localmente — el worker es el
        único generador; ver nota al final del cuerpo. Best-effort e idempotente:
        sin `xml_s3_key` no hace nada; con el XML ya adjunto solo intenta traer
        el PDF del worker."""
        self.ensure_one()
        if self.l10n_pe_biller_xml:
            # Ya adjuntado en una corrida previa: solo traer el PDF del worker si aún no está.
            self._l10n_pe_attach_async_pdf(s3c, bucket, item)
            return
        xml_key = (item.get("xml_s3_key") or {}).get("S", "")
        if not xml_key:
            return
        try:
            body = (
                s3c.get_object(Bucket=bucket, Key=xml_key)["Body"]
                .read()
                .decode("iso-8859-1")
            )
        except Exception as exc:  # noqa: BLE001 — aún no está en S3: se reintenta al próximo poll
            _logger.warning(
                "async biller: XML firmado aún no disponible en %s: %s", self.name, exc
            )
            return
        if not any(tag in body for tag in ("<Invoice", "<CreditNote", "<DebitNote")):
            return
        serie, correlativo = self._l10n_pe_serie_correlativo()
        att = self.env["ir.attachment"].create(
            {
                "name": "%s-%s-%s.xml"
                % (self.company_id.vat, serie, correlativo.zfill(8)),
                "res_model": "account.move",
                "res_id": self.id,
                "mimetype": "application/xml",
                # Normalizado a utf-8 igual que _l10n_pe_apply_emission_response, para que
                # el render del PDF (que decodifica utf-8) no rompa con tildes/ñ.
                "raw": body.encode("utf-8"),
            }
        )
        self.l10n_pe_biller_xml = att.id
        self.l10n_pe_ne_tipo_doc = self._l10n_pe_document_type()
        self.l10n_pe_ne_serie_emit = serie
        self.l10n_pe_ne_corr_emit = correlativo.zfill(8)
        # PDF: SOLO el pre-generado por el worker (pdf_s3_key). NO generarlo acá:
        # en la ventana "firmado" el worker ya está invocando biller-pdf con este
        # mismo XML — hacerlo también desde el cron duplicaba renders (A4+ticket
        # síncronos de hasta ~60s c/u DENTRO del loop del poll: un import masivo
        # bloqueaba el cron varios minutos) y el PDF del worker terminaba
        # descartado. Si el usuario descarga antes de que llegue, el botón usa el
        # camino on-demand de siempre — posible porque el XML ya quedó adjunto.
        self._l10n_pe_attach_async_pdf(s3c, bucket, item)

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
        ddb = self._l10n_pe_boto_client("dynamodb", region)
        s3c = self._l10n_pe_boto_client("s3", region)
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
                else:
                    # Item intermedio (p.ej. "firmado"): el worker ya firmó pero SUNAT aún
                    # no responde. Adjunta el XML firmado + PDF para que ticket/PDF estén
                    # disponibles AL TOQUE en en_proceso, sin esperar el CDR. Sigue en
                    # en_proceso: sin transición de estado no se postea al chatter ni se
                    # notifica (evita spam en cada corrida mientras el item no es final).
                    move._l10n_pe_async_attach_firmado(s3c, bucket, item)
                    continue
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
        use_instant = icp.get_param(
            "l10n_pe_ne_biller.instant_enabled", ""
        ).strip().lower() in ("1", "true")
        for move in self:
            _logger.info(
                "Procesando factura: %s (%s)", move.name, move.l10n_pe_biller_state
            )
            if move.l10n_pe_biller_state in ("enviado", "en_proceso"):
                _logger.info("Factura ya enviada o en proceso: %s", move.name)
                continue
            # Guarda: no aplicar percepción a un cliente exceptuado del régimen (QA-028). El cobro
            # adicional no corresponde; se bloquea con un mensaje claro en vez de emitir mal.
            if move.l10n_pe_ne_percepcion and move.partner_id.l10n_pe_ne_exceptuado_percepcion:
                raise UserError(_(
                    "El cliente %s está exceptuado del régimen de percepciones; no corresponde "
                    "aplicarle percepción. Desactivá la percepción para emitir este comprobante."
                ) % (move.partner_id.display_name or ""))
            # Valida la serie (familia correcta + habilitada, QA-074) ANTES de asignar el
            # correlativo, para no consumir un número si la serie se rechaza.
            move._l10n_pe_check_serie()
            # Fija la serie+correlativo fiscal ANTES de construir el payload/firmar, desde la
            # secuencia POR SERIE (no el folio del diario). A partir de aquí el número es estable
            # e igual en payload, XML firmado, QR, PDF y una eventual baja. Va DESPUÉS del guard
            # para no consumir un correlativo en un comprobante que se bloquea.
            move._l10n_pe_ne_assign_numero()
            if use_async:
                move._l10n_pe_enqueue_emission(icp)
                continue
            if use_instant:
                # Modo instantáneo: FIRMAR (rápido, sin SUNAT) → ticket/PDF ya disponibles y
                # estado 'en_proceso'. El cron _l10n_pe_cron_enviar_pendientes envía a SUNAT.
                endpoint, payload = move._l10n_pe_target()
                headers = {"X-Api-Key": move.company_id.sudo().l10n_pe_ne_api_key or ""}
                try:
                    resp = requests.post(
                        base + "/generator/" + endpoint + "/firmar",
                        json=payload, headers=headers, timeout=(5, 30),
                    )
                    if resp.status_code == 200:
                        move._l10n_pe_apply_signed(resp.json())
                    else:
                        move.l10n_pe_biller_state = "error"
                        move.l10n_pe_biller_message = (
                            "Firma HTTP %s: %s" % (resp.status_code, resp.text)
                        )[:2000]
                except requests.RequestException as exc:
                    move.l10n_pe_biller_state = "error"
                    move.l10n_pe_biller_message = (
                        _("Error de conexión con el facturador (firma): %s") % exc
                    )
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
    def l10n_pe_ne_quick_emit(self, payload, enviar=True):
        """Emite un comprobante desde un payload PLANO (sin contexto contable previo): crea/halla el
        cliente, arma el account.move con sus líneas (impuesto por código cat-05), lo postea y lo envía a
        SUNAT vía el facturador. Devuelve el resultado. Con `enviar=False` arma y postea pero NO envía y
        devuelve el account.move (lo usa el pre-flight para validar sin emitir). Lo consume el BFF por /json/2 — así la
        lógica de negocio queda en Odoo (fuente única) y el dato vive en Odoo (upgrade sin migración)."""
        company = self.env.company
        journal = self.env["account.journal"].search(
            [("type", "=", "sale"), ("company_id", "=", company.id)], limit=1
        )
        if not journal:
            raise UserError(_("No hay diario de ventas configurado para la compañía."))
        # Anti-doble-conversión (QA-098): una cotización/orden ya convertida no puede emitir OTRO
        # comprobante (evita duplicar la venta). Se valida antes de armar el move.
        cotid = payload.get("cotizacionId")
        if cotid:
            cot = self.env["l10n_pe_ne.cotizacion"].browse(int(cotid)).exists()
            if cot and cot.estado == "convertida":
                raise UserError(_(
                    "La cotización %s ya fue convertida en el comprobante %s; no se puede emitir otro."
                ) % (cot.name or cot.id, cot._l10n_pe_ne_comprobante_numero() or "—"))
        tipo = payload.get("tipoDoc") or "01"
        # NC motivo 03 = "Corrección por error en la descripción": SOLO corrige el texto,
        # NO cambia importes. La nota va con importe 0.00 (la factura original conserva su
        # valor). Se fuerza aquí para que la correctitud fiscal no dependa del front.
        es_correccion = tipo == "07" and str(payload.get("motivo") or "") == "03"
        # NC (07) / ND (08): resuelven el documento afectado (mismo cliente, serie derivada del original).
        origin = None
        if tipo in ("07", "08"):
            origin = self._l10n_pe_ne_quick_origin(
                payload.get("docAfectado") or payload.get("afectado")
            )
            origin._l10n_pe_check_afectable_con_nota()
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
            # Sin tax resuelta la línea saldría en el XML como 'gravada con IGV 0.00'
            # (rechazo SUNAT 3111): mejor cortar aquí con el dato accionable.
            if not tax:
                raise UserError(
                    _(
                        "No hay un impuesto de venta con código SUNAT %(code)s configurado "
                        "para la compañía (línea «%(linea)s»). Configura el IGV en "
                        "Contabilidad → Impuestos (o ejecuta el setup de la compañía) y "
                        "vuelve a emitir."
                    )
                    % {
                        "code": ln.get("taxCode") or "1000",
                        "linea": (ln.get("descripcion") or "").strip() or "ITEM",
                    }
                )
            taxes = tax
            if ln.get("icbper"):
                # Bolsa plástica: el ICBPER (monto fijo por unidad) se SUMA al IGV de la línea.
                taxes = tax + self._l10n_pe_ne_ensure_icbper_tax()
            isc_rate = float(ln.get("isc") or 0)
            if isc_rate > 0:
                # ISC (ad-valorem): se agrega a la línea; el IGV se recalcula sobre valor + ISC.
                taxes = taxes + self._l10n_pe_ne_ensure_isc_tax(isc_rate)
            # Notas (07/08): solo resolver el producto, nunca crearlo — sus líneas pueden ser
            # espejo o texto sintético (DICE/DEBE DECIR) que no debe entrar al catálogo.
            # precio_con_igv=False: el payload de emisión trae el valor SIN IGV.
            prod = self._l10n_pe_ne_quick_product(
                ln, tax, create=tipo not in ("07", "08"), precio_con_igv=False
            )
            d = float(ln.get("descuento") or 0)
            disc = round(100.0 * (1 - (1 - d / 100.0) * (1 - g / 100.0)), 6) if g else d
            qty = float(ln.get("cantidad") or 1)
            if ln.get("icbper"):
                # La bolsa es unidad discreta: normalizar la cantidad al entero DESDE EL ORIGEN.
                # Así Odoo computa la base y la tax fija del ICBPER (nº bolsas × monto) sobre el
                # MISMO conteo entero que va al XML, y el reparto IGV/ICBPER del ítem no se
                # descuadra cuando llega una cantidad con decimales (SUNAT valida por ítem).
                qty = float(self._l10n_pe_ne_bolsas(qty))
            lvals = {
                "name": ln.get("descripcion") or (prod.name if prod else "ITEM"),
                "quantity": qty,
                # Motivo 03: importe 0 (solo se corrige la descripción, no el monto).
                "price_unit": 0.0 if es_correccion else float(ln.get("precioUnitario") or 0),
                "discount": 0.0 if es_correccion else disc,
                "tax_ids": [(6, 0, taxes.ids if taxes else [])],
            }
            if prod:
                lvals["product_id"] = prod.id
            if ln.get("unidad"):
                lvals["l10n_pe_ne_unit_code"] = ln["unidad"]
            if ln.get("codSunat"):
                lvals["l10n_pe_ne_cod_producto_sunat"] = ln["codSunat"]
            if ln.get("afectacionGratuita"):
                lvals["l10n_pe_ne_afectacion_gratuita"] = ln["afectacionGratuita"]
            if ln.get("fraccionar"):
                # Farma: vender por sub-unidad. Requiere el factor del producto (unidades por
                # empaque); sin él no hay cómo descontar el stock del empaque.
                if not (prod and prod.l10n_pe_ne_unidades_por_empaque > 0):
                    raise UserError(_(
                        "«%s» no se puede vender fraccionado: configura las unidades por empaque "
                        "en el producto."
                    ) % ((ln.get("descripcion") or "").strip() or (prod.name if prod else "ITEM")))
                lvals["l10n_pe_ne_fraccionado"] = True
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
            or self._l10n_pe_ne_today_lima(),
            "l10n_pe_serie": payload.get("serie")
            or self._l10n_pe_ne_default_serie(tipo, origin),
            "invoice_line_ids": lines,
        }
        # Alinear el tipo latam con el tipoDoc pedido: sin esto, una BOLETA a un cliente
        # con RUC se emitiría como Factura (el fallback decide por el documento del cliente).
        es_boleta = tipo == "03" or (
            tipo in ("07", "08")
            and origin is not None
            and (origin.l10n_pe_ne_tipo_doc or origin._l10n_pe_document_type()) == "03"
        )
        doc_xmlid = {
            "01": "l10n_pe.document_type01",
            "03": "l10n_pe.document_type02",
            "07": "l10n_pe.document_type07b" if es_boleta else "l10n_pe.document_type07",
            "08": "l10n_pe.document_type08b" if es_boleta else "l10n_pe.document_type08",
        }.get(tipo)
        doc_type = doc_xmlid and self.env.ref(doc_xmlid, raise_if_not_found=False)
        if doc_type:
            vals["l10n_latam_document_type_id"] = doc_type.id
        if origin is not None and not payload.get("moneda"):
            # NC/ND heredan la moneda del comprobante afectado: SUNAT exige que la
            # nota vaya en la misma moneda que el documento original (sin esto una
            # NC de una factura en USD salía forzada a PEN).
            moneda = origin.currency_id
        else:
            moneda = self._l10n_pe_ne_quick_currency(payload.get("moneda"))
        if moneda:
            vals["currency_id"] = moneda.id
            # Comprobante en dólares: asegura el TC oficial del día en
            # res.currency.rate para que el PLE y la conversión a soles salgan
            # bien. Best-effort: si la red falla, no bloquea la emisión.
            if moneda.name and moneda.name != "PEN":
                try:
                    fecha_tc = vals.get("invoice_date") or fields.Date.context_today(self)
                    self.env.company._l10n_pe_ne_ensure_tc(fecha_tc)
                except Exception as e:  # noqa: BLE001
                    _logger.warning("TC SUNAT: no se pudo asegurar en emisión (%s)", e)
        if origin is not None:
            vals["l10n_pe_motivo_code"] = str(
                payload.get("motivo") or ("01" if tipo == "07" else "02")
            )
            # Motivo/sustento (texto libre): si el front lo envía se usa como desMotivo;
            # si no, _l10n_pe_build_note_request cae a la descripción del catálogo.
            sustento = (payload.get("sustento") or "").strip()
            if sustento:
                vals["l10n_pe_motivo_desc"] = sustento[:250]
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
        move.l10n_pe_ne_bancarizacion = move._l10n_pe_ne_bancarizacion_estado()
        # Stock: el bien sale (o vuelve, si es NC) cuando la venta existe en Odoo, no cuando
        # SUNAT responde — la mercadería ya cambió de manos. Va después del post y antes de
        # enviar: si SUNAT rechaza, el movimiento se corrige con la NC, igual que el importe.
        move._l10n_pe_ne_mover_stock()
        # Nota de Crédito: no puede acreditar más de lo facturado. Se permiten VARIAS NC
        # sobre el mismo comprobante, pero el ACUMULADO no puede superar su total: el tope
        # de esta nota es el saldo pendiente de acreditar (total − NC previas vigentes).
        # Respaldo del front; la NC de importe 0 (motivo 03) pasa. (La ND suma a la deuda,
        # así que no lleva tope.)
        if tipo == "07" and origin is not None:
            previas = origin._l10n_pe_ne_nc_previas() - move
            acreditado = sum(previas.mapped("amount_total"))
            saldo = (origin.amount_total or 0) - acreditado
            if move.amount_total > saldo + 0.05:
                if previas:
                    raise UserError(
                        _(
                            "El comprobante afectado ya tiene %(n)d nota(s) de crédito por "
                            "%(acred)s (%(lista)s); saldo pendiente de acreditar: %(saldo)s. "
                            "Esta nota (%(nc)s) lo supera."
                        )
                        % {
                            "n": len(previas),
                            "acred": "%.2f" % acreditado,
                            "lista": ", ".join(
                                "%s-%s" % m._l10n_pe_ne_doc_id() for m in previas
                            ),
                            "saldo": "%.2f" % saldo,
                            "nc": "%.2f" % move.amount_total,
                        }
                    )
                raise UserError(
                    _(
                        "La nota de crédito (%(nc)s) no puede superar el total del comprobante "
                        "afectado (%(orig)s)."
                    )
                    % {
                        "nc": "%.2f" % move.amount_total,
                        "orig": "%.2f" % (origin.amount_total or 0),
                    }
                )
        # Si la emisión vino de "Convertir a comprobante", vincula el comprobante
        # recién posteado a la cotización de origen y la marca como 'convertida'.
        cotid = payload.get("cotizacionId")
        if cotid:
            cot = self.env["l10n_pe_ne.cotizacion"].browse(int(cotid)).exists()
            if cot:
                cot.l10n_pe_ne_vincular_comprobante(move.id)
        # Avance de obra (QA-039): la suma de las valorizaciones no puede superar el valor total
        # del contrato. Se valida con el move ya posteado (amount_total disponible); si se pasa,
        # el raise revierte la transacción y no se emite.
        proj = move.l10n_pe_ne_proyecto_id
        if proj:
            otras = move.amount_total or 0.0  # esta valorización
            total = round(proj.valor_total or 0.0, 2)
            if round(proj.facturado + otras, 2) > total + 0.01:
                raise UserError(_(
                    "Esta valorización (%s) haría que lo facturado del contrato «%s» supere su "
                    "valor total. Facturado: %s · Contrato: %s · Esta: %s."
                ) % (
                    self._l10n_pe_fmt(otras), proj.name,
                    self._l10n_pe_fmt(proj.facturado), self._l10n_pe_fmt(proj.valor_total),
                    self._l10n_pe_fmt(otras),
                ))
            # Emitir DESDE la valorización: se numera (las previas del contrato + 1; esta aún no
            # está enviada, no cuenta) y, si el emisor no puso observación propia, se compone la
            # glosa con el avance acumulado del contrato para que el comprobante lo declare.
            move.l10n_pe_ne_valorizacion_nro = self.env["account.move"].sudo().search_count([
                ("l10n_pe_ne_proyecto_id", "=", proj.id),
                ("l10n_pe_biller_state", "in", ("enviado", "en_proceso")),
            ]) + 1
            pct = round((proj.facturado + otras) / total * 100.0, 2) if total else 0.0
            if not (move.narration or "").strip():
                move.narration = _(
                    "Valorización N° %(n)s — avance acumulado %(pct)s%% del contrato «%(c)s»"
                ) % {"n": move.l10n_pe_ne_valorizacion_nro,
                     "pct": self._l10n_pe_fmt(pct), "c": proj.name}
        if not enviar:
            # Pre-flight: el comprobante quedó armado y posteado pero NO se envía a SUNAT.
            # El llamador (l10n_pe_ne_preflight) valida y revierte la transacción.
            return move
        move.action_l10n_pe_send_to_biller()
        return move.l10n_pe_ne_quick_result()

    @api.model
    def l10n_pe_ne_emitir_liquidacion(self, payload, enviar=True):
        """Emite una Liquidación de compra (tipo 04) desde un payload plano de la SPA.

        A diferencia de quick_emit (venta), la liquidación es una COMPRA: la emite el comprador
        (con RUC) a un productor/vendedor SIN RUC (con DNI). Por eso el move es un `in_invoice`,
        la mercadería ENTRA al stock (kardex de compra → PLE de compras) y el pasivo queda a favor
        del productor; pero además se emite electrónicamente a SUNAT como SelfBilledInvoice — la
        plantilla del facturador intercambia los roles (el emisor va como Customer, el productor
        como Supplier), así que el payload reusa el mismo build de factura.

        `enviar=False` arma y postea sin enviar (lo usa el pre-flight)."""
        company = self.env.company
        journal = self.env["account.journal"].search(
            [("type", "=", "purchase"), ("company_id", "=", company.id)], limit=1)
        if not journal:
            raise UserError(_("No hay diario de compras configurado para la compañía."))
        productor = self._l10n_pe_ne_quick_partner(
            payload.get("proveedor") or payload.get("cliente") or {})
        # La liquidación es a un vendedor SIN RUC (productor agropecuario/recolector/artesano):
        # SUNAT rechaza una liquidación a un RUC. Se corta con un mensaje claro.
        if (productor.l10n_latam_identification_type_id.l10n_pe_vat_code or "") == "6":
            raise UserError(_(
                "La liquidación de compra es para un vendedor SIN RUC (con DNI u otro documento). "
                "«%s» tiene RUC; para comprarle usa el registro de compras normal."
            ) % (productor.display_name or ""))
        lines = []
        for ln in payload.get("lineas") or []:
            # La liquidación de compra siempre compra BIENES tangibles (agropecuario, recolección,
            # artesanía) que ENTRAN al inventario: el producto auto-creado lleva stock por defecto
            # (a diferencia de una venta, donde is_storable arranca en False).
            ln = dict(ln)
            ln.setdefault("llevaStock", True)
            tax = self._l10n_pe_ne_tax_by_code(ln.get("taxCode"))
            if not tax:
                raise UserError(_(
                    "No hay un impuesto de venta con código SUNAT %(code)s configurado para la "
                    "compañía (línea «%(linea)s»)."
                ) % {"code": ln.get("taxCode") or "1000",
                     "linea": (ln.get("descripcion") or "").strip() or "ITEM"})
            prod = self._l10n_pe_ne_quick_product(ln, tax, create=True, precio_con_igv=False)
            lvals = {
                "name": ln.get("descripcion") or (prod.name if prod else "ITEM"),
                "quantity": float(ln.get("cantidad") or 1),
                "price_unit": float(ln.get("precioUnitario") or 0),
                "discount": float(ln.get("descuento") or 0),
                "tax_ids": [(6, 0, tax.ids)],
            }
            if prod:
                lvals["product_id"] = prod.id
            if ln.get("unidad"):
                lvals["l10n_pe_ne_unit_code"] = ln["unidad"]
            if ln.get("codSunat"):
                lvals["l10n_pe_ne_cod_producto_sunat"] = ln["codSunat"]
            lines.append((0, 0, lvals))
        if not lines:
            raise UserError(_("La liquidación necesita al menos una línea."))
        doc04 = self.env.ref("l10n_pe.document_type04", raise_if_not_found=False)
        vals = {
            "move_type": "in_invoice",
            "partner_id": productor.id,
            "journal_id": journal.id,
            "invoice_date": payload.get("fechaEmision") or self._l10n_pe_ne_today_lima(),
            "l10n_pe_serie": payload.get("serie") or "E001",
            "l10n_pe_ne_liquidacion": True,
            "invoice_line_ids": lines,
        }
        if doc04:
            vals["l10n_latam_document_type_id"] = doc04.id
        if payload.get("correlativo"):
            vals["l10n_pe_correlativo"] = str(payload["correlativo"])
        moneda = self._l10n_pe_ne_quick_currency(payload.get("moneda"))
        if moneda:
            vals["currency_id"] = moneda.id
        move = self.env["account.move"].create(vals)
        # Un in_invoice (compra) exige el número de documento ANTES de postear. En una liquidación
        # ese número es la serie-correlativo que asigna el COMPRADOR (no un proveedor): se fija el
        # correlativo fiscal ya, y se refleja en l10n_latam_document_number para la contabilidad.
        move._l10n_pe_check_serie()
        move._l10n_pe_ne_assign_numero()
        move.l10n_latam_document_number = "%s-%s" % (
            move.l10n_pe_ne_serie_emit, move.l10n_pe_ne_corr_emit)
        move.action_post()
        # Kardex: la mercadería comprada ENTRA al stock (misma mecánica que una compra normal).
        move._l10n_pe_ne_mover_stock_compra()
        if not enviar:
            return move
        move.action_l10n_pe_send_to_biller()
        return move.l10n_pe_ne_quick_result()

    @api.model
    def l10n_pe_ne_preflight(self, payload):
        """Valida un payload SIN emitir ni persistir. Arma el comprobante EXACTAMENTE como
        quick_emit (misma lógica, misma fidelidad), corre el motor de validaciones L1 y REVIERTE
        todo con un SAVEPOINT — no deja comprobante, producto ni movimiento de stock. Devuelve
        [{code, campo, nivel, mensaje}] para que la SPA muestre avisos/errores ANTES de emitir.

        Cualquier UserError del armado (tax faltante, saldo de NC, avance de obra…) se devuelve
        como un finding bloqueante, así el pre-flight refleja también esos cortes."""
        # Savepoint GESTIONADO por Odoo (cr.savepoint): nombres únicos + limpieza de caché en el
        # rollback. Se fuerza el rollback lanzando un centinela DESPUÉS de extraer los findings
        # (dicts planos que sobreviven al rollback). Con un SAVEPOINT manual + invalidate_all, una
        # segunda llamada en la misma transacción reventaba en el cómputo de impuestos del create.
        class _Revert(Exception):
            pass

        findings = []
        try:
            with self.env.cr.savepoint():
                try:
                    move = self.l10n_pe_ne_quick_emit(dict(payload or {}), enviar=False)
                    findings = move._l10n_pe_ne_validaciones()
                except UserError as e:
                    findings = [{"code": "bloqueo", "campo": "", "nivel": "error",
                                 "mensaje": str(e)}]
                raise _Revert()
        except _Revert:
            pass
        return findings

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

    def _l10n_pe_check_afectable_con_nota(self):
        """Una NC/ND solo puede emitirse sobre una factura o una boleta: SUNAT rechaza la
        referencia a otra nota. La guarda va en el call site de la emisión y no dentro de
        _l10n_pe_ne_quick_origin porque ese helper lo comparte la anulación, que SÍ acepta
        notas (una NC se anula comunicando su baja)."""
        self.ensure_one()
        tipo = self.l10n_pe_ne_tipo_doc or self._l10n_pe_document_type()
        if tipo not in ("01", "03"):
            docname = {
                "07": _("Nota de Crédito"),
                "08": _("Nota de Débito"),
            }.get(tipo, tipo)
            raise UserError(
                _(
                    "Una nota de crédito o débito solo puede emitirse sobre una factura o una "
                    "boleta; el documento afectado (%(doc)s %(serie)s-%(corr)s) es una nota. "
                    "Para anularla, comunique su baja."
                )
                % {
                    "doc": docname,
                    "serie": self.l10n_pe_ne_serie_emit
                    or self._l10n_pe_serie_correlativo()[0],
                    "corr": self.l10n_pe_ne_corr_emit
                    or self._l10n_pe_serie_correlativo()[1],
                }
            )

    @api.model
    def l10n_pe_ne_quick_anular(self, payload):
        """Anula un comprobante ya emitido a SUNAT: boletas por Resumen Diario (RC, tipEstado 3),
        facturas/NC/ND por Comunicación de Baja (RA). payload: {id | serie+correlativo, motivo}.
        Lo consume el BFF por /json/2."""
        # H-5: el modelo es la autoridad, no solo el controller. /ne/api/anular ya devuelve
        # 403 sin este grupo, pero el gate tiene que vivir también aquí para que ninguna vía
        # (backend, tests, un futuro endpoint) pueda saltárselo. Ver
        # docs/procesos-negocio/decision-alta-usuarios.md y hallazgos.md (H6).
        # NOTA (hueco conocido): el botón "Comunicar Baja" del backend llama directo a
        # action_l10n_pe_send_baja y no pasa por aquí; cerrarlo (groups= en la vista o gate
        # en el modelo) queda para un cambio validado con la suite de tests.
        if not self.env.user.has_group('l10n_pe_ne_biller.group_l10n_pe_ne_anulacion'):
            raise AccessError(_("No tienes permiso para anular comprobantes."))
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

    def _l10n_pe_ne_fetch_direccion_padron(self, num):
        """Domicilio fiscal desde el padrón externo (DynamoDB) o, como respaldo, SUNAT.

        Consulta la fuente directamente (no lee el partner ya guardado, que puede tener
        street vacío). Degrada a "" ante cualquier fallo o si la fuente no está
        configurada — NUNCA bloquea la emisión."""
        num = (num or "").strip()
        if not num:
            return ""
        P = self.env["res.partner"].sudo()
        data = None
        # getattr: si el addon l10n_pe_partner_lookup NO está instalado, _l10n_pe_query_external_db
        # no existe en res.partner; se omite en vez de reventar con AttributeError (degradación con
        # gracia — este método NUNCA bloquea la emisión). Acceder al atributo directo en la tupla
        # lanzaba ANTES del try. (Hallazgo del run real en Odoo 19.)
        for fetch in (getattr(P, "_l10n_pe_query_external_db", None),
                      getattr(P, "_l10n_pe_query_sunat", None)):
            if not fetch:
                continue
            try:
                data = fetch(num)
            except Exception:  # noqa: BLE001 — fuente no configurada / red: seguimos
                data = None
            if data:
                break
        return (data or {}).get("address") or ""

    def _l10n_pe_ne_quick_partner(self, c):
        num = (c.get("numDoc") or "").strip()
        nombre = (c.get("razonSocial") or "").strip()
        dire = (c.get("direccion") or "").strip()
        urb = (c.get("urbanizacion") or "").strip()
        Partner = self.env["res.partner"]
        found = Partner.search([("vat", "=", num)], limit=1) if num else Partner.browse()
        if not found and not num and not nombre:
            # Público general SIN documento ni nombre: reusa UN solo 'CONSUMIDOR
            # FINAL' por tenant en vez de crear un partner desechable por venta.
            # (La emisión no reescribe el partner, así que reusarlo es seguro.)
            found = Partner.search([
                ("company_id", "=", self.env.company.id),
                ("vat", "=", False),
                ("name", "=", "CONSUMIDOR FINAL"),
            ], limit=1)
        if not found:
            # company_id del emisor actual: aísla el cliente por RUC (multi-tenant). Sin
            # esto quedaría company_id=False = visible/editable por TODOS los tenants.
            vals = {
                "name": nombre or "CONSUMIDOR FINAL",
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
            if dire:
                vals["street"] = dire
            if urb:
                vals["street2"] = urb
            # País del adquirente (exportación / no domiciliado): alimenta codPaisCliente en la
            # cabecera 0200. Solo al crear (la emisión no reescribe un partner ya existente).
            pais = (c.get("pais") or "").strip().upper()
            if pais:
                country = self.env["res.country"].search([("code", "=", pais)], limit=1)
                if country:
                    vals["country_id"] = country.id
            found = Partner.create(vals)
        # Dirección faltante → la completamos (sin pisar una ya guardada). Primero lo que
        # mandó el front; si no vino, el domicilio fiscal del padrón. Así la representación
        # impresa (A4) muestra la dirección de los RUC 20 y de los 10/naturales que la tengan.
        if not found.street:
            addr = dire or self._l10n_pe_ne_fetch_direccion_padron(num)
            if addr:
                found.street = addr
        if urb and not found.street2:
            found.street2 = urb
        # País del adquirente (exportación / no domiciliado): si un partner ya registrado no tiene
        # país guardado, lo completamos con el del payload — así una factura de exportación a un
        # cliente preexistente sin país no queda bloqueada por el guard 0200. No pisa un país ya
        # guardado (para cambiarlo se usa la API de clientes, que sí lo reescribe).
        if not found.country_id:
            pais = (c.get("pais") or "").strip().upper()
            if pais:
                country = self.env["res.country"].search([("code", "=", pais)], limit=1)
                if country:
                    found.country_id = country.id
        return found

    # Afectaciones de tasa 0% (cat-05) que se auto-crean si el plan no las trae. El IGV (1000)
    # y el IVAP (1016) NO están aquí a propósito: su tasa es una decisión contable y crearlos
    # con una tasa adivinada emitiría montos fiscales incorrectos — si faltan, la emisión corta
    # con un error accionable (ver quick_emit).
    _L10N_PE_NE_TAXES_CERO = {
        "9997": "Exonerado",
        "9998": "Inafecto",
        "9995": "Exportación",
        "9996": "Gratuito",
    }

    def _l10n_pe_ne_tax_by_code(self, code):
        """account.tax de venta por código cat-05 (l10n_pe_edi_tax_code); default 1000 (IGV gravado).

        Las taxes 0% (exonerado/inafecto/exportación/gratuito) se crean si faltan, como
        ICBPER/ISC: una BD recién configurada suele traer solo el IGV, y sin esto la línea
        quedaba SIN impuesto → `_l10n_pe_tax_info` la clasificaba con su default 'gravado
        (1000)' a tasa 0 → XML con TaxableAmount>0 y TaxAmount=0.00 → rechazo SUNAT 3111."""
        code = code or "1000"
        tax = self.env["account.tax"].search(
            [
                ("company_id", "=", self.env.company.id),
                ("type_tax_use", "=", "sale"),
                ("l10n_pe_edi_tax_code", "=", code),
            ],
            limit=1,
        )
        if not tax and code in self._L10N_PE_NE_TAXES_CERO:
            label = self._L10N_PE_NE_TAXES_CERO[code]
            tax = self.env["account.tax"].sudo().create(
                {
                    "name": "%s (0%%)" % label,
                    "amount_type": "percent",
                    "amount": 0.0,
                    "type_tax_use": "sale",
                    "l10n_pe_edi_tax_code": code,
                    "company_id": self.env.company.id,
                    "description": label,
                }
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
            "agentePercepcion": bool(self.env.company.l10n_pe_ne_agente_percepcion),
            # Redondeo de efectivo: el POS lo aplica en vivo con estos parámetros (ver lib/redondeo.ts).
            "redondeoActivo": bool(self.env.company.l10n_pe_ne_redondeo_activo),
            "redondeoModo": self.env.company.l10n_pe_ne_redondeo_modo or "favor",
        }

    @api.model
    def l10n_pe_ne_paises(self):
        """Catálogo de países (ISO 3166 alpha-2) para el selector del cliente extranjero en la
        factura de exportación. Perú primero (default habitual) y el resto por nombre."""
        paises = self.env["res.country"].search([("code", "!=", False)], order="name")
        return [{"code": c.code, "name": c.name} for c in paises]

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
            "datosPago": company.l10n_pe_ne_datos_pago or "",
            "hasLogo": bool(company.logo),
            "agentePercepcion": bool(company.l10n_pe_ne_agente_percepcion),
            "redondeoActivo": bool(company.l10n_pe_ne_redondeo_activo),
            "redondeoModo": company.l10n_pe_ne_redondeo_modo or "favor",
        }

    def l10n_pe_ne_get_logo(self):
        """(bytes, content_type) del logo del emisor para servirlo por HTTP, o (None, None)."""
        logo = self.env.company.logo
        if not logo:
            return None, None
        raw = base64.b64decode(logo)
        ct = (
            "image/png" if raw[:4] == b"\x89PNG"
            else "image/jpeg" if raw[:2] == b"\xff\xd8"
            else "application/octet-stream"
        )
        return raw, ct

    def _l10n_pe_ne_set_logo(self, company, logo_b64):
        """Valida y guarda el logo del emisor. Vacío/None → lo quita. Acepta data-URI o base64
        pelado. Exige PNG/JPEG y ≤ ~1.4 MB (mismo tope que valida biller-pdf al imprimir)."""
        if not logo_b64:
            company.logo = False
            return
        if isinstance(logo_b64, str) and logo_b64.startswith("data:"):
            logo_b64 = logo_b64.split(",", 1)[-1]
        try:
            raw = base64.b64decode(logo_b64, validate=True)
        except Exception as exc:  # noqa: BLE001
            raise UserError(_("El logo no es una imagen válida.")) from exc
        if len(raw) > 1_400_000:
            raise UserError(_("El logo es demasiado grande (máx. ~1.4 MB)."))
        if not (raw[:4] == b"\x89PNG" or raw[:2] == b"\xff\xd8"):
            raise UserError(_("El logo debe ser PNG o JPEG."))
        company.logo = base64.b64encode(raw)

    @api.model
    def l10n_pe_ne_buscar_distrito(self, q=None, limit=20):
        """Busca distritos (ubigeo) por nombre, código, provincia o departamento — así el
        selector llena el ubigeo automáticamente sin tipear los 6 dígitos (escribes 'Miraflores'
        o 'Arequipa' y sale el distrito con su código)."""
        q = (q or "").strip()
        dom = (["|", "|", "|",
                ("name", "ilike", q), ("code", "ilike", q),
                ("city_id.name", "ilike", q), ("city_id.state_id.name", "ilike", q)]
               if q else [])
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
        if "datosPago" in vals:
            company.l10n_pe_ne_datos_pago = (vals.get("datosPago") or "").strip() or False
        if "agentePercepcion" in vals:
            company.l10n_pe_ne_agente_percepcion = bool(vals.get("agentePercepcion"))
        if "redondeoActivo" in vals:
            company.l10n_pe_ne_redondeo_activo = bool(vals.get("redondeoActivo"))
        if vals.get("redondeoModo") in ("favor", "cercano"):
            company.l10n_pe_ne_redondeo_modo = vals["redondeoModo"]
        if "logo" in vals:
            self._l10n_pe_ne_set_logo(company, vals.get("logo"))
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


