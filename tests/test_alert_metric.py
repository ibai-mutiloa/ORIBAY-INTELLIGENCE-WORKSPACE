import unittest

from app import alert_question, parse_scalar_metric


class AlertMetricTests(unittest.TestCase):
    def test_monthly_alert_means_current_month_to_latest_available_data(self):
        question = alert_question({"alert_metric": "facturación", "alert_period": "mensual"})
        self.assertIn("mes natural en curso", question)
        self.assertIn("fecha más reciente con datos", question)
        self.assertIn("No incluyas unidades vendidas", question)

    def test_selects_requested_billing_column_from_row_with_units(self):
        table = (
            "| Mes | Unidades | Facturación (€) |\n"
            "| --- | ---: | ---: |\n"
            "| 2026-10 | 2.354.577 | 2.489.674,80 € |"
        )
        self.assertEqual(parse_scalar_metric([table], "facturación mensual"),
                         (2489674.8, "Facturación (€)"))

    def test_selects_units_when_units_are_requested(self):
        table = (
            "| Mes | Unidades | Facturación (€) |\n"
            "| --- | ---: | ---: |\n"
            "| 2026-10 | 2.354.577 | 2.489.674,80 € |"
        )
        self.assertEqual(parse_scalar_metric([table], "unidades"),
                         (2354577.0, "Unidades"))

    def test_keeps_ambiguous_result_blocked_without_requested_metric(self):
        table = (
            "| Mes | Unidades | Facturación (€) |\n"
            "| --- | ---: | ---: |\n"
            "| 2026-10 | 2.354.577 | 2.489.674,80 € |"
        )
        self.assertEqual(parse_scalar_metric([table]), (None, ""))


if __name__ == "__main__":
    unittest.main()
