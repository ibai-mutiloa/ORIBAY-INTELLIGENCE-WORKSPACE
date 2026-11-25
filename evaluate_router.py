"""Run the routing corpus offline, or with --live using configured Vertex credentials."""
import argparse
import json
from pathlib import Path

from app import followup_source
from router import AgentAccessDenied, _fallback, decide, restrict_to_allowed, semantic_clarification


def evaluate(live=False, case_id=None):
    cases = json.loads((Path(__file__).parent / "evals" / "routing_cases.json").read_text(encoding="utf-8"))
    if case_id:
        cases = [case for case in cases if case["id"] == case_id]
        if not cases:
            raise ValueError(f"Caso desconocido: {case_id}")
    results = []
    for case in cases:
        if semantic_clarification(case["question"]):
            actual = {"route": "clarificacion", "agent": ""}
            results.append({"id": case["id"], "passed": actual == {
                "route": case.get("route"), "agent": case.get("agent")}, "actual": actual})
            continue
        try:
            plan = (decide(case["question"], case.get("history"), case.get("allowed")) if live
                    else restrict_to_allowed(_fallback(case["question"], case.get("history")),
                                             case["question"], case.get("allowed")))
            actual = {"route": plan["route"], "agent": plan["data_agent"]}
            passed = not case.get("denied") and all(actual[key] == case[key] for key in ("route", "agent"))
            if "context" in case:
                actual["context"] = bool(followup_source(case["question"], case.get("history") or []))
                passed = passed and actual["context"] == case["context"]
            if live and plan.get("router_mode") != "Gemini":
                actual["router_mode"] = plan.get("router_mode")
                passed = False
        except AgentAccessDenied:
            actual = {"denied": True}
            passed = bool(case.get("denied"))
        results.append({"id": case["id"], "passed": passed, "actual": actual})
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="Call Vertex AI; requires credentials and incurs usage")
    parser.add_argument("--case", help="Run one case by ID")
    args = parser.parse_args()
    results = evaluate(live=args.live, case_id=args.case)
    for item in results:
        print(("PASS" if item["passed"] else "FAIL") + " " + item["id"] + " " + json.dumps(item["actual"], ensure_ascii=False))
    print(f"{sum(item['passed'] for item in results)}/{len(results)} correct")
    raise SystemExit(0 if all(item["passed"] for item in results) else 1)
