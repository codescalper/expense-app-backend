# Style lock — Family Budget (mobile)

Established: 2026-11-20. Source: starter scaffolding (mood = Premium / confident, classified from
"fintech, banking, family budget") + generated palette (`generate_palette.py --mood premium --seed 8`,
light and dark companion pair) + reference board (layout/number traits viewed; visuals inferred).

## Palette (runtime toggle — both modes ship, follow system setting)
Hue family: base 259.8 (ink-blue, deliberately cooler than the indigo/purple AI default),
triadic accent 19.8 (warm terracotta).

| Token | Light | Dark | Role |
|---|---|---|---|
| background | #F8FAFF | #0D1116 | page |
| surface | #FFFFFF | #161A20 | cards, sheets |
| surfaceMuted | #ECF1F9 | #1F242C | inputs, chips, secondary fills |
| border | #DBE0E8 | #262B33 | hairlines (decorative only) |
| text | #161B23 | #EBF2FE | primary text, hero numbers |
| textMuted | #556070 | #9AA6B8 | labels, meta |
| primary | #4771B6 | #4A74B9 | primary buttons/fills, active tab, focus ring |
| primaryText | #4771B6 | #8FB3EE | links / primary-coloured TEXT (dark needs the lighter tone) |
| onPrimary | #FFFFFF | #FFFFFF | label on primary fill |
| primarySoft | #E1EAF8 | #1C2A40 | selected chip/segment background |
| accent | #B2585A | #DE7F80 | UPI / "pay" highlight, brand warmth — sparingly |
| success / income | #1D7A4C | #4CC38A | income amounts, success states |
| danger | #B42B25 | #F07A72 | errors, overspend, destructive |
| warning | #8A5800 | #E5B552 | budget 75–100 %, pending |
| *Soft tints* (successSoft, dangerSoft, warningSoft, accentSoft) | 8–12 % of the hue over surface | 14–18 % of the hue over surface | pill/banner backgrounds; their text uses the matching semantic colour |

Button label: white on primary — 4.88:1 (light), 4.68:1 (dark).

## Color contract
Floors: text ≥ 4.5 · fill visibility, icons, state borders ≥ 3.0 · hairlines decorative.

Matrices run with `check_contrast.py --matrix` (seed-8 roles + extended semantic set):
- **Light text-safe:** text on bg/surface/surfaceMuted (15.2–17.3); textMuted on bg 6.11 / surface 6.38 / surfaceMuted 5.62; white on primary 4.88; white on accent 4.73; primary on bg 4.67 (link text OK); accent on bg 4.53; success on bg 5.10 / surface 5.33 / surfaceMuted 4.70; danger on bg 6.07; warning on bg 5.78; white on success 5.33 / danger 6.34 / warning 6.04.
- **Light UI-safe only:** primary on surfaceMuted 4.30, accent on surfaceMuted 4.17 → icons/large text only there.
- **Dark text-safe:** text on bg/surface/surfaceMuted (13.9–16.8); textMuted on bg 7.68 / surface 7.08 / surfaceMuted 6.33; **primaryText on bg 8.88 / surface 8.19**; accent on bg 6.72 / surface 6.06; success on bg 8.55; danger 6.97; warning 9.98; white on primary 4.68.
- **Dark UI-safe only:** primary on bg 4.05 / surface 3.65 → primary is a FILL/ICON colour in dark, never body text (use primaryText).
- **Dark decorative (never for labels):** white on success 2.22 / danger 2.72 / warning 1.90 / accent 2.82 → **in dark mode, solid semantic or accent fills take a `background`-coloured (dark) label**, or use the soft tint + coloured text instead.
- Hairline borders are decorative everywhere (bg/border 1.27) — never the only state indicator; focus/error states use primary/danger rings (≥3:1).

## Typography
- Display: **Unbounded SemiBold 600** — screen titles, hero balances, big stat numerals. Geometric, confident, distinctive vs. system fonts.
- Body/UI: **Inter** 400 / 500 / 600 — everything else. Chosen over the catalog's Albert Sans because money needs Inter's tabular figures (`fontVariant: ['tabular-nums']` on all amounts).
- Only these 4 font files are embedded (size budget). Fonts load via the expo-font config plugin (embedded at build time, no runtime fetch).
- Scale (px / line-height): hero 34/40 (Unbounded) · display 28/34 (Unbounded) · title 22/28 (Unbounded) · heading 17/22 (Inter 600) · body 15/21 (Inter 400) · bodyStrong 15/21 (Inter 600) · label 13/18 (Inter 500) · caption 12/16 (Inter 400) · micro 11/14 (Inter 500, +0.4 tracking, uppercase only for tiny section labels).
- Amounts: Inter 600 tabular for rows; Unbounded for one hero number per card max. Currency symbol at 0.7× size, muted.

## Shape language
- Radius: 8 (chips, small buttons, inputs inner) · 14 (inputs, buttons, list groups) · 20 (cards, sheets) · pill only for status pills and filter chips.
- Shadow: light mode only, one level: `shadowColor text, opacity 0.06, radius 16, offset y 6, elevation 2` on cards. Dark mode: no shadow; 1px border `border` + surface lift.
- Borders: 1px hairline on cards in both modes (separation without heavy shadow).
- Buttons: 52px tall primary, 14 radius, full-width in forms; secondary = surfaceMuted fill (no outline buttons).

## Density & spacing
- Base unit 4px; tokens 4 · 8 · 12 · 16 · 20 · 24 · 32 · 48.
- Screen gutter 20. Card padding 20 (content) / 16 (compact rows) / 24 (hero card). Gap between cards 16; between sections 28.
- List rows: 64px min (icon 40 + 2 lines), 12px vertical padding, separators inset 68 (aligned to text).
- Overall density: calm-dense (Linear-like lists, CRED-like one-idea cards on Home).

## Structure
App shell only (bottom tabs + stacks) — Step 2.5 macrostructure rotation skipped per the skill (app-shell screens). Shell: 5 bottom tabs (Home, Transactions, Budgets, Reports, Profile); centre quick-add FAB; large-title headers (Unbounded) that collapse on scroll are NOT used (motion budget) — static large title + compact header on push screens.

## Navigation chrome
- Tab bar: surface background, hairline top border, active = primary icon + label (primaryText in dark), inactive = textMuted. No pill indicator animation.
- Push headers: background colour, no shadow, back chevron, title Inter 600 17.

## Reference intelligence
- Reference board: `.tastemaker/reference-board.md` — viewed sources (text only); visuals inferred.
- Design read: app shell for Indian families, mode Operate, lane premium-confident fintech, India-first.
- Dials: variance 4 · motion 3 · density 6 · art direction 5.
- Foundation: existing repo kit (`src/components/ui`, `src/components/charts`, `src/theme`) — restyle, don't replace; no new UI dependencies.
- Direction contract — Thesis: "your family's money, stated exactly." First viewport (Home): one hero balance, one primary action (Pay via UPI), then one-idea cards. System: tokens above. Risk: Unbounded display can feel loud — limit to titles + one hero number per screen.
- Anti-references: purple gradients, glassmorphism, emoji icons, looping motion, adjective trust copy.

## Taste memory
- Profile priors used: none (cold start; no `~/.tastemaker/profile.md`).
- Decision log: `.tastemaker/decisions.log` (pending-review entries; autonomous run).

## Mood descriptors
exact, calm, warm-confident

## Assets
- Icons: the repo's own SVG `Icon` set (Lucide geometry, 1.75–2px stroke, round caps) — one set only; no emoji as icons anywhere.
- Illustrations: none (performance + size budget); empty states use a single tinted Icon in a 56px rounded tile + one line of copy + one action.
- Logo: existing app icon/adaptive icon preserved (no rebrand requested).

## Motion (battery-first)
- Engine: React Native Animated / Reanimated (already installed) — no new motion libs.
- Feel: quick and restrained. Curve: ease-out `Easing.bezier(0.23, 1, 0.32, 1)`; never ease-in for UI.
- Durations: press feedback 90–120 ms (opacity 0.85 / scale 0.98, native driver) · sheet/modal 220–260 ms · list entrance 180 ms, 8 px rise, stagger 30 ms, max first 6 items, first load only · state change (pill/status) 150 ms crossfade.
- Never: looping/ambient animation, parallax, animated gradients, confetti, layout-property animation, animations on every scroll.
- Skeleton: static tinted blocks (no shimmer loop) — or a single slow opacity pulse that STOPS when data arrives; respects Reduce Motion (no pulse at all).
- Reduced motion: all movement off; crossfades only.
- Verified by: `audit_motion.py` + `anti_slop_scan.py` + `check_component_coherence.py` on `mobile/src` (gate).

## Do not
- No raw hex outside `src/theme/tokens.ts`.
- No primary-coloured body text in dark mode (use primaryText).
- No white labels on semantic/accent fills in dark mode.
- No em dashes in shipped copy (gate fails on them).
- No new font weights/families beyond the 4 embedded files.

## Build stamp
2026-11-20, design-system consolidation pass (after the four-screen redesign). Gates run from
`mobile/` with the tastemaker scripts; scores are from a code walk of each main screen, not from
device screenshots.

### Gate outputs
```
python anti_slop_scan.py src __tests__       Anti-slop scan passed: scanned 253 files.   (0 HIGH, 0 MEDIUM)
python audit_motion.py src                   Motion audit passed: scanned 221 files.
python check_component_coherence.py src      Component coherence check passed: scanned 221 files, one icon family / one motion engine / scale tokens in use.
grep raw hex / rgba() outside src/theme      none (colour maths moved to src/theme/color.ts)
grep fontWeight outside AppText / theme      none (navigation font map moved to src/theme/fonts.ts)
grep emoji characters in src / __tests__     none (legacy goal pictographs stay as \u{...} code points in planning/format.ts, mapped to kit Icons)
grep em dash in src / __tests__              none
grep Animated.loop / withRepeat / repeat:-1  none
npx tsc --noEmit                             exit 0
npx expo lint                                exit 0
npx jest                                     30 suites, 519 tests passed (two consecutive full runs)
npx expo export --platform android           1923 modules, one Hermes bundle 5.7 MB (5,707,036 bytes)
```

### Self-critique (main screens: Home, Transactions, Budgets, Reports, Profile, UPI pay/confirm, transaction form)
| Criterion | Score | Notes |
|---|---|---|
| Hierarchy | 4 | Home now has one hero numeral (balance, 34); net savings and the budget summary there step down to `title` (22). Detail screens keep one hero per card. |
| Consistency | 4 | One entrance (`Entrance`/`useFirstLoad`), one refresh container (`RefreshScreen`), one `SwitchRow`, one divider rule (`inset` / `inset="card"`), one category tile. Key/value detail cards use 8px vertical card padding vs 4 on row groups (denser rows, kept on purpose). |
| Restraint | 4 | Accent fill only on the UPI pay action (Home tile + `Button variant="accent"`). Icons added only to edit/delete/share/export actions. Home still carries both the UPI tile and the quick-add FAB. |
| Specificity | 4 | Exact figures and statuses; India-first copy; placeholder test data replaced with a realistic payee. |
| Color harmony | 4 | Contract re-checked: no primary-coloured text (AppText remaps), `onPrimarySoft` on every primarySoft selection, `onAccent`/`onDanger` on solid fills, EmptyState tile icon moved to `onPrimarySoft`. |
| Typography | 4 | Inter tabular on every amount (Button labels now tabular too); Unbounded limited to titles, the hero and secondary card figures at `title`. |
| Spatial accuracy | 4 | 20 gutter / 20 card padding / 16 between cards / 28 between sections via `RefreshScreen` and `Screen`; savings contributions divider fixed (was 68 inside a padded card, now 52). |

States: every list on these screens has a skeleton (static), an `ErrorState`/Banner and an empty state;
Reports cards carry their own skeleton/error/empty through `ReportCard`.

### Open items
- The Android export bundles `MaterialSymbols_400Regular.ttf` (976 KB) for the NativeTabs `md` icons,
  a fifth font file against the "only 4 font files" rule; needs a build/dependency decision.
- `ActivityIndicator` remains for pagination footers and in-button busy states (never first loads).
