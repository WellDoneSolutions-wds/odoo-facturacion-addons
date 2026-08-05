# -*- coding: utf-8 -*-
"""Configuración por catálogos (Capa 1.5) — impuestos, unidades, medios de pago y monedas.

El rubro (Capa 1) define QUÉ módulos existen; esta capa define CON QUÉ VALORES trabaja
cada catálogo del negocio: qué afectaciones usa, qué unidades de medida ofrece en sus
selects, qué medios de pago acepta (con orden y personalizados) y en qué monedas opera.

La ventaja sobre un «configuración» plano (estilo susii): los catálogos NACEN sembrados
por el rubro — el grifo nace con GALÓN y LITRO activos, la maderera con M³/M², el
exportador con USD — y el dueño solo ajusta con checks. Misma doctrina del motor de
rubros: SIN configuración = legacy = se ofrece el catálogo completo (ausente ≠ prohibido),
así ninguna empresa existente pierde nada.

Los MAESTROS viven en Python (patrón _ROLES/MODULOS: cambiarlos exige PR). La SELECCIÓN
de la empresa vive en un JSON de res.company. Los medios de pago son el único catálogo
con entradas personalizadas (el negocio agrega «Agora», «Mercado Pago»…): su lista
completa vive en la selección, ordenada.
"""
import json

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .l10n_pe_ne_rubro import RUBROS, _json_load

# ─────────────────────────────────────────────────────────────── maestros
# Unidades cat. 03 soportadas por la app (espejo de lib/unidades.ts de la SPA — la SPA
# pone las etiquetas; aquí solo códigos válidos). Fuera de este set, SUNAT rechaza.
UNIDADES_CAT03 = (
    "NIU", "ZZ", "KGM", "LBR", "GRM", "TNE", "LTN", "STN", "LTR", "GLL", "BLL",
    "CA", "BX", "MLL", "MTR", "CMT", "MTK", "MTQ", "DAY", "HUR", "SET", "DZN",
)
# Afectaciones base (cat. 07 + ICBPER como pseudo-afectación de línea de la app).
AFECTACIONES = ("1000", "9997", "9998", "9995", "9996", "1016", "icbper")
# Sub-tipos de gratuita que la emisión soporta hoy (cat. 07, gravadas 11-16).
GRATUITAS = ("11", "12", "13", "14", "15", "16")
MEDIOS_SEMILLA = ("Efectivo", "Yape", "Plin", "Tarjeta", "Transferencia", "Depósito")
MONEDAS = ("PEN", "USD")

# Base común de la siembra: lo que CUALQUIER negocio necesita para operar día uno.
_BASE = {
    "unidades": ("NIU", "ZZ", "KGM"),
    "medios": ("Efectivo", "Yape", "Plin", "Tarjeta"),
    "afectaciones": ("1000", "9997", "9998"),
    "gratuitas": ("11", "15"),   # retiro por premio y bonificación: las 2 más comunes
    "monedas": ("PEN",),
}

# Extras POR RUBRO (se suman a la base; multi-rubro = unión). Aquí vive el «nivel más»
# sobre susii: el negocio nace con los catálogos de su categoría.
RUBRO_CATALOGOS = {
    "grifo": {"unidades": ("GLL", "LTR"), "afectaciones": ("icbper",)},
    "venta-peso": {"unidades": ("GRM", "LBR")},
    "perecibles": {"unidades": ("GRM",)},
    "restaurante": {"unidades": ("GRM",), "afectaciones": ("icbper",)},
    "bodega": {"afectaciones": ("icbper",)},
    "autoservicio": {"unidades": ("BX", "DZN", "MLL", "CA", "SET"), "afectaciones": ("icbper",)},
    "distribuidora": {"unidades": ("BX", "DZN", "MLL", "CA", "SET", "TNE")},
    "licoreria": {"unidades": ("BX", "CA", "LTR"), "afectaciones": ("icbper",)},
    "ferreteria": {"unidades": ("MTR", "CMT", "MTK", "MTQ", "BX", "SET")},
    "maderera": {"unidades": ("MTQ", "MTK", "MTR", "CMT")},
    "textil": {"unidades": ("MTR", "CMT")},
    "arrocera": {"unidades": ("TNE",), "afectaciones": ("1016",)},
    "agropecuario": {"unidades": ("TNE", "GRM")},
    "taller": {"unidades": ("HUR", "DAY", "SET")},
    "servicios-tiempo": {"unidades": ("HUR", "DAY")},
    "estacionamiento": {"unidades": ("HUR", "DAY")},
    "alquiler": {"unidades": ("HUR", "DAY")},
    "construccion": {"unidades": ("MTQ", "MTK", "MTR", "TNE", "DAY")},
    "transporte": {"unidades": ("TNE", "DAY")},
    "exportador": {"monedas": ("USD",), "afectaciones": ("9995",)},
    "tecnologia": {"monedas": ("USD",), "unidades": ("HUR",)},
    "mineria": {"monedas": ("USD",), "unidades": ("TNE",), "afectaciones": ("9995",)},
    "joyeria": {"monedas": ("USD",), "unidades": ("GRM",)},
    "hoteleria": {"unidades": ("DAY",)},
    "lavanderia": {"unidades": ("GRM",)},
    "manufactura": {"unidades": ("TNE", "MLL", "BX")},
    "farmacia": {"gratuitas": ("13",)},   # retiro (muestras) — el sub-tipo que usan las boticas
}


def _sembrado_para(rubros):
    """Config inicial (dict) para el/los rubro(s): base + extras de cada uno, con defaults."""
    u, m, a, g, mo = (set(_BASE["unidades"]), list(_BASE["medios"]),
                      set(_BASE["afectaciones"]), set(_BASE["gratuitas"]), set(_BASE["monedas"]))
    for r in rubros:
        extra = RUBRO_CATALOGOS.get(r, {})
        u.update(extra.get("unidades", ()))
        a.update(extra.get("afectaciones", ()))
        g.update(extra.get("gratuitas", ()))
        mo.update(extra.get("monedas", ()))
    return {
        "unidades": {"activas": sorted(u), "default": "NIU"},
        "medios": {"lista": m, "default": "Efectivo"},
        "afectaciones": {"activas": sorted(a), "gratuitas": sorted(g), "default": "1000"},
        "monedas": {"activas": sorted(mo)},
    }


class ResCompany(models.Model):
    _inherit = "res.company"

    # Selección de catálogos del negocio (JSON). Vacío = legacy = catálogo completo.
    l10n_pe_ne_cfg_catalogos = fields.Char(string="Catálogos del negocio (JSON)", default="")

    def _l10n_pe_ne_cfg(self):
        """Config parseada o None (legacy sin configurar)."""
        self.ensure_one()
        cfg = _json_load(self.l10n_pe_ne_cfg_catalogos, {})
        return cfg or None

    def _l10n_pe_ne_sembrar_catalogos(self, force=False):
        """Siembra los catálogos desde el rubro configurado. Solo escribe si NO hay config
        propia (o con force): la siembra nunca pisa lo que el dueño ya ajustó."""
        self.ensure_one()
        if self._l10n_pe_ne_cfg() and not force:
            return False
        rubros = [r for r in _json_load(self.l10n_pe_ne_rubros, []) if r in RUBROS]
        if not rubros:
            return False
        self.sudo().l10n_pe_ne_cfg_catalogos = json.dumps(
            _sembrado_para(rubros), ensure_ascii=False)
        return True


class AccountMove(models.Model):
    _inherit = "account.move"

    # -------------------------------------------------------------- API SPA
    @api.model
    def l10n_pe_ne_cfg_catalogos_get(self):
        """Estado + maestros para la pantalla Configuración. `sugerido` = lo que la siembra
        del rubro produciría (para el botón «Restablecer desde mi rubro»)."""
        company = self.env.company
        rubros = [r for r in _json_load(company.l10n_pe_ne_rubros, []) if r in RUBROS]
        return {
            "maestros": {
                "unidades": list(UNIDADES_CAT03),
                "afectaciones": list(AFECTACIONES),
                "gratuitas": list(GRATUITAS),
                "mediosSemilla": list(MEDIOS_SEMILLA),
                "monedas": list(MONEDAS),
            },
            "cfg": company._l10n_pe_ne_cfg(),
            "sugerido": _sembrado_para(rubros) if rubros else None,
        }

    @api.model
    def l10n_pe_ne_cfg_catalogos_set(self, payload):
        """Guarda la selección. Mismo muro que el rubro (dueño/supervisor/admin): define con
        qué valores factura TODA la empresa. Validaciones duras: los códigos deben existir en
        el maestro, el default debe estar activo, PEN no se puede apagar (la moneda del país)
        y siempre debe quedar al menos una afectación de venta y un medio de pago."""
        if not self._l10n_pe_ne_puede_config_rubro():
            raise UserError(_(
                "No tienes permiso para configurar los catálogos del negocio. "
                "Pídeselo al dueño o al supervisor."))
        cfg = payload or {}

        uni = cfg.get("unidades") or {}
        u_act = [c for c in (uni.get("activas") or []) if c in UNIDADES_CAT03]
        if not u_act:
            raise UserError(_("Activa al menos una unidad de medida."))
        u_def = uni.get("default") or "NIU"
        if u_def not in u_act:
            raise UserError(_("La unidad por defecto (%s) debe estar activa.") % u_def)

        med = cfg.get("medios") or {}
        m_lista = [str(x).strip() for x in (med.get("lista") or []) if str(x).strip()]
        # sin duplicados (case-insensitive), conservando el orden del dueño
        vistos, m_final = set(), []
        for x in m_lista:
            if x.lower() not in vistos:
                vistos.add(x.lower())
                m_final.append(x[:40])
        if not m_final:
            raise UserError(_("Deja al menos un medio de pago."))
        m_def = (med.get("default") or m_final[0]).strip()
        if m_def not in m_final:
            raise UserError(_("El medio por defecto (%s) debe estar en la lista.") % m_def)

        af = cfg.get("afectaciones") or {}
        a_act = [c for c in (af.get("activas") or []) if c in AFECTACIONES]
        if not any(c in ("1000", "9997", "9998", "1016") for c in a_act):
            raise UserError(_("Activa al menos una afectación de venta (Gravado, Exonerado, Inafecto o IVAP)."))
        a_def = af.get("default") or "1000"
        if a_def not in a_act:
            raise UserError(_("La afectación por defecto (%s) debe estar activa.") % a_def)
        g_act = [c for c in (af.get("gratuitas") or []) if c in GRATUITAS]

        mo = cfg.get("monedas") or {}
        mo_act = [c for c in (mo.get("activas") or []) if c in MONEDAS]
        if "PEN" not in mo_act:
            mo_act = ["PEN"] + mo_act   # el sol no se apaga: es la moneda del comprobante local

        company = self.env.company
        nuevo = {
            "unidades": {"activas": sorted(set(u_act)), "default": u_def},
            "medios": {"lista": m_final, "default": m_def},
            "afectaciones": {"activas": sorted(set(a_act)), "gratuitas": sorted(set(g_act)), "default": a_def},
            "monedas": {"activas": sorted(set(mo_act))},
        }
        antes = company._l10n_pe_ne_cfg() or {}
        if antes != nuevo:
            company.sudo().l10n_pe_ne_cfg_catalogos = json.dumps(nuevo, ensure_ascii=False)
            self.env["l10n_pe_ne.rubro_auditoria"].sudo().create({
                "company_id": company.id,
                "user_id": self.env.user.id,
                "campo": "catalogos",
                "antes": json.dumps(antes, ensure_ascii=False),
                "despues": json.dumps(nuevo, ensure_ascii=False),
            })
        return self.l10n_pe_ne_cfg_catalogos_get()
