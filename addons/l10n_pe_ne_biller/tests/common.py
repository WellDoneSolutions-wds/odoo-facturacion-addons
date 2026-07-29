class L10nPeSeedMixin:
    """Fundación del harness de validación (L2): siembra el entorno fiscal peruano MÍNIMO para
    que los tests corran en CUALQUIER base de datos —local incluida—, no solo en el CI que ya
    trae el plan contable l10n_pe cargado. Deja listo:

      * el RUC de la compañía (varios flujos —comunicación de baja, PLE, reporte— lo exigen);
      * el IGV de venta (cat-05 1000) en ``self.igv`` — sin él una línea gravada revienta con
        el rechazo 3111 (TaxableAmount>0 + TaxAmount 0.00).

    Las demás afectaciones (exonerado 9997 / inafecto 9998 / exportación 9995 / gratuito 9996)
    y el ICBPER/ISC se autocrean on-demand (``_l10n_pe_ne_tax_by_code`` / ``_ensure_*``), así que
    no hace falta sembrarlas aquí. Idempotente: si la compañía ya tiene RUC o el IGV (p.ej. en
    CI), NO los toca. Es la base sobre la que un harness por vertical arma sus casos.

    Uso: heredar ANTES de TransactionCase, p.ej.
        class TestX(L10nPeSeedMixin, TransactionCase): ...
    y usar ``self.igv`` sin volver a buscarlo.
    """

    # RUC de prueba distinto del partner-cliente típico ('20100070970') para no colisionar vats.
    L10N_PE_TEST_RUC = "20100190797"

    def setUp(self):
        super().setUp()
        company = self.env.company
        if not (company.vat or "").strip():
            company.sudo().vat = self.L10N_PE_TEST_RUC
        Tax = self.env["account.tax"].sudo()
        self.igv = Tax.search([
            ("company_id", "=", company.id), ("type_tax_use", "=", "sale"),
            ("l10n_pe_edi_tax_code", "=", "1000")], limit=1)
        if not self.igv:
            self.igv = Tax.create({
                "name": "IGV 18% (test)", "amount_type": "percent", "amount": 18.0,
                "type_tax_use": "sale", "l10n_pe_edi_tax_code": "1000",
                "company_id": company.id})


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
