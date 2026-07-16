class EnvioSincronoMixin:
    """Fija el camino de envío que ejercen los tests: el SÍNCRONO.

    `action_l10n_pe_send_to_biller` tiene tres caminos y elige por config param:

      * async_enabled=1   → encola en SQS y sale;
      * instant_enabled=1 → POST a /firmar y lee la respuesta como JSON (resp.json());
      * ninguno (default) → POST al endpoint y lee el XML firmado del body (resp.text)
                            con el CDR en el header X-Sunat-Cdr.

    Los tests doblan `requests.post` con una respuesta que expone `text`/`headers`, o sea
    que están escritos para el tercero. Sin fijar los params, el camino lo decidía la BD
    donde corrieran: en una BD de dev con instant_enabled=1 el doble no tiene `.json()` y
    reventaban 23 tests de golpe, sin que nada hubiera cambiado en el código.

    Fijarlo aquí los vuelve herméticos: dicen qué camino prueban en vez de heredarlo.
    TransactionCase revierte el set_param al terminar, así que no ensucia la BD; y un test
    que quiera otro camino puede sobrescribirlo (lo hace test_masivo con async_enabled).
    """

    def setUp(self):
        super().setUp()
        icp = self.env["ir.config_parameter"].sudo()
        icp.set_param("l10n_pe_ne_biller.instant_enabled", "0")
        icp.set_param("l10n_pe_ne_biller.async_enabled", "0")
