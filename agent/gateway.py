"""Akatsuki's pure control plane for MCP, A2A, and discovery commands.

The trusted Arena canonicalizes actions into Command, calls Gateway.decide,
enforces the returned Decision, meters the call, and records authoritative
events. ANSWER bypasses this gateway. A gateway instance persists across ten
rounds; it reads the live context on each call and never executes tools or
does I/O. A denied command costs zero credits, while exceptions or malformed
decisions incur integrity penalties.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import re
from typing import Any, Mapping, Protocol, runtime_checkable

# kit.mcp.types is a collaborator's file (workspace hard rule 2: import it,
# degrade gracefully). It is present as of this writing and is core, stable
# infrastructure (CONTRACTS.md 3.1) — but this module must still not fail to
# IMPORT if a concurrent edit ever breaks it transiently. When it is
# unavailable, `Decision.call` type-checking is skipped (not enforced), and
# `Gateway.decide` falls back to a minimal local dict-shaped stand-in so the
# rest of this file — everything that does not need a *real* ToolCall — still
# runs.
try:
    from kit.mcp.types import ToolCall
    _TOOLCALL_AVAILABLE = True
except ImportError:  # pragma: no cover - collaborator file
    ToolCall = Any  # type: ignore[assignment, misc]
    _TOOLCALL_AVAILABLE = False

# kit.loop.agent is also a collaborator's file, used only by this module's
# own __main__ demo (to build real Commands the same way the arena's trusted
# canonicaliser would) — never by decide() itself, which never touches the
# loop. Degraded the same way.
try:
    from kit.loop.agent import canonicalise_action as _canonicalise_action
except ImportError:  # pragma: no cover - collaborator file
    _canonicalise_action = None

from agent.telemetry import RecordingGatewayContext, Telemetry
from kit.mcp.specs import TOOL_SPECS, cost
from kit.mcp.a2a import AGENT_CARDS, verify_card, verify_delegation, SKILL_ROUTES
from agent.guardrails import scan_for_injected_instructions

__all__ = [
    "COMMAND_KINDS",
    "DECISION_VERDICTS",
    "Command",
    "Decision",
    "GatewayContext",
    "Gateway",
]

# CONTRACTS.md 4.1: `Command.kind` — "mcp" | "a2a" | "discover". An "answer"
# action is NEVER a Command (kit/loop/agent.py's own module docstring: "an
# answer is not a tool call routed to a server, so it never becomes a
# Command at all") — it is handled entirely by the loop/arena and never
# reaches `Gateway.decide`.
COMMAND_KINDS: frozenset[str] = frozenset({"mcp", "a2a", "discover"})

# CONTRACTS.md 4.1: `Decision.verdict` — the closed three-member set.
DECISION_VERDICTS: frozenset[str] = frozenset({"forward", "deny", "rewrite"})


@dataclass(frozen=True, slots=True)
class Command:
    """CONTRACTS.md 4.1, field for field — "canonicalised by the arena
    BEFORE the student sees it". You never build one of these from your own
    agent's raw text; the arena's canonicaliser (kit/loop/agent.py's
    `canonicalise_action`, run inside the trusted envelope) already did that
    work and minted `cmd_id` by the time `decide()` sees it. The
    `from_action_dict` classmethod below exists only so this file's own demo
    (and your local tests, if you write any) can build a realistic `Command`
    without duplicating the arena's canonicalisation logic."""

    cmd_id: str
    kind: str  # "mcp" | "a2a" | "discover" — see COMMAND_KINDS
    raw: str
    server: str
    tool: str
    args: dict
    fields: tuple[str, ...]
    headers: dict
    lease_id: str | None
    call_index: int

    def __post_init__(self) -> None:
        if not isinstance(self.cmd_id, str) or not self.cmd_id:
            raise ValueError(f"Command.cmd_id must be a non-empty str, got {self.cmd_id!r}")
        if self.kind not in COMMAND_KINDS:
            raise ValueError(f"Command.kind must be one of {sorted(COMMAND_KINDS)}, got {self.kind!r}")
        if not isinstance(self.server, str) or not self.server:
            raise ValueError(f"Command.server must be a non-empty str, got {self.server!r}")
        if not isinstance(self.tool, str) or not self.tool:
            raise ValueError(f"Command.tool must be a non-empty str, got {self.tool!r}")
        if not isinstance(self.args, dict):
            raise ValueError(f"Command.args must be a dict, got {type(self.args).__name__}")
        if not isinstance(self.headers, dict):
            raise ValueError(f"Command.headers must be a dict, got {type(self.headers).__name__}")
        if (
            not isinstance(self.call_index, int)
            or isinstance(self.call_index, bool)
            or self.call_index < 0
        ):
            raise ValueError(f"Command.call_index must be a non-negative int, got {self.call_index!r}")

    @classmethod
    def from_action_dict(cls, action: Mapping[str, Any], *, cmd_id: str) -> "Command":
        """Build a `Command` from the dict shape `kit.loop.agent.canonicalise_action`
        returns (`kind, raw, server, tool, args, fields, headers, lease_id,
        call_index` — everything except the arena-minted `cmd_id`, supplied
        here as a keyword). Raises `ValueError` if `action["kind"] ==
        "answer"` — an answer is never a Command (see the module docstring).
        This is a convenience for tests/demos, not something the real arena
        calls: the trusted envelope mints `cmd_id` itself and constructs the
        real `Command` on its own side of the boundary."""
        kind = action.get("kind")
        if kind == "answer":
            raise ValueError(
                "an 'answer' action never becomes a Command (kit/loop/agent.py: "
                "\"an answer is not a tool call routed to a server\") — do not "
                "route it through Gateway.decide at all"
            )
        return cls(
            cmd_id=cmd_id,
            kind=kind,
            raw=action["raw"],
            server=action["server"],
            tool=action["tool"],
            args=dict(action.get("args", {})),
            fields=tuple(action.get("fields", ())),
            headers=dict(action.get("headers", {})),
            lease_id=action.get("lease_id"),
            call_index=action.get("call_index", 0),
        )

    def to_dict(self) -> dict:
        return {
            "cmd_id": self.cmd_id,
            "kind": self.kind,
            "raw": self.raw,
            "server": self.server,
            "tool": self.tool,
            "args": dict(self.args),
            "fields": list(self.fields),
            "headers": dict(self.headers),
            "lease_id": self.lease_id,
            "call_index": self.call_index,
        }


@dataclass(frozen=True, slots=True)
class Decision:
    """CONTRACTS.md 4.1, field for field.

    Validated strictly (`__post_init__`) because a *structurally* invalid
    `Decision` is charged exactly like a raised exception — CONTRACTS.md
    4.1's charging table: "malformed Decision (schema-invalid) -> 2 cr
    penalty, command denied." Failing loudly HERE, in your own process
    during development, is strictly better than discovering it live in a
    duel as an unexplained penalty.

    `verdict == "deny"` requires a non-empty `reason` (CONTRACTS.md 4.1:
    "required when verdict == 'deny'; shown in the combat log") and
    forbids `call` — a real denial has nothing left to carry out.
    `verdict` in `("forward", "rewrite")` requires `call` to be set — the
    arena executes exactly that `ToolCall`, nothing else, per the trusted
    envelope's whole point (see the module docstring)."""

    verdict: str  # "forward" | "deny" | "rewrite" — see DECISION_VERDICTS
    reason: str | None = None
    call: "ToolCall | None" = None
    quarantine: bool = False
    note: str | None = None

    def __post_init__(self) -> None:
        if self.verdict not in DECISION_VERDICTS:
            raise ValueError(
                f"Decision.verdict must be one of {sorted(DECISION_VERDICTS)}, got {self.verdict!r}"
            )
        if self.verdict == "deny":
            if not isinstance(self.reason, str) or not self.reason.strip():
                raise ValueError("Decision.verdict=='deny' requires a non-empty 'reason'")
            if self.call is not None:
                raise ValueError("Decision.verdict=='deny' must not carry a 'call' — there is nothing to run")
        else:  # forward | rewrite
            if self.call is None:
                raise ValueError(f"Decision.verdict=={self.verdict!r} requires 'call' to be set")
            if _TOOLCALL_AVAILABLE and not isinstance(self.call, ToolCall):
                raise ValueError(
                    f"Decision.call must be a kit.mcp.types.ToolCall instance, got {type(self.call).__name__}"
                )
        if not isinstance(self.quarantine, bool):
            raise ValueError(f"Decision.quarantine must be a bool, got {self.quarantine!r}")
        if self.note is not None and not isinstance(self.note, str):
            raise ValueError(f"Decision.note must be a str or None, got {self.note!r}")

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "reason": self.reason,
            "call": self.call.to_dict() if self.call is not None and hasattr(self.call, "to_dict") else self.call,
            "quarantine": self.quarantine,
            "note": self.note,
        }


@runtime_checkable
class GatewayContext(Protocol):
    """CONTRACTS.md 4.2 — "read-only, arena-provided". Note what this is
    NOT: unlike `Command`/`Decision` above, CONTRACTS.md writes this as a
    plain `class`, not a `@dataclass(frozen=True, ...)`. That is a real
    signal, not an inconsistency — a `GatewayContext` is a LIVE VIEW the
    arena owns and updates as the duel actually progresses (credits get
    spent, rounds advance, leases mint and expire), not a frozen snapshot
    you receive once. This file therefore models it as a structural
    `Protocol`: the arena hands your `Gateway.__init__` some object shaped
    like this (see `agent/telemetry.py`'s `RecordingGatewayContext` for a
    concrete example your own tests/demos can build), and you read
    `self.ctx.<field>` fresh every time `decide()` runs — NEVER cache a
    context field into a local variable across calls; if you need memory
    that persists across the duel (a cache, a budget tracker, drift notes),
    keep it on YOUR OWN `Gateway` instance attributes instead (CONTRACTS.md
    4.3: "Gateway is instantiated once per duel... persists across the 10
    rounds").

    There is deliberately no `execute` method here — see the module
    docstring's "WHY THERE IS NO execute() METHOD" section. `emit` is the
    ONE thing you can do that has any side effect at all, and it writes to
    `own_telemetry` only: your own side, never scored, never shown to the
    opponent (`agent/telemetry.py` wraps it)."""

    act: str  # "learner:sv-0417" — WHOM YOU SERVE. Authority derives from this.
    sub: str  # "agent:vlearn-tutor" — what you are. NOT authority.
    scopes: frozenset[str]  # {"wiki.read"}; writes need "wiki.write:progress" etc.
    credits: int  # remaining this duel
    round: int
    call_index: int
    leases: tuple[str, ...]  # live lease ids, arena-tracked
    history: tuple[Mapping[str, Any], ...]  # YOUR OWN prior (Command, Decision, outcome) triples this duel

    def emit(self, name: str, **payload: Any) -> None: ...


class Gateway:
    """The control plane. One instance per duel (CONTRACTS.md 4.3) — built
    once at duel start with a `GatewayContext`, then asked to `decide()` on
    every MCP/A2A/DISCOVER command either side of the duel makes for all 10
    rounds. See the module docstring for the trusted-envelope diagram and
    why there is no `execute()` to call instead.

    Instance attributes below are this starter's ENTIRE per-duel memory —
    all currently unused by `decide()`'s naive body, but declared here
    (rather than invented ad hoc later) so the four TODO jobs below have
    somewhere obvious to keep state once you implement them. `agent/
    strategy.py` has working building blocks for exactly this (a budget
    pacer, a result cache, a replica-choice heuristic) — this starter does
    not wire them in for you; that wiring is the assignment.
    """

    def __init__(self, ctx: GatewayContext) -> None:
        self.ctx = ctx
        self._telemetry = Telemetry(ctx)

        # --- per-duel memory, unused by the naive starter below ---------
        # A cache of anchor -> body-ish data you have already paid for this
        # duel (agent/strategy.py's ResultCache is a ready-made version of
        # this). Populating it needs the *result* of a call, which decide()
        # never sees (it only sees the outgoing Command) — you would fill
        # this from whatever the arena hands back to your agent loop AFTER
        # a call executes, then consult it here on the NEXT decide() call
        # for the same anchor.
        self._seen_anchors: dict[str, Any] = {}
        # Credits you have personally authorised so far this duel — your
        # own running total, independent of (and a cross-check against)
        # `ctx.credits`, which the arena maintains authoritatively.
        self._credits_authorised: int = 0
        # Command ids you have already denied, in case a later job wants to
        # know "have I already said no to this once".
        self._denied_cmd_ids: set[str] = set()
        self._cards: dict[str, Any] = {}
        self._rejected_cards: set[str] = set()
        self._token_ids: set[str] = set()
        self._round_spend: dict[int, int] = {}
        self._last_credits = getattr(ctx, "credits", 100)
        self._last_round = getattr(ctx, "round", 1)

    def note_card(self, server: str, card: Any) -> None:
        """Record only a signature-checked card, never a caller's verified flag."""
        if isinstance(card, Mapping) and "signature" not in card and "name" not in card:
            return  # an unsigned hint is not an attempted registry card
        try:
            card_name = card.get("name") if isinstance(card, Mapping) else card.name
            if verify_card(card).admitted and card_name == server:
                self._cards[server] = card
                self._rejected_cards.discard(server)
            else:
                self._rejected_cards.add(server)
        except (TypeError, AttributeError, ValueError):
            self._rejected_cards.add(server)

    def decide(self, cmd: Command) -> Decision:
        """SYNCHRONOUS. PURE. NO I/O. 250 ms wall (RULES.md section 3).
        Raising anything, or returning a `Decision` `__post_init__` rejects,
        is treated by the arena exactly like an explicit deny PLUS a 2
        credit penalty PLUS a scored `integrity` event (CONTRACTS.md 4.1's
        charging table) — so the one thing this method must never do is
        blow up or wander off into I/O, no matter how tempting a "quick
        check" against something external looks. Everything you need to
        decide is already sitting in `cmd` and `self.ctx`.

        The decision path checks route, peer admission, learner authority,
        hard-mode preconditions, and duel budget before returning a valid
        forward, rewrite, or reasoned deny."""
        self._telemetry.decision_seen(cmd)
        try:
            return self._decide_checked(cmd)
        except Exception:
            return self.deny(cmd, "Malformed or unsupported command")

    def _decide_checked(self, cmd: Command) -> Decision:
        spec = TOOL_SPECS.get((cmd.server, cmd.tool))
        if spec is None:
            return self.deny(cmd, "Unknown server or tool")
        if cmd.kind == "a2a" and cmd.server not in AGENT_CARDS:
            return self.deny(cmd, "Unknown A2A peer")
        if cmd.kind != "a2a" and cmd.server in AGENT_CARDS:
            return self.deny(cmd, "Peer call must use A2A")

        headers = {str(k).lower(): v for k, v in cmd.headers.items()}
        if len(headers) != len(cmd.headers):
            return self.deny(cmd, "Conflicting duplicate header names")
        if any(cmd.args.get(name) for name in ("route", "_route", "replica")):
            return self.deny(cmd, "Replica route must be in a header, not the body")
        if headers.get("x-server-fingerprint") == "unvouched":
            return self.deny(cmd, "Server fingerprint is not registry-vouched")
        if cmd.args.get("peer_unverified"):
            return self.deny(cmd, "Unverified peer output cannot establish a fact")
        if isinstance(cmd.args.get("note"), str) and scan_for_injected_instructions(cmd.args["note"]).suspicious:
            return self.deny(cmd, "Injected instruction in retrieved note")
        for name in ("mcp-replica", "a2a-aud", "aud", "act", "x-act"):
            if re.search(r"(?i)(?:^|[\s,;])" + re.escape(name) + r"\s*[:=]", cmd.raw) and name not in headers:
                return self.deny(cmd, "Route or identity declared outside trusted headers")
        if "mcp-replica" in headers and headers["mcp-replica"] not in ("w", "c"):
            return self.deny(cmd, "Unknown replica header")
        anchor_arg = cmd.args.get("anchor")
        if isinstance(anchor_arg, str):
            match = re.search(r"/(w|c)(?:/|$)", anchor_arg)
            if match:
                headers["mcp-replica"] = match.group(1)
        routed = replace(cmd, headers=headers)
        if spec.deprecated and spec.successor:
            server, tool = spec.successor.split(".", 1)
            successor = TOOL_SPECS[(server, tool)]
            mask = tuple("body" if f == "snippet" and "body" in successor.all_fields else f
                         for f in routed.fields if f in successor.all_fields or f == "snippet")
            routed = replace(routed, server=server, tool=tool, fields=mask)
            spec = successor

        if spec.needs_lease and (not routed.lease_id or routed.lease_id not in self.ctx.leases):
            return self.deny(cmd, "A current-round lease is required")
        history = [item for item in self.ctx.history if isinstance(item, Mapping)]
        same_calls = []
        for item in history:
            prior = item.get("command", {})
            if not isinstance(prior, Mapping):
                continue
            prior_headers = {str(k).lower(): v for k, v in (prior.get("headers") or {}).items()}
            if ((prior.get("server"), prior.get("tool"), prior.get("args"), tuple(prior.get("fields") or ()),
                 prior_headers, prior.get("lease_id")) ==
                    (routed.server, routed.tool, routed.args, routed.fields, headers, routed.lease_id)):
                same_calls.append(item)
        if same_calls:
            last = same_calls[-1]
            result = last.get("result", last.get("tool_result", {}))
            if isinstance(result, Mapping) and not result.get("ok", False):
                error = result.get("error", {})
                code = (error.get("code") if isinstance(error, Mapping) else None) or result.get("error_code")
                if code == "unavailable":
                    if spec.is_write or sum(1 for item in same_calls if
                                            (item.get("result", item.get("tool_result", {})) or {}).get("error_code") == "unavailable" or
                                            ((item.get("result", item.get("tool_result", {})) or {}).get("error") or {}).get("code") == "unavailable") > 1:
                        return self.deny(cmd, "Opaque write or repeated unavailable read must not retry")
                elif code == "rate_limited":
                    last_round = last.get("round")
                    if last_round is None or int(last_round) >= int(self.ctx.round):
                        return self.deny(cmd, "Rate-limited call must wait for the next window")
                elif code in ("not_found", "bad_request", "unauthorized", "lease_required", "lease_expired",
                              "precondition_missing", "conflict"):
                    return self.deny(cmd, f"Unchanged retry after {code} is unsafe")
        if (routed.server, routed.tool) == ("registry", "list_servers") and any(
                (item.get("command") or {}).get("server") == "registry" and
                (item.get("command") or {}).get("tool") == "list_servers" for item in history):
            return self.deny(cmd, "Registry catalog is limited to one call per duel")
        if (routed.server, routed.tool) == ("citation-checker", "verify_source"):
            recent = sum(1 for item in history if isinstance(item.get("command"), Mapping) and
                         (item["command"].get("server"), item["command"].get("tool")) ==
                         ("citation-checker", "verify_source") and
                         int(item.get("round", 0)) >= int(self.ctx.round) - 2 and
                         (item.get("result", item.get("tool_result", {})) or {}).get("ok"))
            if recent >= 2:
                return self.deny(cmd, "Citation-checker rate window exhausted")
        token = None
        if routed.kind == "a2a":
            if "x-card-signature" in headers:
                if headers["x-card-signature"] != AGENT_CARDS[routed.server].signature:
                    return self.deny(cmd, "Presented peer card signature is invalid")
            if routed.server in self._rejected_cards:
                return self.deny(cmd, "Presented peer card failed signature verification")
            supplied = self._cards.get(routed.server, AGENT_CARDS.get(routed.server))
            if supplied is None or not verify_card(supplied).admitted:
                return self.deny(cmd, "Peer card signature not verified")
            skill = routed.args.get("skill") or ("role_of" if routed.server == "roster" else routed.tool)
            if SKILL_ROUTES.get(skill) != (routed.server, routed.tool):
                return self.deny(cmd, "Peer skill is not declared for this tool")
            audience = headers.get("a2a-aud", headers.get("aud"))
            if audience is None:
                return self.deny(cmd, "A2A delegation audience is missing")
            if audience == routed.server:
                headers["aud"] = f"a2a:{routed.server}"
                routed = replace(routed, headers=headers)
            elif audience is not None and audience != f"a2a:{routed.server}":
                return self.deny(cmd, "A2A audience mismatch")
            token = routed.args.get("delegation") or headers.get("delegation")
            if token is not None:
                admission = verify_delegation(token, aud=f"a2a:{routed.server}",
                                              call_index=routed.call_index,
                                              expected_act=self.ctx.act,
                                              seen_token_ids=self._token_ids)
                if not admission.admitted:
                    return self.deny(cmd, f"Invalid delegation: {admission.reason}")

        expected_learner = self.ctx.act.removeprefix("learner:")
        if "wiki.read" not in self.ctx.scopes and not spec.is_write:
            return self.deny(cmd, "Missing read scope")
        for name in ("learner", "learner_id", "act"):
            target = routed.args.get(name)
            if target is not None:
                target_id = str(target).removeprefix("Learner:").removeprefix("learner:")
                if target_id != expected_learner:
                    return self.deny(cmd, "Target learner differs from authenticated act")
        if routed.server == "progress":
            anchor_target = routed.args.get("anchor")
            if anchor_target is not None and str(anchor_target).removeprefix("Learner:") != expected_learner:
                return self.deny(cmd, "Progress write anchor differs from authenticated act")
        if spec.is_write:
            needed = "wiki.write:progress" if routed.server == "progress" else "wiki.write:content"
            if needed not in self.ctx.scopes:
                return self.deny(cmd, "Missing write scope")
            if any(not headers.get(h) for h in spec.required_headers):
                return self.deny(cmd, "Write needs If-Match and idempotency-key")
            if not re.fullmatch(r"[A-Za-z0-9._:-]{6,128}", str(headers["idempotency-key"])):
                return self.deny(cmd, "Invalid idempotency-key")
            anchor = routed.args.get("anchor") or routed.args.get("learner")
            if not isinstance(anchor, str):
                return self.deny(cmd, "Write target anchor is missing")
            proven_etag = None
            for item in reversed(self.ctx.history):
                if not isinstance(item, Mapping):
                    continue
                prior = item.get("command", {})
                result = item.get("result", item.get("tool_result", {}))
                if not isinstance(prior, Mapping) or not isinstance(result, Mapping):
                    continue
                if (prior.get("server"), prior.get("tool")) == ("registry", "provenance") and (
                        prior.get("args") or {}).get("anchor") == anchor and result.get("ok"):
                    proven_etag = result.get("etag") or next((r.get("etag") for r in result.get("rows", ())
                                                              if isinstance(r, Mapping) and r.get("etag")), None)
                    break
            if not proven_etag or headers["if-match"] != proven_etag:
                return self.deny(cmd, "If-Match is not backed by successful target provenance")
            for item in self.ctx.history:
                if not isinstance(item, Mapping):
                    continue
                prior = item.get("command", {})
                result = item.get("result", item.get("tool_result", {}))
                if isinstance(prior, Mapping) and isinstance(result, Mapping) and result.get("ok"):
                    old_headers = {str(k).lower(): v for k, v in (prior.get("headers") or {}).items()}
                    if old_headers.get("idempotency-key") == headers["idempotency-key"]:
                        return self.deny(cmd, "Idempotency key was already committed")

        fields = routed.fields
        if not fields or fields == ("*",):
            if (routed.server, routed.tool) == ("registry", "list_servers"):
                fields = ("name",)
            elif (routed.server, routed.tool) == ("glossary", "list_terms"):
                fields = ("term",)
            elif fields == ("*",):
                fields = ("*",)
        if fields and fields != ("*",) and any(f not in spec.all_fields for f in fields):
            return self.deny(cmd, "Field mask contains unsupported fields")
        routed = replace(routed, fields=tuple(fields))
        estimate = cost(routed.server, routed.tool, routed.fields)
        round_no = max(1, min(10, int(self.ctx.round)))
        targets = (8, 8, 8, 9, 9, 9, 10, 10, 10, 11)
        if round_no != self._last_round:
            self._last_round = round_no
            self._round_spend.setdefault(round_no, 0)
            self._last_credits = self.ctx.credits
        observed_delta = max(0, self._last_credits - self.ctx.credits)
        if observed_delta:
            self._round_spend[round_no] = self._round_spend.get(round_no, 0) + observed_delta
        self._last_credits = self.ctx.credits
        if estimate > self.ctx.credits or self._round_spend.get(round_no, 0) + estimate > min(11, targets[round_no - 1]):
            return self.deny(cmd, "Insufficient round or duel credit budget")
        if routed.kind == "a2a" and token is not None:
            token_id = token.get("token_id") if isinstance(token, Mapping) else token.token_id
            self._token_ids.add(token_id)
        decision = Decision(verdict="forward" if routed == cmd else "rewrite", call=self._to_tool_call(routed))
        self._telemetry.decision_made(cmd, decision)
        return decision

    def deny(self, cmd: Command, reason: str) -> Decision:
        """Not called anywhere in this starter's `decide()` — a ready-made
        helper for when you fill in JOB 2 / JOB 3 above, so denying doesn't
        mean hand-building a `Decision` inline at every call site. Kept as
        a real method (not a stub) because the shape of a correct denial —
        no `call`, a non-empty `reason` — is exactly the thing worth
        getting right by construction rather than by convention."""
        self._denied_cmd_ids.add(cmd.cmd_id)
        decision = Decision(verdict="deny", reason=reason)
        self._telemetry.decision_made(cmd, decision)
        return decision

    def _to_tool_call(self, cmd: Command) -> "ToolCall":
        """`Command` -> the `ToolCall` (CONTRACTS.md 3.1) the arena will
        actually execute on a `forward`/`rewrite` verdict. When
        `kit.mcp.types` is unavailable (see the module-level import guard),
        falls back to a plain dict carrying the identical fields — `Decision`
        accepts it either way (the `ToolCall` isinstance check inside
        `Decision.__post_init__` only runs when the real class loaded)."""
        fields = {
            "server": cmd.server,
            "tool": cmd.tool,
            "args": dict(cmd.args),
            "fields": cmd.fields,
            "headers": dict(cmd.headers),
            "lease_id": cmd.lease_id,
            "call_index": cmd.call_index,
        }
        if _TOOLCALL_AVAILABLE:
            return ToolCall(**fields)
        return fields  # type: ignore[return-value]


if __name__ == "__main__":
    print("=== agent.gateway: Command / Decision validation ===\n")

    good_cmd = Command(
        cmd_id="cmd:0000",
        kind="mcp",
        raw="MCP slides.get_frame anchor=Frame:3f2a9c11/w/041 fields=title,body lease=lse_7f21",
        server="slides",
        tool="get_frame",
        args={"anchor": "Frame:3f2a9c11/w/041"},
        fields=("body", "title"),
        headers={},
        lease_id="lse_7f21",
        call_index=0,
    )
    print(f"  Command constructed: {good_cmd}")
    assert good_cmd.kind == "mcp"

    print("\n  Rejection demo (each must raise ValueError):")

    def _expect_value_error(label: str, fn) -> None:
        try:
            fn()
        except ValueError as exc:
            print(f"    [{label:38}] -> ValueError: {exc}")
        else:
            raise AssertionError(f"expected ValueError for case {label!r}")

    _expect_value_error("Command.kind == 'answer'", lambda: Command(
        cmd_id="cmd:0001", kind="answer", raw="x", server="slides", tool="get_frame",
        args={}, fields=(), headers={}, lease_id=None, call_index=0,
    ))
    _expect_value_error("Decision verdict='deny' with no reason", lambda: Decision(verdict="deny"))
    _expect_value_error(
        "Decision verdict='forward' with no call", lambda: Decision(verdict="forward")
    )
    _expect_value_error(
        "Decision verdict='deny' carrying a call",
        lambda: Decision(verdict="deny", reason="nope", call={"server": "x", "tool": "y"}),
    )
    _expect_value_error("Decision verdict='?' unknown", lambda: Decision(verdict="???"))

    print("\n=== Command.from_action_dict — real canonicaliser integration ===\n")
    if _canonicalise_action is None:
        print("  kit.loop.agent not importable yet — skipping the live canonicaliser demo")
        demo_commands: list[Command] = [good_cmd]
    else:
        raw_actions = [
            "MCP registry.provenance anchor=Frame:3f2a9c11/w/041 fields=etag",
            'MCP slides.query q="streamable http replaces http+sse" fields=title,body',
            "A2A curriculum-analyst.which_days_cover concept=Concept:streamable-http fields=anchor,course_day,track",
            "DISCOVER registry.list_servers fields=name",
        ]
        demo_commands = []
        for i, raw in enumerate(raw_actions):
            action = _canonicalise_action(raw, call_index=i)
            cmd = Command.from_action_dict(action, cmd_id=f"cmd:{i:04d}")
            print(f"  {raw!r}\n    -> {cmd.kind}: {cmd.server}.{cmd.tool} fields={cmd.fields}")
            demo_commands.append(cmd)
        assert {c.kind for c in demo_commands} == {"mcp", "a2a", "discover"}

        answer_action = _canonicalise_action(
            'ANSWER {"text": "day 26, track P2T2"}', call_index=None
        )
        try:
            Command.from_action_dict(answer_action, cmd_id="cmd:9999")
        except ValueError as exc:
            print(f"\n  an 'answer' action correctly refuses to become a Command: {exc}")
        else:
            raise AssertionError("expected ValueError for an 'answer' action")

    print("\n=== Gateway.decide — valid decisions for every canonical command ===\n")
    ctx = RecordingGatewayContext(
        act="learner:sv-0401",
        sub="agent:demo-team",
        scopes=frozenset({"wiki.read"}),
        credits=100,
        round=1,
        call_index=0,
        leases=(),
        history=(),
    )
    assert isinstance(ctx, GatewayContext), "RecordingGatewayContext must structurally satisfy GatewayContext"
    gw = Gateway(ctx)
    for cmd in demo_commands:
        decision = gw.decide(cmd)
        print(f"  decide({cmd.server}.{cmd.tool}) -> verdict={decision.verdict!r} quarantine={decision.quarantine}")
        assert decision.verdict in DECISION_VERDICTS
        if decision.verdict != "deny":
            assert decision.call is not None
            call_dict = decision.call.to_dict() if hasattr(decision.call, "to_dict") else decision.call
            assert (call_dict["server"], call_dict["tool"]) in TOOL_SPECS

    print(f"\n=== Gateway.deny — the unused-by-default free-abstention path ===\n")
    denial = gw.deny(demo_commands[0], reason="demo: withholding pending a fresher registry.provenance read")
    print(f"  gw.deny(...) -> verdict={denial.verdict!r} reason={denial.reason!r} call={denial.call!r}")
    assert denial.verdict == "deny"
    assert denial.call is None
    assert demo_commands[0].cmd_id in gw._denied_cmd_ids

    print(f"\n=== own_telemetry — recorded on YOUR side only, never shown to the opponent ===\n")
    print(f"  {len(ctx.events)} events recorded on this ctx this run:")
    for ev in ctx.events:
        print(f"    {ev['name']}: {sorted(ev['payload'].keys())}")
    assert len(ctx.events) >= len(demo_commands) * 2 + 1  # decision_seen + decision_made per call, plus the deny

    print("\nAll agent/gateway.py demos passed.")
