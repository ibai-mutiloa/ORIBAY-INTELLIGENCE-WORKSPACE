"""Run the routing corpus offline, or with --live using configured Vertex credentials."""
import argparse
import json
from pathlib import Path

from router import AgentAccessDenied, _fallback, decide, restrict_to_allowed


def evaluate(live=False):
    cases = json.loads((Path(__file__).parent / "evals" / "routing_cases.json").read_text(encoding="utf-8"))
    results = []
    for case in cases:
        try:
            plan = (decide(case["question"], case.get("history"), case.get("allowed")) if live
                    else restrict_to_allowed(_fallback(case["question"], case.get("history")),
                                             case["question"], case.get("allowed")))
            actual = {"route": plan["route"], "agent": plan["data_agent"]}
            passed = not case.get("denied") and all(actual[key] == case[key] for key in ("route", "agent"))
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
    args = parser.parse_args()
    results = evaluate(live=args.live)
    for item in results:
        print(("PASS" if item["passed"] else "FAIL") + " " + item["id"] + " " + json.dumps(item["actual"], ensure_ascii=False))
    print(f"{sum(item['passed'] for item in results)}/{len(results)} correct")
    raise SystemExit(0 if all(item["passed"] for item in results) else 1)
