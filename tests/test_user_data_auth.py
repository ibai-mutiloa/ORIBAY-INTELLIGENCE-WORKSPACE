import json
import os
import importlib.util
import unittest
from pathlib import Path

from cryptography.fernet import Fernet

from user_data_auth import _extract_result, _fernet

_worker_path = Path(__file__).resolve().parents[1] / "worker" / "user_data_auth.py"
_worker_spec = importlib.util.spec_from_file_location("worker_user_data_auth", _worker_path)
_worker_auth = importlib.util.module_from_spec(_worker_spec)
_worker_spec.loader.exec_module(_worker_auth)


class UserDataAuthTests(unittest.TestCase):
    def test_worker_extracts_current_conversational_analytics_messages(self):
        def item(message):
            return {"type": "text", "text": json.dumps({"systemMessage": message})}

        result = _worker_auth._extract_result({"content": [
            item({"text": {"textType": "PROGRESS", "parts": ["Ejecutando"]}}),
            item({"data": {"generatedSql": "SELECT 1 AS Importe_EUR"}}),
            item({"data": {"result": {"data": [
                {"Mes": "2026-01", "Importe_EUR": "12,50"},
                {"Mes": "2026-02", "Importe_EUR": "15,00"},
            ]}}}),
            item({"text": {"textType": "FINAL_RESPONSE", "parts": ["Resumen final"]}}),
        ]}, "gráfico de barras")

        self.assertEqual(result["answer"], "Resumen final")
        self.assertEqual(result["sql"], ["SELECT 1 AS Importe_EUR"])
        self.assertEqual(len(result["tables"]), 1)
        self.assertEqual(result["charts"][0]["data"]["values"][1]["Importe_EUR"], 15.0)

    def test_mcp_response_keeps_only_final_answer_and_structured_data(self):
        def item(message):
            return {"type": "text", "text": json.dumps({"systemMessage": message})}
        response = {"content": [
            item({"text": {"textType": "PROGRESS", "parts": ["Running a query"]}}),
            item({"data": {"generatedSql": "SELECT 12 AS importe"}}),
            item({"data": {"result": {"data": [{"importe": 12}]}}}),
            item({"text": {"textType": "FINAL_RESPONSE", "parts": ["**Importe:** 12 €"]}}),
            item({"text": {"textType": "FOLLOWUP_QUESTIONS", "parts": ["Desglosar"]}}),
        ]}
        result = _extract_result(response)
        self.assertEqual(result["answer"], "**Importe:** 12 €")
        self.assertEqual(result["tables"], ["| importe |\n| --- |\n| 12 |"])
        self.assertEqual(result["sql"], ["SELECT 12 AS importe"])

    def test_requested_chart_uses_returned_rows_when_agent_omits_chart(self):
        def item(message):
            return {"type": "text", "text": json.dumps({"systemMessage": message})}
        response = {"content": [
            item({"data": {"result": {"data": [
                {"Mes": "Agosto", "Importe_EUR": "2.083.849,69 €"},
                {"Mes": "Septiembre", "Importe_EUR": "2679652.73"},
            ]}}}),
            item({"text": {"textType": "FINAL_RESPONSE", "parts": ["Resumen"]}}),
        ]}
        result = _extract_result(response, "Muestra un gráfico de barras")
        self.assertGreaterEqual(result["charts"][0]["width"], 600)
        self.assertEqual(result["charts"][0]["data"]["values"], [
            {"Mes": "Agosto", "Importe_EUR": 2083849.69},
            {"Mes": "Septiembre", "Importe_EUR": 2679652.73},
        ])

    def test_extracts_data_agent_answer_table_sql_and_chart(self):
        chart = {"mark": "bar", "data": {"values": [{"month": "2026-08", "value": 12}]}}
        response = {"content": [
            {"type": "text", "text": json.dumps({"artifactUpdate": {"artifact": {
                "name": "Running a query", "parts": [{"text": "SELECT 12"}]}}})},
            {"type": "text", "text": json.dumps({"artifactUpdate": {"artifact": {
                "name": "Final response", "parts": [{"text": "Resumen listo"}]}}})},
            {"type": "text", "text": json.dumps({"artifactUpdate": {"artifact": {
                "name": "Data result", "parts": [{"text": "| Mes | Importe_EUR |\n| --- | --- |\n| 2026-08 | 12 |"}]}}})},
            {"type": "text", "text": json.dumps({"artifactUpdate": {"artifact": {
                "name": "Generated SQL", "parts": [{"text": "SELECT 12"}]}}})},
            {"type": "text", "text": json.dumps(chart)},
        ]}

        result = _extract_result(response)

        self.assertEqual(result["answer"], "Resumen listo")
        self.assertEqual(len(result["tables"]), 1)
        self.assertIn("2026-08", result["tables"][0])
        self.assertEqual(result["sql"], ["SELECT 12"])
        self.assertEqual(result["charts"], [chart])

    def test_refresh_token_ciphertext_round_trips_without_plaintext(self):
        old_key = os.environ.get("ORIBAY_OAUTH_TOKEN_ENCRYPTION_KEY")
        os.environ["ORIBAY_OAUTH_TOKEN_ENCRYPTION_KEY"] = Fernet.generate_key().decode("ascii")
        try:
            plaintext = b"example-refresh-token"
            ciphertext = _fernet().encrypt(plaintext)
            self.assertEqual(_fernet().decrypt(ciphertext), plaintext)
            self.assertNotIn(plaintext, ciphertext)
        finally:
            if old_key is None:
                os.environ.pop("ORIBAY_OAUTH_TOKEN_ENCRYPTION_KEY", None)
            else:
                os.environ["ORIBAY_OAUTH_TOKEN_ENCRYPTION_KEY"] = old_key


if __name__ == "__main__":
    unittest.main()
