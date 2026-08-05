# -*- coding: utf-8 -*-
"""Configuración por RUBRO de negocio (Capa 1 del modelo de dos capas).

La Capa 2 (rol → qué puede HACER cada usuario) ya existe en l10n_pe_ne_roles
(_VIS_MENU + has_group por endpoint). Esta capa responde la otra pregunta: qué
módulos EXISTEN para esta empresa según su(s) rubro(s). Una bodega no necesita
detracciones ni valorizaciones de obra; un grifo sí necesita ISC y placa.

Diseño (spec: ne-express/docs/superpowers/specs/2026-08-05-rubros-modulos-design.md):
  * Los catálogos viven en PYTHON (patrón _ROLES/_VIS_MENU de l10n_pe_ne_roles):
    cambiarlos exige PR + review + deploy, no un UPDATE en producción.
  * Empresa SIN rubro configurado = legacy = SIN gating (mismo principio
    «ausente ≠ prohibido» del menú por rol). La migración de tenants viejos es
    así un no-evento: nadie pierde nada hasta que el dueño elige rubro.
  * Multi-rubro = UNIÓN de módulos (nunca resta). El núcleo es inviolable: ni
    un override puede apagar la facturación básica.
  * Módulos aún no construidos van en el catálogo con disponible=False (dato
    listo para la fase 2); la resolución los filtra.
"""
import json

from odoo import _, api, fields, models, SUPERUSER_ID
from odoo.exceptions import AccessError, UserError
from odoo.tools import config

# ─────────────────────────────────────────────────────── catálogo de módulos
# Núcleo: activos para el 100% de los rubros, sin excepción y sin apagado posible.
# Son la obligación mínima de cualquier emisor (facturar, notas, caja, catálogo,
# clientes, reportes y el registro de compras/consulta que exige la contabilidad).
NUCLEO = ("E01", "E02", "E03", "E04", "G01", "G02", "G03", "G04", "G05", "C10", "C11")

# codigo -> (nombre, categoría E/V/I/C/G/R, disponible hoy). `nucleo` se deriva de NUCLEO.
# disponible=False: existe en el catálogo (y en defaults de rubro, para la fase 2)
# pero la resolución lo filtra — no se puede activar lo que aún no está construido.
MODULOS = {
    # Emisión electrónica
    "E01": ("Factura electrónica", "E", True),
    "E02": ("Boleta de venta electrónica", "E", True),
    "E03": ("Nota de crédito", "E", True),
    "E04": ("Nota de débito", "E", True),
    "E05": ("Liquidación de compra", "E", True),
    "E06": ("Comprobante de retención", "E", True),
    "E07": ("Comprobante de percepción", "E", True),
    "E08": ("Guía de remisión — remitente", "E", True),
    "E09": ("Guía de remisión — transportista", "E", True),
    "E10": ("Comunicación de baja", "E", True),
    "E11": ("Resumen diario de boletas", "E", True),
    "E12": ("Multi-moneda (PEN/USD)", "E", True),
    "E13": ("Series por establecimiento", "E", True),
    "E14": ("Emisión masiva desde Excel", "E", True),
    # Ventas y cobro
    "V01": ("Venta rápida (POS)", "V", True),
    "V02": ("Ticket 80mm y representación A4", "V", True),
    "V03": ("Nota de venta (documento interno)", "V", True),
    "V04": ("Cotizaciones convertibles", "V", True),
    "V05": ("Órdenes de taller / servicio", "V", True),
    "V06": ("Venta a crédito con cuotas", "V", True),
    "V07": ("Medios de pago (efectivo, Yape, tarjeta)", "V", True),
    "V08": ("Redondeo de efectivo (Ley 29571)", "V", True),
    "V09": ("Reserva / apartado (layaway)", "V", False),
    "V10": ("Venta al peso con balanza (EAN-13)", "V", True),
    "V11": ("Facturación recurrente / membresías", "V", True),
    # Inventario
    "I01": ("Stock perpetuo", "I", True),
    "I02": ("Kardex por producto", "I", True),
    "I03": ("Ajustes de inventario", "I", True),
    "I04": ("Lotes con vencimiento (FEFO)", "I", True),
    "I05": ("Fraccionamiento por sub-unidad", "I", True),
    "I06": ("Importación masiva de productos", "I", True),
    # Contabilidad y SUNAT
    "C01": ("Libro PLE — Registro de Ventas 14.1", "C", True),
    "C02": ("Libro PLE — Registro de Compras 8.1", "C", True),
    "C03": ("Libro PLE — Inventario Permanente 12.1", "C", True),
    "C04": ("Detracción (SPOT)", "C", True),
    "C05": ("Percepción del IGV", "C", True),
    "C06": ("Retención del IGV", "C", True),
    "C07": ("ISC (selectivo al consumo)", "C", True),
    "C08": ("ICBPER (bolsas plásticas)", "C", True),
    "C09": ("Bancarización (Ley 28194)", "C", True),
    "C10": ("Registro de compras / crédito fiscal", "C", True),
    "C11": ("Consulta pública de comprobantes", "C", True),
    "C12": ("IVAP (arroz pilado)", "C", True),
    "C13": ("Partes vinculadas / umbrales UIT", "C", True),
    # Gestión
    "G01": ("Caja: apertura, cierre y arqueo", "G", True),
    "G02": ("Roles y permisos por perfil", "G", True),
    "G03": ("Clientes con padrón RUC/DNI", "G", True),
    "G04": ("Catálogo de productos", "G", True),
    "G05": ("Análisis de ventas y reportes", "G", True),
    "G06": ("Multi-establecimiento", "G", True),
    # Específicos de rubro
    "R01": ("Placa de vehículo en la venta", "R", True),
    "R02": ("Control de recetas médicas", "R", True),
    "R03": ("Cola de atención FIFO", "R", True),
    "R04": ("Cálculo por tiempo (horas/días)", "R", True),
    "R05": ("Valorizaciones de obra", "R", True),
    "R06": ("Gestión de flota", "R", True),
    "R07": ("Exportación (DUA/DAM)", "R", True),
    "R08": ("Contratación estatal", "R", True),
    "R09": ("Cálculo por volumen/área (m³/m²)", "R", True),
    "R10": ("Agenda de citas / turnos", "R", False),
    "R11": ("Variantes de producto (talla/color)", "R", False),
}

GRUPOS = {
    "comercio": "Comercio y retail",
    "servicios": "Servicios",
    "regulados": "Sectores regulados",
    "educacion-salud": "Educación y Salud",
    "belleza-mascotas": "Belleza, bienestar y mascotas",
    "automotriz-hogar-moda": "Automotriz, hogar y moda",
    "hoteleria-entretenimiento": "Hotelería y entretenimiento",
    "industria-tec-mineria": "Industria, tecnología y minería",
    "especializados": "Otros servicios especializados",
    "comodin": "Comodín",
}

# codigo -> (nombre, grupo, módulos por defecto). El núcleo NO se lista (aplica siempre).
# Defaults = spec del usuario + ajustes de experto SUNAT (p.ej. transporte de carga lleva
# C04: el servicio de transporte ES sujeto a detracción 4%; una botica vende mostrador
# con stock → V01/I01). Ver la spec §4 para la tabla completa con los ajustes marcados.
RUBROS = {
    # Comercio y retail
    "bodega": ("Bodega / minimarket", "comercio", ("V01", "V02", "V07", "V08", "C08", "I01")),
    "autoservicio": ("Supermercado / autoservicio", "comercio",
                     ("V01", "V02", "V07", "V08", "C08", "I01", "E13", "G06", "I06")),
    "distribuidora": ("Distribuidora / mayorista", "comercio", ("V06", "I02", "C04", "E08", "I01")),
    "grifo": ("Grifo / combustibles", "comercio", ("R01", "C05", "C07", "V07", "E13", "I01")),
    "licoreria": ("Licorería / bebidas", "comercio", ("C07", "C08", "V01", "V07", "I01")),
    "farmacia": ("Farmacia / botica", "comercio", ("I04", "R02", "V07", "V01", "I01")),
    "perecibles": ("Perecibles", "comercio", ("I04", "I03", "V01")),
    "arrocera": ("Arrocera / agroindustria", "comercio", ("C12", "E05")),
    "venta-peso": ("Venta al peso / balanza", "comercio", ("V10", "I05", "V01")),
    "ferreteria": ("Ferretería", "comercio", ("I05", "I02", "R09", "V01")),
    "maderera": ("Maderera / aserradero", "comercio", ("R09", "I02")),
    "textil": ("Textil / telas", "comercio", ("I05",)),
    "agropecuario": ("Agropecuario / insumos", "comercio", ("E05",)),
    # Servicios
    "taller": ("Taller mecánico / técnico", "servicios", ("V05", "R03", "V04", "V07")),
    "apartado": ("Reserva / apartado", "servicios", ("V09",)),
    "restaurante": ("Restaurante / comida", "servicios", ("V01", "V10", "C08", "V03", "V07")),
    "consultoria": ("Consultoría / profesionales", "servicios", ()),
    "servicios-tiempo": ("Servicios por hora / día", "servicios", ("R04",)),
    "alquiler": ("Alquiler / rental", "servicios", ("C04", "V06", "V11", "R04")),
    # Sectores regulados
    "transporte": ("Transporte de carga", "regulados", ("E08", "E09", "R06", "C04")),
    "exportador": ("Exportador / comercio exterior", "regulados", ("R07", "E12", "E08")),
    "construccion": ("Construcción / obras", "regulados", ("R05", "C04", "C06", "E08", "E09")),
    "contratista-estado": ("Contratista del Estado", "regulados", ("R08", "C04")),
    "vinculadas": ("Grupos y partes vinculadas", "regulados", ("C13",)),
    # Educación y Salud
    "educacion": ("Educación (colegios, institutos)", "educacion-salud", ("V11", "R10", "E11", "V06")),
    "salud": ("Salud (clínicas, consultorios)", "educacion-salud", ("R10", "E11", "G06", "V01")),
    # Belleza, bienestar y mascotas
    "spa-estetica": ("Peluquería / Spa / Estética", "belleza-mascotas", ("R10", "V01", "V02")),
    "gimnasio": ("Gimnasio / centro deportivo", "belleza-mascotas", ("V11", "V02", "V06")),
    "veterinaria": ("Veterinaria / pet shop", "belleza-mascotas", ("R10", "I01", "V01", "V02")),
    # Automotriz, hogar y moda
    "lavanderia": ("Lavandería", "automotriz-hogar-moda", ("V01", "V02", "V10")),
    "automotriz": ("Tienda automotriz / repuestos", "automotriz-hogar-moda", ("I02", "I01", "V01")),
    "ropa-calzado": ("Tienda de ropa / calzado", "automotriz-hogar-moda",
                     ("R11", "I01", "V01", "V02", "V07")),
    # Hotelería y entretenimiento
    "hoteleria": ("Hotelería / hospedaje", "hoteleria-entretenimiento", ("E11", "G06")),
    "restobar": ("Restobar / entretenimiento", "hoteleria-entretenimiento", ("V01", "C08", "V02")),
    # Industria, tecnología y minería
    "tecnologia": ("Tecnología / software / SaaS", "industria-tec-mineria", ("V11", "R07", "E12")),
    "manufactura": ("Manufactura / industria", "industria-tec-mineria",
                    ("E08", "E09", "C04", "I02", "I01")),
    "mineria": ("Minería e hidrocarburos", "industria-tec-mineria",
                ("E08", "E09", "C04", "R07", "G06")),
    # Otros servicios especializados
    "joyeria": ("Joyería / casa de cambio", "especializados", ("C09", "E12")),
    "imprenta": ("Imprenta / gráfica", "especializados", ("V04",)),
    "eventos": ("Organización de eventos", "especializados", ("V04",)),
    "estacionamiento": ("Estacionamiento / parking", "especializados", ("R04", "V02")),
    "ong": ("ONG / asociación sin fines de lucro", "especializados", ()),
    # Comodín: solo el núcleo; todo lo demás lo decide el dueño a mano.
    "otro": ("Personalizado / otro", "comodin", ()),
}

_DISPONIBLES = frozenset(c for c, (_n, _c, disp) in MODULOS.items() if disp)


def _json_load(raw, default):
    """JSON defensivo: un campo corrupto/vacío cae al default en vez de romper el perfil."""
    try:
        v = json.loads(raw or "")
        return v if isinstance(v, type(default)) else default
    except (ValueError, TypeError):
        return default


class ResCompany(models.Model):
    _inherit = "res.company"

    # Rubro(s) elegidos y overrides manuales. JSON en Char (no m2m a un catálogo-tabla):
    # el catálogo vive en Python y estos campos solo guardan la SELECCIÓN de la empresa.
    l10n_pe_ne_rubros = fields.Char(
        string="Rubros de negocio (JSON)", default="",
        help='Lista JSON de códigos de rubro, ej. ["bodega","alquiler"]. '
             "Vacío = sin rubro configurado (legacy): la empresa ve TODOS los módulos.")
    l10n_pe_ne_modulos_override = fields.Char(
        string="Overrides de módulos (JSON)", default="",
        help='Dict JSON codigo→bool con los módulos que el dueño activó/desactivó a mano, '
             'ej. {"C04": true, "I04": false}. El núcleo no se puede desactivar.')

    def l10n_pe_ne_modulos_efectivos(self):
        """Módulos activos de la empresa, o None si no configuró rubro (= sin gating).

        NUCLEO ∪ unión(defaults de cada rubro) ∪ overrides-on − overrides-off,
        con el núcleo inviolable y filtrado por disponibles. Multi-rubro suma, nunca
        resta: la empresa Ferretería+Alquiler tiene los módulos de ambas."""
        self.ensure_one()
        rubros = [r for r in _json_load(self.l10n_pe_ne_rubros, []) if r in RUBROS]
        if not rubros:
            return None
        overrides = _json_load(self.l10n_pe_ne_modulos_override, {})
        activos = set(NUCLEO)
        for r in rubros:
            activos.update(RUBROS[r][2])
        for cod, on in overrides.items():
            if cod not in MODULOS:
                continue
            if on:
                activos.add(cod)
            elif cod not in NUCLEO:   # el núcleo no se apaga ni a mano
                activos.discard(cod)
        return activos & _DISPONIBLES

    def l10n_pe_ne_modulo_activo(self, cod):
        """True si el módulo está activo para la empresa (o si no hay rubro: legacy sin gating)."""
        self.ensure_one()
        efectivos = self.l10n_pe_ne_modulos_efectivos()
        return True if efectivos is None else cod in efectivos


class RubroAuditoria(models.Model):
    """Bitácora de la configuración por rubro (spec §15): quién cambió qué y cuándo, y los
    rechazos del muro de emisión (intento de usar un régimen sin su módulo = evento de
    seguridad). Solo-lectura para el emisor; escribe el sistema vía sudo."""
    _name = "l10n_pe_ne.rubro_auditoria"
    _description = "Auditoría de configuración por rubro"
    _order = "id desc"

    company_id = fields.Many2one("res.company", required=True, index=True, ondelete="cascade")
    user_id = fields.Many2one("res.users", string="Usuario responsable")
    campo = fields.Char(required=True)   # 'rubros' | 'overrides' | 'rechazo:C04' …
    antes = fields.Char()
    despues = fields.Char()


class AccountMove(models.Model):
    _inherit = "account.move"

    # ------------------------------------------------------------- API rubro
    @api.model
    def l10n_pe_ne_rubro_config(self):
        """Estado + catálogos para la pantalla «Rubro del negocio» (GET /ne/api/rubro)."""
        company = self.env.company
        efectivos = company.l10n_pe_ne_modulos_efectivos()
        return {
            "catalogoRubros": [
                {"codigo": cod, "nombre": nombre, "grupo": grupo,
                 "grupoNombre": GRUPOS[grupo], "modulos": list(mods)}
                for cod, (nombre, grupo, mods) in RUBROS.items()
            ],
            "catalogoModulos": [
                {"codigo": cod, "nombre": nombre, "categoria": cat,
                 "nucleo": cod in NUCLEO, "disponible": disp}
                for cod, (nombre, cat, disp) in MODULOS.items()
            ],
            "rubros": _json_load(company.l10n_pe_ne_rubros, []),
            "overrides": _json_load(company.l10n_pe_ne_modulos_override, {}),
            "modulos": sorted(efectivos) if efectivos is not None else None,
        }

    def _l10n_pe_ne_puede_config_rubro(self):
        """Quién configura el rubro: dueño o supervisor de la empresa (spec §10), o el admin
        de plataforma (aprovisiona tenants y da soporte — mismo trato que config_series).
        El addon de roles puede no estar instalado (tenant mínimo): ahí decide solo el admin."""
        user = self.env.user
        if user.has_group("base.group_system") or user.has_group("base.group_erp_manager"):
            return True
        for xmlid in ("l10n_pe_ne_roles.group_l10n_pe_ne_duenio",
                      "l10n_pe_ne_roles.group_l10n_pe_ne_supervisor"):
            grupo = self.env.ref(xmlid, raise_if_not_found=False)
            # all_group_ids (no group_ids): cubre los grupos por IMPLICACIÓN, igual que el
            # aviso de descuadre de caja — con group_ids un rol implicado quedaría fuera.
            if grupo and grupo in user.all_group_ids:
                return True
        return False

    @api.model
    def l10n_pe_ne_set_rubro(self, payload):
        """Guarda rubro(s) y overrides (POST /ne/api/rubro). Muro PRIMERO: cambiar el rubro
        redefine qué funciones ve TODA la empresa — decisión del dueño/supervisor, nunca de
        un cajero. Valida códigos contra el catálogo y deja bitácora por campo cambiado."""
        if not self._l10n_pe_ne_puede_config_rubro():
            raise AccessError(_(
                "No tienes permiso para configurar el rubro del negocio: define qué "
                "funciones ve toda la empresa. Pídeselo al dueño o al supervisor."))
        payload = payload or {}
        rubros = payload.get("rubros") or []
        overrides = payload.get("overrides") or {}
        desconocidos = [r for r in rubros if r not in RUBROS]
        if desconocidos:
            raise UserError(_("Rubro desconocido: %s") % ", ".join(desconocidos))
        malos = [c for c in overrides if c not in MODULOS]
        if malos:
            raise UserError(_("Módulo desconocido: %s") % ", ".join(malos))
        overrides = {c: bool(v) for c, v in overrides.items()}

        company = self.env.company
        cambios = (
            ("rubros", _json_load(company.l10n_pe_ne_rubros, []), rubros,
             "l10n_pe_ne_rubros"),
            ("overrides", _json_load(company.l10n_pe_ne_modulos_override, {}), overrides,
             "l10n_pe_ne_modulos_override"),
        )
        for campo, antes, despues, field_name in cambios:
            if antes == despues:
                continue
            company.sudo().write({field_name: json.dumps(despues, ensure_ascii=False)})
            self.env["l10n_pe_ne.rubro_auditoria"].sudo().create({
                "company_id": company.id,
                "user_id": self.env.user.id,
                "campo": campo,
                "antes": json.dumps(antes, ensure_ascii=False),
                "despues": json.dumps(despues, ensure_ascii=False),
            })
        return self.l10n_pe_ne_rubro_config()

    # ------------------------------------------------------- muro de emisión
    def _l10n_pe_ne_check_modulo(self, cod, etiqueta):
        """Muro por módulo en la EMISIÓN: si la empresa configuró rubro y el payload trae un
        régimen cuyo módulo no está activo, se corta ANTES de armar el move. Vive en el
        modelo (no solo en la UI) para que ninguna vía —API directa, curl— lo salte; el
        intento queda en la bitácora como evento de seguridad (spec §15). Empresa sin
        rubro (legacy) pasa siempre: ausente ≠ prohibido."""
        company = self.env.company
        # Super Admin (plataforma) ignora la Capa 1: opera todo en cualquier empresa (regla 1
        # de la spec, misma doctrina «fuera de la matriz» del menú por rol).
        if (self.env.user.has_group("base.group_system")
                or self.env.user.has_group("base.group_erp_manager")):
            return
        if company.l10n_pe_ne_modulo_activo(cod):
            return
        # La fila debe SOBREVIVIR al rollback que provoca el propio UserError (la transacción
        # del request se revierte y se llevaría la evidencia): se escribe en un cursor APARTE.
        # EXCEPTO en tests: ahí registry.cursor() devuelve el MISMO cursor del test y el `with`
        # lo cerraría (rompiendo todo lo posterior) — se escribe con el env normal.
        # La bitácora nunca debe impedir el mensaje al usuario — de ahí el except amplio.
        vals = {
            "company_id": company.id,
            "user_id": self.env.user.id,
            "campo": "rechazo:%s" % cod,
            "antes": "",
            "despues": etiqueta,
        }
        try:
            if config["test_enable"]:
                self.env["l10n_pe_ne.rubro_auditoria"].sudo().create(vals)
            else:
                with self.env.registry.cursor() as cr:
                    api.Environment(cr, SUPERUSER_ID, {})["l10n_pe_ne.rubro_auditoria"].create(vals)
        except Exception:  # noqa: BLE001
            pass
        raise UserError(_(
            "«%(etiqueta)s» no está activo para el rubro de tu negocio. "
            "Actívalo en Datos del negocio → Rubro, o pídeselo al dueño.",
            etiqueta=etiqueta))
