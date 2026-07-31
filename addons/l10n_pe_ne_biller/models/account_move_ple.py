# -*- coding: utf-8 -*-
"""account.move — Libros electrónicos PLE (14.1 ventas, 8.1 compras, 12.1 inventario).
Extraído de account_move_biller.py (refactor sin cambio de comportamiento)."""
import base64
import io
import logging
import re
import zipfile

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import float_round
from .account_move_biller import DEFAULT_UNIT_CODE, _percep_float

_logger = logging.getLogger(__name__)


class AccountMove(models.Model):
    _inherit = "account.move"

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
    def l10n_pe_ne_reporte_vinculadas(self, ejercicio=None):
        """Reporte de operaciones con partes vinculadas del ejercicio (año, default el actual) —
        sustento de la DJ Informativa de Precios de Transferencia / Reporte Local (V1). Lista los
        comprobantes emitidos a clientes marcados como parte vinculada, agrupados por cliente, con
        el tipo de vínculo, si es no domiciliada y el total (las NC restan). Aislado por compañía."""
        company = self.env.company
        year = int(ejercicio) if ejercicio else fields.Date.context_today(self).year
        d0 = fields.Date.to_date("%04d-01-01" % year)
        d1 = fields.Date.to_date("%04d-12-31" % year)
        moves = self.search(
            [
                ("company_id", "=", company.id),
                ("move_type", "in", ("out_invoice", "out_refund")),
                ("state", "=", "posted"),
                ("invoice_date", ">=", d0), ("invoice_date", "<=", d1),
                ("l10n_pe_biller_state", "not in", ("por_enviar", "rechazado", False)),
                ("partner_id.l10n_pe_ne_parte_vinculada", "=", True),
            ],
            order="partner_id, invoice_date, id",
        )
        tipos = dict(self.env["res.partner"]._fields["l10n_pe_ne_tipo_vinculo"].selection)
        por_cliente = {}
        total = 0.0
        for m in moves:
            p = m.partner_id
            g = por_cliente.setdefault(p.id, {
                "cliente": p.name or "", "numDoc": p.vat or "",
                "tipoVinculo": p.l10n_pe_ne_tipo_vinculo or "",
                "tipoVinculoNombre": tipos.get(p.l10n_pe_ne_tipo_vinculo, ""),
                "noDomiciliada": p.l10n_pe_ne_no_domiciliada,
                "pais": p.country_id.code or "", "comprobantes": 0, "total": 0.0,
            })
            signo = 1.0 if m.move_type == "out_invoice" else -1.0
            g["comprobantes"] += 1
            g["total"] = round(g["total"] + signo * (m.amount_total or 0.0), 2)
            total += signo * (m.amount_total or 0.0)
        items = sorted(por_cliente.values(), key=lambda x: -x["total"])
        return {
            "ejercicio": year,
            "items": items,
            "clientes": len(items),
            "comprobantes": len(moves),
            "total": round(total, 2),
            "moneda": company.currency_id.name or "PEN",
            # Cruce con los umbrales de obligación (V4) para que el reporte diga si hay que presentar.
            "umbrales": self.env["res.company"].l10n_pe_ne_vinculadas_umbrales(year),
        }

    @api.model
    def l10n_pe_ne_reporte_vinculadas_csv(self, ejercicio=None):
        """Descarga el reporte de operaciones con partes vinculadas como CSV — el sustento que el
        contador presenta con la Declaración Jurada (Reporte Local, precios de transferencia).

        Devuelve {filename, contentB64, count}, mismo contrato que los PLE. CSV separado por ';'
        (Excel es-PE) y en UTF-8 con BOM para que las tildes (vínculo, país) salgan bien. Lleva
        una cabecera con el cruce de umbrales (¿obligado al Reporte Local?) y la tabla por cliente."""
        rep = self.l10n_pe_ne_reporte_vinculadas(ejercicio)
        u = rep["umbrales"]
        mon = rep["moneda"]

        def celda(v):
            # Escapa comillas/;/salto de línea para un CSV robusto.
            s = "" if v is None else str(v)
            return '"%s"' % s.replace('"', '""') if any(c in s for c in ';"\n') else s

        def fila(*vals):
            return ";".join(celda(v) for v in vals)

        fmt = self._l10n_pe_fmt   # 2 decimales
        si_no = lambda b: _("Sí") if b else _("No")
        lineas = [
            fila(_("Reporte de operaciones con partes vinculadas")),
            fila(_("Ejercicio"), rep["ejercicio"]),
            fila(_("UIT"), fmt(u["uit"])),
            fila(_("Ingresos del ejercicio"), fmt(u["ingresos"]), _("Umbral Reporte Local (2300 UIT)"), fmt(u["umbralReporteLocal"])),
            fila(_("Operaciones con vinculadas"), fmt(u["operacionesVinculadas"]), _("Umbral operaciones (400 UIT)"), fmt(u["umbralOperaciones"])),
            fila(_("Obligado a presentar Reporte Local"), si_no(u["obligadoReporteLocal"])),
            "",
            fila(_("Documento"), _("Cliente"), _("Tipo de vínculo"), _("Cód. vínculo"),
                 _("No domiciliada"), _("País"), _("Comprobantes"), _("Total (%s)") % mon),
        ]
        for it in rep["items"]:
            lineas.append(fila(
                it["numDoc"], it["cliente"], it["tipoVinculoNombre"], it["tipoVinculo"],
                si_no(it["noDomiciliada"]), it["pais"], it["comprobantes"], fmt(it["total"])))
        lineas.append(fila("", _("TOTAL"), "", "", "", "", rep["comprobantes"], fmt(rep["total"])))

        content = "﻿" + "\r\n".join(lineas) + "\r\n"
        ruc = self.env.company.vat or ""
        return {
            "filename": "vinculadas-%s-%s.csv" % (ruc, rep["ejercicio"]),
            "contentB64": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "count": rep["clientes"],
        }

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

    # ------------------------------------------------------------- PLE 8.1 (compras)
    #
    # ⚠ LA ESTRUCTURA DE ESTE FORMATO ESTÁ PENDIENTE DE VALIDACIÓN CONTABLE.
    #
    # Los anexos de SUNAT con el layout del 8.1 (RS 286-2009 anexo 2, y sus modificatorias)
    # se publican como PDF ESCANEADO: no hay de dónde extraerlo de forma fiable. Lo de abajo
    # espeja las convenciones del 14.1 de este mismo addon —que sí está en producción— y el
    # orden de campos que documenta SUNAT para el registro de compras, pero NADIE lo verificó
    # contra la norma vigente.
    #
    # Cada campo va NUMERADO a propósito, para que un contador pueda auditarlo uno por uno y
    # decir "el 14 no es ese" sin leer Python.
    #
    # El modo de fallo es benigno: el validador del PLE de SUNAT revisa la estructura, así que
    # un layout corrido se RECHAZA al subirlo — no entra mal en silencio. Aun así, que nadie
    # lo presente sin que su contador lo confirme primero.

    def _l10n_pe_ne_ple_compra_breakdown(self):
        """Desglose de una compra por afectación. Espeja _l10n_pe_ne_ple_breakdown (ventas),
        pero el registro de compras separa la base gravada por su DESTINO (a operaciones
        gravadas / mixtas / no gravadas), no por el tipo de tributo.

        Hoy todo va al destino "gravadas" (campo 14): es el caso de un negocio que vende
        gravado, que es el de esta app. Prorratear a operaciones no gravadas exige saber a qué
        se destina cada compra, dato que no se pide en ningún lado — sería inventarlo."""
        self.ensure_one()
        gravado = exonerado = inafecto = igv = 0.0
        for ln in self.invoice_line_ids:
            codes = ln.tax_ids.mapped("l10n_pe_edi_tax_code")
            base = ln.price_subtotal or 0.0
            if "1000" in codes:
                gravado += base
            elif "9997" in codes:
                exonerado += base
            else:
                inafecto += base
        igv = (self.amount_total or 0.0) - (self.amount_untaxed or 0.0)
        rnd = self.currency_id.rounding or 0.01
        return {
            "gravado": float_round(gravado, precision_rounding=rnd),
            "exonerado": float_round(exonerado, precision_rounding=rnd),
            "inafecto": float_round(inafecto, precision_rounding=rnd),
            "igv": float_round(igv, precision_rounding=rnd),
            "total": self.amount_total or 0.0,
        }

    def _l10n_pe_ne_ple_compra_linea(self, periodo8, cuo):
        """Una línea del PLE 8.1. Campos numerados: son POSICIONALES y separados por '|', así
        que un campo de más o de menos corre todos los siguientes."""
        self.ensure_one()
        num = self._l10n_pe_ne_ple_num
        b = self._l10n_pe_ne_ple_compra_breakdown()
        doc = self.l10n_latam_document_number or self.ref or ""
        serie, _sep, corr = doc.partition("-")
        tipo = (
            self.l10n_latam_document_type_id.code
            if self.l10n_latam_document_type_id
            else "01"
        )
        fecha = self.invoice_date.strftime("%d/%m/%Y") if self.invoice_date else ""
        moneda = self.currency_id.name or "PEN"
        # Tipo de documento del proveedor (tabla 2): 6 = RUC. Sin RUC no hay crédito fiscal,
        # así que el caso normal de este registro es 6.
        ndoc = (self.partner_id.vat or "").strip()
        tdoc = "6" if len(ndoc) == 11 else ("1" if ndoc else "0")
        campos = [
            periodo8,  # 1  Periodo (AAAAMM00)
            str(self.id),  # 2  CUO (único por operación)
            "",  # 3  Nro correlativo del asiento (solo estados 8/9)
            fecha,  # 4  Fecha de emisión del comprobante
            "",  # 5  Fecha de vencimiento o pago
            tipo,  # 6  Tipo de comprobante (tabla 10)
            serie,  # 7  Serie del comprobante
            "",  # 8  Año de emisión de la DUA/DSI (solo importaciones)
            corr,  # 9  Número del comprobante
            "",  # 10 Número final (rango) / DUA
            tdoc,  # 11 Tipo de documento del proveedor (tabla 2)
            ndoc,  # 12 Número de documento del proveedor
            (self.partner_id.name or "").upper(),  # 13 Razón social del proveedor
            num(b["gravado"]),  # 14 Base imponible destinada a operaciones GRAVADAS
            num(b["igv"]),  # 15 IGV/IPM de 14
            "0.00",  # 16 Base destinada a operaciones gravadas Y no gravadas
            "0.00",  # 17 IGV/IPM de 16
            "0.00",  # 18 Base destinada a operaciones NO gravadas
            "0.00",  # 19 IGV/IPM de 18
            num(b["exonerado"] + b["inafecto"]),  # 20 Valor de adquisiciones no gravadas
            "0.00",  # 21 ISC
            "0.00",  # 22 ICBPER
            "0.00",  # 23 Otros tributos y cargos
            num(b["total"]),  # 24 Importe total
            "",  # 25 Código de la moneda (tabla 4) — ver nota abajo
            "",  # 26 Tipo de cambio
            "",  # 27 Fecha de emisión del comprobante modificado
            "",  # 28 Tipo del comprobante modificado
            "",  # 29 Serie del comprobante modificado
            "",  # 30 Número del comprobante modificado
            "",  # 31 Fecha de la constancia de detracción
            "",  # 32 Número de la constancia de detracción
            "",  # 33 Marca del comprobante sujeto a retención
            "",  # 34 Clasificación de bienes y servicios
            "",  # 35 Identificación del contrato o proyecto
            "",  # 36 Error tipo 1
            "",  # 37 Error tipo 9
            "",  # 38 Errores tipo 4
            "",  # 39 Indicador de comprobante de pago cancelado con medio de pago
            "1",  # 40 Estado (1 = registro que corresponde al periodo)
        ]
        # Moneda y tipo de cambio: se llenan acá y no en la lista para no repetir el cálculo.
        campos[24] = moneda
        campos[25] = (
            "1.000"
            if moneda == "PEN"
            else "%.3f"
            % (1.0 / (self.currency_id.with_context(date=self.invoice_date).rate or 1.0))
        )
        return "|".join(campos) + "|"

    @api.model
    def _l10n_pe_ne_compras_periodo(self, periodo):
        """Compras posteadas del periodo YYYYMM, ordenadas. Aislado por compañía.
        Espeja _l10n_pe_ne_ventas_periodo, con move_type de proveedor."""
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
                ("move_type", "in", ("in_invoice", "in_refund")),
                ("state", "=", "posted"),
                ("invoice_date", ">=", d0),
                ("invoice_date", "<=", d1),
            ],
            order="invoice_date, id",
        )

    @api.model
    def l10n_pe_ne_ple_compras(self, periodo):
        """PLE 8.1 (Registro de Compras) del periodo YYYYMM. Devuelve
        {filename, contentB64, count, periodo, total}. Espeja l10n_pe_ne_ple_ventas.

        ⚠ Estructura pendiente de validación contable (ver la nota del bloque)."""
        import base64

        periodo = (periodo or "").strip()
        moves = self._l10n_pe_ne_compras_periodo(periodo)
        periodo8 = periodo + "00"
        lines = [
            m._l10n_pe_ne_ple_compra_linea(periodo8, i) for i, m in enumerate(moves, 1)
        ]
        content = ("\r\n".join(lines) + "\r\n") if lines else ""
        ruc = (self.env.company.vat or "").strip()
        ind_cont = "1" if lines else "0"
        # Mismo patrón que el 14.1, cambiando el código del libro: 140100 → 080100.
        filename = "LE%s%s00080100%s11.txt" % (ruc, periodo, "1" + ind_cont)
        return {
            "filename": filename,
            "contentB64": base64.b64encode(content.encode("latin-1", "replace")).decode(
                "ascii"
            ),
            "count": len(lines),
            "periodo": periodo,
            "total": sum(moves.mapped("amount_total")),
        }

    # ------------------------------------- PLE 12.1 (inventario en unidades físicas)
    #
    # ⚠ ESTRUCTURA PENDIENTE DE VALIDACIÓN CONTABLE, igual que el 8.1: los anexos de SUNAT
    # con el layout se publican como PDF escaneado. Cada campo va numerado para auditarlo.
    #
    # Se hace el de UNIDADES FÍSICAS y NO el valorizado, y no por comodidad: el valorizado
    # exige el costo de cada movimiento, y con la valorización en `periodic` —el default de
    # Odoo, el que esta app deja puesto— `stock.move.value` y `price_unit` salen en CERO.
    # Verificado sobre los movimientos reales de la BD. Inventar ese costo (p. ej. usando el
    # precio de lista) sería declararle a SUNAT un número que no salió de ningún lado.
    # Para el valorizado hay que pasar la compañía a valorización perpetua, que cambia los
    # asientos contables: es una decisión del contador, no un efecto colateral de un reporte.

    @api.model
    def _l10n_pe_ne_kardex_periodo(self, periodo):
        """Movimientos de inventario del periodo YYYYMM, ordenados. Solo los que cruzan la
        frontera del almacén (entradas y salidas reales); los internos no son del kardex."""
        import calendar

        periodo = (periodo or "").strip()
        if len(periodo) != 6 or not periodo.isdigit():
            raise UserError(_("Periodo inválido. Usa YYYYMM (p.ej. 202606)."))
        year, month = int(periodo[:4]), int(periodo[4:6])
        if not (1 <= month <= 12):
            raise UserError(_("Mes inválido en el periodo."))
        last = calendar.monthrange(year, month)[1]
        return self.env["stock.move.line"].search(
            [
                ("state", "=", "done"),
                ("company_id", "=", self.env.company.id),
                ("date", ">=", "%04d-%02d-01 00:00:00" % (year, month)),
                ("date", "<=", "%04d-%02d-%02d 23:59:59" % (year, month, last)),
                ("product_id.is_storable", "=", True),
                # Solo lo que entra o sale del almacén: un traslado interno no es del kardex.
                "|",
                ("location_id.usage", "in", ("supplier", "customer", "inventory")),
                ("location_dest_id.usage", "in", ("supplier", "customer", "inventory")),
            ],
            order="date, id",
        )

    @api.model
    def _l10n_pe_ne_kardex_linea(self, ml, periodo8, cuo, saldo):
        """Una línea del PLE 12.1. Campos POSICIONALES separados por '|'."""
        num = self._l10n_pe_ne_ple_num
        entra = ml.location_dest_id.usage == "internal"
        cant = abs(ml.quantity or 0)
        doc = ml.move_id.l10n_pe_ne_move_id
        if doc and doc.move_type in ("out_invoice", "out_refund"):
            # VENTA: la serie/correlativo salen del mismo helper que usa la emisión y la
            # baja. Partir l10n_latam_document_number por "-" no sirve: en una venta ese
            # campo trae solo el número, y la serie terminaba llevándose el correlativo.
            serie = doc.l10n_pe_ne_serie_emit or doc._l10n_pe_serie_correlativo()[0]
            corr = doc.l10n_pe_ne_corr_emit or doc._l10n_pe_serie_correlativo()[1]
            tipo = doc.l10n_pe_ne_tipo_doc or doc._l10n_pe_document_type()
            fecha = doc.invoice_date.strftime("%d/%m/%Y") if doc.invoice_date else ""
        elif doc:
            # COMPRA: acá el documento es del proveedor y sí viene como "F001-00095001".
            serie, _sep, corr = (doc.l10n_latam_document_number or doc.ref or "").partition("-")
            tipo = (
                doc.l10n_latam_document_type_id.code
                if doc.l10n_latam_document_type_id
                else "01"
            )
            fecha = doc.invoice_date.strftime("%d/%m/%Y") if doc.invoice_date else ""
        else:
            # Ajuste de inventario: no nace de un comprobante. Tipo 00 = "otros" (tabla 10).
            serie, corr, tipo = "", "", "00"
            fecha = ml.date.strftime("%d/%m/%Y") if ml.date else ""
        p = ml.product_id
        campos = [
            periodo8,  # 1  Periodo (AAAAMM00)
            str(ml.id),  # 2  CUO (único por movimiento)
            "",  # 3  Nro correlativo del asiento
            fecha,  # 4  Fecha de emisión del documento
            tipo,  # 5  Tipo de documento (tabla 10)
            serie,  # 6  Serie del documento
            corr,  # 7  Número del documento
            "01" if entra else "02",  # 8  Tipo de operación (tabla 12): 01 entrada, 02 salida
            p.default_code or "",  # 9  Código de la existencia
            "01",  # 10 Tipo de existencia (tabla 5): 01 mercadería
            (p.name or "")[:100],  # 11 Descripción de la existencia
            p.l10n_pe_ne_unit_code or "NIU",  # 12 Unidad de medida (tabla 6)
            "",  # 13 Método de valuación (solo en el valorizado)
            num(cant) if entra else "0.00",  # 14 Entradas — cantidad
            "0.00" if entra else num(cant),  # 15 Salidas — cantidad
            num(saldo),  # 16 Saldo final — cantidad
            "1",  # 17 Estado (1 = del periodo)
        ]
        return "|".join(campos) + "|"

    @api.model
    def l10n_pe_ne_ple_inventario(self, periodo):
        """PLE 12.1 (Registro de Inventario Permanente en Unidades Físicas) del periodo.

        ⚠ Estructura pendiente de validación contable (ver la nota del bloque)."""
        import base64
        from collections import defaultdict

        periodo = (periodo or "").strip()
        lineas_ml = self._l10n_pe_ne_kardex_periodo(periodo)
        periodo8 = periodo + "00"
        # El saldo se arrastra POR PRODUCTO en el orden de los movimientos: es lo que hace
        # legible un kardex — cada renglón muestra con cuánto quedó esa existencia.
        saldos = defaultdict(float)
        lines = []
        for i, ml in enumerate(lineas_ml, 1):
            entra = ml.location_dest_id.usage == "internal"
            cant = abs(ml.quantity or 0)
            saldos[ml.product_id.id] += cant if entra else -cant
            lines.append(
                self._l10n_pe_ne_kardex_linea(ml, periodo8, i, saldos[ml.product_id.id])
            )
        content = ("\r\n".join(lines) + "\r\n") if lines else ""
        ruc = (self.env.company.vat or "").strip()
        ind_cont = "1" if lines else "0"
        # Mismo patrón que el 14.1/8.1, con el código del libro 120100.
        filename = "LE%s%s00120100%s11.txt" % (ruc, periodo, "1" + ind_cont)
        return {
            "filename": filename,
            "contentB64": base64.b64encode(content.encode("latin-1", "replace")).decode(
                "ascii"
            ),
            "count": len(lines),
            "periodo": periodo,
            "total": 0.0,
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
                a = prod.setdefault(
                    key, {"cantidad": 0.0, "total": 0.0, "base": 0.0, "costo": 0.0}
                )
                a["cantidad"] += ln.quantity or 0.0
                a["total"] += ln.price_total or 0.0
                # Rentabilidad: valor de venta SIN IGV (price_subtotal) vs costo del
                # producto (standard_price × cantidad). El costo es 0 si el producto no
                # lo tiene registrado → la utilidad de esa línea queda sobrestimada.
                a["base"] += ln.price_subtotal or 0.0
                a["costo"] += (ln.quantity or 0.0) * (
                    ln.product_id.standard_price or 0.0
                )
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
                    "venta": round(v["base"], 2),
                    "costo": round(v["costo"], 2),
                    "utilidad": round(v["base"] - v["costo"], 2),
                    # Margen % sobre el valor de venta. None si el producto no tiene costo
                    # registrado (no se puede calcular una utilidad real).
                    "margen": round((v["base"] - v["costo"]) / v["base"] * 100, 1)
                    if v["base"] and v["costo"]
                    else None,
                }
                for k, v in prod.items()
            ),
            key=lambda x: -x["total"],
        )[:50]
        # Resumen de rentabilidad del periodo. Se calcula SOLO sobre productos con costo
        # registrado (los de costo 0 inflarían la utilidad como si todo fuera ganancia).
        # `conCosto`/`totalProductos` le dice al front qué tan completa es la estimación.
        rent_venta = sum(v["base"] for v in prod.values() if v["costo"])
        rent_costo = sum(v["costo"] for v in prod.values() if v["costo"])
        rentabilidad = {
            "venta": round(rent_venta, 2),
            "costo": round(rent_costo, 2),
            "utilidad": round(rent_venta - rent_costo, 2),
            "margen": round((rent_venta - rent_costo) / rent_venta * 100, 1)
            if rent_venta and rent_costo
            else None,
            "conCosto": sum(1 for v in prod.values() if v["costo"]),
            "totalProductos": len(prod),
        }
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
            "rentabilidad": rentabilidad,
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

