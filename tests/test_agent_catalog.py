import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_catalog import CATALOG, load_catalog
from router import AgentAccessDenied, decide, restrict_to_allowed
from app import app, query_agent


class AgentCatalogTests(unittest.TestCase):
    def test_catalog_contains_valid_current_agents(self):
        self.assertEqual(set(CATALOG), {"ventas", "adhesiva"})
        self.assertEqual(len({item["resource"] for item in CATALOG.values()}), 2)

    def test_duplicate_catalog_id_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agents.json"
            path.write_text(json.dumps([CATALOG["ventas"], CATALOG["ventas"]]), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_catalog(path)

    def test_inaccessible_explicit_agent_is_not_substituted(self):
        plan = {"route": "adhesiva", "data_agent": "adhesiva"}
        with self.assertRaises(AgentAccessDenied):
            restrict_to_allowed(plan, "Muestra el OEE de RB4", ["ventas"])

    def test_forged_execution_plan_is_rejected(self):
        plan = {"route": "adhesiva", "data_agent": "adhesiva"}
        with self.assertRaises(AgentAccessDenied):
            restrict_to_allowed(plan, "Muestra el resultado", ["ventas"], strict=True)
        self.assertEqual(plan["data_agent"], "adhesiva")

    def test_query_does_not_call_agent_without_discovered_access(self):
        with app.test_request_context("/api/ask"), patch("app.allowed_data_agents", return_value=["ventas"]), \
                patch("app.query_data_agent_as_user") as query:
            with self.assertRaises(AgentAccessDenied):
                query_agent("OEE de RB4", user="tester@oribay.com", agent="adhesiva")
            query.assert_not_called()

    def test_execute_endpoint_rejects_forged_agent_before_query(self):
        payload = {"question": "Muestra el resultado", "plan": {"route": "adhesiva", "data_agent": "adhesiva"}}
        with patch("app.allowed_data_agents", return_value=["ventas"]), \
                patch("app.query_data_agent_as_user") as query:
            response = app.test_client().post("/api/execute", json=payload)
        self.assertEqual(response.status_code, 403)
        query.assert_not_called()

    def test_router_receives_only_accessible_catalog_entries(self):
        class FakeClient:
            class models:
                @staticmethod
                def generate_content(**kwargs):
                    payload = json.loads(kwargs["contents"])
                    assert [item["id"] for item in payload["available_agents"]] == ["ventas"]
                    return type("Response", (), {"text": '{"route":"ventas","data_agent":"ventas"}'})()
        with patch("router.Client", return_value=FakeClient()):
            plan = decide("Facturación de la pieza 24", allowed_agents=["ventas"])
        self.assertEqual(plan["data_agent"], "ventas")


if __name__ == "__main__":
    unittest.main()
