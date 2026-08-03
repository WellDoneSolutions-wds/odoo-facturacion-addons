# -*- coding: utf-8 -*-
"""account.move — Compras (margen, XML de compra).
Extraído de account_move_biller.py (refactor sin cambio de comportamiento)."""
import base64
import re

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import float_round

from ..tools.caja_arqueo import es_efectivo


class AccountMove(models.Model):
    _inherit = "account.move"

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
            # Base e IGV separados: es lo que pide el Registro de Compras y lo que sostiene el
            # crédito fiscal. Antes la compra iba sin impuesto y solo se guardaba el total.
            # Redondeado a la moneda: la resta en float da 1.1000000000000005 y ese ruido
            # llegaría tal cual a la pantalla.
            "base": self.amount_untaxed or 0.0,
            "igv": float_round(
                (self.amount_total or 0.0) - (self.amount_untaxed or 0.0),
                precision_rounding=self.currency_id.rounding or 0.01,
            ),
            "afectacion": (
                self.invoice_line_ids[:1].tax_ids[:1].l10n_pe_edi_tax_code or "1000"
            ) if self.invoice_line_ids[:1].tax_ids[:1] else "9998",
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
    def l10n_pe_ne_importar_compra_xml(self, payload):
        """Lee el XML de la factura electrónica del PROVEEDOR y devuelve el payload de una
        compra, listo para que el usuario lo revise y guarde. NO registra nada.

        El proveedor está obligado a entregar el XML: es el documento fiscal de verdad (el PDF
        es solo su representación impresa). Leerlo evita teclear —y equivocarse— en el dato
        que va al Registro de Compras.

        Devuelve, no guarda: el mapeo de productos necesita a un humano (ver abajo) y el
        usuario debe poder revisar antes de que entre mercadería al kardex.
        """
        b64 = (payload or {}).get("xml") or ""
        try:
            raw = base64.b64decode(b64)
        except Exception:
            raise UserError(_("El archivo no es un XML válido (base64 ilegible)."))
        return self._l10n_pe_ne_parse_compra_xml(raw)

    @api.model
    def _l10n_pe_ne_parse_compra_xml(self, raw):
        """Parseo puro del UBL 2.1 (Invoice/CreditNote) → payload de compra. Sin ORM salvo el
        match de productos, para poder testearlo con un XML real."""
        from xml.etree import ElementTree as ET

        try:
            root = ET.fromstring(raw)
        except ET.ParseError as e:
            raise UserError(_("No se pudo leer el XML: %s") % e)
        # Los tags vienen con namespace; se ignora el prefijo y se busca por nombre local.
        # Es lo mismo que hace el biller al depurar el XML para el PDF: los namespaces de
        # SUNAT varían por versión y atarse a ellos rompe con el primer proveedor distinto.
        def hijos(el, nombre):
            return [c for c in el if c.tag.rsplit("}", 1)[-1] == nombre] if el is not None else []

        def uno(el, *ruta):
            cur = el
            for nombre in ruta:
                hs = hijos(cur, nombre)
                if not hs:
                    return None
                cur = hs[0]
            return cur

        def txt(el, *ruta):
            n = uno(el, *ruta) if ruta else el
            return (n.text or "").strip() if n is not None and n.text else ""

        if root.tag.rsplit("}", 1)[-1] not in ("Invoice", "CreditNote", "DebitNote"):
            raise UserError(
                _("El XML no es un comprobante electrónico (se esperaba Invoice/CreditNote).")
            )
        sup = uno(root, "AccountingSupplierParty", "Party")
        ruc = txt(sup, "PartyIdentification", "ID")
        razon = txt(sup, "PartyLegalEntity", "RegistrationName") or txt(sup, "PartyName", "Name")
        if not ruc:
            raise UserError(_("El XML no trae el RUC del emisor."))
        doc_id = txt(root, "ID")
        serie, _sep, numero = doc_id.partition("-")
        tipo = txt(root, "InvoiceTypeCode") or "01"
        total = txt(root, "LegalMonetaryTotal", "PayableAmount")
        igv = ""
        for tt in hijos(root, "TaxTotal"):
            igv = txt(tt, "TaxAmount")
            if igv:
                break

        lineas = []
        for ln in hijos(root, "InvoiceLine") + hijos(root, "CreditNoteLine"):
            item = uno(ln, "Item")
            cant = txt(ln, "InvoicedQuantity") or txt(ln, "CreditedQuantity")
            # Precio CON IGV: AlternativeConditionPrice con PriceTypeCode 01 (catálogo 16 de
            # SUNAT) es el precio unitario que incluye el impuesto — la misma convención que
            # usa toda la app. cac:Price (sin IGV) NO sirve acá: el detalle se compara contra
            # el total del documento, que va con IGV.
            precio = ""
            for pr in hijos(uno(ln, "PricingReference") or ln, "AlternativeConditionPrice"):
                if txt(pr, "PriceTypeCode") == "01":
                    precio = txt(pr, "PriceAmount")
                    break
            cod_prov = txt(item, "SellersItemIdentification", "ID")
            barcode = txt(item, "StandardItemIdentification", "ID")
            lineas.append({
                "descripcion": txt(item, "Description"),
                "cantidad": float(cant or 0),
                "precioUnitario": float(precio or 0),
                "codigoProveedor": cod_prov,
                "barcode": barcode,
                # El match con NUESTRO catálogo es una propuesta, no un hecho: el proveedor
                # nombra y codifica los productos a su manera. Sin coincidencia se deja en
                # None y lo elige el usuario — inventar el mapeo ensuciaría el kardex.
                "productId": self._l10n_pe_ne_match_producto(barcode, cod_prov),
            })
        # Afectación: se LEE del XML (TaxScheme/ID, catálogo 05 de la primera línea), no se
        # deduce del IGV. Asumir "gravado" en una factura exonerada le inventaría al usuario
        # un crédito fiscal que no tiene — un error fiscal, no una imprecisión.
        afect = ""
        primera = (hijos(root, "InvoiceLine") + hijos(root, "CreditNoteLine"))[:1]
        if primera:
            st = uno(primera[0], "TaxTotal", "TaxSubtotal")
            afect = txt(st, "TaxCategory", "TaxScheme", "ID")
        return {
            "proveedor": {"tipoDoc": "6", "numDoc": ruc, "razonSocial": razon},
            "tipoComprobante": tipo,
            "serie": serie,
            "numero": numero.lstrip("0") or numero,
            "fecha": txt(root, "IssueDate"),
            "total": float(total or 0),
            "igv": float(igv or 0),
            "afectacion": afect or ("1000" if float(igv or 0) > 0 else "9998"),
            "descripcion": "",
            "lineas": lineas,
        }

    @api.model
    def _l10n_pe_ne_match_producto(self, barcode, codigo):
        """Propone un producto NUESTRO para una línea del XML del proveedor.

        Por código de barras primero (el GTIN es universal: si coincide, es el mismo producto)
        y por código propio después (más débil: 'P001' puede ser cualquier cosa en otro
        catálogo). Sin coincidencia devuelve None y decide el usuario."""
        Product = self.env["product.product"]
        if barcode:
            p = Product.search([("barcode", "=", barcode)], limit=1)
            if p:
                return p.id
        if codigo:
            p = Product.search([("default_code", "=", codigo)], limit=1)
            if p:
                return p.id
        return None

    @api.model
    def _l10n_pe_ne_tax_compra_by_code(self, code):
        """account.tax de COMPRA por código cat-05; default 1000 (IGV gravado).

        Existe aparte del de venta porque el crédito fiscal se imputa con impuestos de
        compra: usar el de venta metería el IGV en la cuenta equivocada. La localización ya
        trae los cuatro (IGV 18%, 0% Exo, 0% Ina, 0% Exp)."""
        return self.env["account.tax"].search(
            [
                ("company_id", "=", self.env.company.id),
                ("type_tax_use", "=", "purchase"),
                ("l10n_pe_edi_tax_code", "=", code or "1000"),
            ],
            limit=1,
        )

    @api.model
    def _l10n_pe_ne_base_sin_igv(self, bruto, tax):
        """Precio CON IGV → base SIN IGV, que es lo que espera `price_unit` con un impuesto
        tax_excluded (la convención de esta app: el usuario ve y teclea precios con IGV).

        No se redondea a 2: con `round_globally` en la compañía, Odoo calcula el impuesto
        sobre la base sin redondear y el total vuelve a dar el bruto redondo. Redondear acá
        rompería justo eso (118 → base 100.00 ✓, pero 7.20 → 6.10 y el total daría 7.198)."""
        rate = (tax.amount or 0) if tax else 0
        return (bruto or 0) / (1 + rate / 100.0) if rate else (bruto or 0)

    @api.model
    def _l10n_pe_ne_compra_lineas(self, compra):
        """invoice_line_ids de una compra, desde `lineas` si vienen, o la línea única del total.

        El detalle es OPCIONAL a propósito: no toda compra es mercadería (luz, alquiler,
        servicios), y el flujo de "solo el total" existe para registrar el crédito fiscal sin
        inventariar nada. Quien necesita kardex, detalla; el resto sigue como siempre.
        Solo las líneas con producto pueden mover stock (ver _l10n_pe_ne_lineas_con_stock)."""
        # Afectación del documento (cat-05): 1000 gravado por defecto — es la compra normal.
        # Va a nivel documento y no por línea: una factura de compra suele ser toda gravada o
        # toda no gravada (un recibo de servicios, un RH). El caso mixto necesita afectación
        # por línea y es otra iteración; hoy no hay dato para adivinarlo.
        tax = self._l10n_pe_ne_tax_compra_by_code(compra.get("afectacion"))
        tax_ids = [(6, 0, tax.ids if tax else [])]
        lineas = compra.get("lineas") or []
        if not lineas:
            total = float(compra.get("total") or 0)
            return [
                (0, 0, {
                    "name": compra.get("descripcion") or "COMPRA",
                    "quantity": 1,
                    # El total va CON IGV: se guarda la base y Odoo repone el impuesto, así
                    # el Registro de Compras tiene base e IGV separados (antes iba sin
                    # impuesto y el crédito fiscal no existía).
                    "price_unit": self._l10n_pe_ne_base_sin_igv(total, tax),
                    "tax_ids": tax_ids,
                })
            ]
        out = []
        suma = 0.0
        for ln in lineas:
            # create=False: una compra NO da de alta productos en el catálogo. El proveedor
            # los llama a su manera y crearlos aquí llenaría el catálogo de duplicados; se
            # elige uno existente desde la UI. Sin producto, la línea es solo un importe.
            prod = self._l10n_pe_ne_quick_product(ln, create=False)
            cant = float(ln.get("cantidad") or 0)
            if cant <= 0:
                raise UserError(_("Cada línea de la compra necesita una cantidad mayor a 0."))
            costo = float(ln.get("precioUnitario") or 0)
            if costo < 0:
                raise UserError(_("El costo de una línea no puede ser negativo."))
            suma += cant * costo
            # El costo de compra se guarda SIEMPRE: es un hecho del documento, no una
            # opinión — es lo que se pagó. El precio de VENTA solo se toca si el usuario lo
            # pidió (actualizarPrecio), porque cambiarlo solo movería la etiqueta de la
            # vitrina sin que nadie se entere.
            if prod and costo > 0:
                prod.sudo().standard_price = costo
                if ln.get("actualizarPrecio"):
                    prod.sudo().list_price = self._l10n_pe_ne_precio_con_margen(
                        costo, prod.l10n_pe_ne_margen or None
                    )
            vals = {
                "name": ln.get("descripcion") or (prod.name if prod else "ITEM"),
                "quantity": cant,
                # `costo` viene CON IGV (la convención de la app y lo que trae el XML del
                # proveedor en AlternativeConditionPrice); se guarda la base y Odoo repone
                # el impuesto. La suma para el cuadre sigue siendo sobre el bruto.
                "price_unit": self._l10n_pe_ne_base_sin_igv(costo, tax),
                "tax_ids": tax_ids,
                # El lote entra con la mercadería: viaja con la línea hasta el movimiento.
                "l10n_pe_ne_lote": (ln.get("lote") or "").strip() or False,
                "l10n_pe_ne_vence": ln.get("vence") or False,
            }
            if prod:
                vals["product_id"] = prod.id
            out.append((0, 0, vals))
        # El detalle MANDA: la compra se registra por la suma de las líneas y el `total` del
        # payload queda ignorado. Si no cuadran, lo que entra al Registro de Compras no es lo
        # que el usuario cree — un error fiscal. Se corta acá y no solo en el front: el
        # backend es la autoridad y a /ne/api/compras puede llamar cualquiera.
        total = float(compra.get("total") or 0)
        if total and abs(suma - total) > 0.01:
            raise UserError(
                _(
                    "El detalle suma %(suma).2f y el total de la compra dice %(total).2f. "
                    "Deben coincidir."
                )
                % {"suma": suma, "total": total}
            )
        return out

    @api.model
    def l10n_pe_ne_create_compra(self, compra):
        """Registra una compra (factura de proveedor). payload: {proveedor:{numDoc,
        razonSocial,tipoDoc}, tipoComprobante(cat.10), serie, numero, fecha, total,
        descripcion, moneda, lineas?}. Sin `lineas` es el registro simple de siempre
        (línea = total); con `lineas` se detalla por producto y la mercadería ENTRA al stock."""
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
            "invoice_line_ids": self._l10n_pe_ne_compra_lineas(compra),
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
        # La otra mitad del kardex: la mercadería detallada ENTRA al stock. Sin líneas con
        # producto no mueve nada, así que la compra "solo total" de siempre no cambia.
        move._l10n_pe_ne_mover_stock_compra()
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
            if d.get("cuentaBN"):
                # La cuenta se guarda EN el comprobante (lo tecleado siempre gana y sale
                # tal cual en su PDF/XML). Además, si la empresa aún no tiene cuenta de
                # detracción por defecto, se fija con la primera para futuras emisiones.
                move.l10n_pe_ne_detraccion_cuenta = d["cuentaBN"]
                if not move.company_id.l10n_pe_ne_cuenta_detraccion:
                    move.company_id.sudo().l10n_pe_ne_cuenta_detraccion = d["cuentaBN"]
        p = payload.get("percepcion")
        if p:
            move.l10n_pe_ne_percepcion = True
            move.l10n_pe_ne_percepcion_rate = float(p.get("tasa") or 2)
        if payload.get("esAnticipo"):
            move.l10n_pe_ne_es_anticipo = True
        # Descuento que NO afecta la base del IGV: el por ítem (descNoAfecta de cada línea) + el global
        # (descuentoGlobalNoAfecta) se agregan en un solo importe. NO reduce gravada/IGV: el emisor lo
        # aplica como AllowanceCharge global que solo baja el total (ver _l10n_pe_desc_no_afecta).
        desc_no_afecta = round(
            sum(float(ln.get("descNoAfecta") or 0) for ln in (payload.get("lineas") or []))
            + float(payload.get("descuentoGlobalNoAfecta") or 0),
            2,
        )
        if desc_no_afecta > 0:
            move.l10n_pe_ne_desc_no_afecta = desc_no_afecta
        # Anticipos regularizados: lista JSON (varios anticipos / pagos escalonados). Retrocompat
        # con el payload viejo de un solo anticipo (objeto): se envuelve en lista de 1.
        anticipos = payload.get("anticipos")
        if anticipos is None and payload.get("anticipo"):
            anticipos = [payload["anticipo"]]
        if anticipos:
            move.l10n_pe_ne_anticipos = [
                {
                    "doc": a.get("doc"),
                    "monto": float(a.get("monto") or a.get("total") or 0),
                    "tipo": a.get("tipo") or "02",
                    # Enlace al anticipo local (doc. A) elegido en el autocompletado, para
                    # llevar su saldo.
                    "origenId": a.get("origenId"),
                }
                for a in anticipos
            ]
        # Forma de pago: Crédito (con cuotas) emite cac:PaymentTerms; medios de pago
        # (efectivo/Yape/…) se guardan como dato interno del POS (no van al XML SUNAT).
        # El establecimiento emisor NO se toca aquí: llega resuelto y validado en el create
        # (_l10n_pe_ne_resolver_establecimiento). Este método corre después, y para entonces la
        # serie ya está elegida; volver a escribir el código del payload le pasaría por encima a
        # la herencia dura de las notas, que ignoran el payload a propósito.
        # Orden de compra del cliente (cac:OrderReference).
        if payload.get("ordenCompra"):
            move.l10n_pe_ne_orden_compra = str(payload["ordenCompra"]).strip()
        # Observación general (print-only): va a narration y sale como "Observación: <texto>"
        # en el ticket y el A4 (adicionalTxt). NO va al XML firmado.
        if payload.get("observacion"):
            move.narration = payload["observacion"]
        # Razón social override por-comprobante: solo boleta (03), constancia institucional. NO renombra
        # el partner (que ya existe con su nombre RENIEC al llegar acá; ver diseño).
        if (payload.get("tipoDoc") or "01") == "03":
            cn = ((payload.get("cliente") or {}).get("razonSocial") or "").strip()
            if cn:
                move.l10n_pe_ne_cliente_nombre = cn
        if payload.get("placa"):
            move.l10n_pe_ne_placa = str(payload["placa"]).strip().upper()
        # Ventas al Estado (proveedor del Estado): 4 datos del proceso de contratación pública.
        ve = payload.get("ventaEstado") or {}
        if ve:
            move.l10n_pe_ne_estado_expediente = (ve.get("expediente") or "").strip()
            move.l10n_pe_ne_estado_unidad_ejecutora = (ve.get("unidadEjecutora") or "").strip()
            move.l10n_pe_ne_estado_proceso_seleccion = (ve.get("procesoSeleccion") or "").strip()
            move.l10n_pe_ne_estado_contrato = (ve.get("contrato") or "").strip()
            # Conformidad / acta de recepción (opcional): dato de registro, no va al XML.
            move.l10n_pe_ne_conformidad = (ve.get("conformidad") or "").strip() or False
        # Guía de remisión referenciada (DespatchDocumentReference).
        if payload.get("guiaRef"):
            move.l10n_pe_ne_guia_ref = payload["guiaRef"]
            if payload.get("guiaTipo"):
                move.l10n_pe_ne_guia_tipo = payload["guiaTipo"]
        # DUA/DAM de exportación (QA-023): dato del ERP, NO va al XML (ver l10n_pe_ne_dua). Opcional
        # —la exportación se emite sin ella (QA-024)— y solo se guarda si el front la mandó.
        if payload.get("dua"):
            move.l10n_pe_ne_dua = str(payload["dua"]).strip()
        # Proyecto/contrato (avance de obra).
        if payload.get("proyectoId"):
            move.l10n_pe_ne_proyecto_id = int(payload["proyectoId"])
        # Retención de garantía (fiel cumplimiento de obra): % que el cliente retiene y libera al
        # final. Reduce el neto a cobrar; no toca el total ni el IGV.
        if payload.get("retencionGarantia"):
            move.l10n_pe_ne_retencion_garantia_rate = float(
                (payload["retencionGarantia"] or {}).get("tasa") or 0)
        # Amortización de adelanto de obra (deducción contractual, no anticipo SUNAT).
        if payload.get("amortizacionAdelanto"):
            move.l10n_pe_ne_amortizacion_adelanto = float(payload["amortizacionAdelanto"] or 0)
        # Penalidad del contrato (Estado/obra): descuento fijo por incumplimiento; reduce el neto.
        if payload.get("penalidad"):
            move.l10n_pe_ne_penalidad = float(payload["penalidad"] or 0)
        # Convenio / tercero pagador (SIS/aseguradora): la parte cubierta reduce el copago del paciente.
        if payload.get("convenio"):
            c = payload["convenio"] or {}
            move.l10n_pe_ne_tercero_pagador = (c.get("tercero") or "").strip() or False
            move.l10n_pe_ne_monto_cubierto = float(c.get("montoCubierto") or 0)
        # Receta retenida (venta de productos controlados).
        if payload.get("receta"):
            r = payload["receta"] or {}
            move.l10n_pe_ne_receta_numero = (r.get("numero") or "").strip() or False
            move.l10n_pe_ne_receta_colegiatura = (r.get("colegiatura") or "").strip() or False
        fp = payload.get("formaPago") or {}
        if fp.get("tipo") == "Credito" or fp.get("cuotas"):
            move.l10n_pe_ne_forma_pago = "Credito"
            move.l10n_pe_ne_cuotas = fp.get("cuotas") or []
            # Forma de pago mixta: inicial al contado; el saldo a crédito lo llevan las cuotas.
            if fp.get("inicial"):
                move.l10n_pe_ne_inicial_contado = float(fp["inicial"])
            venc = (fp.get("cuotas") or [{}])[-1].get("fecha")
            if venc:
                move.invoice_date_due = venc
        if fp.get("medios"):
            move.l10n_pe_ne_medios_pago = fp.get("medios")
        # El vencimiento (cbc:DueDate) es AUTOMÁTICO, no lo decide el emisor: al crédito la fija la
        # última cuota (arriba); al contado la cabecera usa la fecha de emisión (vence el mismo día).
        # Por eso ya no se recibe una fechaVencimiento manual desde el front.
        # Redondeo de efectivo (dato de caja, no del XML): el POS lo calcula en vivo (≤ 0). Se
        # persiste solo si el pago es efectivo y el flag de la compañía está activo; ausente/0 = sin
        # redondeo. El importe entregado en efectivo = amount_total + redondeo.
        red = payload.get("redondeo")
        if red and move.company_id.l10n_pe_ne_redondeo_activo and self._l10n_pe_ne_solo_efectivo(fp.get("medios")):
            move.l10n_pe_ne_redondeo = float(red)

    @staticmethod
    def _l10n_pe_ne_solo_efectivo(medios):
        """¿el pago es 100% efectivo? Sin medios detallados el POS asume efectivo (True). Un solo
        medio no-efectivo o mezcla desactiva el redondeo (espeja lib/redondeo.ts:esSoloEfectivo).

        C1: la comparación es case/tilde-insensitive (es_efectivo). Con el `== "Efectivo"` literal,
        un pago escrito 'efectivo' perdía el redondeo de la Ley 29571 y el cliente pagaba la
        fracción que no existe en monedas."""
        con_monto = [m for m in (medios or []) if float(m.get("monto") or 0) > 0]
        if not con_monto:
            return True
        return all(es_efectivo(m.get("medio") or "Efectivo") for m in con_monto)

    def l10n_pe_ne_quick_result(self):
        self.ensure_one()
        serie, corr = self._l10n_pe_serie_correlativo()
        m = re.search(r"ResponseCode (\d+)", self.l10n_pe_biller_message or "")
        return {
            "id": self.id,
            "tipoDoc": self.l10n_pe_ne_tipo_doc or self._l10n_pe_document_type(),
            "serie": self.l10n_pe_ne_serie_emit or serie,
            "correlativo": (self.l10n_pe_ne_corr_emit or corr).zfill(8),
            # Local que se DECLARÓ (no el que la pantalla creía): el resolver puede haberlo
            # sacado de la caja abierta o del comprobante afectado, y el POS ni siquiera lo
            # manda. Devolverlo aquí es lo que deja al ticket 80mm decir la verdad y al cajero
            # detectar en el acto que cobró desde el local equivocado.
            "establecimiento": self.l10n_pe_ne_cod_establecimiento or "0000",
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
                              forma_pago=None, monto_min=None, monto_max=None, serie=None,
                              moneda=None, bancarizacion=None, establecimiento=None,
                              limit=100, offset=None):
        """Lista de comprobantes emitidos (sin los blobs), para la UI. Filtros
        opcionales: query (cliente/RUC/correlativo), rango de fechas (desde/hasta),
        estado del facturador (por_enviar/en_proceso/enviado/anulado/rechazado/error),
        tipo de comprobante (01/03/07/08), forma de pago (Contado/Credito), rango de
        monto total (monto_min/monto_max) y establecimiento emisor. `estado`, `tipo`,
        `serie` y `establecimiento` aceptan varios valores (lista o CSV "a,b") →
        filtran con `in` (multiselect en la UI).

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
        series = _as_list(serie)
        locales = _as_list(establecimiento)
        mmin, mmax = _num(monto_min), _num(monto_max)
        # Se incluyen los 'por_enviar' (pendientes de envío) para que sean visibles y
        # reenviables desde la UI; antes se excluían y quedaban sin dónde verse.
        domain = [("l10n_pe_biller_state", "!=", False)]
        if estados:
            domain.append(("l10n_pe_biller_state", "in", estados))
        if tipos:
            domain.append(("l10n_pe_ne_tipo_doc", "in", tipos))
        if series:
            domain.append(("l10n_pe_ne_serie_emit", "in", series))
        if locales:
            # «Cuánto vendió Miraflores» es la primera pregunta del dueño con dos locales.
            # El '0000' arrastra los comprobantes sin código: un NULL o un '' es domicilio
            # fiscal (así lo entiende el XML y así lo declaró el emisor), y dejarlos fuera
            # escondería toda la historia anterior a esta fase justo en el filtro que se usa
            # para cuadrar el mes.
            valores = list(locales)
            if "0000" in valores:
                valores += [False, ""]
            domain.append(("l10n_pe_ne_cod_establecimiento", "in", valores))
        if moneda:
            domain.append(("currency_id.name", "=", moneda))
        if forma_pago:
            domain.append(("l10n_pe_ne_forma_pago", "=", forma_pago))
        if bancarizacion:
            domain.append(("l10n_pe_ne_bancarizacion", "=", bancarizacion))
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
        # NC vigentes por comprobante en UNA consulta agrupada: la lista marca las
        # facturas/boletas acreditadas ("tiene NC") sin una búsqueda por fila. Mismo
        # criterio de "vigente" que _l10n_pe_ne_nc_previas (las en cola cuentan).
        nc_por_doc = {}
        if moves:
            grupos = self.env["account.move"]._read_group(
                [
                    ("move_type", "=", "out_refund"),
                    ("reversed_entry_id", "in", moves.ids),
                    ("state", "=", "posted"),
                    ("l10n_pe_biller_state", "not in", ("rechazado", "error", "anulado")),
                ],
                groupby=["reversed_entry_id"],
                aggregates=["__count", "amount_total:sum"],
            )
            nc_por_doc = {rev.id: (count, total or 0.0) for rev, count, total in grupos}
        items = [
            {
                "id": m.id,
                "tipoDoc": m.l10n_pe_ne_tipo_doc or m._l10n_pe_document_type(),
                "serie": m.l10n_pe_ne_serie_emit or m.l10n_pe_serie or "",
                "correlativo": m.l10n_pe_ne_corr_emit or "",
                "estado": m.l10n_pe_biller_state,
                "bancarizacion": m.l10n_pe_ne_bancarizacion,
                "total": m.amount_total,
                "moneda": m.currency_id.name or "PEN",
                "cliente": m.partner_id.name or "",
                "fechaEmision": m.invoice_date.strftime("%Y-%m-%d")
                if m.invoice_date
                else "",
                # Fecha de vencimiento (cbc:DueDate): al crédito = última cuota; al contado, el
                # plazo opcional que fijó el emisor. Vacío si el comprobante no tiene vencimiento.
                "vencimiento": m.invoice_date_due.strftime("%Y-%m-%d")
                if m.invoice_date_due
                else "",
                # Hora de creación del comprobante (≈ emisión), en tz local (Lima).
                "hora": fields.Datetime.context_timestamp(m, m.create_date).strftime("%H:%M")
                if m.create_date
                else "",
                "mensaje": m.l10n_pe_biller_message or "",
                # Local emisor declarado (codLocalEmisor del XML). El comprobante anterior a
                # esta fase no tiene código y es domicilio fiscal: se dice '0000' en vez de
                # dejar la celda vacía, que se leería como "no se sabe".
                "establecimiento": m.l10n_pe_ne_cod_establecimiento or "0000",
                # Notas de crédito vigentes que afectan este comprobante (0 si no tiene).
                "ncCount": nc_por_doc.get(m.id, (0, 0.0))[0],
                "ncTotal": round(nc_por_doc.get(m.id, (0, 0.0))[1], 2),
            }
            for m in moves
        ]
        if offset is None:
            return items
        return {"items": items, "total": self.search_count(domain)}

    def _l10n_pe_ne_nc_previas(self):
        """Notas de crédito VIGENTES que afectan este comprobante: posteadas y no
        rechazadas/anuladas/con error. Las que siguen en cola (por_enviar/en_proceso)
        también cuentan, para que dos NC simultáneas no acrediten más que el total."""
        self.ensure_one()
        return self.env["account.move"].search(
            [
                ("move_type", "=", "out_refund"),
                ("reversed_entry_id", "=", self.id),
                ("state", "=", "posted"),
                ("l10n_pe_biller_state", "not in", ("rechazado", "error", "anulado")),
            ]
        )

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
                    "unidad": self._l10n_pe_unit_code(ln),
                    "subtotal": ln.price_subtotal or 0.0,
                    # El front lo usa para conservar el producto real al espejar una NC
                    # o al refacturar (post-NC motivo 02).
                    "productId": ln.product_id.id or None,
                }
            )
        of, ot, os_, on = self._l10n_pe_ne_ple_origen()
        # NC previas vigentes (solo aplica a facturas/boletas): el front muestra el saldo
        # pendiente de acreditar y las notas asociadas al elegir el comprobante a afectar.
        ncs = (
            self._l10n_pe_ne_nc_previas()
            if self.move_type == "out_invoice"
            else self.env["account.move"]
        )
        return {
            "id": self.id,
            "tipoDoc": self.l10n_pe_ne_tipo_doc or self._l10n_pe_document_type(),
            "serie": serie,
            "correlativo": num,
            "fecha": self.invoice_date.strftime("%Y-%m-%d")
            if self.invoice_date
            else "",
            # Vencimiento (cbc:DueDate) y cuotas del crédito, para mostrarlos en el detalle.
            "vencimiento": self.invoice_date_due.strftime("%Y-%m-%d")
            if self.invoice_date_due
            else "",
            "cliente": self.partner_id.name or "",
            "clienteDoc": self.partner_id.vat or "",
            "moneda": self.currency_id.name or "PEN",
            # Local emisor tal como salió en el XML (codLocalEmisor) + su dirección, para que
            # el detalle y la representación impresa (ticket 80mm / A4) digan desde dónde se
            # vendió. En una NC/ND es el local HEREDADO del comprobante afectado.
            "establecimiento": self.l10n_pe_ne_cod_establecimiento or "0000",
            "establecimientoDireccion": self._l10n_pe_ne_direccion_local(
                self.l10n_pe_ne_cod_establecimiento),
            "estado": self.l10n_pe_biller_state or "",
            "mensaje": self.l10n_pe_biller_message or "",
            "formaPago": self.l10n_pe_ne_forma_pago or "Contado",
            "cuotas": self.l10n_pe_ne_cuotas or [],
            "docOrigen": ("%s %s-%s" % (ot, os_, on)) if on else "",
            # DUA/DAM de exportación (QA-023): referencia del ERP para el detalle. Vacía mientras
            # aduanas no la haya numerado (se emite sin ella, QA-024).
            "dua": self.l10n_pe_ne_dua or "",
            # Avance de obra: N° de valorización de este comprobante y el estado del contrato
            # (avance acumulado y saldo) para mostrarlo en el detalle. None si no es valorización.
            "valorizacionNro": self.l10n_pe_ne_valorizacion_nro or None,
            "proyecto": self.l10n_pe_ne_proyecto_id._l10n_pe_ne_dict()
            if self.l10n_pe_ne_proyecto_id else None,
            # Retención de garantía de obra: % y monto retenido de ESTA valorización (None si no
            # aplica). Reduce el neto a cobrar; no cambia el total del comprobante.
            "retencionGarantia": {
                "tasa": self.l10n_pe_ne_retencion_garantia_rate,
                "monto": self._l10n_pe_ne_retencion_garantia_monto(),
            } if self.l10n_pe_ne_retencion_garantia_rate else None,
            # Amortización de adelanto de obra recuperada en esta valorización (None si no aplica).
            "amortizacionAdelanto": self.l10n_pe_ne_amortizacion_adelanto or None,
            # Penalidad del contrato descontada en este comprobante (None si no aplica).
            "penalidad": self.l10n_pe_ne_penalidad or None,
            # Conformidad / acta de recepción (venta al Estado). "" si no se registró.
            "conformidad": self.l10n_pe_ne_conformidad or "",
            # Convenio (tercero pagador): quién cubre, cuánto, y el copago que paga el paciente.
            "convenio": {
                "tercero": self.l10n_pe_ne_tercero_pagador or "",
                "montoCubierto": self.l10n_pe_ne_monto_cubierto,
                "copago": round(max(0.0, self._l10n_pe_importe_cobrar() - (self.l10n_pe_ne_monto_cubierto or 0.0)), 2),
            } if self.l10n_pe_ne_monto_cubierto else None,
            # Receta retenida (productos controlados): número + colegiatura del médico. None si no aplica.
            "receta": {
                "numero": self.l10n_pe_ne_receta_numero or "",
                "colegiatura": self.l10n_pe_ne_receta_colegiatura or "",
            } if self.l10n_pe_ne_receta_numero else None,
            "anticipos": self._l10n_pe_ne_anticipos_list(),
            "lineas": lineas,
            "notasCredito": [
                {
                    "id": m.id,
                    "numero": "%s-%s" % m._l10n_pe_ne_doc_id(),
                    "total": round(m.amount_total or 0.0, 2),
                    "estado": m.l10n_pe_biller_state or "",
                }
                for m in ncs
            ],
            "saldoAcreditable": round(
                (self.amount_total or 0.0) - sum(ncs.mapped("amount_total")), 2
            ),
            "totales": {
                "gravada": round(b["gravado"], 2),
                "exonerada": round(b["exonerado"], 2),
                "inafecta": round(b["inafecto"], 2),
                "igv": round(b["igv"], 2),
                "icbper": round(b["icbper"], 2),
                # Total = importe a COBRAR (lo que paga el cliente), no amount_total: excluye los
                # bienes gratuitos, el anticipo aplicado y el descuento que no afecta el IGV. Así el
                # "Total" del detalle == la suma de su propio desglose (gravada+exo+ina+IGV+ICBPER) y
                # no confunde con un total inflado por una línea gratuita (== el mtoImpVenta emitido).
                "total": round(self._l10n_pe_importe_cobrar(), 2),
            },
            # Bancarización (Ley 28194): estado + constancia/fecha/medio + nombre del documento
            # de respaldo (el binario se sirve aparte, no en el detalle).
            "bancarizacion": self.l10n_pe_ne_bancarizacion,
            "bancarizacionConstancia": self.l10n_pe_ne_bancarizacion_constancia or "",
            "bancarizacionFecha": (self.l10n_pe_ne_bancarizacion_fecha and str(self.l10n_pe_ne_bancarizacion_fecha)) or "",
            "bancarizacionMedio": self.l10n_pe_ne_bancarizacion_medio or "",
            "bancarizacionDocNombre": self.l10n_pe_ne_bancarizacion_doc_name or "",
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
        # El PDF/ticket se renderiza contra el micro a partir del XML firmado (no del
        # CDR): está disponible apenas FIRMADO (en_proceso), sin esperar a SUNAT. Se
        # genera SOLO el formato pedido (un pedido de xml/cdr no debe disparar el micro).
        # Antes se generaba el PDF SIEMPRE y se tragaba cualquier fallo → el controller
        # devolvía un 404 opaco ("no tiene pdf") aunque el problema real fuera el micro
        # caído o un timeout. Ahora, si falla justo el formato pedido, se propaga el
        # motivo real (el controller lo traduce a un mensaje legible).
        want_pdf = kind in (None, "pdf", "ticket")
        if want_pdf and self.l10n_pe_biller_xml:
            es_ticket = kind == "ticket"
            try:
                att = self._l10n_pe_get_pdf_attachment(
                    formato="TICKET" if es_ticket else "A4"
                )
                if att:
                    out["ticket" if es_ticket else "pdf"] = b64(att)
            except Exception:
                # Solo se propaga cuando el cliente pedía EXACTAMENTE ese archivo; si el
                # kind era None (uso interno/tests) se degrada en silencio como antes.
                if kind in ("pdf", "ticket"):
                    raise
        return out

