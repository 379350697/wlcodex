# WLCodex Live Stream UI Interaction Design

> **SUPERSEDED — historical design only.** Do not use this document as current
> product fact; use [the current semantic contract](../../product-semantics.md).

> Review date: 2026-06-04
> Scope: interaction quality, visual feedback, and typography improvements for all live stream frontend pages served by `wlcodex/live_stream/server.py`.

## Goal

Bring the live stream frontend from "functional but static" to "responsive and polished" by adding consistent visual feedback to every interactive element, improving reading comfort for CJK-heavy transcripts, and eliminating CSS duplication across eight inline templates.

The optimization is interaction-first. It does not introduce a frontend framework, build step, or component library. All changes remain within the current vanilla HTML/CSS/JS architecture embedded in Python string templates.

## Verified Baseline

The following facts were verified against the current repository:

- `wlcodex/live_stream/server.py` is 195 KB / 4539 lines. Eight HTML pages are embedded as Python triple-quoted string templates. Seven pages contain inline `<style>` blocks; one uses an external stylesheet.
- The only external CSS file is `wlcodex/live_stream/static/native_index.css` (59 lines / 757 bytes).
- Static asset delivery is already implemented via `_send_static_asset` (L1171–L1200) with path traversal protection, explicit content-type mapping, and `Cache-Control: no-cache`.
- The same CSS reset (`color-scheme: dark`, `box-sizing`, `body` font/background/color), button rules, and input rules are duplicated across all seven inline style blocks — approximately 400+ lines of redundancy.
- The entire frontend has one `@keyframes` animation (`composerPulse`, L2639) and three CSS transitions (`composer-activity-dot` sizing, `turn-fold-chevron` rotation, `approval-action` state changes).
- No `input:focus`, `textarea:focus`, or `select:focus` rules exist anywhere. Focus state is entirely unstyled.
- No `button:hover` or `button:active` rules exist. Buttons provide only disabled opacity (`0.56`) as visual state feedback.
- The `model-popover` component (L2678–L2679) toggles visibility via the `hidden` HTML attribute — no transition or animation.
- The composer activity indicator is a single dot that pulses between 7px and 13px. There is no typing-indicator pattern.
- Pages are designed mobile-first with touch targets ≥ 44px. iOS safe area insets are handled for the live page input dock. Responsive breakpoints exist for 760px and 820px.
- All pages are dark theme only. No light theme, no `prefers-color-scheme` media query, no theme toggle.
- ARIA coverage is limited to `aria-label` on a few buttons (`返回`, `菜单`, `发送`, `选择模型`, `上传照片`). No `role`, landmark, or skip-link infrastructure.

## Principles

1. Preserve current behavior first. Every visual change must keep existing page structure, route behavior, and JavaScript logic working.
2. No framework churn. Do not introduce React, Vue, Svelte, TailwindCSS, or any build system.
3. Interaction feedback is not decoration. Every hover, focus, active, and loading state must communicate a change in element availability or intent.
4. Respect the phone context. WLCodex is operated from a phone screen. Animations must be fast (≤ 250ms), touch targets must remain ≥ 44px, and `prefers-reduced-motion` must be honored.
5. CJK readability matters. Chinese content has higher information density per character. Line height, letter spacing, and paragraph rhythm must account for this.
6. CSS tokens before CSS rules. Extract shared values into custom properties once before writing page-specific overrides. This prevents another round of duplication.

## Spec 1: Shared CSS Extraction and Design Tokens

Create `wlcodex/live_stream/static/base.css` to hold the common reset, design tokens, and shared component styles that are currently duplicated across seven inline style blocks.

### Design Tokens

```css
:root {
  /* Background layers */
  --bg-root:     #000;
  --bg-elevated: #0d0e12;
  --bg-surface:  #111217;
  --bg-input:    #12151d;
  --bg-interact: #20242d;

  /* Borders */
  --border-subtle:  #24262d;
  --border-default: #30333a;
  --border-input:   #3f4550;

  /* Text */
  --text-primary:   #f7f7f8;
  --text-secondary: #d4d7de;
  --text-muted:     #9ca3af;
  --text-heading:   #ffffff;

  /* Semantic colors */
  --color-success: #22c55e;
  --color-warning: #f59e0b;
  --color-error:   #ef4444;
  --color-link:    #93c5fd;

  /* Button colors */
  --btn-primary-bg:    #f4f4f5;
  --btn-primary-color: #101114;
  --btn-secondary-bg:  #1b1f29;

  /* Timing */
  --duration-fast:   150ms;
  --duration-normal: 200ms;
  --ease-default: cubic-bezier(0.4, 0, 0.2, 1);

  /* Typography */
  --font-sans: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
  --font-mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;

  /* Radii */
  --radius-sm:     8px;
  --radius-md:     10px;
  --radius-lg:     13px;
  --radius-pill:   28px;
  --radius-circle: 50%;
}
```

### Shared Rules (in `base.css`)

The file contains: CSS reset, body defaults, `.circle` navigation buttons, `button` base + `.secondary` + `.warn` variants, `input`/`textarea`/`select` base, `.empty` state, `*:focus-visible` outline, and `prefers-reduced-motion` media query.

### Per-Page Changes

Each inline `<style>` block adds `<link rel="stylesheet" href="/static/base.css">` in `<head>` and removes all rules that are now provided by `base.css`. Page-specific styles remain inline.

### Acceptance Criteria

- All existing live stream, native route, and council page tests pass.
- New test asserts `/static/base.css` returns 200 with `text/css` content type.
- No visible rendering change when comparing pages before and after the extraction.
- `server.py` line count decreases by approximately 350–450 lines from removed inline CSS.

---

## Spec 2: Input Focus State

Add focus styling to all `input`, `textarea`, and `select` elements.

Current state: no `:focus` rules anywhere in the codebase. Users have no visual feedback when an input field is focused.

Target state:

```css
input:focus, textarea:focus, select:focus {
  outline: none;
  border-color: var(--color-link);
  box-shadow: 0 0 0 3px rgba(147, 197, 253, 0.15);
}
```

This goes into `base.css` and applies globally. No page-specific overrides needed.

Acceptance criteria:

- Every input field on every page shows a blue border and subtle glow ring when focused.
- The focus ring is visible against the dark background at WCAG 3:1 non-text contrast.
- No change to existing test assertions beyond new tests for the focus CSS presence.

---

## Spec 3: Button Micro-Interactions

Add hover, active, and loading states to all buttons.

Current state: buttons have only `opacity: .56` for disabled. No hover feedback, no active press feedback, no loading indicator.

Target state:

```css
/* Hover — brightness shift */
button:not(:disabled):hover { filter: brightness(0.92); }

/* Active — press scale */
button:not(:disabled):active {
  transform: scale(0.97);
  transition-duration: 50ms;
}

/* Primary button hover */
button:not(.secondary):not(.warn):not(:disabled):hover { background: #e8e8e9; }

/* Secondary button hover */
button.secondary:not(:disabled):hover {
  background: #252a36;
  border-color: #4f5560;
}

/* Send button stronger press */
.primary-action:not(:disabled):active { transform: scale(0.93); }

/* Loading spinner */
button.loading {
  pointer-events: none;
  position: relative;
  color: transparent !important;
}
button.loading::after {
  content: "";
  position: absolute;
  width: 18px; height: 18px;
  top: 50%; left: 50%;
  margin: -9px 0 0 -9px;
  border: 2px solid currentColor;
  border-right-color: transparent;
  border-radius: 50%;
  animation: btnSpin 0.5s linear infinite;
}
@keyframes btnSpin { to { transform: rotate(360deg); } }
```

JavaScript change: `submitPrompt()` adds `.loading` to the send button during the fetch, removes it in `finally`.

Acceptance criteria:

- Every button responds to hover with a visual brightness change.
- Every button responds to press with a scale-down effect.
- The send button shows a spinner while `submitPrompt` is in flight.
- Approval buttons (`approve`, `danger`) have their own hover colors.
- `prefers-reduced-motion` disables the spinner animation.

---

## Spec 4: Transcript Typography Optimization

Improve reading comfort of the live page transcript area, especially for Chinese text.

Current state (L2611): `font-size: 17px; line-height: 1.62`. Inline code uses `padding: 1px 5px` without a border. Code blocks use `background: #111318`.

Target state:

| Element | Change |
|---------|--------|
| `.transcript-body` | `line-height: 1.68`, add `letter-spacing: 0.01em` |
| `.transcript-body code` (inline) | Add `border: 1px solid rgba(255,255,255,0.06)`, change color to `#c4ccdb` |
| `.transcript-body pre` | Darken background to `#0c0e14`, increase padding to `14px 16px`, add `scrollbar-width: thin` |
| `.transcript-body a` | Add `transition: border-color 150ms ease`, `:hover` strengthens underline opacity |
| `.transcript-item.user .transcript-body` | Increase radius to `20px 20px 4px 20px`, shift background to `#1c2030` for slight blue tint |

Acceptance criteria:

- Visual diff on a transcript with mixed Chinese text, code, and links shows improved readability.
- No layout shift or overflow regression on mobile widths.
- Existing transcript rendering tests pass without assertion changes.

---

## Spec 5: Model Popover Transition

Replace `hidden` attribute toggle with CSS opacity/transform transition on `.model-popover`.

Current state (L2678): `model-popover[hidden] { display: none; }` — instant show/hide.

Target state:

```css
.model-popover {
  opacity: 1;
  transform: translateY(0) scale(1);
  transform-origin: bottom left;
  transition: opacity 180ms var(--ease-default),
              transform 180ms var(--ease-default);
}
.model-popover.closed {
  opacity: 0;
  transform: translateY(8px) scale(0.96);
  pointer-events: none;
}
```

JavaScript change: replace `modelPopover.hidden = ...` with `modelPopover.classList.toggle("closed", ...)`. Initial HTML uses `class="model-popover closed"` instead of `hidden`.

Acceptance criteria:

- Popover fades in over 180ms with a slight upward scale.
- Popover fades out over 180ms with a slight downward scale.
- `pointer-events: none` prevents clicks during close transition.
- Existing model settings tests pass.

---

## Spec 6: Typing Indicator Upgrade

Replace the single pulsing dot with a three-dot typing bounce animation.

Current state (L2637–2639): single `.composer-activity-dot` with `composerPulse` keyframe.

Target HTML:

```html
<div class="composer-activity" id="composerActivity" aria-hidden="true">
  <span class="composer-activity-dot"></span>
  <span class="composer-activity-dot"></span>
  <span class="composer-activity-dot"></span>
</div>
```

Target CSS:

```css
.composer-activity {
  display: flex; gap: 5px; align-items: center;
  height: 20px; margin: 8px 0 14px 2px;
  opacity: 0; transition: opacity 200ms ease;
}
.composer-activity.active { opacity: 1; }
.composer-activity-dot {
  width: 6px; height: 6px; border-radius: 50%;
  background: var(--text-muted);
  animation: typingBounce 1.4s ease-in-out infinite;
}
.composer-activity-dot:nth-child(2) { animation-delay: 0.15s; }
.composer-activity-dot:nth-child(3) { animation-delay: 0.30s; }
@keyframes typingBounce {
  0%, 60%, 100% { transform: translateY(0); opacity: 0.4; }
  30% { transform: translateY(-6px); opacity: 1; }
}
```

JavaScript change: update all references from `composerActivityDot` to `composerActivity`, toggle `.active` class instead of the old single-dot class logic.

Acceptance criteria:

- Three dots bounce sequentially when agent is working.
- Dots fade in/out smoothly when activity starts/stops.
- `prefers-reduced-motion` disables the bounce animation.

---

## Spec 7: Status Flow Bar Enhancement

Add breathing pulse animation to the run status dot during busy state.

Current state (L2603–2606): static `box-shadow` glow, no animation.

Target state:

```css
.run-pulse {
  transition: background 300ms ease, box-shadow 300ms ease;
}
.run-state.busy .run-pulse {
  animation: statusPulse 2s ease-in-out infinite;
}
@keyframes statusPulse {
  0%, 100% { box-shadow: 0 0 8px rgba(245,158,11,.3); }
  50% { box-shadow: 0 0 20px rgba(245,158,11,.7); }
}
```

Acceptance criteria:

- Busy state dot has a slow breathing glow effect.
- State transitions (idle→busy→done→failed) use color transitions, not instant jumps.
- `prefers-reduced-motion` disables the pulse animation.

---

## Spec 8: Fold/Expand Transition

Add height transition to `turn-fold` expand/collapse using `grid-template-rows` technique.

Current state (L2642–2654): chevron rotation has a 160ms transition but content toggle uses `display: none` hard cut.

Target state:

```css
.turn-fold-preview {
  display: grid; grid-template-rows: 1fr;
  transition: grid-template-rows 200ms ease, opacity 150ms ease;
  opacity: 1;
}
.turn-fold[open] .turn-fold-preview {
  grid-template-rows: 0fr; opacity: 0; overflow: hidden; padding: 0;
}
.turn-fold-body {
  display: grid; grid-template-rows: 0fr;
  opacity: 0; overflow: hidden;
  transition: grid-template-rows 200ms ease, opacity 200ms ease 50ms;
}
.turn-fold[open] .turn-fold-body {
  grid-template-rows: 1fr; opacity: 1;
}
```

Acceptance criteria:

- Expand and collapse animate height smoothly over 200ms.
- Preview text fades out as body fades in, and vice versa.
- Chevron rotation remains synchronized.

---

## Spec 9: Message Entry Animation

Add entry animation to new transcript items.

Target state:

```css
.transcript-item {
  animation: messageEnter 250ms var(--ease-default) forwards;
}
@keyframes messageEnter {
  from { opacity: 0; transform: translateY(8px); }
  to { opacity: 1; transform: translateY(0); }
}
.transcript-item.user {
  animation-name: userMessageEnter;
}
@keyframes userMessageEnter {
  from { opacity: 0; transform: translateX(12px); }
  to { opacity: 1; transform: translateX(0); }
}
.transcript-item.no-animate { animation: none; }
```

JavaScript change: add `.no-animate` class when loading history events; omit it for new streaming events.

Acceptance criteria:

- New messages fade in with a slight directional slide (assistant: up, user: right).
- History load does not trigger animation.
- `prefers-reduced-motion` disables entry animation.

---

## Spec 10: Setting Pill Modified State

Add visual indicator when model settings differ from defaults.

Current state (L2676): `.setting-pill` has static styling with no state variants.

Target state:

```css
.setting-pill {
  border: 1px solid transparent;
  transition: background var(--duration-fast) ease, border-color var(--duration-fast) ease;
}
.setting-pill.modified {
  border-color: rgba(147, 197, 253, 0.35);
  background: #1e2435;
}
.setting-pill:not(:disabled):hover { background: #353538; }
```

JavaScript change: `updateSettingSummary()` checks if current model/effort/tier differ from catalog defaults and toggles `.modified` class on the pill button.

Acceptance criteria:

- Pill shows blue border tint when settings differ from default.
- Pill reverts to neutral when settings return to default.

---

## Spec 11: Session List Hover and Active States

Add hover feedback and active-item indicator to native codex page list items.

Current state (L2260–2276): `.nav-row.active` changes label color to white. No hover rules exist.

Target state:

```css
.nav-row:not(:disabled):hover,
.project:not(:disabled):hover,
.recent:not(:disabled):hover {
  background: rgba(255, 255, 255, 0.04);
  border-radius: 12px;
}
.nav-row.active, .project.active {
  background: rgba(255, 255, 255, 0.05);
  border-radius: 12px;
  border-left: 3px solid var(--color-link);
  padding-left: 12px;
}
.nav-row:active, .project:active, .recent:active {
  background: rgba(255, 255, 255, 0.07);
}
```

Acceptance criteria:

- List items show background highlight on hover.
- Active item has a left border indicator.
- Touch press shows a slightly stronger background.

---

## Spec 12: Circle Button Press Effect

Add hover and active feedback to `.circle` navigation buttons (back, menu).

Target state:

```css
.circle {
  transition: background var(--duration-fast) ease, transform var(--duration-fast) ease;
}
.circle:hover { background: #2a2d35; }
.circle:active { transform: scale(0.90); background: #343840; }
#back:active { transform: scale(0.90) translateX(-2px); }
```

Acceptance criteria:

- Circle buttons respond to press with a scale-down effect.
- Back button has an additional leftward shift on press.

---

## Spec 13: Send Status Color Transition

Add color transition to `.send-status` state changes.

Current state (L2692–2694): instant color switch between default/error/ok.

Target state:

```css
.send-status {
  transition: color 300ms ease, opacity 300ms ease;
}
```

Acceptance criteria:

- Status text color changes smoothly when switching between idle, error, and ok states.
