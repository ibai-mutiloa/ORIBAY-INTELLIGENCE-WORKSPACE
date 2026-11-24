import unittest
from unittest.mock import MagicMock, patch

from app import (app, bounded_conversation_artifacts, conversation_recent_agent_turns, exceeds_requested_top,
                 focused_prior_answer, followup_source, memory_evidence, missing_report_evidence,
                 needs_previous_turn, reconcile_top_two_share, save_conversation_result, updated_scope)
from router import decide, followup_agent, requested_intent, _fallback


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

    def test_visual_followups_recover_original_top_five(self):
        turns = [
            {"route": "ventas", "question": "top 5 de piezas mas vendidas en 2026 vs sus ventas en 2025",
             "answer": "Cinco piezas con ventas comparadas."},
            {"route": "ventas", "question": "haz un grafico visual",
             "answer": "¿Qué datos te gustaría visualizar?"},
            {"route": "ventas", "question": "el top de piezas que vimos antes",
             "answer": "Top 10 de piezas en 2026."},
        ]
        self.assertTrue(needs_previous_turn("haz un grafico visual"))
        self.assertEqual(followup_source("genera un google slides con estos datos", turns)["question"],
                         turns[0]["question"])
        self.assertIsNone(followup_source("muestra ventas de la pieza 191", turns))
        table = "| Artículo | Importe |\n| --- | ---: |\n" + "\n".join(
            f"| P{i} | {i} |" for i in range(10))
        self.assertTrue(exceeds_requested_top(turns[0]["question"], [table]))

    def test_visual_chart_cannot_be_routed_to_pdf(self):
        client = MagicMock()
        client.models.generate_content.return_value.text = (
            '{"route":"pdf","data_agent":"ventas","data_question":"haz un gráfico visual"}')
        with patch("router.Client", return_value=client):
            plan = decide("haz un gráfico visual", [{"route": "ventas", "question": "top 5 piezas", "answer": "datos"}])
        self.assertEqual(plan["route"], "ventas")
        self.assertTrue(needs_previous_turn("haz un gráfico visual"))
        self.assertTrue(needs_previous_turn("haz un grafico visual"))

    def test_material_reference_followup_stays_in_sales(self):
        first = {"route": "ventas", "question": "Qué preveemos vender de la pieza 24 en acero inoxidable y zamak",
                 "answer": "Zamak y acero inoxidable por año"}
        request = "puedes desglosar las referencias que has considerado en zamak y en acero inoxidable?"
        self.assertTrue(needs_previous_turn(request))
        self.assertEqual(followup_source(request, [first])["question"], first["question"])
        self.assertEqual(followup_agent(request, [first]), "ventas")
        self.assertEqual(_fallback(request, [first])["route"], "ventas")
        client = MagicMock()
        client.models.generate_content.return_value.text = (
            '{"route":"adhesiva","data_agent":"adhesiva","data_question":"referencias de materiales"}')
        with patch("router.Client", return_value=client):
            plan = decide(request, [first])
        self.assertEqual((plan["route"], plan["data_agent"]), ("ventas", "ventas"))

    def test_explicit_domain_switch_is_respected(self):
        history = [{"route": "ventas", "question": "Ventas de pieza 24", "answer": "datos"}]
        self.assertIsNone(followup_agent("Desglosa su OEE en la máquina RB4", history))

    def test_intent_and_agent_are_separate(self):
        history = [{"route": "adhesiva", "question": "OEE de RB4 semana 22", "answer": "54,51 %"}]
        self.assertEqual(requested_intent("Haz un PDF sobre ese resultado"), "pdf")
        self.assertEqual(_fallback("Haz un PDF sobre ese resultado", history)["data_agent"], "adhesiva")
        self.assertEqual(requested_intent("Haz un gráfico visual"), "consulta")
        self.assertEqual(_fallback("Haz un gráfico visual", history)["route"], "adhesiva")

    def test_data_only_summary_cannot_become_email(self):
        client = MagicMock()
        client.models.generate_content.return_value.text = '{"route":"correo","data_agent":"ventas"}'
        with patch("router.Client", return_value=client):
            plan = decide("Resume la facturación de los últimos tres meses completos", allowed_agents=["ventas"])
        self.assertEqual(plan["route"], "ventas")

    def test_structured_context_preserves_original_scope_across_followups(self):
        ref = MagicMock()
        with patch("app.conversation_message") as write:
            save_conversation_result(ref, "desglosa esas referencias", {
                "route": "ventas", "data_agent": "ventas", "answer": "Referencias consultadas",
                "plan": {"followup_source": "top 5 de ventas AC032 en 2026 frente a 2025"}})
        context = write.call_args.kwargs["context"]
        self.assertEqual(context["top_n"], 5)
        self.assertEqual(context["years"], ["2026", "2025"])
        turns = [{"route": "ventas", "question": "desglosa esas referencias", "answer": "datos", "context": context}]
        self.assertEqual(followup_source("haz un gráfico visual", turns)["question"], context["source_question"])

    def test_refinement_survives_another_followup(self):
        source = "Top 5 de piezas vendidas en 2026"
        refinement = "solo en España"
        self.assertTrue(needs_previous_turn(refinement))
        scope = updated_scope(source, refinement)
        self.assertIn("España", scope)
        turn = {"route": "ventas", "question": refinement, "answer": "Cinco piezas españolas",
                "context": {"source_question": scope, "anchored": True}}
        self.assertEqual(followup_source("haz un gráfico visual", [turn])["question"], scope)
        self.assertEqual(updated_scope(scope, "haz un gráfico visual"), scope)

    def test_memory_keeps_source_facts_and_tables_not_document_copy(self):
        result = {"answer": "PDF redactado", "source_answer": "Ventas verificadas",
                  "tables": ["| Pieza | Importe |\n| --- | --- |\n| AC032 | 100 € |"]}
        evidence = memory_evidence(result)
        self.assertIn("AC032", evidence)
        self.assertIn("Ventas verificadas", evidence)
        self.assertNotIn("PDF redactado", evidence)

    def test_conversation_message_persists_bounded_visuals_and_sql(self):
        ref = MagicMock()
        result = {"route": "ventas", "data_agent": "ventas", "answer": "Resultado",
                  "charts": [{"image": "x" * 600_000}, {"title": "Gráfico pequeño"}],
                  "tables": ["| Pieza | Importe |\n| --- | --- |\n| AC032 | 100 € |"],
                  "sql": ["SELECT 1"], "plan": {}}
        with patch("app.conversation_message") as write:
            save_conversation_result(ref, "Ventas de AC032", result)
        saved = write.call_args.kwargs
        self.assertEqual(saved["charts"], [])
        self.assertEqual(saved["tables"], result["tables"])
        self.assertEqual(saved["sql"], result["sql"])
        self.assertIn("AC032", saved["memory_evidence"])

    def test_memory_recovered_from_old_table_and_isolated_by_agent(self):
        items = [
            {"kind": "user", "question": "Top 5 de ventas"},
            {"kind": "assistant", "route": "ventas", "data_agent": "ventas", "answer": "Top 5",
             "tables": ["| Pieza | Importe |\n| --- | --- |\n| AC032 | 100 € |"]},
            {"kind": "user", "question": "OEE de RB4"},
            {"kind": "assistant", "route": "adhesiva", "data_agent": "adhesiva", "answer": "54,51 %"},
        ]
        ref = MagicMock()
        ref.collection.return_value.order_by.return_value.limit.return_value.stream.return_value = [
            type("Snapshot", (), {"to_dict": lambda self, item=item: item})() for item in reversed(items)]
        turns = conversation_recent_agent_turns(ref)
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["route"], "adhesiva")
        self.assertNotIn("AC032", turns[0]["answer"])

    def test_reopened_conversation_returns_saved_artifacts(self):
        message = {"kind": "assistant", "route": "ventas", "answer": "Ventas verificadas",
                   "charts": [{"title": "Ventas"}], "tables": ["| Mes | Importe |"],
                   "sql": ["SELECT 1"]}
        ref = MagicMock()
        ref.collection.return_value.order_by.return_value.stream.return_value = [
            type("Snapshot", (), {"to_dict": lambda self: message})()]
        with patch("app.owned_conversation", return_value=(ref, {"title": "Ventas"})):
            response = app.test_client().get("/api/conversations/test-id")
        self.assertEqual(response.status_code, 200)
        restored = response.get_json()["messages"][0]
        self.assertEqual(restored["charts"], message["charts"])
        self.assertEqual(restored["tables"], message["tables"])
        self.assertEqual(restored["sql"], message["sql"])

    def test_interleaved_requests_keep_their_own_questions(self):
        items = [
            {"kind": "user", "turn_id": "one", "question": "Ventas de pieza 24"},
            {"kind": "user", "turn_id": "two", "question": "OEE de RB4"},
            {"kind": "assistant", "turn_id": "two", "route": "adhesiva", "answer": "54 %"},
            {"kind": "assistant", "turn_id": "one", "route": "ventas", "answer": "100 €"},
        ]
        ref = MagicMock()
        ref.collection.return_value.order_by.return_value.limit.return_value.stream.return_value = [
            type("Snapshot", (), {"to_dict": lambda self, item=item: item})() for item in reversed(items)]
        turns = conversation_recent_agent_turns(ref)
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["question"], "Ventas de pieza 24")
        self.assertEqual(turns[0]["answer"], "100 €")

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
