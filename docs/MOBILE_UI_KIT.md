# Mobile UI kit reference

Use these instead of building one-off components. Import UI from `@/components/ui`, charts from
`@/components/charts`, utils from `@/utils/money` and `@/utils/dates`, tokens from `@/theme`
(`sizes`, `fontScale` from `@/theme/tokens`).

## Design rules for screens (style lock: `.tastemaker/style-lock.md`)
1. Colours only from `useTheme().colors`. No raw hex, no `rgba()` literals, no opacity on text to "soften" it (use `textMuted`).
2. Primary-coloured text = `primaryText` (dark `primary` fails as text). `AppText color="primary"` is remapped for you; raw `style={{ color: colors.primary }}` is not.
3. Text on a tinted fill (`primarySoft`, `surfaceMuted`) in the primary hue = `onPrimarySoft`.
4. Solid semantic/accent fills take `onSuccess/onDanger/onWarning/onAccent` labels (dark label in dark mode). Prefer soft tint + semantic text (`Badge`, `Banner`).
5. Money: `MoneyText` everywhere (Inter 600 tabular, muted ₹ at 0.7x). Income = success colour, expense = neutral text. Unbounded (`hero/display/title`) for screen titles and at most ONE hero number per card.
6. Never set `fontWeight` with our fonts: use `AppText weight="regular|medium|semibold"` or a variant.
7. Shape: cards 20 radius (`Card`), inputs/buttons 14, icon tiles 12, chips/pills/segments pill. Shadow only via `Card`/`elevation.card` (light only).
8. Spacing: screen gutter 20 (`spacing.gutter`, `Screen` does it), 16 between cards, 28 between sections (`spacing.section`), card padding 20.
9. Lists: `TransactionRow`/`ListRow` inside a `Card`, separated by a text-aligned `Divider`:
   `<Divider inset />` (68) for full-bleed rows in an unpadded card, `<Divider inset="card" />` (52)
   inside a padded `Card`/`RowGroup`. Never hand-roll the inset.
10. Buttons: one primary per screen; secondary = `variant="secondary"` (filled), never outline buttons.
    The UPI "Pay" action is `variant="accent"` (the only accent-filled button).
11. Empty states: `EmptyState` (56px tinted icon tile + one line + one action). No emoji or illustrations anywhere; icons from `Icon` only.
12. Motion: only kit motion (`PressableScale`, sheets, `Entrance`). If you animate: transform/opacity, native driver, `duration.*` + `easeOut`, no loops, and skip movement when `useReducedMotion()` is true.
13. Loading: `Skeleton`/`SkeletonList` (static blocks) sized like the content; `LoadingState` is the
    generic static skeleton page when the layout is unknown. No spinners for first loads.
14. Copy: no em dashes, no adjective trust claims; state exact figures and statuses.

## Tokens (light + dark)
Colours: `background, surface, surfaceMuted, border, text, textMuted, textInverse, primary,
primaryPressed, primaryText, primarySoft, onPrimarySoft, onPrimary, accent, accentSoft, onAccent,
success/successSoft/onSuccess, danger/dangerSoft/onDanger, warning/warningSoft/onWarning,
info/infoSoft, overlay, shadow, chartGrid, chartTrack, chart1…chart8` (categorical, derived from the
ink-blue base + terracotta accent, ≥3:1 on surface in both modes; ratios in `tokens.ts`). Legacy
money aliases: `income(Soft)` = success, `expense(Soft)` = text / surfaceMuted, `transfer(Soft)` =
primaryText / primarySoft. Contrast contract and measured ratios are documented at the top of
`src/theme/tokens.ts`.

Typography (`typography`, `TypographyVariant`): `hero` 34/40, `display` 28/34, `title` 22/28
(Unbounded 600) · `heading` 17/22, `bodyStrong` 15/21 (Inter 600) · `body` 15/21, `caption` 12/16
(Inter 400) · `label` 13/18, `micro` 11/14 +0.4 tracking (Inter 500) · `amount` 15/21 Inter 600
tabular. Font family names: `fonts.display|regular|medium|semibold` (`Unbounded_600SemiBold`,
`Inter_400Regular`, `Inter_500Medium`, `Inter_600SemiBold`), embedded via the expo-font plugin and
also loaded by `useAppFonts()` (`@/theme/fonts`) in the root layout.

`radius`: `sm 8 · tile 12 · md 14 · lg 20 · xl 20 (alias) · pill`. `spacing`: `xxs 2, xs 4, sm 8,
md 12, lg 16, gutter 20, xl 24, section 28, xxl 32, xxxl 48`. `sizes`: `touchTarget 44, control 52,
fab 56, row 64, tile 40, rowInset 68, iconSm/Md/Lg`. `elevation.card` (light only; use
`useTheme().elevation.card`, already resolved per scheme). `useTheme()` also returns `fonts`,
`sizes`, `motion`. No raw hex outside `tokens.ts`.

## Motion (`@/theme`)
- `motionTokens` / `duration`: `pressIn 90, pressOut 120, stateChange 150, listEntrance 180,
  sheet 240` ms; `easeOut` = `Easing.bezier(0.23, 1, 0.32, 1)`; `motion.press` (opacity 0.85,
  scale 0.98); `motion.list` (rise 8, stagger 30, first 6 items, first load only).
- `useReducedMotion()` (one shared AccessibilityInfo subscription) and `isReducedMotion()`.
- `PressableScale` (`@/components/ui`): Pressable with native-driver press feedback; props =
  PressableProps + `pressedScale?`, `pressedOpacity?`, `style` (object/array, not a function). Under
  Reduce Motion it only dims. Used by Button, FAB, QuickAdd tiles, pressable StatCard.
- Nothing in the kit loops. `BottomSheet` slides up 240ms ease-out (fade only under Reduce Motion).

## money.ts — amounts are strings, maths in bigint paise
- `parseAmountInput(text)` → `"1234.50"` | null (accepts commas, ₹/Rs/INR prefix; rejects negatives,
  >2 dp, > `MAX_AMOUNT` 999999999999.99).
- `toPaise(str)` → bigint (throws `RangeError`), `fromPaise(bigint)` → string, `normalizeAmount`,
  `isAmountString`.
- `addAmounts(...a)`, `sumAmounts(list)`, `subtractAmounts`, `compareAmounts` (-1|0|1), `isZeroAmount`,
  `isNegativeAmount`, `negateAmount`, `absAmount`.
- `formatINR(amt, { sign?: 'auto'|'always'|'never', compact?, hideZeroPaise?, symbol? })` →
  ₹1,23,45,678.90; compact ₹12.5K / ₹1.2L / ₹3.4Cr.
- `formatCurrency(amt, code='INR', opts)` (Intl with fallback).
- `percentOf(part, total, 0|1)` → number|null, `formatPercent` → "33.3%"|"–" (en dash; invalid `formatINR` input is "₹–"), `amountRatio` (for
  progress), `percentChange`. `amountToNumber` — chart geometry only.
- Typing helpers: `sanitizeAmountTyping`, `groupAmountTyping`, `groupIndian`.

## dates.ts — device-local
- `DatePreset = 'today'|'this_week'|'this_month'|'last_month'|'custom'`; `DATE_PRESETS` ({value,label}).
- `getPresetRange(preset, now?, custom?)` → `{from, to}` YYYY-MM-DD inclusive (Mon–Sun weeks).
- **Parse API dates with `parseISODate`/`toDate`, never `new Date('YYYY-MM-DD')` (that's UTC).**
- `toISODate`, `toISOTime` ("HH:mm"), `startOfDay/Week/Month`, `endOfWeek/Month`, `addDays`, `addMonths`,
  `isSameDay`, `differenceInCalendarDays`.
- Display: `formatShortDate` ("20 Nov"), `formatTime` ("3:05 pm"), `formatTimeString`, `formatDateTime`,
  `relativeDay` (Today/Yesterday/…), `monthLabel` ("Nov 2026"; accepts "YYYY-MM"), `monthShortLabel`,
  `monthKey`, `formatDateRange` ("1 – 30 Nov").
- `groupByDay(items, getDate, now?)` → `{key, title, data}[]` (SectionList-ready).

## Charts (`@/components/charts`) — plain numbers, fixed height, a11y summary, empty state
- `DonutChart({ data: {label, value, color?}[], size=176, thickness=20, centerValue?, centerLabel?, formatValue?, title?, emptyText? })`
- `DonutLegend({ data, formatValue?, showPercent=true, maxItems? })` (collapses to "Other").
- `BarChart({ data: {label, values: number[]}[], series: {key, label, color?}[], height=200, width?, formatValue?, showLegend, highlightIndex?, title?, emptyText? })`
  — income vs expense: series colours `colors.income` / `colors.expense`.
- `LineChart({ data: {label, value}[], height=180, width?, color?, area=true, baseline: 'zero'|'auto', dots: 'none'|'last'|'all', curve: 'smooth'|'linear', formatValue?, title? })`
- `ProgressBar({ progress /*1 = 100%*/, tone='auto', thresholds?, height=8, accessibilityLabel, accessibilityValueText? })`
  (auto: <75% primary, 75–100% warning, >100% danger); `progressTone(p)`.
- `ProgressRing({ progress, size=72, thickness=8, tone?, children? })`.

Charts use the categorical palette, Inter 500 axis labels with tabular figures, and no animation.

## UI (`@/components/ui`)
Base: `AppText, Button, TextField, FormField, Screen, RefreshScreen, Card, Banner, EmptyState,
ErrorState, LoadingState, ListRow, Divider, Avatar, PressableScale`. Optional props added in the redesign
(all existing props unchanged):
- `AppText`: `weight?: 'regular'|'medium'|'semibold'`, `tabular?`; per-variant Dynamic Type caps
  (hero/display 1.2, title/micro 1.3, else 1.6); `color="primary"` renders `primaryText`; a
  `fontWeight` in `style` is converted to the matching Inter file.
- `Button`: `variant?: 'primary'|'secondary'|'ghost'|'danger'|'accent'` (`ButtonVariant`),
  `icon?: IconName`, `size?: 'md'|'sm'` (52 / 40px), `style?`. secondary = surfaceMuted fill;
  danger = dangerSoft fill + danger label; **accent** = solid accent fill + `onAccent` label (white
  4.73 in light; the dark background colour in dark, where white on accent is 2.82). Accent is for
  the UPI pay action only (`<Button variant="accent" icon="arrowUpRight" title="Pay via UPI" />`);
  it replaced the old `components/upi/PayButton`. Labels use tabular figures.
- `Banner`: `title?`; leading icon per tone. `EmptyState`: `icon?: IconName` (default `inbox`),
  `tone?: 'primary'|'danger'` (primary tile icon = `onPrimarySoft`). `ListRow`: `leading?`.
- `Divider({ inset?: boolean | number | 'card' })`: `true` = 68 (16 row padding + 40 tile + 12 gap)
  for full-bleed rows in an unpadded card (transaction and UPI lists); `'card'` = 52 (40 tile + 12
  gap) for rows inside a padded `Card` or `RowGroup`, where the card padding supplies the first 16;
  a number for other leading widths (e.g. `36 + spacing.md` for 36px tiles).
- `LoadingState({ label? })`: static skeleton page (title line, one hero card block, one card of 3
  `SkeletonRow`s), no spinner, nothing moves; `label` (default "Loading") is the busy region's
  accessibility label (`testID="loading-state"`).
- `RefreshScreen({ children, onRefresh, fab?, edges=['bottom'], bottomInset=0, gap='card'|'section', testID? })`:
  scroll screen with pull to refresh, 20 gutter, 16 between cards (`gap="section"` = 28) and room
  for a floating `fab`. The one container for Home, Reports, report drill-downs, Budgets, planning
  lists/details, family screens and notification preferences (replaced `ReportScreen`,
  `PlanningScreen` and the family `RefreshableScreen`, which were identical apart from the gap).
- `Entrance({ index, enabled, style?, children })` + `useFirstLoad(key, ready = true)`: the one
  first-load list entrance. 180ms ease-out fade + 8px rise, 30ms stagger, first 6 items only,
  native driver, never loops. `useFirstLoad` is true only while a list's first data mounts (a 400ms
  window after `ready`), once per app session per `key`: revisits, refetches, filter/scope
  switches and pagination never replay it. `enabled` is read once at mount. Reduce Motion: no
  movement (items render in place; switching it on mid-entrance jumps to the end).
  `const e = useFirstLoad('transactions', items.length > 0); <Entrance index={i} enabled={e}>…`.
  Keys in use: `home`, `accounts`, `transactions`, `upi-history`, `budgets`, `savings`, `families`,
  `notifications`. `resetFirstLoad()` is for tests.
- Grouped rows (moved from `components/settings`, which now only re-exports them):
  `IconTile({ icon, tone?: TileTone, size=40 })` (soft tint + matching fg, `TILE_TONES`; 56 for
  status tiles), `SectionLabel({ children })` (micro uppercase muted header above a group),
  `RowGroup({ children, testID? })` (Card with 16 compact padding, skips null children, separates
  rows with `<Divider inset="card" />`), `ThemedSwitch` (primary track on, `chartTrack` off),
  `SwitchRow({ title, subtitle?, icon?, tone?, value, onValueChange, disabled?, testID? })` (ListRow
  + switch; without `icon` it is the plain form toggle, which replaced planning's own SwitchRow).
- `MoneyText`: default variant is now `amount`; `symbol?: 'scaled'|'inline'` (default scaled ₹).
- `Skeleton*` are static blocks (no pulse). `Icon` adds `alert-circle`, `info`, `inbox`, and the
  account/action set `bell, user, log-out, trash, copy, share, link, server, mail, pencil,
  settings, download` (Lucide geometry, 2px round stroke). Use them, not stand-ins: server URL =
  `server`, sign out / leave = `log-out`, delete = `trash`, copy / share = `copy` / `share`,
  notifications = `bell` (`NotificationBell` draws the kit `bell`), edit = `pencil`, export =
  `download`.
- `SegmentedControl<T>({ options: {value,label}[], value, onChange, accessibilityLabel?, tones? })`
- `Chip({ label, selected?, onPress?, dotColor?, icon?, onRemove? })`; `ChipGroup<T>` single
  (`{options, value, onChange, allowDeselect?}`) or `multiple` (`value: T[]`), `layout: 'scroll'|'wrap'`.
- Selected states on `primarySoft` (Chip, SegmentedControl primary/transfer tones, ScopeSwitcher via
  SegmentedControl, SelectSheet/PickerField checks) label in `onPrimarySoft` (5.25:1 light), never
  `primaryText` (4.02:1 on primarySoft in light). Selected chips also carry a 1px primary ring.
- `Badge({ label, tone: success|warning|danger|info|neutral|primary, dot?, size })`, `StatusPill`;
  `statusPillProps(status)` → {label, tone} (pending→Pending/warning, user_confirmed→User-confirmed/info,
  failed→Failed/danger, …).
- `MoneyText({ amount, currency='INR', type: income|expense|transfer|refund|adjustment|neutral, signed?, color?, variant, compact?, hideZeroPaise?, struck?, align?, adjustsFontSizeToFit? })`
- `AmountInput({ value: raw, onChangeValue(raw, parsed|null), label, error?, hint?, tone?, autoFocus?, onSubmitEditing? })`
  — keep `raw` in state; parse on submit.
- `StatCard({ label, amount, type?, signed, compact?, subtitle?, delta?: {label, direction, upIsGood?, caption?}, icon?, loading?, onPress?, size: 'default'|'hero' })`
- `SectionHeader({ title, subtitle?, action?: {label, onPress} })`, `SearchBar({ value, onChangeText, placeholder?, onSubmit?, autoFocus? })`
- `SelectSheet<T>({ visible, onClose, title, options: {value,label,subtitle?,icon?,color?,disabled?}[], value?, onSelect, searchable? })`;
  `SelectField<T>` (form field + sheet: adds `label, placeholder, error`); `BottomSheet({ visible, onClose, title?, children })`.
- `DateField({ label, value: 'YYYY-MM-DD'|'HH:mm'|null, onChange, mode: 'date'|'time', minimumDate?, maximumDate?, error? })`
- `CategoryIcon({ name, color?, icon?, size=40, shape, accessible? })`: kit Icon for a known Lucide
  name, otherwise the name's first letter. Emoji from the API (older clients) are ignored, never
  drawn. `pickers/CategoryGlyph` is the same component.
- `TransactionRow({ title, subtitle?, meta?, amount, currency?, type, status?: {label,tone}, category?: {name,color?,icon?}, leading?, struck?, onPress?, onLongPress? })`
- `FAB({ onPress, bottom?, right?, floating })`, `QuickAddMenu({ visible, onClose, onSelect(action: 'expense'|'income'|'upi'|'transfer'), actions? })`,
  `QuickAddFab({ onSelect, ...fab })` — place outside ScrollView; pass `bottom` to clear the tab bar.
- `confirmDialog({ title, message?, confirmLabel='Delete', cancelLabel, destructive })` → Promise<boolean>;
  `confirmDestructive(opts, onConfirm)`.
- `Skeleton`, `SkeletonRow`, `SkeletonList({ rows })`.
- `ScopeSwitcher({ options: {id,label,subtitle?}[], value, onChange })` — presentational; wire to `src/store/scope.ts`.
- Extras: `Icon` (UI glyphs + Lucide category icons), `haptics.*`, `withAlpha`, `chartColor`, `colorForKey`, `isHexColor`
  (the colour helpers live in `@/theme/color` and are re-exported here, so no colour maths sits outside the theme).
