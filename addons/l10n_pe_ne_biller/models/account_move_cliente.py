# -*- coding: utf-8 -*-
"""account.move — Cliente/partner: alta rápida, padrón, CRUD de clientes.
Extraído de account_move_biller.py (refactor sin cambio de comportamiento)."""
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from .account_move_biller import _percep_float

_logger = logging.getLogger(__name__)


class AccountMove(models.Model):
    _inherit = "account.move"

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
            "pais": p.country_id.code or "",
            "exceptuadoPercepcion": p.l10n_pe_ne_exceptuado_percepcion,
            "parteVinculada": p.l10n_pe_ne_parte_vinculada,
            "tipoVinculo": p.l10n_pe_ne_tipo_vinculo or "",
            "noDomiciliada": p.l10n_pe_ne_no_domiciliada,
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
        # País del adquirente (exportación / no domiciliado): ISO 3166 alpha-2 = res.country.code.
        # Alimenta codPaisCliente en la cabecera 0200. "" limpia el país.
        if "pais" in c:
            code = (c.get("pais") or "").strip().upper()
            country = self.env["res.country"].search([("code", "=", code)], limit=1) if code else False
            vals["country_id"] = country.id if country else False
        for key, field in (
            ("email", "email"),
            ("telefono", "phone"),
            ("direccion", "street"),
            ("exceptuadoPercepcion", "l10n_pe_ne_exceptuado_percepcion"),
            ("parteVinculada", "l10n_pe_ne_parte_vinculada"),
            ("tipoVinculo", "l10n_pe_ne_tipo_vinculo"),
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
    def l10n_pe_ne_list_productos(self, query=None, limit=50, offset=None, categ_id=None):
        """Productos de Odoo para que React liste/autocomplete y los documentos los referencien.
        Busca por nombre, código interno (default_code) o código de barras (barcode).
        `categ_id` filtra por categoría INCLUYENDO sus subcategorías (child_of) — para navegar
        el catálogo por departamento (super).

        Paginación opt-in: con `offset` devuelve {items, total}; sin él, lista plana."""
        domain = [("sale_ok", "=", True)]
        if categ_id:
            domain.append(("categ_id", "child_of", int(categ_id)))
        if query:
            domain += [
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
    def l10n_pe_ne_list_categorias(self):
        """Árbol de categorías de producto bajo 'Supermercado' (departamento → subcategoría),
        con el conteo de productos vendibles de cada rama (child_of). Para navegar el catálogo
        por departamento en la SPA. Vacío si aún no se sembró la raíz 'Supermercado'."""
        Cat = self.env["product.category"]
        Product = self.env["product.product"]
        root = Cat.search([("name", "=", "Supermercado"), ("parent_id", "=", False)], limit=1)
        if not root:
            return {"rootId": None, "items": []}
        cats = Cat.search([("id", "child_of", root.id)], order="complete_name")
        items = [{
            "id": c.id,
            "name": c.name,
            "parentId": c.parent_id.id if c.parent_id else None,
            "count": Product.search_count([("sale_ok", "=", True), ("categ_id", "child_of", c.id)]),
        } for c in cats]
        return {"rootId": root.id, "items": items}

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
                "codSunat": producto.get("codSunat"),
                "detraCod": producto.get("detraCod"),
                "percepTasa": producto.get("percepTasa"),
                "precioUnitario": producto.get("precio"),
                "unidad": producto.get("unidad"),
                "tipo": producto.get("tipo"),
                "llevaStock": producto.get("llevaStock"),
                "rastreo": producto.get("rastreo"),
                "vence": producto.get("vence"),
                "margen": producto.get("margen"),
                "costo": producto.get("costo"),
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
        if "codSunat" in producto:
            vals["l10n_pe_ne_cod_producto_sunat"] = (producto.get("codSunat") or "").strip() or False
        if "detraCod" in producto:
            vals["l10n_pe_ne_detraccion_cod"] = (producto.get("detraCod") or "").strip() or False
        if "registroSanitario" in producto:
            vals["l10n_pe_ne_registro_sanitario"] = (producto.get("registroSanitario") or "").strip() or False
        if "controlado" in producto:
            vals["l10n_pe_ne_controlado"] = bool(producto.get("controlado"))
        if producto.get("percepTasa") is not None:
            vals["l10n_pe_ne_percepcion_tasa"] = _percep_float(producto.get("percepTasa"))
        if "unidad" in producto:
            vals["l10n_pe_ne_unit_code"] = (producto.get("unidad") or "").strip() or False
        if producto.get("tipo"):
            # Solo si viene explícito: aquí NO se deduce de la unidad. Cambiar la unidad de un
            # producto ya clasificado no debe reclasificarlo a su espalda.
            vals["type"] = self._l10n_pe_ne_tipo_producto(producto["tipo"])
        if "llevaStock" in producto:
            vals["is_storable"] = bool(producto.get("llevaStock"))
        if "rastreo" in producto:
            vals["tracking"] = self._l10n_pe_ne_rastreo_producto(producto.get("rastreo"))
        if producto.get("margen") is not None:
            vals["l10n_pe_ne_margen"] = float(producto.get("margen") or 0)
        if producto.get("costo") is not None:
            vals["standard_price"] = float(producto.get("costo") or 0)
        if "vence" in producto:
            vals["use_expiration_date"] = bool(producto.get("vence"))
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
