"""A persistent gateway/context across ten synthetic-world rounds."""
from pathlib import Path
import tempfile
import unittest

from agent.gateway import Command, Gateway
from agent.telemetry import RecordingGatewayContext
from kit.loop.agent import Agent
from kit.mcp.hardmode import HardMode
from kit.mcp.servers import handle
from kit.world.fixture import build_fixture_world
from kit.world.loader import World


class PersistentDuel(unittest.TestCase):
    def test_agent_loop_routes_through_gateway_and_records_answer(self):
        class ScriptedModel:
            def __init__(self):
                self.turn = 0

            def query(self, messages, **kwargs):
                self.turn += 1
                if self.turn == 1:
                    return {"content": '```action\nDISCOVER slides.query q=streamable fields=title\n```'}
                self_anchor = next(a for a in env.last_result["anchors"] if a.startswith("Frame:"))
                return {"content": ('```action\nANSWER {"text":"Located a frame",'
                                    '"cited_anchors":["' + self_anchor + '"]}\n```')}

        class TrustedEnvironment:
            def __init__(self, world):
                self.ctx = RecordingGatewayContext(
                    act="learner:sv-0417", sub="agent:Akatsuki",
                    scopes=frozenset({"wiki.read"}), credits=100, round=1, call_index=0,
                )
                self.gateway = Gateway(self.ctx)
                self.world = world
                self.last_result = None
                self.decisions = []

            def execute(self, action):
                if action["kind"] == "answer":
                    return {"ok": True, "recorded": "answer"}
                cmd = Command.from_action_dict(action, cmd_id=f"loop:{action['call_index']}")
                decision = self.gateway.decide(cmd)
                self.decisions.append(decision)
                if decision.verdict == "deny":
                    return {"ok": False, "error": {"code": "denied"}, "credits_left": self.ctx.credits}
                result = handle(self.world, decision.call)
                self.last_result = result
                self.ctx.credits -= result["cost"]
                return {**result, "credits_left": self.ctx.credits}

        with tempfile.TemporaryDirectory() as scratch:
            world = World.load(build_fixture_world(Path(scratch) / "world"))
            env = TrustedEnvironment(world)
            run = Agent(ScriptedModel(), env, system_prompt="test", task="Locate a frame").run()
            self.assertEqual(run.exit_status, "answered")
            self.assertEqual(run.calls, 1)
            self.assertEqual(len(env.decisions), 1)
            self.assertNotEqual(env.decisions[0].verdict, "deny")
            self.assertIn(run.answer["cited_anchors"][0], env.last_result["anchors"])

    def test_ten_round_credit_and_lease_flow(self):
        with tempfile.TemporaryDirectory() as scratch:
            world = World.load(build_fixture_world(Path(scratch) / "world"))
            hardmode = HardMode(world=world, opaque_enabled=False, degraded_replica_enabled=False)
            hardmode.reset("akatsuki-test", world_id="fixture")
            ctx = RecordingGatewayContext(
                act="learner:sv-0417", sub="agent:Akatsuki",
                scopes=frozenset({"wiki.read"}), credits=100, round=1, call_index=0,
            )
            gateway = Gateway(ctx)
            for round_no in range(1, 11):
                ctx.round = round_no
                ctx.leases = ()
                hardmode.begin_round(round_no)
                query = Command(f"query:{round_no}", "mcp", "slides.query", "slides", "query",
                                {"q": "streamable"}, ("title",), {}, None, 0)
                decision = gateway.decide(query)
                self.assertNotEqual(decision.verdict, "deny", (round_no, decision.reason))
                result = handle(world, decision.call, hardmode=hardmode)
                self.assertTrue(result["ok"], result)
                ctx.credits -= result["cost"]
                ctx.leases = (result["lease_id"],)
                frame_anchor = next(a for a in result["anchors"] if a.startswith("Frame:"))
                frame = Command(f"frame:{round_no}", "mcp", "slides.get_frame", "slides", "get_frame",
                                {"anchor": frame_anchor}, ("body", "title"), {}, result["lease_id"], 1)
                decision = gateway.decide(frame)
                self.assertNotEqual(decision.verdict, "deny", (round_no, decision.reason))
                result2 = handle(world, decision.call, hardmode=hardmode)
                self.assertTrue(result2["ok"], result2)
                ctx.credits -= result2["cost"]
                self.assertLessEqual(result["cost"] + result2["cost"], 11)
            self.assertGreaterEqual(ctx.credits, 0)
            self.assertEqual(len(gateway._round_spend), 10)


if __name__ == "__main__":
    unittest.main()
