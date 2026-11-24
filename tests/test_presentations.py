import unittest

from presentations import _chart_data, _chart_slide, _format_value


class PresentationChartTests(unittest.TestCase):
    def test_sales_table_generates_editable_bars_with_spanish_labels(self):
        evidence = [{"agent": "Ventas Oribay", "tables": [
            "| Mes | Unidades | Importe_EUR |\n"
            "| :--- | ---: | ---: |\n"
            "| 2026-08 | 1592320 | 2083849.69 |\n"
            "| 2026-09 | 2269855 | 2679652.73 |\n"
            "| 2026-10 | 2354577 | 2489674.80 |"]}]
        chart = _chart_data(evidence)
        self.assertEqual(chart[1], "€")
        self.assertEqual(chart[2][0], ("2026-08", 2083849.69))
        requests = _chart_slide("page_1", {"title": "Facturación mensual"}, evidence, 1)
        self.assertEqual(sum("createShape" in request and
                             request["createShape"]["objectId"].startswith("oribay_1_bar_")
                             for request in requests), 3)
        self.assertTrue(any(request.get("insertText", {}).get("text") == "2,08 M€" for request in requests))

    def test_missing_or_single_row_stays_as_cards(self):
        evidence = [{"agent": "Aplicación Adhesiva", "tables": [
            "| maquina | pct oee |\n| :--- | ---: |\n| RB4 | 54,51 |"]}]
        self.assertIsNone(_chart_data(evidence))
        self.assertEqual(_format_value(54.51, "%"), "54,51 %")


if __name__ == "__main__":
    unittest.main()
