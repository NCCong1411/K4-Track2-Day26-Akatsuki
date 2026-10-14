"""Pure routing, budgeting and result-cache policy for the Day 26 agent.

The kit prices tools from TOOL_SPECS. A disciplined query/body/provenance
round costs nine credits under the current table; the 100-credit duel
budget still requires pacing across ten rounds. Catalog defaults are costly.
Replica drift alone establishes no freshness direction: choose only from
provenance. Cached results require the same round, replica, etag and mask.
No network, files, randomness or wall-clock reads.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

# kit.mcp.specs is a collaborator's file (workspace hard rule 2). It is
# present and stable as of this writing, but this module must still degrade
# gracefully if a concurrent edit ever makes it briefly unimportable — the
# fallback table below covers exactly the tools this file's own functions
# and demo reference, nothing more.
try:
    from kit.mcp.specs import TOOL_SPECS, cost as _spec_cost
    _SPECS_AVAILABLE = True
except ImportError:  # pragma: no cover - collaborator file
    TOOL_SPECS = {}
    _SPECS_AVAILABLE = False

    def _spec_cost(server: str, tool: str, fields: tuple[str, ...] = (), n_rows: int = 1) -> int:
        """Degraded fallback: a small, hand-copied anchor-price table
        (CONTRACTS.md 3.4's own named anchor prices) covering only the
        (server, tool) pairs this file's functions/demo touch. Real pricing
        always comes from kit.mcp.specs when it is importable — this table
        exists so the file still RUNS, not so it stays authoritative."""
        anchors = {
            ("slides", "query"): 4,  # fields=[title,body], n_rows=1
            ("slides", "get_frame"): 4,  # default fields
            ("registry", "provenance"): 1,
            ("registry", "list_servers"): 12,  # fields=[*]
            ("glossary", "list_terms"): 10,  # default == full dump
        }
        return anchors.get((server, tool), 5)  # 5: an honest "I don't know" default, not 0


__all__ = [
    "ROUNDS_PER_DUEL",
    "SAFE_STARTING_RESERVE",
    "CATALOG_TRAP_TOOLS",
    "DEPRECATED_SUCCESSORS",
    "disciplined_round_cost",
    "careless_round_cost",
    "is_catalog_trap",
    "cheap_mask",
    "successor_of",
    "BudgetPacer",
    "ReplicaChoice",
    "pick_replica",
    "ResultCache",
    "should_delegate",
]

ROUNDS_PER_DUEL = 10

# A pacing target, not a hard rule: if you have spent MORE than this
# fraction of your remaining budget by the time you decide whether THIS
# round's call is affordable, you are trending toward the careless curve
# above, not the disciplined one. `BudgetPacer.is_affordable` below uses it
# as its one, deliberately simple, heuristic.
SAFE_STARTING_RESERVE = 0.5  # keep at least half the ORIGINAL pool as a floor

# The two named "punishment button" tools (FINAL-PLAN.md 4.1): their
# DEFAULT field mask is their full, most expensive dump, not a cheap
# starting point. Calling either with no `fields=` is never an accident
# worth repeating.
CATALOG_TRAP_TOOLS: frozenset[tuple[str, str]] = frozenset(
    {("registry", "list_servers"), ("glossary", "list_terms")}
)

# CONTRACTS.md 4.2 mechanic 8: `slides.search` is deprecated in favour of
# `slides.query`. Kept here as data (mirroring kit/mcp/specs.py's own
# "the economy is data, not code" philosophy) so a `route`/`budget` job can
# look a tool up without re-deriving deprecation from `TOOL_SPECS` by hand.
DEPRECATED_SUCCESSORS: Mapping[tuple[str, str], tuple[str, str]] = {
    ("slides", "search"): ("slides", "query"),
}


def disciplined_round_cost() -> int:
    """The module docstring's "DISCIPLINED" total, computed live against
    the real cost table (or the degraded fallback) rather than hard-coded —
    so if `kit/mcp/specs.py` is ever retuned, this number moves with it
    instead of silently lying."""
    return (
        _spec_cost("slides", "query", fields=("title", "body"), n_rows=1)
        + _spec_cost("slides", "get_frame")
        + _spec_cost("registry", "provenance")
    )


def careless_round_cost() -> int:
    """The module docstring's "CARELESS" total, same live-computation
    reasoning as `disciplined_round_cost` above."""
    return (
        _spec_cost("registry", "list_servers", fields=("*",))
        + _spec_cost("glossary", "list_terms")
        + 3 * _spec_cost("slides", "get_frame", fields=("*",))
    )


def is_catalog_trap(server: str, tool: str, fields: tuple[str, ...]) -> bool:
    """True iff `(server, tool)` is one of the two "punishment button"
    tools AND the caller passed no explicit mask (`fields` empty) or asked
    for everything (`("*",)`) — i.e. is about to pay the DEFAULT/full price
    rather than a deliberately chosen cheap one. A `route`/`budget` job can
    use this as the trigger for "rewrite this call's fields before letting
    it through"."""
    if (server, tool) not in CATALOG_TRAP_TOOLS:
        return False
    return fields in ((), ("*",))


def cheap_mask(server: str, tool: str, fields_you_will_actually_cite: tuple[str, ...]) -> tuple[str, ...]:
    """Given the fields your answer will actually cite, return exactly
    those, sorted — the discipline that keeps `slides.get_frame` at 4
    credits instead of 9, and `registry.list_servers` at 2 instead of 12
    (CONTRACTS.md 3.4's own named anchor prices). This function does not
    know what your answer needs; YOU do — pass the honest set. Passing an
    EMPTY set here is itself informative: it means you are about to make a
    call whose result you do not plan to cite, which is the `wasteful`
    class waiting to happen (CONTRACTS.md 6.4's detector: "credits spent >
    the round allowance").

    `server`/`tool` are accepted (and validated against `TOOL_SPECS` when
    available) purely so a caller gets an early, loud `KeyError` for a typo
    rather than a silently wrong mask two calls later."""
    if _SPECS_AVAILABLE and (server, tool) not in TOOL_SPECS:
        raise KeyError(f"{server}.{tool} is not a known tool in kit.mcp.specs.TOOL_SPECS")
    return tuple(sorted(set(fields_you_will_actually_cite)))


def successor_of(server: str, tool: str) -> tuple[str, str] | None:
    """`(server, tool)`'s non-deprecated replacement, or `None` if it is
    not deprecated at all. A `route` job's cheapest possible win: rewriting
    `slides.search` to `slides.query` before forwarding costs you nothing
    and removes the `wasteful` "used a deprecated tool" detector hit
    (CONTRACTS.md 6.4) entirely."""
    return DEPRECATED_SUCCESSORS.get((server, tool))


@dataclass
class BudgetPacer:
    """Tracks YOUR OWN running spend across a duel and answers one
    question: "can I still afford this round the way I've been playing?"
    Deliberately simple — a flat per-round reserve check, not a model of
    the whole remaining duel — because the arithmetic in this file's module
    docstring is already the whole lesson: stay near the disciplined
    number every round and the pacing question barely matters; drift
    toward the careless number even once and no pacer saves you.

    This is YOUR bookkeeping, independent of `GatewayContext.credits` (the
    arena's authoritative figure) — the two SHOULD agree; if they ever
    disagree, trust `ctx.credits`, and treat the mismatch itself as
    something worth a `Telemetry.note(...)` (agent/telemetry.py)."""

    starting_pool: int = 100
    rounds_total: int = ROUNDS_PER_DUEL
    _spent: int = field(default=0, init=False)
    _spent_by_round: dict[int, int] = field(default_factory=dict, init=False)

    def record_spend(self, round_no: int, cost: int) -> None:
        if cost < 0:
            raise ValueError(f"cost must be non-negative, got {cost}")
        self._spent += cost
        self._spent_by_round[round_no] = self._spent_by_round.get(round_no, 0) + cost

    @property
    def credits_left(self) -> int:
        return self.starting_pool - self._spent

    @property
    def credits_spent(self) -> int:
        return self._spent

    def is_affordable(self, round_no: int, cost: int, *, reserve: float = SAFE_STARTING_RESERVE) -> bool:
        """`True` iff spending `cost` now leaves at least `reserve` of the
        ORIGINAL pool in hand — a simple, conservative floor. Reasonable
        for the FIRST half of a duel; by round 8-10 you know your real
        remaining need better than a flat reserve does, and a `budget` job
        that only ever consults this without ever revisiting the reserve
        as rounds run out will end up over-cautious late, not over-spent —
        the safer of the two failure directions, but still a real
        one-line simplification worth outgrowing."""
        floor = self.starting_pool * reserve
        return (self.credits_left - cost) >= floor

    def bankrupt_by(self) -> int | None:
        """The first round number (1-indexed) at which `credits_left`
        actually went negative, or `None` if it never did. Used by this
        file's own `__main__` demo to make the module docstring's "bankrupt
        by round 3" claim a live, checked fact instead of an assertion in
        prose."""
        running = self.starting_pool
        for round_no in sorted(self._spent_by_round):
            running -= self._spent_by_round[round_no]
            if running < 0:
                return round_no
        return None


@dataclass(frozen=True, slots=True)
class ReplicaChoice:
    replica: str  # "w" | "c"
    reason: str


def pick_replica(*, path_id: str | None, known_drifting: bool,
                 prefers_fresh: bool = True,
                 proven_fresher: str | None = None) -> ReplicaChoice:
    """Prefer the proven fresher replica; drift alone proves no direction.

    Without provenance, w is only a routing default. A current-version
    answer still needs both replicas compared before claiming freshness.
    """
    if proven_fresher not in (None, "w", "c"):
        raise ValueError("proven_fresher must be 'w', 'c', or None")
    if prefers_fresh and proven_fresher:
        return ReplicaChoice(proven_fresher,
                             f"path_id={path_id!r}: provenance establishes {proven_fresher} as fresher")
    qualifier = "drift unresolved" if known_drifting else "freshness not compared"
    return ReplicaChoice("w", f"path_id={path_id!r}: default route only; {qualifier}")


@dataclass
class ResultCache:
    """Cache only a result from the same round, replica, etag and field mask.

    Missing metadata yields a miss or is not stored, preserving the old call
    shape without granting stale content authority in a later round.
    """

    _store: dict[tuple[int, str, str, str, tuple[str, ...]], Mapping[str, Any]] = field(default_factory=dict)

    @staticmethod
    def _key(anchor: str, fields: tuple[str, ...], round_no: int,
             replica: str, etag: str) -> tuple[int, str, str, str, tuple[str, ...]]:
        return (round_no, replica, etag, anchor, tuple(sorted(set(fields))))

    def get(self, anchor: str, fields: tuple[str, ...], *, round_no: int | None = None,
            replica: str | None = None, etag: str | None = None) -> Mapping[str, Any] | None:
        if round_no is None or replica not in ("w", "c") or not etag:
            return None
        return self._store.get(self._key(anchor, fields, round_no, replica, etag))

    def put(self, anchor: str, fields: tuple[str, ...], row: Mapping[str, Any], *,
            round_no: int | None = None, replica: str | None = None,
            etag: str | None = None) -> None:
        if round_no is None or replica not in ("w", "c") or not etag:
            return
        self._store[self._key(anchor, fields, round_no, replica, etag)] = dict(row)

    def __len__(self) -> int:
        return len(self._store)


def should_delegate(
    *,
    own_confidence: float,
    calls_used_this_window: int,
    calls_allowed_this_window: int,
    credits_left: int,
    delegate_cost: int,
    min_confidence_to_skip: float = 0.85,
) -> bool:
    """JOB-neutral heuristic for "is an A2A verifier (e.g.
    `citation-checker.verify_source`, rate-limited 2-per-3-rounds per
    CONTRACTS.md section 4.2 mechanic 5) worth its cost RIGHT NOW". Three
    gates, all must pass:

      1. You are not already confident enough to skip it
         (`own_confidence < min_confidence_to_skip`) — delegating when you
         are already sure is the exact `wasteful` pattern the per-tool
         rate window (mechanic 5) exists to make you ration.
      2. The rate window has room left this duel
         (`calls_used_this_window < calls_allowed_this_window`) — a call
         that would just come back `rate_limited` is worse than skipping
         it: it still costs credits (CONTRACTS.md 3.3: `rate_limited` is
         "yes, no refund") for zero information.
      3. You can afford it without breaking your own reserve
         (`credits_left >= delegate_cost`, checked bare here — combine with
         `BudgetPacer.is_affordable` for the full picture including your
         reserve floor).

    STARTER HEURISTIC: `min_confidence_to_skip=0.85` is a placeholder
    threshold, not a tuned one — `own_confidence` itself is not computed by
    anything in this file; it is whatever your own reasoning (guided by
    `agent/prompt.md`'s citation contract) decides it is. Wire a real
    confidence signal in before you trust this gate under pressure."""
    if own_confidence >= min_confidence_to_skip:
        return False
    if calls_used_this_window >= calls_allowed_this_window:
        return False
    if credits_left < delegate_cost:
        return False
    return True


if __name__ == "__main__":
    print("=== agent.strategy: the round-cost arithmetic, computed live ===\n")
    disciplined = disciplined_round_cost()
    careless = careless_round_cost()
    print(f"  disciplined_round_cost() = {disciplined} cr  (FINAL-PLAN.md 4.3: '8-11')")
    print(f"  careless_round_cost()    = {careless} cr  (FINAL-PLAN.md 4.3: '~49')")
    assert 8 <= disciplined <= 11, disciplined
    assert careless >= 45, careless
    print(f"  kit.mcp.specs available: {_SPECS_AVAILABLE}")

    print("\n=== is_catalog_trap / cheap_mask / successor_of ===\n")
    assert is_catalog_trap("registry", "list_servers", ()) is True
    assert is_catalog_trap("registry", "list_servers", ("name",)) is False
    assert is_catalog_trap("slides", "get_frame", ()) is False
    print("  registry.list_servers with no mask -> catalog trap: True")
    print("  registry.list_servers with fields=(name,) -> catalog trap: False")

    mask = cheap_mask("slides", "get_frame", ("title", "title", "body"))
    print(f"  cheap_mask('slides','get_frame', ('title','title','body')) -> {mask}")
    assert mask == ("body", "title")

    succ = successor_of("slides", "search")
    print(f"  successor_of('slides','search') -> {succ}")
    assert succ == ("slides", "query")
    assert successor_of("slides", "query") is None

    print("\n=== BudgetPacer: disciplined-at-the-CEILING barely lasts the duel; careless does not ===\n")
    disciplined_pacer = BudgetPacer()
    for round_no in range(1, ROUNDS_PER_DUEL + 1):
        disciplined_pacer.record_spend(round_no, disciplined)
    print(
        f"  disciplined (ceiling, {disciplined}cr) x10 rounds -> spent={disciplined_pacer.credits_spent} "
        f"credits_left={disciplined_pacer.credits_left} bankrupt_by={disciplined_pacer.bankrupt_by()}"
    )
    # The retuned kit economy makes this 9 credits per round, leaving ten.
    assert disciplined_pacer.bankrupt_by() is None
    assert disciplined_pacer.credits_left == 100 - ROUNDS_PER_DUEL * disciplined
    nine_rounds_pacer = BudgetPacer()
    for round_no in range(1, ROUNDS_PER_DUEL):  # 9 rounds, not 10
        nine_rounds_pacer.record_spend(round_no, disciplined)
    print(f"  disciplined (ceiling) x9 rounds  -> credits_left={nine_rounds_pacer.credits_left} (still positive)")
    assert nine_rounds_pacer.credits_left >= 0

    careless_pacer = BudgetPacer()
    bankrupt_round = None
    for round_no in range(1, ROUNDS_PER_DUEL + 1):
        careless_pacer.record_spend(round_no, careless)
        if bankrupt_round is None and careless_pacer.credits_left < 0:
            bankrupt_round = round_no
    print(
        f"  careless per round -> spent={careless_pacer.credits_spent} "
        f"credits_left={careless_pacer.credits_left} bankrupt_by={careless_pacer.bankrupt_by()}"
    )
    assert careless_pacer.bankrupt_by() == bankrupt_round
    assert careless_pacer.bankrupt_by() <= 3, careless_pacer.bankrupt_by()

    print("\n=== BudgetPacer.is_affordable: the reserve floor ===\n")
    mid_pacer = BudgetPacer()
    mid_pacer.record_spend(1, 60)
    print(f"  after spending 60/100, credits_left={mid_pacer.credits_left}")
    assert mid_pacer.is_affordable(2, 5) is False  # would drop below the 50-credit reserve floor
    assert mid_pacer.is_affordable(2, -20) is True  # nonsense cost, but arithmetic still holds
    fresh_pacer = BudgetPacer()
    assert fresh_pacer.is_affordable(1, disciplined) is True

    print("\n=== pick_replica: the naive heuristic, and why it is naive ===\n")
    choice_clean = pick_replica(path_id="d8f95a7b", known_drifting=False)
    choice_drifting = pick_replica(path_id="d8f95a7b", known_drifting=True)
    print(f"  known_drifting=False -> {choice_clean}")
    print(f"  known_drifting=True  -> {choice_drifting}")
    assert choice_clean.replica == "w"
    assert choice_drifting.replica == "w"  # drift alone does not prove freshness
    assert pick_replica(path_id="d8f95a7b", known_drifting=True,
                        proven_fresher="c").replica == "c"

    print("\n=== ResultCache: same (anchor, fields) is a hit; a wider mask is a genuine miss ===\n")
    cache = ResultCache()
    anchor = "Frame:3f2a9c11/w/041"
    assert cache.get(anchor, ("title", "body")) is None
    cache.put(anchor, ("title", "body"), {"title": "Streamable HTTP", "body": "..."},
              round_no=1, replica="w", etag="sha256:one")
    hit = cache.get(anchor, ("body", "title"), round_no=1, replica="w", etag="sha256:one")
    print(f"  cache.get(anchor, ('body','title')) after put(('title','body')) -> {hit}")
    assert hit == {"title": "Streamable HTTP", "body": "..."}
    miss = cache.get(anchor, ("title", "body", "meta"), round_no=1,
                     replica="w", etag="sha256:one")
    print(f"  cache.get(anchor, wider mask incl. 'meta') -> {miss}  (a real miss, not stale data)")
    assert miss is None
    assert len(cache) == 1

    print("\n=== should_delegate: three gates, all must pass ===\n")
    cases = [
        dict(own_confidence=0.4, calls_used_this_window=0, calls_allowed_this_window=2, credits_left=50, delegate_cost=6),
        dict(own_confidence=0.95, calls_used_this_window=0, calls_allowed_this_window=2, credits_left=50, delegate_cost=6),
        dict(own_confidence=0.4, calls_used_this_window=2, calls_allowed_this_window=2, credits_left=50, delegate_cost=6),
        dict(own_confidence=0.4, calls_used_this_window=0, calls_allowed_this_window=2, credits_left=3, delegate_cost=6),
    ]
    expected = [True, False, False, False]
    for case, want in zip(cases, expected):
        got = should_delegate(**case)
        print(f"  should_delegate({case}) -> {got}")
        assert got == want, (case, got, want)

    print("\nAll agent/strategy.py demos passed.")
