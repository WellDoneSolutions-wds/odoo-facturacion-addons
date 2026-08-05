# -*- coding: utf-8 -*-
"""R11 · Variantes de producto — talla / color (fase 2 del módulo de rubros).

Para ropa y calzado: del producto base («Polo básico») se generan productos
HIJOS por cada combinación («Polo básico — M / Rojo»), copiando precio,
categoría e impuestos. Son productos normales e independientes (cada uno con
su stock, su kardex y su código de barras si se le pone) — no el sistema de
atributos de Odoo, que arrastra la venta a plantillas/atributos que la SPA no
usa. El vínculo con el base queda registrado para poder listarlos juntos.
"""
import itertools

from odoo import _, api, fields, models
from odoo.exceptions import UserError

# Tope de sanidad: 6 tallas × 8 colores ya son 48 productos. Más que esto suele ser un
# error de tipeo (una lista pegada de más), no un catálogo real.
_MAX_COMBINACIONES = 60


class ProductTemplate(models.Model):
    _inherit = "product.template"

    l10n_pe_ne_variante_de = fields.Many2one(
        "product.template", string="Variante de", index=True, ondelete="set null",
        help="Producto base del que esta variante se generó (R11).")
    l10n_pe_ne_atributos = fields.Char(
        string="Atributos de la variante", help='Ej. "M / Rojo".')


class AccountMove(models.Model):
    _inherit = "account.move"

    @api.model
    def l10n_pe_ne_generar_variantes(self, payload):
        """Genera las combinaciones de atributos como productos hijos del base.
        payload = {"productId": id, "atributos": {"Talla": ["S","M"], "Color": ["Rojo"]}}.
        Idempotente por nombre: la combinación que ya existe se salta (re-ejecutar con una
        talla nueva solo crea la nueva)."""
        self._l10n_pe_ne_check_modulo("R11", "Variantes de producto (talla/color)")
        payload = payload or {}
        base = self.env["product.product"].browse(int(payload.get("productId") or 0)).exists()
        if not base:
            raise UserError(_("Producto base no encontrado."))
        atributos = payload.get("atributos") or {}
        # Solo los ejes con valores; cada valor limpio y sin duplicados (preserva el orden).
        ejes = []
        for _nombre, valores in atributos.items():
            vistos, limpios = set(), []
            for v in valores or []:
                v = str(v).strip()
                if v and v.lower() not in vistos:
                    vistos.add(v.lower())
                    limpios.append(v)
            if limpios:
                ejes.append(limpios)
        if not ejes:
            raise UserError(_("Ingresa al menos un atributo con valores (ej. Talla: S, M, L)."))
        combos = list(itertools.product(*ejes))
        if len(combos) > _MAX_COMBINACIONES:
            raise UserError(_(
                "Son %(n)d combinaciones — más de %(max)d. Revisa las listas (¿se pegó algo de más?).",
                n=len(combos), max=_MAX_COMBINACIONES))

        existentes = {
            p.name for p in self.env["product.product"].search(
                [("product_tmpl_id.l10n_pe_ne_variante_de", "=", base.product_tmpl_id.id)])
        }
        creados = []
        for combo in combos:
            etiqueta = " / ".join(combo)
            nombre = "%s — %s" % (base.name, etiqueta)
            if nombre in existentes:
                continue
            sufijo = "-".join(c[:3].upper() for c in combo)
            # copy() del product.product: hereda precio, categoría, impuestos, unidad y tipo.
            # barcode NO se copia (es único): cada variante recibe el suyo al etiquetar.
            nuevo = base.copy({
                "name": nombre,
                "default_code": ("%s-%s" % (base.default_code, sufijo)) if base.default_code else False,
                "barcode": False,
            })
            nuevo.product_tmpl_id.write({
                "l10n_pe_ne_variante_de": base.product_tmpl_id.id,
                "l10n_pe_ne_atributos": etiqueta,
            })
            creados.append({"id": nuevo.id, "nombre": nombre})
        return {"creados": creados, "omitidos": len(combos) - len(creados)}
