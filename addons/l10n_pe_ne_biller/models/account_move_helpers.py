# -*- coding: utf-8 -*-
"""account.move — Helpers de emisión (tax_info, líneas, formato…).
Extraído de account_move_biller.py (refactor sin cambio de comportamiento)."""
import base64
import re
import pytz

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from ..tools.amount_to_words import leyenda_monto
from .account_move_biller import TAX_CODE_MAP, DEFAULT_TAX_CODE, UOM_CODE_BY_XMLID, DEFAULT_UNIT_CODE


class AccountMove(models.Model):
    _inherit = "account.move"

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
                    "La serie '%(serie)s' no está habilitada para %(ruc)s. Declárala en "
                    "Series (o en el campo Serie de un diario de venta) o usa una de las ya "
                    "habilitadas: %(lista)s."
                )
                % {
                    "serie": serie,
                    "ruc": self.company_id.vat or self.company_id.display_name or "",
                    "lista": ", ".join(sorted(habilitadas)),
                }
            )

    def _l10n_pe_check_serie_establecimiento(self):
        """Una serie declarada para un local solo se emite DESDE ese local.

        Corre junto a _l10n_pe_check_serie, o sea ANTES de _l10n_pe_ne_assign_numero: un
        comprobante mal armado tiene que rebotar sin consumir número, porque un correlativo
        quemado deja un hueco en la serie que después hay que justificar ante SUNAT.

        Solo veta lo que el dueño ató a un anexo concreto. Una serie declarada SIN local (la del
        domicilio fiscal) no ata a nadie: el tenant de una sola serie la emite desde donde
        quiera, exactamente como hoy. Y la elección explícita del payload sigue mandando en el
        resolver a propósito —quien pide el local 0003 con la serie de 0002 se está
        contradiciendo, y lo que corresponde es decírselo, no corregirlo por dentro—."""
        self.ensure_one()
        serie, _corr = self._l10n_pe_serie_correlativo()
        local = self.env["l10n_pe_ne.serie"]._l10n_pe_ne_local_de_serie(
            self.company_id, serie)
        if not local:
            return
        actual = self.l10n_pe_ne_cod_establecimiento or "0000"
        if actual == local.codigo:
            return
        alterna = self.env["l10n_pe_ne.serie"]._l10n_pe_ne_serie_de(
            self.company_id, self._l10n_pe_document_type(),
            self.env["l10n_pe_ne.establecimiento"].sudo().search(
                [("codigo", "=", actual), ("company_id", "=", self.company_id.id)], limit=1),
            familia=self._l10n_pe_serie_prefix(),
        )
        raise UserError(
            _(
                "La serie '%(serie)s' es del establecimiento %(suyo)s%(dir)s, pero este "
                "comprobante declara el %(actual)s. Una serie pertenece a UN solo local (SUNAT "
                "numera por RUC y serie): emitirla desde otro mezclaría la numeración de los "
                "dos. Emite desde %(suyo)s, o usa la serie del %(actual)s%(sugerida)s."
            )
            % {
                "serie": serie,
                "suyo": local.codigo,
                "dir": " (%s)" % local.direccion if local.direccion else "",
                "actual": actual if actual != "0000" else _("0000 (domicilio fiscal)"),
                "sugerida": " (%s)" % alterna if alterna else "",
            }
        )

    def _l10n_pe_ne_series_habilitadas(self):
        """Series válidas del emisor (QA-074): las declaradas en el registro por local
        (l10n_pe_ne.serie), las configuradas en sus diarios de venta (l10n_pe_ne_serie) con su
        variante de familia (F↔B), y los defaults que genera el sistema (F001/B001 y las notas
        FC01/FD01/BC01/BD01). No se usa el histórico de series ya emitidas a propósito: una serie
        inventada usada por error no debe volverse 'válida'.

        Es una UNIÓN, nunca un reemplazo: ninguna serie que valida hoy deja de validar cuando
        aparece el registro, y con el registro vacío el conjunto es exactamente el de siempre.
        El diario sigue siendo un origen vivo (no se deprecia en esta tanda)."""
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
        # Solo las ACTIVAS: apagar una serie en el registro es decir "ya no la emito", y eso
        # tiene que cortar la emisión, no quedarse en un adorno de la pantalla.
        for s in self.env["l10n_pe_ne.serie"].sudo().search(
            [("company_id", "=", self.company_id.id), ("activa", "=", True)]
        ):
            validas.add((s.codigo or "").upper())
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
        Mismo patrón, ya probado, que las Guías de Remisión (l10n_pe_ne_guia_remision).

        CONTRATO: la clave de la secuencia es (compañía, serie) y NUNCA incluye el
        establecimiento. Meter el local en el `code` es la tentación natural cuando cada sucursal
        emite lo suyo, y es exactamente el bug: dos locales que por olvido compartieran F001
        obtendrían cada uno F001-00000001, o sea comprobantes duplicados que solo se corrigen con
        comunicación de baja ante SUNAT. La unicidad del número la garantiza la serie; que una
        serie sea de un local es una restricción de CONFIGURACIÓN (l10n_pe_ne.serie, único por
        RUC), jamás de numeración. Como el código de serie ya es único por RUC, «correlativo por
        serie» ES «correlativo por local» sin tocar una línea de este motor."""
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

