# Memory: Smart Loops (Forge Field Manual No.09, Ferrox Labs, July 2026)

Source: Dropbox PDF (verified readable 2026-07-18). Vendor-flavored (promotes Flux Router/Wayland) —
trust the PATTERN, not their first-party benchmark numbers. Pattern matches FrugalGPT/RouteLLM lineage.

## Core pattern — the four seats
| Seat | Model class | Job | Cost shape |
|---|---|---|---|
| Planner | Frontier (Fable/Opus) | ONE pass: writes plan artifact + the /goal condition. Planner always writes the finish line, never the driver. | front-loaded, once |
| Driver | Mid/cheap (Sonnet/Haiku) | Grinds the turns against the plan | per turn × N (~90% of tokens) |
| Evaluator | Near-free (Haiku) | Reads transcript TAIL only, returns yes/no + reason. Never runs commands; judges surfaced output. | cents/loop if tail-scoped |
| Valve | Frontier | On stall: ONE diagnostic turn — names what driver misses, rewrites next step. Never does the work. | $ per stall, metered |

Same-family worked math: 10-turn loop all-frontier $7.00 vs routed $2.11–2.81 → 60–70% saved.

## Condition engineering (4 mandatory parts)
1. Measurable end state; 2. Stated check (a COMMAND whose output lands in transcript);
3. Constraints — what must NOT change (anti-condition-gaming); 4. Turn budget ("stop after N turns").
Test: could the evaluator answer yes/no from transcript alone, zero interpretation? If not, it's a prompt.

## Stall rule (valve trigger)
3 consecutive evaluator NOs with the SAME reason = stall → buy ONE frontier diagnostic turn → resume cheap.
3 different NOs = progress through a hard patch, keep going. Valve fires twice on same stall → stop loop,
return to planner (plan is wrong).

## Five rules the code taught the manual
1. Score the probe first — turn zero may already pass; loop never runs.
2. Freeze the finish line at loop start (no mid-loop renegotiation).
3. Only machinery earns "verified" — deterministic checks decide; LLM judges advise.
4. Ratchet acceptance: accept a turn only if it fixes ≥1 failing check and regresses 0.
5. Escalate narrow: valve gets the one uncracked check, never the whole task.

## Token-economy levers
- Input dominates loop bills (~79–97%); transcript growth compounds at input prices.
- Stable prefix first (plan + rules + condition) → cache-read discount every turn.
- Evaluator reads tail + surfaced results only (~1–2K tokens), never full history.
- Route by cost-of-error + verifiability, not task difficulty. Anything downstream of a check goes cheap.

## Failure catalog (routed-loop specific)
1 Evaluator theater (zero NOs = decoration). 2 Driver writes own finish line. 3 Valve thrash (escalating every NO).
4 Condition gaming (edits the findings/tests — constraints + separate evaluator model are the locks).
5 The clean-pass cost lie (instrument $/accepted-change). 6 Seat inflation ("just this once" upgrades).

## Cost log (per loop, 5 numbers)
turns · tokens in/out · evaluator NO-rate · valve fires · $ per accepted change.
