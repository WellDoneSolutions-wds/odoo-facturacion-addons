# -*- coding: utf-8 -*-
"""account.move — Validación pre-emisión (L1: reglas SUNAT).
Extraído de account_move_biller.py (refactor sin cambio de comportamiento)."""

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import float_round
from .account_move_biller import DETRACCION_TASAS, DESC_GLOBAL_NO_AFECTA_COD


class AccountMove(models.Model):
    _inherit = "account.move"

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

