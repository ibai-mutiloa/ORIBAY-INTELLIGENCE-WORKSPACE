import unittest

from evaluate_router import evaluate


class RoutingEvaluationTests(unittest.TestCase):
    def test_offline_routing_corpus(self):
        failures = [item for item in evaluate() if not item["passed"]]
        self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main()
