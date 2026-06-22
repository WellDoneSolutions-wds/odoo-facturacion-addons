"""E2E Capa 1 — flujo por navegador (browser-harness) Odoo -> biller -> SUNAT beta.

Requisitos previos:
  - Biller corriendo en :8090   (cd ms-ne-biller && JAVA_HOME=<jdk17> ./gradlew bootRun)
  - Odoo Community en :8169      (odoo-bin -c config/odoo-community.conf -d odoo_ne_biller)
  - Factura posteada sembrada    (scripts/seed_e2e_data.py -> imprime E2E_MOVE_ID)

Conexión del navegador
----------------------
Chrome moderno (>=144) exige autorizar el remote debugging con un clic manual
("Allow") cuando browser-harness se adjunta al Chrome del usuario. Para un E2E
autónomo se usa la "Vía 2" del install.md: lanzar un Chrome AISLADO (perfil y
puerto propios), que no dispara ese popup, y apuntar el harness con BU_CDP_URL:

    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
        --remote-debugging-port=9333 --user-data-dir=/tmp/bh-chrome-e2e \
        --no-first-run --no-default-browser-check about:blank &

    export BU_CDP_URL=http://127.0.0.1:9333
    export BU_NAME=e2e

Flujo (reemplazar <ID> por E2E_MOVE_ID)
---------------------------------------
    browser-harness <<'PY'
    import time
    # 1) login admin/admin
    new_tab("http://localhost:8169/web/login"); wait_for_load(); time.sleep(1)
    js("document.querySelector('input[name=login]').value='admin';"
       "document.querySelector('input[name=password]').value='admin';"
       "document.querySelector('button[type=submit]').click();")
    time.sleep(5)
    # 2) abrir la factura (router nuevo de Odoo 19)
    goto_url("http://localhost:8169/odoo/action-account.action_move_out_invoice_type/<ID>")
    wait_for_load(); time.sleep(5)
    capture_screenshot()                       # estado "Por enviar" + botón visible
    # 3) ubicar y clickear "Enviar al Facturador"
    rect = js(r'''(() => {const b=[...document.querySelectorAll("button")]
        .find(x => (x.textContent||"").includes("Enviar al Facturador"));
        const r=b.getBoundingClientRect();
        return JSON.stringify({x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2)});})()''')
    import json; c = json.loads(rect)
    click_at_xy(c["x"], c["y"])
    time.sleep(12)                             # biller firma/valida y envía a SUNAT beta
    capture_screenshot()                       # estado "Enviado"
    PY

Aserción (determinística, vía odoo-bin shell)
---------------------------------------------
    m = env['account.move'].browse(<ID>)
    assert m.l10n_pe_biller_state == 'enviado'
    assert m.l10n_pe_biller_xml                 # UBL firmado adjunto
    # CDR de SUNAT lo guarda el biller en ms-ne-biller/.../RPTA/R<RUC>-01-<serie>-<corr>.zip
    # con <cbc:ResponseCode>0</cbc:ResponseCode> = aceptada.
"""
