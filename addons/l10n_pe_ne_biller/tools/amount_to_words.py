"""Conversión de números a letras en español (mayúsculas), rango 0–999999."""

UNIDADES = ['', 'UNO', 'DOS', 'TRES', 'CUATRO', 'CINCO', 'SEIS', 'SIETE', 'OCHO',
            'NUEVE', 'DIEZ', 'ONCE', 'DOCE', 'TRECE', 'CATORCE', 'QUINCE',
            'DIECISEIS', 'DIECISIETE', 'DIECIOCHO', 'DIECINUEVE', 'VEINTE']
DECENAS = ['', '', 'VEINTE', 'TREINTA', 'CUARENTA', 'CINCUENTA', 'SESENTA',
           'SETENTA', 'OCHENTA', 'NOVENTA']
CENTENAS = ['', 'CIENTO', 'DOSCIENTOS', 'TRESCIENTOS', 'CUATROCIENTOS', 'QUINIENTOS',
            'SEISCIENTOS', 'SETECIENTOS', 'OCHOCIENTOS', 'NOVECIENTOS']


def _decenas(n):
    if n <= 20:
        return UNIDADES[n]
    if n < 30:
        return 'VEINTI' + UNIDADES[n - 20]
    d, u = divmod(n, 10)
    out = DECENAS[d]
    if u:
        out += ' Y ' + UNIDADES[u]
    return out


def _centenas(n):
    if n == 0:
        return ''
    if n == 100:
        return 'CIEN'
    c, r = divmod(n, 100)
    out = CENTENAS[c] if c else ''
    if r:
        out = (out + ' ' if out else '') + _decenas(r)
    return out


def numero_a_letras(entero):
    entero = int(entero)
    if entero == 0:
        return 'CERO'
    if entero < 1000:
        return _centenas(entero)
    miles, resto = divmod(entero, 1000)
    pal = 'MIL' if miles == 1 else _centenas(miles) + ' MIL'
    if resto:
        pal += ' ' + _centenas(resto)
    return pal


def leyenda_monto(monto, moneda='SOLES'):
    entero = int(monto)
    centimos = int(round((float(monto) - entero) * 100))
    return '%s CON %02d/100 %s' % (numero_a_letras(entero), centimos, moneda)
