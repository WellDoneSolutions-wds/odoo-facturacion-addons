"""Test puro (sin Odoo) del helper de monto en letras.

Vive fuera del paquete del addon a propósito: si estuviera dentro de
addons/l10n_pe_ne_biller/tests/, pytest importaría la cadena de __init__.py del
addon y dispararía el modelo Odoo fuera de runtime. Carga el módulo por ruta.
"""
import importlib.util
import pathlib

_p = (pathlib.Path(__file__).resolve().parent.parent
      / "addons" / "l10n_pe_ne_biller" / "tools" / "amount_to_words.py")
_spec = importlib.util.spec_from_file_location("amount_to_words", _p)
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)


def test_numero_a_letras():
    assert m.numero_a_letras(0) == 'CERO'
    assert m.numero_a_letras(10) == 'DIEZ'
    assert m.numero_a_letras(21) == 'VEINTIUNO'
    assert m.numero_a_letras(100) == 'CIEN'
    assert m.numero_a_letras(115) == 'CIENTO QUINCE'
    assert m.numero_a_letras(1000) == 'MIL'
    assert m.numero_a_letras(1234) == 'MIL DOSCIENTOS TREINTA Y CUATRO'


def test_leyenda_monto():
    assert m.leyenda_monto(8.50) == 'OCHO CON 50/100 SOLES'
    assert m.leyenda_monto(10.50) == 'DIEZ CON 50/100 SOLES'
