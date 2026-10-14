"""Team-owned mechanical deck checks; live Arena mutation is not available."""
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class DeckChecks(unittest.TestCase):
    def test_pool_and_lineup(self):
        deck = json.loads((ROOT / "deck.json").read_text(encoding="utf-8"))
        order = json.loads((ROOT / "lineup.json").read_text(encoding="utf-8"))["order"]
        cards = deck["cards"]
        self.assertEqual(deck["team"], "Akatsuki")
        self.assertEqual(len(cards), 14)
        self.assertEqual(sum(card["kind"] == "attack" for card in cards), 10)
        self.assertEqual(sum(card["kind"] == "blank" for card in cards), 4)
        self.assertEqual(len(order), 10)
        self.assertEqual(len(set(order)), 10)
        self.assertTrue(set(order).issubset({card["id"] for card in cards}))
        self.assertTrue(all(card_id.startswith("atk_") for card_id in order))


if __name__ == "__main__":
    unittest.main()
