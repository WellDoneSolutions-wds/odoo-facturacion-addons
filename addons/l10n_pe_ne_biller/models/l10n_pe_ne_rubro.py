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

# codigo -> (nombre, categoría E/V/I/C/G/R, disponible hoy, descripción). `nucleo` se
# deriva de NUCLEO. disponible=False: existe en el catálogo (y en defaults de rubro, para
# la fase 2) pero la resolución lo filtra — no se puede activar lo que aún no está construido.
#
# La DESCRIPCIÓN es contrato de producto, no comentario: viaja a la SPA y es lo único que
# un dueño de PyME lee para decidir si necesita «IVAP» o «Bancarización». Regla al escribirla:
# explica PARA QUÉ SIRVE en lenguaje de negocio, no qué es en lenguaje tributario. Una línea.
MODULOS = {
    # Emisión electrónica
    "E01": ("Factura electrónica", "E", True,
            "Para vender a empresas con RUC que necesitan usar el IGV como crédito fiscal."),
    "E02": ("Boleta de venta electrónica", "E", True,
            "Para vender al consumidor final. Es el comprobante del día a día en mostrador."),
    "E03": ("Nota de crédito", "E", True,
            "Para anular o corregir un comprobante ya emitido, o devolver mercadería."),
    "E04": ("Nota de débito", "E", True,
            "Para cobrar de más sobre un comprobante ya emitido: intereses, mora o un cargo omitido."),
    "E05": ("Liquidación de compra", "E", True,
            "Cuando TÚ le compras a un productor sin RUC (agro, reciclaje, artesanía) y emites por él."),
    "E06": ("Comprobante de retención", "E", True,
            "Solo si SUNAT te nombró agente de retención: retienes parte del pago a tus proveedores."),
    "E07": ("Comprobante de percepción", "E", True,
            "Solo si SUNAT te nombró agente de percepción: cobras un adelanto del IGV a tu cliente."),
    "E08": ("Guía de remisión — remitente", "E", True,
            "Documento que ampara el traslado de tu mercadería. Obligatorio para que viaje en regla."),
    "E09": ("Guía de remisión — transportista", "E", True,
            "Si tú prestas el servicio de transporte a terceros con tus propias unidades."),
    "E10": ("Comunicación de baja", "E", True,
            "Para dar de baja ante SUNAT un comprobante que no debió emitirse."),
    "E11": ("Resumen diario de boletas", "E", True,
            "Envía tus boletas del día a SUNAT en un solo lote. Si emites boletas, lo necesitas."),
    "E12": ("Multi-moneda (PEN/USD)", "E", True,
            "Para facturar en dólares con el tipo de cambio SUNAT del día."),
    "E13": ("Series por establecimiento", "E", True,
            "Numeración propia por local, para que dos tiendas no choquen de correlativo."),
    "E14": ("Emisión masiva desde Excel", "E", True,
            "Sube una planilla y emite cientos de comprobantes de una pasada."),
    # Ventas y cobro
    "V01": ("Venta rápida (POS)", "V", True,
            "Pantalla de mostrador: cobras rápido con lector de código de barras y das vuelto."),
    "V02": ("Ticket 80mm y representación A4", "V", True,
            "Imprime en ticketera térmica o en hoja A4, según lo que pida el cliente."),
    "V03": ("Nota de venta (documento interno)", "V", True,
            "Comprobante interno que NO va a SUNAT: pedidos, adelantos o ventas aún sin facturar."),
    "V04": ("Cotizaciones convertibles", "V", True,
            "Pasas un presupuesto al cliente y, si acepta, se convierte en venta sin recargarlo."),
    "V05": ("Órdenes de taller / servicio", "V", True,
            "Sigues el trabajo desde que entra hasta que se entrega: recibido, en proceso, listo."),
    "V06": ("Venta a crédito con cuotas", "V", True,
            "Vendes al crédito con fechas de pago y llevas el control de lo que te deben."),
    "V07": ("Medios de pago (efectivo, Yape, tarjeta)", "V", True,
            "Divides un cobro entre varios medios y sabes cuánto entró por cada uno."),
    "V08": ("Redondeo de efectivo (Ley 29571)", "V", True,
            "Ajusta el vuelto al décimo cuando no hay monedas de 1 y 2 céntimos."),
    "V09": ("Reserva / apartado (layaway)", "V", True,
            "El cliente separa un producto con abonos y se lo lleva al terminar de pagarlo."),
    "V10": ("Venta al peso con balanza (EAN-13)", "V", True,
            "Lees la etiqueta de la balanza y el peso entra solo a la venta."),
    "V11": ("Facturación recurrente / membresías", "V", True,
            "Cobras lo mismo cada mes sin volver a cargarlo: mensualidades, planes, alquileres."),
    # Inventario
    "I01": ("Stock perpetuo", "I", True,
            "El sistema descuenta solo al vender y siempre sabes cuánto te queda."),
    "I02": ("Kardex por producto", "I", True,
            "El historial de entradas y salidas de cada producto, con saldo y costo."),
    "I03": ("Ajustes de inventario", "I", True,
            "Corriges el stock tras un conteo físico, una merma o una rotura."),
    "I04": ("Lotes con vencimiento (FEFO)", "I", True,
            "Controlas fechas de vencimiento y sale primero lo que vence antes."),
    "I05": ("Fraccionamiento por sub-unidad", "I", True,
            "Compras por caja y vendes por unidad, sin descuadrar el stock."),
    "I06": ("Importación masiva de productos", "I", True,
            "Cargas todo tu catálogo desde un Excel en vez de producto por producto."),
    # Contabilidad y SUNAT
    "C01": ("Libro PLE — Registro de Ventas 14.1", "C", True,
            "Genera el archivo de ventas que tu contador presenta a SUNAT."),
    "C02": ("Libro PLE — Registro de Compras 8.1", "C", True,
            "Genera el archivo de compras que sustenta tu crédito fiscal."),
    "C03": ("Libro PLE — Inventario Permanente 12.1", "C", True,
            "Libro de inventario valorizado. Exigible al superar el tope de ingresos de SUNAT."),
    "C04": ("Detracción (SPOT)", "C", True,
            "Tu cliente deposita un % en tu cuenta de detracciones del Banco de la Nación."),
    "C05": ("Percepción del IGV", "C", True,
            "Cobras un adelanto del IGV al vender combustible u otros bienes del régimen."),
    "C06": ("Retención del IGV", "C", True,
            "Retienes parte del pago a tu proveedor y se lo entregas a SUNAT."),
    "C07": ("ISC (selectivo al consumo)", "C", True,
            "Impuesto extra sobre licores, cigarros, combustibles y bebidas azucaradas."),
    "C08": ("ICBPER (bolsas plásticas)", "C", True,
            "El impuesto por cada bolsa de plástico que entregas al cliente."),
    "C09": ("Bancarización (Ley 28194)", "C", True,
            "Desde S/ 2,000 el pago debe ser bancario para que el gasto sea deducible."),
    "C10": ("Registro de compras / crédito fiscal", "C", True,
            "Registras las facturas que recibes para descontar su IGV del que pagas."),
    "C11": ("Consulta pública de comprobantes", "C", True,
            "Tu cliente verifica en línea que el comprobante que le diste es real."),
    "C12": ("IVAP (arroz pilado)", "C", True,
            "Régimen especial del 4% que reemplaza al IGV solo en la venta de arroz pilado."),
    "C13": ("Partes vinculadas / umbrales UIT", "C", True,
            "Si vendes a empresas de tu mismo grupo, controla los topes que exige SUNAT."),
    # Gestión
    "G01": ("Caja: apertura, cierre y arqueo", "G", True,
            "Abres y cierras el turno y cuadras cuánto efectivo debería haber."),
    "G02": ("Roles y permisos por perfil", "G", True,
            "Cada trabajador ve solo lo suyo: el cajero no entra a los reportes del dueño."),
    "G03": ("Clientes con padrón RUC/DNI", "G", True,
            "Escribes el RUC o DNI y los datos del cliente se completan solos."),
    "G04": ("Catálogo de productos", "G", True,
            "Tu lista de productos y servicios con precios, códigos y categorías."),
    "G05": ("Análisis de ventas y reportes", "G", True,
            "Qué se vende más, cuánto vendiste y cómo va el negocio frente al mes pasado."),
    "G06": ("Multi-establecimiento", "G", True,
            "Manejas varios locales desde una sola cuenta, cada uno con su stock y caja."),
    # Específicos de rubro
    "R01": ("Placa de vehículo en la venta", "R", True,
            "Pide la placa al vender combustible: SUNAT la exige en el comprobante."),
    "R02": ("Control de recetas médicas", "R", True,
            "Registra la receta al vender medicamentos que la requieren."),
    "R03": ("Cola de atención FIFO", "R", True,
            "Atiendes en orden de llegada y ves quién sigue."),
    "R04": ("Cálculo por tiempo (horas/días)", "R", True,
            "El importe sale del tiempo: pones entrada y salida y calcula solo."),
    "R05": ("Valorizaciones de obra", "R", True,
            "Facturas por avance de obra, no todo al final."),
    "R06": ("Gestión de flota", "R", True,
            "Tus vehículos y conductores frecuentes, listos para la guía de remisión."),
    "R07": ("Exportación (DUA/DAM)", "R", True,
            "Facturas al exterior sin IGV, con el número de aduana en el comprobante."),
    "R08": ("Contratación estatal", "R", True,
            "Para vender al Estado: expediente, conformidad y penalidades del contrato."),
    "R09": ("Cálculo por volumen/área (m³/m²)", "R", True,
            "Pones medidas y calcula el volumen o el área a cobrar."),
    "R10": ("Agenda de citas / turnos", "R", True,
            "Agendas al cliente por día y hora y ves la agenda del local."),
    "R11": ("Variantes de producto (talla/color)", "R", True,
            "Un mismo producto en varias tallas o colores, cada uno con su stock."),
}

# Dependencias técnicas entre módulos: activar la clave exige activar sus valores.
# No es preferencia de negocio, es coherencia: un Kardex sin Stock perpetuo no tiene de
# dónde sacar los movimientos, y un Resumen diario sin Boleta no tiene qué resumir.
# Se aplica en la RESOLUCIÓN (cierre transitivo), no al guardar: así un preset de rubro
# incompleto se autocompleta en vez de producir una configuración rota.
DEPENDENCIAS = {
    "I02": ("I01",),          # Kardex necesita movimientos de stock
    "I03": ("I01",),          # Ajustar inventario exige llevar inventario
    "I04": ("I01",),          # Lotes/vencimiento se controlan sobre el stock
    "I05": ("I01",),          # Fraccionar caja→unidad mueve stock
    "V10": ("V01",),          # La balanza alimenta la venta rápida
    "V08": ("V07",),          # Redondear efectivo exige distinguir medios de pago
    "E09": ("E08",),          # La guía de transportista comparte el motor de la de remitente
    "R06": ("E08",),          # La flota existe para las guías
    "C03": ("I01",),          # El inventario permanente valorizado sale del stock
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

_DISPONIBLES = frozenset(c for c, (_n, _c, disp, _d) in MODULOS.items() if disp)


def _con_dependencias(activos):
    """Cierre transitivo de DEPENDENCIAS sobre un set de módulos.

    Se aplica al RESOLVER (no al guardar) para que un preset de rubro incompleto o un
    override suelto no produzcan una configuración imposible (Kardex sin Stock perpetuo).
    El bucle converge: cada vuelta solo puede añadir, y el catálogo es finito."""
    activos = set(activos)
    while True:
        extra = set()
        for cod in activos:
            for dep in DEPENDENCIAS.get(cod, ()):
                if dep not in activos:
                    extra.add(dep)
        if not extra:
            return activos
        activos |= extra


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

        NUCLEO ∪ unión(defaults de cada rubro) ∪ overrides-on − overrides-off, más el
        cierre de DEPENDENCIAS, con el núcleo inviolable y filtrado por disponibles.
        Multi-rubro suma, nunca resta: la empresa Ferretería+Alquiler tiene los de ambas.

        El cierre de dependencias va DESPUÉS de los overrides-off: apagar «Stock perpetuo»
        teniendo «Kardex» encendido lo vuelve a encender, porque el Kardex sin movimientos
        de stock no es una configuración válida sino una rota. Para apagarlo de verdad hay
        que apagar antes lo que depende de él — y la UI lo dice."""
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
        return _con_dependencias(activos) & _DISPONIBLES

    def l10n_pe_ne_modulo_activo(self, cod):
        """True si el módulo está activo para la empresa (o si no hay rubro: legacy sin gating)."""
        self.ensure_one()
        efectivos = self.l10n_pe_ne_modulos_efectivos()
        return True if efectivos is None else cod in efectivos

    def l10n_pe_ne_modulos_en_uso(self):
        """Módulos con USO REAL en la historia de la empresa (fase 4 de la spec): comprobantes
        con detracción, guías emitidas, cotizaciones, membresías… Se usa al elegir rubro para
        PROTEGER lo que ya se usa (override automático) — elegir «Bodega» no puede empezar a
        rechazar las detracciones que la empresa emite hace meses. Es un sondeo barato: un
        search limit=1 por módulo.

        COBERTURA (importante, la UI depende de esto para no mentir): 27 de los 61 módulos
        tienen un rastro persistente que se pueda sondear. El resto —los que no dejan dato
        propio, como los libros PLE o la consulta pública— no es detectable y NO se protege
        por esta vía; a esos los cubre la fusión de overrides de `l10n_pe_ne_set_rubro`, que
        conserva lo que el dueño activó a mano. Al añadir un módulo con dato propio, añade
        aquí su sondeo."""
        self.ensure_one()
        Move = self.env["account.move"].sudo()
        dom = [("company_id", "=", self.id)]

        def hay(model, extra=None):
            return bool(self.env[model].sudo().search(
                (extra or []) + [("company_id", "=", self.id)], limit=1))

        def hay_move(extra):
            return bool(Move.search(dom + extra, limit=1))

        def hay_linea(extra):
            # OJO: hay campos que viven en la LÍNEA, no en el move (placa por línea de
            # combustible, fraccionamiento por línea). Sondearlos en account.move daría
            # siempre False y el módulo quedaría desprotegido en silencio.
            return bool(self.env["account.move.line"].sudo().search(dom + extra, limit=1))

        def hay_tax(code):
            taxes = self.env["account.tax"].sudo().search(
                [("company_id", "=", self.id), ("l10n_pe_edi_tax_code", "=", code)])
            if not taxes:
                return False
            return bool(self.env["account.move.line"].sudo().search(
                [("company_id", "=", self.id), ("tax_ids", "in", taxes.ids)], limit=1))

        en_uso = set()
        if hay_move([("l10n_pe_ne_detraccion", "=", True)]):
            en_uso.add("C04")
        if hay_move([("l10n_pe_ne_percepcion", "=", True)]):
            en_uso.add("C05")
        if hay_move([("l10n_pe_ne_estado_expediente", "!=", False)]):
            en_uso.add("R08")
        if hay_move([("l10n_pe_ne_proyecto_id", "!=", False)]):
            en_uso.add("R05")
        if hay_move([("l10n_pe_ne_tipo_doc", "=", "04")]):
            en_uso.add("E05")
        if hay_move([("currency_id", "!=", self.currency_id.id)]):
            en_uso.add("E12")
        if hay_tax("2000"):
            en_uso.add("C07")
        if hay_tax("1016"):
            en_uso.add("C12")
        # ── Ampliación de cobertura ──────────────────────────────────────────
        if hay_tax("7152"):                                        # bolsas plásticas
            en_uso.add("C08")
        # OJO: 'no_aplica' es un valor legítimo del selection, no ausencia. Sondear
        # «!= False» marcaría C09 en uso para TODA empresa que haya tocado el campo.
        if hay_move([("l10n_pe_ne_bancarizacion", "in", ("pendiente", "bancarizado"))]):
            en_uso.add("C09")
        if hay_move([("l10n_pe_ne_dua", "!=", False)]):
            en_uso.add("R07")
        # `l10n_pe_ne_cuotas` es Json y `_cuotas_display` es compute NO almacenado (no se
        # puede buscar): la forma de pago es el rastro almacenado equivalente.
        if hay_move([("l10n_pe_ne_forma_pago", "=", "Credito")]):
            en_uso.add("V06")
        if hay_move([("l10n_pe_ne_tipo_doc", "=", "20")]):
            en_uso.add("E06")
        if hay_move([("l10n_pe_ne_tipo_doc", "=", "40")]):
            en_uso.add("E07")
        if hay_linea([("l10n_pe_ne_placa", "!=", False)]):
            en_uso.add("R01")
        if hay_linea([("l10n_pe_ne_fraccionado", "=", True)]):
            en_uso.add("I05")
        if hay("stock.move"):                                      # llevó stock alguna vez
            en_uso.add("I01")
        if hay("stock.lot", [("expiration_date", "!=", False)]):
            en_uso.add("I04")
        for cod, model in (("E08", "l10n_pe_ne.guia_remision"), ("V04", "l10n_pe_ne.cotizacion"),
                           ("V03", "l10n_pe_ne.nota_venta"), ("E14", "l10n_pe_ne.lote"),
                           ("V11", "l10n_pe_ne.recurrencia"), ("R10", "l10n_pe_ne.cita"),
                           ("V09", "l10n_pe_ne.apartado"), ("R06", "l10n_pe_ne.vehiculo"),
                           ("G06", "l10n_pe_ne.establecimiento"),
                           ("R05", "l10n_pe_ne.proyecto")):
            if hay(model):
                en_uso.add(cod)
        return en_uso


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
        out = {
            "catalogoRubros": [
                {"codigo": cod, "nombre": nombre, "grupo": grupo,
                 "grupoNombre": GRUPOS[grupo], "modulos": list(mods)}
                for cod, (nombre, grupo, mods) in RUBROS.items()
            ],
            "catalogoModulos": [
                {"codigo": cod, "nombre": nombre, "categoria": cat,
                 "nucleo": cod in NUCLEO, "disponible": disp,
                 # `descripcion` es lo que el dueño lee para decidir; `requiere` deja que la
                 # UI explique por qué encender Kardex encendió también Stock perpetuo.
                 "descripcion": desc,
                 "requiere": list(DEPENDENCIAS.get(cod, ()))}
                for cod, (nombre, cat, disp, desc) in MODULOS.items()
            ],
            "rubros": _json_load(company.l10n_pe_ne_rubros, []),
            "overrides": _json_load(company.l10n_pe_ne_modulos_override, {}),
            # Quién puede editar lo decide el MISMO gate que corta el guardado, y viaja al
            # cliente. Antes la SPA reimplementaba la regla a partir de los flags del perfil
            # y lo hacía en sentido permisivo (`puedeSupervisar !== false` es true cuando el
            # campo no viene): un usuario sin permiso veía los controles habilitados y solo
            # descubría el muro al guardar. Duplicar una regla de permisos es pedir que se
            # desincronice; esto la deja en un solo sitio.
            "puedeEditar": self._l10n_pe_ne_puede_config_rubro(),
            "modulos": sorted(efectivos) if efectivos is not None else None,
            # Fase 4: módulos con historia real — la UI los marca «en uso» y el guardado los
            # protege con override automático si el rubro elegido no los trae.
            "enUso": sorted(company.l10n_pe_ne_modulos_en_uso()),
        }
        # Fase 3 · analítica de adopción (solo admin de plataforma): cuántas empresas del
        # servidor usan cada rubro y qué módulos se activan más a mano por override. Es el
        # dato para decidir qué opcional «se gana» el default de su rubro (spec, fase 3).
        user = self.env.user
        if user.has_group("base.group_system") or user.has_group("base.group_erp_manager"):
            por_rubro, por_override = {}, {}
            for c in self.env["res.company"].sudo().search([]):
                for r in _json_load(c.l10n_pe_ne_rubros, []):
                    if r in RUBROS:
                        por_rubro[r] = por_rubro.get(r, 0) + 1
                for cod, on in _json_load(c.l10n_pe_ne_modulos_override, {}).items():
                    if on and cod in MODULOS:
                        por_override[cod] = por_override.get(cod, 0) + 1
            out["adopcion"] = {"empresasPorRubro": por_rubro,
                               "overridesActivados": por_override}
        return out

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
        desconocidos = [r for r in rubros if r not in RUBROS]
        if desconocidos:
            raise UserError(_("Rubro desconocido: %s") % ", ".join(desconocidos))

        company = self.env.company
        # ── Semántica de `overrides`: AUSENTE ≠ VACÍO ────────────────────────
        # Ausente  → el llamador solo cambia de rubro: se CONSERVAN los ajustes manuales
        #            que el dueño ya había hecho. Antes se reemplazaba por {} y cambiar de
        #            tipo de negocio borraba en silencio todo lo activado a mano.
        # Presente → es el estado autoritativo del ajuste fino (la pantalla manda el dict
        #            completo, y quitar una clave es cómo se vuelve al default del rubro).
        overrides = payload.get("overrides")
        if overrides is None:
            overrides = _json_load(company.l10n_pe_ne_modulos_override, {})
        malos = [c for c in overrides if c not in MODULOS]
        if malos:
            raise UserError(_("Módulo desconocido: %s") % ", ".join(malos))
        # Un override sobre el núcleo no tiene efecto (l10n_pe_ne_modulos_efectivos lo ignora):
        # se descarta aquí para no persistir una intención falsa que además la bitácora
        # rendería como «apagó Factura electrónica» — una traza de auditoría mintiendo.
        overrides = {c: bool(v) for c, v in overrides.items() if c not in NUCLEO}
        # Fase 4 · protección de lo EN USO (spec: «con todos sus módulos actualmente en uso
        # marcados como activos (override), para que la migración nunca les oculte algo que ya
        # estaban usando»): si la selección dejaría fuera un módulo con historia real, se
        # enciende su override automáticamente. Elegir «Bodega» no puede empezar a rechazar
        # las detracciones que la empresa emite hace meses. Un override en False EXPLÍCITO del
        # payload se respeta (apagarlo a sabiendas es una decisión, no un accidente).
        protegidos = []
        if rubros:
            activos = set(NUCLEO)
            for r in rubros:
                activos.update(RUBROS[r][2])
            for cod, on in overrides.items():
                if on:
                    activos.add(cod)
                elif cod not in NUCLEO:
                    activos.discard(cod)
            efectivos_sim = _con_dependencias(activos) & _DISPONIBLES
            en_uso = company.l10n_pe_ne_modulos_en_uso() & _DISPONIBLES
            protegidos = sorted(cod for cod in en_uso - efectivos_sim
                                if overrides.get(cod) is not False)
            for cod in protegidos:
                overrides[cod] = True
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
        # Capa 1.5: al (re)elegir rubro se siembran los CATÁLOGOS (unidades/medios/afectaciones/
        # monedas) si el negocio aún no los configuró — nace configurado, el dueño solo ajusta.
        conservados = None
        if rubros and payload.get("aplicarCatalogos"):
            # Cambio de TIPO DE NEGOCIO explícito: re-siembra los catálogos a la sugerencia del
            # nuevo rubro, FUSIONANDO lo intocable (personalizados, en-uso, defaults vigentes).
            conservados = company._l10n_pe_ne_resembrar_catalogos(rubros)
        elif not rubros and payload.get("aplicarCatalogos"):
            # «Todos — sin restricción»: el camino de vuelta. Módulos quedan legacy (rubros
            # vacíos = sin gating) y los catálogos pasan a TODO el maestro, preservando los
            # medios personalizados del negocio.
            antes_cfg = company._l10n_pe_ne_cfg() or {}
            todo = company._l10n_pe_ne_catalogos_todos()
            if antes_cfg != todo:
                company.sudo().l10n_pe_ne_cfg_catalogos = json.dumps(todo, ensure_ascii=False)
                self.env["l10n_pe_ne.rubro_auditoria"].sudo().create({
                    "company_id": company.id, "user_id": self.env.user.id,
                    "campo": "catalogos(todos)",
                    "antes": json.dumps(antes_cfg, ensure_ascii=False),
                    "despues": json.dumps(todo, ensure_ascii=False)})
            conservados = {"unidades": [], "medios": [], "afectaciones": [], "monedas": []}
        elif rubros and company._l10n_pe_ne_sembrar_catalogos():
            self.env["l10n_pe_ne.rubro_auditoria"].sudo().create({
                "company_id": company.id, "user_id": self.env.user.id,
                "campo": "catalogos(siembra)", "antes": "",
                "despues": ",".join(rubros)})
        out = self.l10n_pe_ne_rubro_config()
        out["protegidos"] = protegidos   # la SPA avisa qué se mantuvo activo por estar en uso
        if conservados is not None:
            out["catalogosConservados"] = conservados
        return out

    @api.model
    def l10n_pe_ne_salud(self):
        """B · «Salud del negocio»: checklist de completitud de la configuración, calculado
        server-side en una pasada. Cada ítem trae ok + a dónde ir a resolverlo (ruta SPA).
        No es un bloqueo: es la guía del dueño para dejar el negocio fino."""
        company = self.env.company
        p = company.partner_id
        rubro_ok = bool([r for r in _json_load(company.l10n_pe_ne_rubros, []) if r in RUBROS])
        # «Series declaradas POR LOCAL»: el check debe verificar lo que su etiqueta promete.
        # Antes era un search global limit=1: con dos locales y una sola serie daba ✓, y el
        # segundo local descubría que no podía emitir recién al intentarlo.
        Serie = self.env["l10n_pe_ne.serie"].sudo()
        locales = self.env["l10n_pe_ne.establecimiento"].sudo().search(
            [("company_id", "=", company.id), ("active", "=", True)])
        if locales:
            sin_serie = [loc for loc in locales
                         if not any(s.activa for s in loc.serie_ids)]
            series_ok = not sin_serie
            series_detalle = ("Qué serie emite cada local (numeración fiscal)" if series_ok
                              else "Sin serie activa: %s" % ", ".join(
                                  loc.codigo or loc.direccion or "local" for loc in sin_serie[:3]))
        else:
            # Negocio de un solo punto de emisión: aún no declaró establecimientos anexos.
            series_ok = bool(Serie.search(
                [("company_id", "=", company.id), ("activa", "=", True)], limit=1))
            series_detalle = "Qué serie emite tu negocio (numeración fiscal)"
        productos_ok = bool(self.env["product.template"].sudo().search(
            [("company_id", "in", (company.id, False)), ("sale_ok", "=", True)], limit=1))
        items = [
            {"clave": "rubro", "titulo": "Tipo de negocio elegido", "ok": rubro_ok,
             "detalle": "El menú y los catálogos se enfocan en tu rubro",
             "ruta": "/configuracion"},
            {"clave": "series", "titulo": "Series declaradas por local", "ok": series_ok,
             "detalle": series_detalle,
             "ruta": "/series"},
            {"clave": "logo", "titulo": "Logo del negocio", "ok": bool(company.logo),
             "detalle": "Sale en el ticket y la representación A4",
             "ruta": "/negocio"},
            {"clave": "datosPago", "titulo": "Cuentas para tus clientes",
             "ok": bool((company.l10n_pe_ne_datos_pago or "").strip()),
             "detalle": "Cuentas/CCI que se imprimen en cotizaciones y comprobantes",
             "ruta": "/negocio"},
            {"clave": "direccion", "titulo": "Dirección fiscal completa",
             "ok": bool((p.street or "").strip() and p.l10n_pe_district),
             "detalle": "Va en el bloque emisor del XML a SUNAT",
             "ruta": "/negocio"},
            {"clave": "productos", "titulo": "Catálogo con productos", "ok": productos_ok,
             "detalle": "Al menos un producto o servicio para vender",
             "ruta": "/productos"},
        ]
        hechos = sum(1 for i in items if i["ok"])
        return {"items": items, "hechos": hechos, "total": len(items),
                "pct": round(hechos * 100 / len(items))}

    @api.model
    def l10n_pe_ne_auditoria_list(self, limit=25):
        """C · Historial de configuración, legible: quién cambió qué y cuándo (rubro, módulos,
        catálogos, roles, rechazos del muro). Solo quien puede configurar lo ve — es la
        traza de decisiones del negocio."""
        if not self._l10n_pe_ne_puede_config_rubro():
            raise AccessError(_("Solo el dueño o supervisor puede ver el historial de configuración."))
        filas = self.env["l10n_pe_ne.rubro_auditoria"].sudo().search(
            [("company_id", "=", self.env.company.id)], limit=int(limit or 25))
        out = []
        for f in filas:
            campo = f.campo or ""
            if campo == "rubros":
                antes = [RUBROS.get(r, (r,))[0] for r in _json_load(f.antes, [])]
                despues = [RUBROS.get(r, (r,))[0] for r in _json_load(f.despues, [])]
                titulo = "Cambio de tipo de negocio"
                resumen = "%s → %s" % (", ".join(antes) or "Todos", ", ".join(despues) or "Todos")
            elif campo == "overrides":
                titulo = "Módulos ajustados a mano"
                despues = _json_load(f.despues, {})
                on = [MODULOS.get(c, (c,))[0] for c, v in despues.items() if v]
                off = [MODULOS.get(c, (c,))[0] for c, v in despues.items() if not v]
                partes = []
                if on:
                    partes.append("activó " + ", ".join(on))
                if off:
                    partes.append("apagó " + ", ".join(off))
                resumen = "; ".join(partes) or "sin overrides"
            elif campo.startswith("catalogos"):
                titulo = {"catalogos": "Catálogos ajustados",
                          "catalogos(siembra)": "Catálogos sembrados por el rubro",
                          "catalogos(resembrado)": "Catálogos re-armados por cambio de tipo",
                          "catalogos(todos)": "Catálogos abiertos a todo (sin restricción)",
                          }.get(campo, "Catálogos")
                resumen = ""
            elif campo.startswith("roles:"):
                titulo = "Roles de %s" % campo.split(":", 1)[1]
                resumen = "%s → %s" % (f.antes or "—", f.despues or "—")
            elif campo.startswith("rechazo:"):
                cod = campo.split(":", 1)[1]
                titulo = "Rechazo de seguridad (muro)"
                resumen = "intento de usar %s sin el módulo activo" % MODULOS.get(cod, (cod,))[0]
            else:
                titulo = campo
                resumen = ""
            out.append({
                "id": f.id, "titulo": titulo, "resumen": resumen,
                "usuario": f.user_id.name or "—",
                "fecha": fields.Datetime.to_string(f.create_date),
                "esRechazo": campo.startswith("rechazo:"),
            })
        return out

    @api.model
    def l10n_pe_ne_rubro_preview(self, payload):
        """P3 · Preview del cambio de tipo de negocio (SIN escribir nada): qué módulos entran
        y salen, qué queda protegido por estar en uso, y cómo quedarían los catálogos.
        La SPA lo muestra ANTES del «Aplicar» — el select cambia todo, pero con los ojos
        abiertos."""
        payload = payload or {}
        rubros = [r for r in (payload.get("rubros") or []) if r in RUBROS]
        company = self.env.company

        # «Todos — sin restricción»: preview del camino de vuelta (rubros vacíos).
        if not rubros:
            actuales = company.l10n_pe_ne_modulos_efectivos()
            base_actual = actuales if actuales is not None else set(MODULOS) & _DISPONIBLES
            todos = set(MODULOS) & _DISPONIBLES
            return {
                "rubros": [],
                "todos": True,
                "legacyAntes": actuales is None,
                "modulos": {
                    "entran": [{"codigo": c, "nombre": MODULOS[c][0]} for c in sorted(todos - base_actual)],
                    "salen": [],
                    "protegidos": [],
                    "total": len(todos),
                },
                "efectivos": sorted(todos),
                "catalogos": company._l10n_pe_ne_catalogos_todos(),
                "catalogosConservados": {"unidades": [], "medios": [], "afectaciones": [], "monedas": []},
                # «Todos» ABRE el catálogo completo y preserva los medios del negocio: por
                # definición no retira nada. Va explícito para que la SPA no tenga que
                # distinguir entre «no retira nada» y «este backend no me lo dice».
                "catalogosRetirados": {"unidades": [], "medios": [], "afectaciones": [], "monedas": []},
            }

        actuales = company.l10n_pe_ne_modulos_efectivos()   # None = legacy (ve todo)
        nuevos = set(NUCLEO)
        for r in rubros:
            nuevos.update(RUBROS[r][2])
        # Los overrides YA guardados sobreviven al cambio de rubro (ver l10n_pe_ne_set_rubro),
        # así que el preview debe contarlos o mentiría sobre lo que va a salir.
        for cod, on in _json_load(company.l10n_pe_ne_modulos_override, {}).items():
            if cod not in MODULOS:
                continue
            if on:
                nuevos.add(cod)
            elif cod not in NUCLEO:
                nuevos.discard(cod)
        nuevos = _con_dependencias(nuevos) & _DISPONIBLES
        en_uso = company.l10n_pe_ne_modulos_en_uso() & _DISPONIBLES
        # MISMO criterio que l10n_pe_ne_set_rubro: un override en False EXPLÍCITO se respeta
        # y NO se protege (apagar algo a sabiendas es una decisión). Sin este filtro el
        # preview pintaba como «conservado» un módulo que el aplicar sí iba a quitar —
        # mentía exactamente sobre la promesa que vende.
        overrides_guardados = _json_load(company.l10n_pe_ne_modulos_override, {})
        protegidos = sorted(cod for cod in en_uso - nuevos
                            if overrides_guardados.get(cod) is not False)
        efectivos_nuevos = _con_dependencias(nuevos | set(protegidos)) & _DISPONIBLES

        def _nombres(cods):
            return [{"codigo": c, "nombre": MODULOS[c][0]} for c in sorted(cods)]

        base_actual = actuales if actuales is not None else set(MODULOS) & _DISPONIBLES
        cfg_nueva, conservados, retirados = company._l10n_pe_ne_calc_resiembra(rubros)
        return {
            "rubros": rubros,
            "legacyAntes": actuales is None,   # antes veía TODO (sin rubro configurado)
            "modulos": {
                "entran": _nombres(efectivos_nuevos - base_actual),
                "salen": _nombres(base_actual - efectivos_nuevos),
                "protegidos": _nombres(protegidos),
                "total": len(efectivos_nuevos),
            },
            "efectivos": sorted(efectivos_nuevos),
            "catalogos": cfg_nueva,
            "catalogosConservados": conservados,
            # Lo que la empresa PIERDE en catálogos al aplicar. La re-siembra pisa el JSON
            # entero y no hay deshacer, así que esto tiene que verse ANTES de aplicar.
            "catalogosRetirados": retirados,
        }

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
