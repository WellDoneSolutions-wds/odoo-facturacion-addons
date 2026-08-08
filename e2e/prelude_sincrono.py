# -*- coding: utf-8 -*-
"""Prólogo del harness: fuerza el canal SÍNCRONO para poder verificar el CDR en el acto.

Uso (se CONCATENA delante del harness, no se ejecuta suelto):

    cat e2e/prelude_sincrono.py e2e/harness.py > /tmp/harness-sync.py
    docker exec -i -e E2E_CASES_FILE=/tmp/casos.json -e E2E_RESULTS_FILE=/tmp/res.json \
      <contenedor-odoo> odoo shell -d odoo_ne_biller \
      --db_host=db --db_user=odoo --db_password=odoo --http-port=8897 --gevent-port=8903 \
      --log-level=error < /tmp/harness-sync.py

POR QUÉ EXISTE
--------------
`action_l10n_pe_send_to_biller` elige camino por `ir.config_parameter`:

  * `async_enabled=1`    → encola en SQS y sale.
  * `instant_enabled=1`  → solo FIRMA; el envío real lo hace el cron
                           `_l10n_pe_cron_enviar_pendientes`.
  * ninguno (síncrono)   → firma + envía + lee el CDR del header `X-Sunat-Cdr`.

La BD de dev suele traer `instant_enabled='1'`. Con ese modo el harness reporta
«Firmado — ticket listo. Pendiente de envío a SUNAT» con `state=en_proceso` y `code=""`:
parece un fallo y no lo es, pero tampoco sirve para verificar CDR 0. Peor: el harness hace
`env.cr.rollback()` al final asumiendo que el envío ya ocurrió como efecto externo, así que
en modo instant el rollback llega ANTES de que SUNAT reciba nada.

POR QUÉ CONCATENADO Y NO UN `set_param` SUELTO
----------------------------------------------
Escrito así, el cambio de parámetros ocurre DENTRO de la misma transacción que el harness
revierte al terminar: la BD de dev queda exactamente como estaba (verificado: `instant`
vuelve a `'1'` solo). Un `odoo shell` aparte con commit dejaría el dev en síncrono y habría
que acordarse de restaurarlo a mano.
"""
icp = env['ir.config_parameter'].sudo()  # noqa: F821  (env lo provee odoo shell)
print('[prelude] ANTES  async=%r instant=%r' % (
    icp.get_param('l10n_pe_ne_biller.async_enabled'),
    icp.get_param('l10n_pe_ne_biller.instant_enabled')))
icp.set_param('l10n_pe_ne_biller.async_enabled', '0')
icp.set_param('l10n_pe_ne_biller.instant_enabled', '0')
print('[prelude] canal SÍNCRONO forzado para esta transacción')
