from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    """Re-aplica la precisión de 3 decimales en cantidad de UoM.

    NO es un duplicado de migrations/19.0.1.21.0/post-precision-uom.py: es su REPARACIÓN.
    Las series por sucursal y la precisión de UoM salieron en dos ramas a la vez y AMBAS
    numeraron su migración 19.0.1.21.0. Git no ve ese choque (son archivos con nombres
    distintos en el mismo directorio, así que mergea limpio), pero Odoo sí: solo corre los
    scripts con `instalado < versión <= nueva`, de modo que un tenant que ya había subido a
    19.0.1.21.0 con la rama de sucursales NUNCA ejecuta el 19.0.1.21.0 de la otra — se queda
    con la precisión por defecto (2) para siempre y en silencio.

    El síntoma es de inventario, no de pantalla: vender 1.625 kg descuenta 1.62 y el kardex
    se va en falso ~5 g por venta, justo en las verticales (balanza, ferretería, maderera)
    donde la cantidad fraccionaria es el negocio.

    Idempotente y sin riesgo de pisar nada: `set_uom_precision_3` solo SUBE la precisión
    (`if prec.digits < 3`), así que para quien ya la tenía en 3 —o la subió a más— esto no
    hace absolutamente nada.
    """
    from odoo.addons.l10n_pe_ne_biller.hooks import set_uom_precision_3
    env = api.Environment(cr, SUPERUSER_ID, {})
    set_uom_precision_3(env)
