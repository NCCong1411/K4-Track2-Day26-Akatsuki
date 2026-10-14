"""Direct mutation-primitive probes, separate from scripted spar scoring.

The student world has no truth.json, so `which_days_cover` A2A success
cannot be exercised against it. Those cards remain Arena-only contact items.
"""
import json
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from kit.mcp.mutations import MutableStack
from kit.mcp.types import ToolCall
from kit.world.loader import World
from kit.world.fixture import build_fixture_world


ROOT = Path(__file__).resolve().parents[2]
WORLD_PATH = ROOT / "kit" / "world" / "df8c55dabb35"


@unittest.skipUnless((WORLD_PATH / "manifest.json").exists(), "student world not installed")
class MutationContact(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.world = World.load(WORLD_PATH)
        deck = json.loads((ROOT / "deck" / "deck.json").read_text(encoding="utf-8"))
        cls.cards = {card["id"]: card for card in deck["cards"]}

    def probe(self, card_id, call):
        stack = MutableStack(world=self.world, act="learner:sv-0417")
        if call.tool == "get_frame":
            located, _ = stack.execute(ToolCall(server="slides", tool="query",
                                                args={"q": "streamable"}, call_index=0))
            self.assertTrue(located.get("lease_id"), located)
            call = replace(call, lease_id=located["lease_id"], call_index=1)
        stack.arm(self.cards[card_id])
        result, mutation = stack.execute(call)
        self.assertIsNotNone(mutation, card_id)
        self.assertTrue(mutation["trigger_matched"], card_id)
        self.assertTrue(mutation["applied"], (card_id, result.get("error")))
        if card_id == "atk_01":
            self.assertTrue(any("3326cb76" in a for a in result.get("anchors", ())))
        elif card_id == "atk_02":
            self.assertEqual(result.get("replica"), "c")
        elif card_id == "atk_03":
            self.assertTrue(any("/c/" in a for a in result.get("anchors", ())))
        elif card_id == "atk_04":
            self.assertIn("Note:learner-sv-0412/w/005", result.get("anchors", ()))
        elif card_id == "atk_05":
            self.assertGreaterEqual(len(result.get("rows", ())), 40)
        elif card_id == "atk_06":
            self.assertEqual(result.get("error", {}).get("code"), "precondition_missing")
        elif card_id == "atk_08":
            self.assertFalse(result["rows"][0]["agent_card_admission"]["admitted"])
        elif card_id == "atk_10":
            self.assertFalse(result["rows"][0]["delegation_admission"]["admitted"])

    def test_direct_contact_for_reachable_mcp_cards(self):
        probes = {
            "atk_01": ToolCall(server="slides", tool="get_frame",
                                args={"anchor": "Frame:e0614beb/w/001"}, call_index=0),
            "atk_02": ToolCall(server="slides", tool="get_frame",
                                args={"anchor": "Frame:e0614beb/w/001"}, call_index=0),
            "atk_03": ToolCall(server="slides", tool="get_frame",
                                args={"anchor": "Frame:053195a5/w/001"}, call_index=0),
            "atk_04": ToolCall(server="glossary", tool="define",
                                args={"term": "streamable http transport"}, call_index=0),
            "atk_05": ToolCall(server="registry", tool="list_servers",
                                args={}, fields=("name",), call_index=0),
            "atk_06": ToolCall(server="progress", tool="record_mastery",
                                args={"learner": "Learner:sv-0417", "concept": "Concept:traceparent-header/w/062"},
                                headers={"If-Match": "sha256:dummy", "Idempotency-Key": "idem-contact"},
                                call_index=2),
        }
        for card_id, call in probes.items():
            with self.subTest(card=card_id):
                self.probe(card_id, call)

    def test_direct_contact_for_reachable_a2a_cards(self):
        self.probe("atk_08", ToolCall(server="citation-checker", tool="verify_source",
                                      args={"anchor": "Source:0014ec8ff84d"}, call_index=0))
        self.probe("atk_10", ToolCall(server="roster", tool="lookup_learner",
                                      args={"learner": "Learner:sv-0417"}, call_index=0))

    def test_truth_dependent_a2a_mutation_on_synthetic_world(self):
        with tempfile.TemporaryDirectory() as scratch:
            world = World.load(build_fixture_world(Path(scratch) / "world", include_truth=True))
            for card_id in ("atk_07", "atk_09"):
                with self.subTest(card=card_id):
                    stack = MutableStack(world=world, act="learner:sv-0417")
                    stack.arm(self.cards[card_id])
                    result, mutation = stack.execute(ToolCall(
                        server="curriculum-analyst", tool="which_days_cover",
                        args={"concept": "Concept:streamable-http"}, call_index=0,
                    ))
                    self.assertTrue(result["ok"], result)
                    self.assertTrue(mutation["trigger_matched"] and mutation["applied"])
