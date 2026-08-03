# -*- coding: utf-8 -*-
"""account.move — Importación masiva de productos.
Extraído de account_move_biller.py (refactor sin cambio de comportamiento)."""
import base64
import io
import re

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from .account_move_biller import DEFAULT_UNIT_CODE, UNIDAD_IMPORT, AFECT_IMPORT, TIPO_IMPORT, _UNIDAD_CODES


class AccountMove(models.Model):
    _inherit = "account.move"

    # ------------------------------------------------------- importación productos
    @api.model
    def l10n_pe_ne_plantilla_productos(self):
        """Plantilla xlsx para importar/actualizar el catálogo (hoja 'Productos' con las
        cabeceras + ejemplos + listas de unidad/afectación, y una hoja 'Instrucciones').
        Devuelve {filename, contentB64}. Mismo estilo visual que la plantilla de la masiva."""
        import io
        import base64
        import xlsxwriter

        headers = ["CÓDIGO", "CÓDIGO DE BARRAS", "NOMBRE", "TIPO", "UNIDAD", "PRECIO VENTA", "COSTO", "AFECTACIÓN", "BOLSA", "DETRACCIÓN"]
        ejemplos = [
            ["PROD0001", "7751234000018", "CEMENTO SOL 42.5 KG", "BIEN", "UNIDAD", 33.90, 28.00, "GRAVADO", "NO", ""],
            ["PROD0002", "7751234000025", "FIERRO CORRUGADO 1/2 PULG", "BIEN", "KILOGRAMO", 4.50, 3.20, "GRAVADO", "NO", ""],
            ["SERV0001", "", "SERVICIO DE CONSULTORÍA", "SERVICIO", "", 150.00, "", "GRAVADO", "NO", ""],
            ["PROD0004", "", "BOLSA PLÁSTICA", "BIEN", "UNIDAD", 0.50, 0.10, "GRAVADO", "SI", ""],
        ]
        buf = io.BytesIO()
        wb = xlsxwriter.Workbook(buf, {"in_memory": True})
        ws = wb.add_worksheet("Productos")
        head = wb.add_format({"bold": True, "bg_color": "#2563eb", "font_color": "white", "border": 1})
        # El código de barras se escribe como TEXTO para no perder ceros a la izquierda
        # ni que Excel lo pase a notación científica (ej. 7.75E+12).
        txtfmt = wb.add_format({"num_format": "@"})
        for c, h in enumerate(headers):
            ws.write(0, c, h, head)
            # DETRACCIÓN también va como TEXTO: sus códigos (027, 019, 022...) empiezan con
            # cero y Excel se lo comería si la celda quedara en formato numérico.
            ws.set_column(c, c, max(16, len(h) + 4), txtfmt if c in (1, 9) else None)
        for r, row in enumerate(ejemplos, 1):
            ws.write_row(r, 0, row)
        # Comentarios de ayuda al pasar el mouse por la cabecera (el triangulito rojo).
        note = {"x_scale": 2.2, "y_scale": 1.8, "author": "Ekipu"}
        ws.write_comment(0, 1, (
            "Opcional. El código de barras (EAN) que trae el producto, para escanearlo "
            "en el POS. Déjalo vacío si el producto no tiene."), note)
        ws.write_comment(0, 3, (
            "BIEN o SERVICIO. Un SERVICIO no lleva stock en Odoo y va con unidad ZZ a SUNAT.\n"
            "Si lo dejas vacío se deduce de la UNIDAD (SERVICIO/ZZ → servicio; el resto → bien)."), note)
        ws.write_comment(0, 5, "Precio final CON IGV incluido (lo que paga el cliente).", note)
        ws.write_comment(0, 6, "Opcional. Precio de compra referencial. NO afecta la facturación.", note)
        ws.write_comment(0, 7, (
            "Tipo de afectación de IGV. Elígelo del desplegable.\n"
            "• GRAVADO = con IGV 18% (lo normal)\n"
            "• EXONERADO / INAFECTO = sin IGV\n"
            "• EXPORTACION / GRATUITO = casos especiales\n"
            "Si lo dejas vacío se asume GRAVADO."), note)
        ws.write_comment(0, 8, (
            "SI / NO. Márcalo SI solo si el producto es una BOLSA PLÁSTICA: "
            "cobra el ICBPER (monto fijo por unidad) al venderlo. Vacío = NO."), note)
        ws.write_comment(0, 9, (
            "Opcional. Código cat. 54 de SUNAT si el producto está sujeto a detracción "
            "(ej. 027 transporte de carga). Vacío = no sujeto."), note)
        # Desplegable (select) BIEN/SERVICIO para TIPO.
        ws.data_validation(1, 3, 1000, 3, {
            "validate": "list", "source": ["BIEN", "SERVICIO"],
            "input_title": "Bien o servicio",
            "input_message": (
                "SERVICIO no lleva stock (va con unidad ZZ a SUNAT); BIEN sí puede llevar.\n"
                "Vacío = se deduce de la UNIDAD.")})
        # Desplegable (select) para UNIDAD, con ayuda al hacer clic en la celda. (El bien/servicio va
        # en la columna TIPO; acá es solo la unidad de medida.)
        ws.data_validation(1, 4, 1000, 4, {
            "validate": "list", "source": [
                "UNIDAD", "KILOGRAMO", "GRAMO", "LITRO", "GALON", "CAJA",
                "METRO", "METRO CUADRADO", "METRO CUBICO", "MILLAR", "DOCENA", "HORA", "DIA"],
            "input_title": "Unidad de medida",
            "input_message": "Elige de la lista o escribe el código SUNAT (NIU, KGM…). Vacío = UNIDAD."})
        # Desplegable (select) para AFECTACIÓN, con ayuda + alerta suave si no es de la lista.
        ws.data_validation(1, 7, 1000, 7, {
            "validate": "list", "source": [
                "GRAVADO", "EXONERADO", "INAFECTO", "EXPORTACION", "GRATUITO"],
            "input_title": "Afectación de IGV",
            "input_message": (
                "GRAVADO = con IGV 18% (lo normal).\n"
                "EXONERADO / INAFECTO = sin IGV.\n"
                "Vacío = GRAVADO."),
            "error_type": "information",
            "error_title": "Valor sugerido",
            "error_message": "Usa: GRAVADO, EXONERADO, INAFECTO, EXPORTACION o GRATUITO."})
        # Desplegable (select) SI/NO para BOLSA (ICBPER).
        ws.data_validation(1, 8, 1000, 8, {
            "validate": "list", "source": ["SI", "NO"],
            "input_title": "Bolsa plástica (ICBPER)",
            "input_message": (
                "SI solo si es una bolsa plástica: cobra ICBPER por unidad.\n"
                "Para todo lo demás: NO (o déjalo vacío).")})
        ws.freeze_panes(1, 0)
        wi = wb.add_worksheet("Instrucciones")
        wi.set_column(0, 0, 110)
        for r, line in enumerate([
            "Ekipu — Plantilla de importación de productos",
            "",
            "1. Una fila = un producto. 'CÓDIGO' es la clave: si ya existe, se ACTUALIZA; si no, se CREA.",
            "   Al ACTUALIZAR, una celda VACÍA MANTIENE el valor actual del producto (no lo borra ni lo resetea).",
            "2. 'CÓDIGO DE BARRAS' es opcional: el EAN del producto para escanearlo en el POS. No puede repetirse entre productos.",
            "3. 'NOMBRE' es obligatorio. 'PRECIO VENTA' es el precio final CON IGV incluido.",
            "4. 'TIPO' (elígelo del desplegable): BIEN o SERVICIO. Un servicio no lleva stock y va con unidad ZZ a SUNAT.",
            "     Si lo dejas vacío se deduce de la UNIDAD (SERVICIO/ZZ → servicio; el resto → bien).",
            "5. 'UNIDAD': el nombre (UNIDAD, KILOGRAMO, HORA…) o el código SUNAT (NIU, KGM…). Vacío = UNIDAD (NIU), o ZZ si el TIPO es SERVICIO.",
            "6. 'AFECTACIÓN' (elígela del desplegable de la celda): define el IGV del producto.",
            "     • GRAVADO = lleva IGV 18% (la mayoría de productos).  • EXONERADO / INAFECTO = sin IGV.",
            "     • EXPORTACION / GRATUITO = casos especiales.  Si la dejas vacía se asume GRAVADO.",
            "7. 'COSTO' es opcional (precio de compra, referencial). No afecta la facturación.",
            "8. 'BOLSA' = SI solo para bolsas plásticas (cobran ICBPER por unidad al venderlas). Para el resto: NO o vacío.",
            "9. 'DETRACCIÓN' es opcional: código cat. 54 de SUNAT (3 dígitos, ej. 027 transporte de carga) si el producto está sujeto a detracción. Vacío = no sujeto.",
            "10. Sube el archivo, revisa el resumen (nuevos / actualizados / errores) y recién ahí confirma.",
        ]):
            wi.write(r, 0, line)
        wb.close()
        return {"filename": "plantilla-productos-ekipu.xlsx",
                "contentB64": base64.b64encode(buf.getvalue()).decode("ascii")}

    @api.model
    def l10n_pe_ne_revisar_tipos(self, payload=None):
        """Propone reclasificar los productos que quedaron como SERVICIO por el default viejo.

        Hasta hace poco todo producto nacía con type='service' —estuviera bien o no—, así que
        un catálogo existente tiene tornillos declarados como servicios. Y un servicio no
        lleva stock en Odoo: mientras no se corrijan, esos productos no mueven inventario.

        PROPONE, no decide. La deducción usa la misma regla que la creación
        (_l10n_pe_ne_tipo_producto: ZZ → servicio, el resto → bien), pero acá puede
        equivocarse: el formulario trae NIU por defecto, así que una consultora que no lo
        cambió tiene servicios con NIU y saldrían propuestos como bienes. Por eso se devuelve
        la lista para que la revise un humano y se aplica solo lo que confirme —
        `l10n_pe_ne_aplicar_tipos` recibe los ids elegidos, no un "aplicar todo".

        No propone nada sobre `llevaStock`: llevar inventario es una decisión del negocio y
        no hay señal ninguna que la delate. Se activa producto por producto.
        """
        Product = self.env["product.product"]
        # Solo los 'service': un 'consu' ya fue clasificado (por el usuario o por la regla).
        sospechosos = Product.search(
            [("type", "=", "service"), ("company_id", "in", (False, self.env.company.id))],
            order="name",
        )
        propuestas = []
        for p in sospechosos:
            uni = p.l10n_pe_ne_unit_code or ""
            propuesto = self._l10n_pe_ne_tipo_producto(None, uni)
            if propuesto != "service":
                propuestas.append({
                    "id": p.id,
                    "descripcion": p.name or "",
                    "codigo": p.default_code or "",
                    # Sin unidad no significa "servicio": a SUNAT se le declara NIU por
                    # defecto (DEFAULT_UNIT_CODE), o sea un bien. Se muestra para que el
                    # usuario juzgue con el mismo dato que usó la regla.
                    "unidad": uni,
                    "tipoPropuesto": "bien",
                })
        return {
            "propuestas": propuestas,
            "total": len(propuestas),
            "revisados": len(sospechosos),
        }

    @api.model
    def l10n_pe_ne_aplicar_tipos(self, payload):
        """Aplica la reclasificación SOLO a los ids que el usuario confirmó.
        payload = {ids: [...], tipo: "bien"|"servicio"}."""
        payload = payload or {}
        ids = [int(i) for i in (payload.get("ids") or [])]
        if not ids:
            return {"actualizados": 0}
        tipo = self._l10n_pe_ne_tipo_producto(payload.get("tipo") or "bien")
        prods = self.env["product.product"].browse(ids).exists()
        prods.write({"type": tipo})
        return {"actualizados": len(prods)}

    @api.model
    def l10n_pe_ne_importar_productos(self, payload):
        """Importa/actualiza productos desde el xlsx de la plantilla. payload = {contentB64, commit}.
        UPSERT por CÓDIGO. commit=False → solo valida y devuelve el reporte (dry-run, no escribe);
        commit=True → aplica y devuelve creados/actualizados/errores. Aislado por compañía."""
        import io
        import base64
        import unicodedata

        payload = payload or {}
        commit = bool(payload.get("commit"))
        try:
            data = base64.b64decode(payload.get("contentB64") or "")
        except Exception:
            raise UserError(_("Archivo inválido."))

        import openpyxl
        try:
            wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
        except Exception:
            raise UserError(_("No se pudo leer el archivo. Sube un .xlsx válido (no un .xls antiguo)."))
        ws = wb["Productos"] if "Productos" in wb.sheetnames else wb[wb.sheetnames[0]]
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            raise UserError(_("El archivo está vacío."))

        def norm(h):
            s = unicodedata.normalize("NFKD", str(h or "")).encode("ascii", "ignore").decode("ascii")
            return " ".join(s.lower().split())

        header = [norm(h) for h in rows[0]]
        idx = {h: i for i, h in enumerate(header) if h}
        faltan = [h for h in ("codigo", "nombre") if h not in idx]
        if faltan:
            raise UserError(_("Faltan columnas obligatorias: %s. Usa la plantilla.") % ", ".join(faltan))

        def cell(row, name):
            i = idx.get(name)
            return row[i] if i is not None and i < len(row) else None

        def txt(v):
            if v is None:
                return ""
            if isinstance(v, float) and v.is_integer():
                return str(int(v))
            return str(v).strip()

        def num(v):
            if v is None or (isinstance(v, str) and not v.strip()):
                return None
            if isinstance(v, (int, float)):
                return float(v)
            try:
                return float(str(v).strip().replace(" ", "").replace(",", "."))
            except ValueError:
                return "ERROR"

        Product = self.env["product.product"]
        icbper_tax = self._l10n_pe_ne_ensure_icbper_tax()

        def afe_bolsa_actuales(product):
            """(código de afectación, ¿tiene ICBPER?) de un producto ya existente. Afectación y
            bolsa viven en el MISMO campo (taxes_id); esto permite pisar solo la que el usuario trajo
            en el Excel sin borrar la otra (ver 'vacío = mantener')."""
            sale = product.taxes_id.filtered(lambda t: t.type_tax_use == "sale")
            tiene_icbper = bool(sale.filtered(lambda t: t.l10n_pe_edi_tax_code == "7152"))
            afe = sale.filtered(lambda t: t.l10n_pe_edi_tax_code and t.l10n_pe_edi_tax_code != "7152")[:1]
            return (afe.l10n_pe_edi_tax_code or "1000"), tiene_icbper

        def tax_ids_de(afe_code, bolsa):
            tax = self._l10n_pe_ne_tax_by_code(afe_code)
            ids = list(tax.ids) if tax else []
            if bolsa:  # bolsa plástica → suma la tax ICBPER (monto fijo por unidad)
                ids += icbper_tax.ids
            return ids

        creados = actualizados = 0
        errores = []
        avisos = []
        vistos = {}  # CÓDIGO → fila donde apareció por primera vez (duplicados dentro del archivo)
        for n, row in enumerate(rows[1:], start=2):
            if row is None or all(c is None or str(c).strip() == "" for c in row):
                continue
            cod = txt(cell(row, "codigo"))
            nombre = txt(cell(row, "nombre"))
            if not cod:
                errores.append({"fila": n, "msg": "Falta el CÓDIGO"})
                continue
            if not nombre:
                errores.append({"fila": n, "msg": "Falta el NOMBRE"})
                continue
            precio = num(cell(row, "precio venta"))
            costo = num(cell(row, "costo"))
            if precio == "ERROR" or costo == "ERROR":
                errores.append({"fila": n, "msg": "PRECIO o COSTO no es un número válido"})
                continue
            # UNIDAD (código SUNAT cat. 03). Vacía = NO se toca al actualizar; al CREAR, el default
            # se alinea al tipo: servicio → ZZ, si no → NIU (así Odoo y SUNAT no se contradicen).
            tipo = TIPO_IMPORT.get(norm(cell(row, "tipo")), "")  # "" | "bien" | "servicio"
            uni_raw = norm(cell(row, "unidad"))
            uni_provisto = bool(uni_raw)
            if not uni_raw:
                unidad = "ZZ" if tipo == "servicio" else "NIU"
            elif uni_raw in UNIDAD_IMPORT:
                unidad = UNIDAD_IMPORT[uni_raw]
            elif uni_raw.upper() in _UNIDAD_CODES:
                unidad = uni_raw.upper()
            else:
                unidad = "NIU"
                avisos.append({"fila": n, "msg": "Unidad '%s' no reconocida, se usó UNIDAD (NIU)" % txt(cell(row, "unidad"))})
            afe_raw = norm(cell(row, "afectacion"))
            afe_provisto = bool(afe_raw)
            if afe_provisto and afe_raw not in AFECT_IMPORT:
                avisos.append({"fila": n, "msg": "Afectación '%s' no reconocida, se usó GRAVADO" % txt(cell(row, "afectacion"))})
            afe_code = AFECT_IMPORT.get(afe_raw, "1000")
            bolsa_raw = norm(cell(row, "bolsa"))
            bolsa_provisto = bool(bolsa_raw)
            bolsa = bolsa_raw in ("si", "s")  # ICBPER: SI/NO
            detra_raw = txt(cell(row, "detraccion"))
            detra_provisto = bool(detra_raw)
            if detra_provisto and not re.fullmatch(r"[0-9]{3}", detra_raw):
                errores.append({"fila": n, "msg": "DETRACCIÓN debe ser el código de 3 dígitos del catálogo 54 (ej. 027) o vacío"})
                continue
            barcode = txt(cell(row, "codigo de barras"))

            existing = Product.search([("default_code", "=", cod)], limit=1)
            # El código de barras no puede pertenecer a OTRO producto (Odoo lo exige único).
            if barcode:
                dup = Product.search([("barcode", "=", barcode)], limit=1)
                if dup and dup.id != existing.id:
                    errores.append({"fila": n, "msg": "El código de barras '%s' ya pertenece a otro producto" % barcode})
                    continue
            # CÓDIGO repetido dentro del MISMO archivo: la última fila manda (se aplica igual), pero
            # se avisa y NO se cuenta dos veces (el preview marcaría 2 'nuevos' para un solo producto).
            repetido = cod in vistos
            if repetido:
                avisos.append({"fila": n, "msg": "CÓDIGO '%s' repetido (ya venía en la fila %d); vale la última fila" % (cod, vistos[cod])})
            else:
                vistos[cod] = n
            if not commit:
                if not repetido:
                    actualizados += 1 if existing else 0
                    creados += 0 if existing else 1
                continue
            if existing:
                # Actualización: "vacío = mantener". Solo se pisa lo que el usuario TRAJO con valor;
                # afectación y bolsa comparten campo, así que la que no venga se lee del producto.
                vals = {"name": nombre}
                if uni_provisto:
                    vals["l10n_pe_ne_unit_code"] = unidad
                if tipo:
                    vals["type"] = self._l10n_pe_ne_tipo_producto(tipo)
                if detra_provisto:
                    vals["l10n_pe_ne_detraccion_cod"] = detra_raw
                if precio is not None:
                    vals["list_price"] = precio
                if costo is not None:
                    vals["standard_price"] = costo
                if barcode:
                    vals["barcode"] = barcode
                if afe_provisto or bolsa_provisto:
                    cur_afe, cur_bolsa = afe_bolsa_actuales(existing)
                    vals["taxes_id"] = [(6, 0, tax_ids_de(
                        afe_code if afe_provisto else cur_afe,
                        bolsa if bolsa_provisto else cur_bolsa))]
                existing.write(vals)
                if not repetido:
                    actualizados += 1
            else:
                # Alta: se aplican los defaults. El tipo lo manda la columna TIPO si vino; si no, se
                # deduce de la unidad (ZZ → servicio, resto → bien), la señal que sí trae la fila.
                vals = {"name": nombre, "default_code": cod, "l10n_pe_ne_unit_code": unidad,
                        "type": self._l10n_pe_ne_tipo_producto(tipo or None, unidad),
                        "taxes_id": [(6, 0, tax_ids_de(afe_code, bolsa))],
                        "sale_ok": True, "company_id": self.env.company.id}
                if precio is not None:
                    vals["list_price"] = precio
                if costo is not None:
                    vals["standard_price"] = costo
                if detra_provisto:
                    vals["l10n_pe_ne_detraccion_cod"] = detra_raw
                if barcode:
                    vals["barcode"] = barcode
                Product.create(vals)
                if not repetido:
                    creados += 1
        return {"commit": commit, "creados": creados, "actualizados": actualizados,
                "errores": errores, "avisos": avisos,
                "totalOk": creados + actualizados, "totalError": len(errores)}

