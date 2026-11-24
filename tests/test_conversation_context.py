import unittest

from app import focused_prior_answer, needs_previous_turn


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


if __name__ == "__main__":
    unittest.main()
