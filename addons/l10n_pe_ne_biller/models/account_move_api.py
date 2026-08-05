# -*- coding: utf-8 -*-
"""account.move — API ligera BFF (React) + datos negocio + resumen estado.
Extraído de account_move_biller.py (refactor sin cambio de comportamiento)."""
import base64
import re
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError, AccessError

_logger = logging.getLogger(__name__)


class AccountMove(models.Model):
    _inherit = "account.move"

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
        # Idem para una nota de venta ya convertida (no duplicar la venta).
        nvid = payload.get("notaVentaId")
        if nvid:
            nv = self.env["l10n_pe_ne.nota_venta"].browse(int(nvid)).exists()
            if nv and nv.estado == "convertida":
                raise UserError(_(
                    "La nota de venta %s ya fue convertida en el comprobante %s; no se puede emitir otro."
                ) % (nv.name or nv.id, nv._l10n_pe_ne_comprobante_numero() or "—"))
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
            # FACTURA (01) exige RUC (hallazgo de auditoría): SUNAT rechaza una factura a
            # DNI/consumidor final, pero el rechazo llegaba DESPUÉS de emitir (comprobante en
            # 'error' con correlativo consumido). Se corta aquí, antes de armar el move. La
            # boleta (03) acepta cualquier documento; NC/ND heredan el cliente del original.
            # Se valida el NÚMERO (11 dígitos), no el tipo de documento latam: ese catálogo
            # se completa de forma desigual y bloquearía emisiones legítimas.
            if tipo == "01":
                ruc = (partner.vat or "").strip()
                if len(ruc) != 11 or not ruc.isdigit():
                    raise UserError(_(
                        "Una FACTURA exige un cliente con RUC válido (11 dígitos). "
                        "«%(cli)s» tiene el documento n.º %(nd)s: emite una BOLETA, o "
                        "corrige el cliente con su RUC.",
                        cli=partner.name or "-", nd=ruc or "(vacío)"))
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
            if ln.get("placa"):
                # Placa del vehículo POR LÍNEA (grifo/combustible) → cat-55 código 7000 en el XML.
                lvals["l10n_pe_ne_placa"] = str(ln["placa"]).strip().upper()
            if ln.get("chofer"):
                # Chofer que acompaña a la placa (grifo/combustible). NO va al XML SUNAT (no es
                # campo electrónico); solo se imprime en la representación. Ver account_move_pdf.
                lvals["l10n_pe_ne_chofer"] = str(ln["chofer"]).strip()
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
        # Local emisor ANTES del create: decide la serie, y la serie decide el contador. El
        # payload sigue pudiendo imponerlo; si lo que impone contradice a la serie pedida, el
        # gate de _l10n_pe_check_serie_establecimiento lo corta antes de quemar el correlativo.
        cod_estab = self._l10n_pe_ne_resolver_establecimiento(payload, origin)
        # C1: auto-apertura de caja. Va AQUÍ —después de resolver el local y ANTES del create—
        # por dos razones que no son de estilo:
        #   * después de resolver, porque el resolver mira la caja abierta (escalón 4) y abrirla
        #     antes le cambiaría la respuesta a este mismo comprobante;
        #   * antes del create, porque el arqueo amarra las ventas por `create_date >=
        #     fecha_apertura`: una caja abierta después del move dejaría la venta igual de
        #     huérfana que hoy, con la sesión abierta de adorno.
        caja_abierta = self._l10n_pe_ne_autoabrir_caja(tipo, cod_estab, enviar)
        vals = {
            "move_type": "out_refund" if tipo == "07" else "out_invoice",
            "partner_id": partner.id,
            "journal_id": journal.id,
            "invoice_date": payload.get("fechaEmision")
            or self._l10n_pe_ne_today_lima(),
            "l10n_pe_serie": payload.get("serie")
            or self._l10n_pe_ne_default_serie(tipo, origin, cod_estab),
            "l10n_pe_ne_cod_establecimiento": cod_estab,
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
        # Si la emisión CONVIERTE una nota de venta, el stock YA salió al registrar la nota: no se
        # vuelve a mover (evita el doble descuento); el movimiento existente se re-vincula al
        # comprobante en _l10n_pe_ne_vincular_nota_venta (más abajo).
        if not payload.get("notaVentaId"):
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
        # Idem para una nota de venta convertida a comprobante (la marca 'convertida' e inmutable).
        if payload.get("notaVentaId"):
            self._l10n_pe_ne_vincular_nota_venta(payload["notaVentaId"], move.id)
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
        res = move.l10n_pe_ne_quick_result()
        if caja_abierta:
            # La SPA avisa «se abrió tu caja»: el cajero TIENE que enterarse, porque al cerrar va a
            # contar un turno que no abrió él (y con saldo inicial 0, no el sencillo del cajón).
            res["cajaAbierta"] = caja_abierta._l10n_pe_ne_aviso_apertura()
        return res

    @api.model
    def _l10n_pe_ne_autoabrir_caja(self, tipo, cod_estab, enviar=True):
        """C1: abre la caja del local si no hay ninguna que cuente esta venta. Devuelve la sesión
        recién abierta (para avisarlo en la respuesta) o un recordset vacío.

        Se queda fuera:
          * las NOTAS (07/08): el arqueo solo mira `out_invoice`, así que una NC nunca cae en él y
            abrir una caja por corregir un documento sería ruido puro;
          * el PRE-FLIGHT (`enviar=False`): valida y revierte, no debe dejar rastro;
          * el LOTE masivo (contexto `l10n_pe_ne_sin_autoapertura`): subir 200 comprobantes desde
            la oficina no es un cobro de mostrador, y le abriría al contador una caja que nadie
            atiende;
          * el RUC que apagó `l10n_pe_ne_caja_autoapertura` (vuelve al comportamiento anterior).

        Nunca lanza: la caja no bloquea una venta (si la apertura falla, el peor caso es el de
        hoy — la venta sale igual)."""
        if not enviar or tipo in ("07", "08"):
            return self.env["l10n_pe_ne.caja.sesion"].browse()
        if self.env.context.get("l10n_pe_ne_sin_autoapertura"):
            return self.env["l10n_pe_ne.caja.sesion"].browse()
        if not self.env.company.l10n_pe_ne_caja_autoapertura:
            return self.env["l10n_pe_ne.caja.sesion"].browse()
        try:
            sesion, abierta_ahora = self.env["l10n_pe_ne.caja.sesion"]._l10n_pe_ne_asegurar_sesion(
                cod_estab)
        except Exception as e:  # noqa: BLE001 — la caja jamás tumba una emisión
            _logger.warning("Caja: no se pudo asegurar la sesión al cobrar (%s)", e)
            return self.env["l10n_pe_ne.caja.sesion"].browse()
        return sesion if abierta_ahora else self.env["l10n_pe_ne.caja.sesion"].browse()

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
        # Cliente inexistente = FINDING, no excepción (hallazgo de auditoría: un clienteId
        # huérfano hacía crear-y-revertir el partner dentro del savepoint y el request moría
        # en 404 HTML al flushear la caché envenenada — un validador nunca debe explotar).
        payload = dict(payload or {})
        cid = (payload.get("cliente") or {}).get("id") or payload.get("clienteId")
        if cid:
            try:
                cid_ok = self.env["res.partner"].browse(int(cid)).exists()
            except (TypeError, ValueError):
                cid_ok = False
            if not cid_ok:
                return [{"code": "cliente_no_existe", "campo": "cliente",
                         "nivel": "error",
                         "mensaje": _("El cliente indicado (id %s) no existe.") % cid}]
        try:
            with self.env.cr.savepoint():
                try:
                    move = self.l10n_pe_ne_quick_emit(payload, enviar=False)
                    findings = move._l10n_pe_ne_validaciones()
                except UserError as e:
                    findings = [{"code": "bloqueo", "campo": "", "nivel": "error",
                                 "mensaje": str(e)}]
                raise _Revert()
        except _Revert:
            pass
        return findings

    def _l10n_pe_ne_vincular_nota_venta(self, nota_venta_id, move_id):
        """Vincula el comprobante emitido a la nota de venta de origen (la marca 'convertida' e
        inmutable). Lo llama quick_emit cuando la emisión vino de 'Convertir a comprobante'."""
        nv = self.env["l10n_pe_ne.nota_venta"].browse(int(nota_venta_id)).exists()
        if nv:
            nv.l10n_pe_ne_vincular_comprobante(int(move_id))
            # El stock ya lo movió la nota al registrarse (la emisión lo saltó); se re-atribuye al
            # comprobante para que el kardex/PLE 12.1 lo muestre bajo el documento fiscal.
            self.env["stock.move"].search([
                ("l10n_pe_ne_nota_venta_id", "=", nv.id),
                ("l10n_pe_ne_move_id", "=", False),
            ]).write({"l10n_pe_ne_move_id": int(move_id)})

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

    def _l10n_pe_ne_default_serie(self, tipo, origin=None, cod_estab=None):
        """Serie por defecto para (tipo, local): la que ese local declaró en el registro y, si no
        declaró ninguna, F001/B001 para factura/boleta y FC01/FD01 (o BC01/BD01 si el afectado es
        boleta) para NC/ND, derivando la familia del documento original.

        El fallback está intacto carácter por carácter: con el registro vacío —que es como
        arranca y como se queda quien no configura nada— esta función responde exactamente lo
        mismo que antes de que existieran las series por local."""
        base = (
            "B"
            if origin is not None and (origin.l10n_pe_serie or "F")[:1].upper() == "B"
            else "F"
        )
        # '0000' (y la ausencia de código) es el domicilio fiscal: recordset vacío, no una fila
        # que buscar (D3). Sus series son las que no apuntan a ningún establecimiento.
        estab = self.env["l10n_pe_ne.establecimiento"].browse()
        if cod_estab and cod_estab != "0000":
            estab = estab.sudo().search(
                [("codigo", "=", cod_estab),
                 ("company_id", "=", self.env.company.id)], limit=1)
        declarada = self.env["l10n_pe_ne.serie"]._l10n_pe_ne_serie_de(
            self.env.company, tipo, estab,
            # La familia solo acota en las notas: un tipo 07 admite FC02 y BC02 en el registro,
            # pero la de ESTA nota la manda el documento afectado.
            familia=base if tipo in ("07", "08") else None,
        )
        if declarada:
            return declarada
        if tipo == "03":
            return "B001"
        if tipo in ("07", "08"):
            return base + ("C01" if tipo == "07" else "D01")
        return "F001"

    @api.model
    def _l10n_pe_ne_resolver_establecimiento(self, payload, origin=None):
        """Local emisor (codLocalEmisor) del comprobante que se va a crear.

        UN SOLO resolver y llamado ANTES del create: el local decide la serie y la serie decide
        el contador, así que resolverlo después —donde estaba, en _l10n_pe_ne_quick_flags— llega
        tarde para elegirla y demasiado tarde para rebotar sin quemar un correlativo. Todos los
        canales (SPA, orden de trabajo, cobro de cotización, lote masivo) pasan por quick_emit,
        de modo que esta es la única regla de local que existe.

        Cadena, de lo más específico a lo más general:

          1. NC/ND: el local del comprobante afectado. Herencia DURA, el payload se ignora: una
             nota no se emite «desde otro local», es dato derivado del documento que corrige, y
             hasta ahora todas declaraban '0000' aunque corrigieran una venta de la sucursal.
          2. `codEstablecimiento` del payload: la elección explícita del emisor manda.
          3. Local de la serie pedida, si esa serie está declarada con local: pedir F002 ES decir
             «emito desde San Isidro», y obligar a repetirlo en dos campos solo da para
             contradecirse.
          4. Local de la caja abierta: se declara una vez por turno, no una vez por venta.
          5. '0000' (domicilio fiscal), que es lo que hacían todos hasta esta fase."""
        payload = payload or {}
        if origin is not None:
            # Sin validar contra el catálogo: es historia ya emitida y su código viaja congelado
            # en el XML del afectado. Si el local se archivó después, la nota igual debe salir.
            return origin.l10n_pe_ne_cod_establecimiento or "0000"
        Estab = self.env["l10n_pe_ne.establecimiento"]
        if payload.get("codEstablecimiento"):
            return Estab._l10n_pe_ne_check_codigo(payload["codEstablecimiento"])
        serie = (payload.get("serie") or "").strip().upper()
        if serie:
            local = self.env["l10n_pe_ne.serie"]._l10n_pe_ne_local_de_serie(
                self.env.company, serie)
            if local:
                return local.codigo
        return self.env["l10n_pe_ne.caja.sesion"]._l10n_pe_ne_local_abierto() or "0000"

    @api.model
    def l10n_pe_ne_contexto_emision(self, tipo_doc=None, serie=None, doc_afectado_id=None):
        """Con qué local y qué serie saldría AHORA MISMO un comprobante de ese tipo, sin emitir
        nada.

        Existe para que la pantalla PINTE la decisión del backend en vez de deducirla: la cadena
        (nota → payload → serie → caja → domicilio fiscal) es una sola y vive en el resolver. Una
        SPA que la reimplementara mostraría un local y emitiría otro — y desde que el POS y
        Emitir mandan el código explícito, lo que muestran es lo que se declara ante SUNAT.

        `doc_afectado_id` es lo que hace útil esto en una nota: su local es dato DERIVADO del
        comprobante que corrige y no una elección, así que la pantalla lo pinta bloqueado en vez
        de ofrecer un selector cuyo valor el emit va a ignorar."""
        tipo = str(tipo_doc or "01")
        origin = None
        if tipo in ("07", "08") and doc_afectado_id:
            # browse sin sudo: la record rule de compañía es la que impide preguntar por el
            # local de un comprobante de otro tenant.
            origin = self.browse(int(doc_afectado_id)).exists() or None
        cod = self._l10n_pe_ne_resolver_establecimiento(
            {"serie": serie} if serie else {}, origin)
        return {
            "tipoDoc": tipo,
            "codEstablecimiento": cod,
            "establecimiento": self._l10n_pe_ne_direccion_local(cod),
            # Mismo orden que el armado de vals: la serie tecleada manda, y si no hay, la que
            # ese local declaró (o el default de siempre si no declaró ninguna).
            "serie": (serie or "").strip().upper()
                     or self._l10n_pe_ne_default_serie(tipo, origin, cod),
            # La nota no elige local: la pantalla lo muestra en solo lectura.
            "heredado": origin is not None,
        }

    @api.model
    def _l10n_pe_ne_direccion_local(self, cod):
        """Dirección del local con ese código, para que la pantalla muestre «0002 · Av. Larco
        100» y no un número suelto que el dueño tiene que traducir de memoria.

        El domicilio fiscal no tiene fila (D3): su dirección es la del partner de la compañía,
        igual que la fila sintética que fabrica `l10n_pe_ne_list`. Un código archivado o ya
        inexistente devuelve '' en vez de romper: en un comprobante emitido el código es
        historia congelada y el local pudo darse de baja después.

        sudo + company_id explícito: es una lectura de catálogo propio para pintar una etiqueta,
        y el emisor puede no tener acceso al modelo de establecimientos."""
        if not cod or cod == "0000":
            return self.env.company.partner_id.street or ""
        return self.env["l10n_pe_ne.establecimiento"].sudo().with_context(
            active_test=False).search(
            [("codigo", "=", cod), ("company_id", "=", self.env.company.id)],
            limit=1).direccion or ""

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
        if not found and not num:
            # Público general SIN documento: reusa el partner del MISMO nombre en el tenant
            # ('CONSUMIDOR FINAL' si no vino nombre) en vez de crear uno desechable por
            # venta — hallazgo de auditoría: la SPA manda 'CLIENTE VARIOS' y cada venta
            # creaba un duplicado idéntico, ensuciando la lista de clientes.
            # (La emisión no reescribe el partner, así que reusarlo es seguro.)
            found = Partner.search([
                ("company_id", "=", self.env.company.id),
                ("vat", "=", False),
                ("name", "=", nombre or "CONSUMIDOR FINAL"),
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

