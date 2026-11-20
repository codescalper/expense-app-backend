# Reference board — Family Budget (mobile app redesign)

Built: 2026-11-20. Sourcing: web fetch of live sites this session (no `inspo` MCP connected).
**Honesty note:** the fetch tool returned rendered text/markdown, not CSS. Layout, copy and number
presentation below are **viewed**; palette, typography, radii and motion of these sites were **not
retrievable** and are **inferred, not viewed** — the palette was therefore generated
(`generate_palette.py`), not extracted.

## Design read
Mobile **app shell** (daily-use money tool) for Indian families (5–10 members, mixed tech comfort),
mode **Operate** (record, check, decide), lane **premium-confident fintech, India-first**.
Dials: variance 4/10 · motion 3/10 (battery-first, functional only) · density 6/10 · art direction 5/10.

## Sources
| Source | Status | Real traits retrieved |
|---|---|---|
| jupiter.money | viewed (text) | Big hero numerals with small labels ("50%", "₹5 Lakh"); Indian grouping (Lakh/Cr); explicit fee numbers; tiers framed as personas; dense stacked sections |
| fi.money | viewed (text) | Stat band with Indian grouping ("₹10,000 Cr+", "₹10 Lakh in 5 mins"); three product blocks; plain-language privacy line ("Your Privacy. Minus the jargon.") |
| cred.club | viewed (text) | Sparse, one idea per section; line-broken display headlines; exclusivity voice; trust stated as figures (rating, member count) |
| linear.app | viewed (text) | Interface-system reference: dense lists with small inline status counts ("Todo 71"), spec-sheet captions; calm, information-first |
| revolut.com | blocked (403) | — |
| monzo.com | cookie wall only | — |

## Traits we take
1. **Money is the hero**: one large numeral per card, label small and muted; Indian digit grouping everywhere (₹1,23,456.78; compact ₹1.2L / ₹3.4Cr).
2. **Trust through exact figures**, never adjectives: show the real amount, the real date, the real status ("Confirmed by you, not verified with your bank").
3. **Linear-style calm density** for lists: tight rows, small inline counts, status as small pills, no decorative chrome.
4. **One idea per card** (CRED's sparseness) on Home: each card answers one question.

## Anti-references (avoid)
- Unquantified trust claims ("secure", "smart") as copy.
- Purple-to-indigo gradients, glassmorphism, neon glows (generic AI fintech look).
- Emoji as icons; illustration-heavy empty states that slow the app down.
- Looping/ambient animation (battery) and confetti-style celebrations.
