import unittest

from app import focused_prior_answer, missing_report_evidence, needs_previous_turn, reconcile_top_two_share


class ConversationContextTests(unittest.TestCase):
    def test_independent_sales_question_does_not_inherit_previous_analysis(self):
        self.assertFalse(needs_previous_turn("¿A qué plantas de Saint-Gobain vendemos la pieza 191?"))

    def test_last_query_pdf_uses_only_matching_section(self):
        question = "Genera un PDF solo con las piezas de Saint-Gobain de la última consulta"
        previous = (
            "1. Piezas en tarifa para 106-VFX sin ventas en 2026\n"
            "La pieza `437SEVR` no se vendió a 106-VFX.\n\n"
            "2. Plantas de Saint-Gobain con ventas de la pieza 191\n"
            "Tailandia: `9L15H191PC0D06`. Estonia: `191EVR`."
        )
        self.assertTrue(needs_previous_turn(question))
        focused = focused_prior_answer(question, previous)
        self.assertIn("9L15H191PC0D06", focused)
        self.assertIn("191EVR", focused)
        self.assertNotIn("106-VFX", focused)

    def test_oee_pdf_followup_keeps_machine_and_week_context(self):
        self.assertTrue(needs_previous_turn(
            "Explícame ese resultado y prepara un PDF solo sobre esa máquina y semana"))

    def test_missing_agent_data_cannot_become_a_pdf(self):
        self.assertTrue(missing_report_evidence({
            "answer": "No se dispone de la información necesaria. Por favor, proporcione la máquina y la semana.",
            "tables": [], "charts": [],
        }))
        self.assertFalse(missing_report_evidence({
            "answer": "OEE agregado: 54,51 %", "tables": ["| OEE |\n| --- |\n| 54,51 |"], "charts": [],
        }))

    def test_false_top_two_client_share_is_recomputed_from_rows(self):
        table = ("| Cliente | Facturación (€) |\n| --- | --- |\n"
                 "| A | 47.428,78 € |\n| B | 22.154,89 € |\n"
                 "| C | 12.000,00 € |\n| D | 12.380,24 € |")
        answer = "Los dos principales clientes concentran más del **79%** de la facturación."
        self.assertIn("el **74,05 %**", reconcile_top_two_share(answer, [table]))


if __name__ == "__main__":
    unittest.main()
