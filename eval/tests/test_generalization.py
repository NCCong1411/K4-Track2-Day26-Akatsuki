"""Generalization checks independent of fixture IDs and event numbering."""
import copy
import json
import unittest

from eval.prosecute import load_fixtures, prosecute, score_prosecutor, group_calls, _HOOKS


class ProsecutorGeneralization(unittest.TestCase):
    def test_interleaved_command_ids_correlate(self):
        trace = [
            {"seq": 1, "type": "command", "p": {"cmd_id": "one"}},
            {"seq": 2, "type": "command", "p": {"cmd_id": "two"}},
            {"seq": 3, "type": "tool_result", "p": {"cmd_id": "one", "ok": True}},
            {"seq": 4, "type": "tool_result", "p": {"cmd_id": "two", "ok": False}},
        ]
        groups = group_calls(trace)
        self.assertTrue(groups[0].tool_result["p"]["ok"])
        self.assertFalse(groups[1].tool_result["p"]["ok"])

    def test_public_fixtures_and_clean_cases(self):
        fixtures = load_fixtures()
        result = score_prosecutor(prosecute, fixtures)
        self.assertEqual((result["precision"], result["recall"]), (1.0, 1.0))
        self.assertEqual((result["false"], result["rejected"], result["n_errors"]), (0, 0, 0))
        for fixture in fixtures:
            if fixture["fixture_id"].startswith("clean__"):
                self.assertEqual(prosecute(fixture["trace"], fixture["answer"], fixture["card"])["claims"], [])

    def test_each_named_hook_finds_its_positive_example(self):
        positives = {fixture["primary_class"]: fixture for fixture in load_fixtures()
                     if fixture["variant"] == "positive"}
        for hook in _HOOKS:
            name = hook.__name__.removeprefix("_hook_")
            fixture = positives[name]
            self.assertTrue(hook(fixture["trace"], fixture["answer"], fixture["card"]), name)

    def test_renumbered_events_unordered_trace_and_no_command_ids(self):
        for original in load_fixtures():
            fixture = copy.deepcopy(original)
            fixture["fixture_id"] = "renumbered"
            for event in fixture["trace"]:
                event["seq"] += 100
                if event["type"] == "command":
                    event["p"].pop("cmd_id", None)
            for item in fixture["label"]["present_classes"].values():
                item["proof_refs"] = [f"evt:{int(ref[4:]) + 100:04d}" if ref.startswith("evt:") else ref
                                      for ref in item["proof_refs"]]
            fixture["trace"].reverse()
            result = score_prosecutor(prosecute, [fixture])
            self.assertEqual(result["false"], 0, original["fixture_id"])
            self.assertEqual(result["rejected"], 0, original["fixture_id"])
            if fixture["label"]["present_classes"]:
                self.assertEqual(result["recall"], 1.0, original["fixture_id"])

    def test_changed_learner_and_anchor_identity(self):
        for original in load_fixtures():
            encoded = json.dumps(original, ensure_ascii=False)
            encoded = encoded.replace("sv-0417", "sv-0888").replace("sv-0392", "sv-0777")
            encoded = encoded.replace("d8f95a7b", "a1b2c3d4").replace("053195a5", "0a1b2c3d")
            fixture = json.loads(encoded)
            result = score_prosecutor(prosecute, [fixture])
            self.assertEqual((result["false"], result["rejected"]), (0, 0), original["fixture_id"])
            if fixture["label"]["present_classes"]:
                self.assertEqual(result["recall"], 1.0, original["fixture_id"])


if __name__ == "__main__":
    unittest.main()
