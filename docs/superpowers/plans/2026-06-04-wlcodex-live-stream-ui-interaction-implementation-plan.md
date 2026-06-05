# WLCodex Live Stream UI Interaction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add consistent visual feedback, typography improvements, and CSS architecture to the live stream frontend without introducing a framework or build step.

**Architecture:** Create a shared `base.css` static asset with design tokens and common component styles. Each inline template removes duplicated CSS and links `base.css`. Page-specific styles remain inline. All JavaScript changes are minimal — class toggles and loading state management.

**Tech Stack:** Vanilla CSS custom properties, vanilla JavaScript, existing Python string templates, existing `asyncio` static asset server, existing pytest suite, GitNexus impact/detect-changes.

**Spec:** `docs/superpowers/specs/2026-06-04-wlcodex-live-stream-ui-interaction-design.md`

---

## Scope Order

This plan is intentionally phased. Complete each task and verify tests before starting the next.

1. Create `base.css` with design tokens and shared styles (Spec 1).
2. Link `base.css` from each template and remove duplicated inline CSS (Spec 1 continued).
3. Add button micro-interactions (Spec 3) — included in `base.css`.
4. Add transcript typography improvements to live page (Spec 4).
5. Add model popover transition to live page (Spec 5).
6. Add typing indicator upgrade to live page (Spec 6).
7. Add status pulse, fold transition, message entry, and remaining polish (Specs 7–13).

---

## Task 1: Create `base.css` with Design Tokens and Shared Styles

**Implements:** Spec 1 (partial — file creation), Spec 2 (input focus), Spec 3 (button interactions), Spec 12 (circle button effects).

**Files:**
- Create: `wlcodex/live_stream/static/base.css`
- Test: `tests/test_worker_live_stream_server.py`

- [ ] **Step 1: Run GitNexus impact before editing**

Run:

```bash
npx gitnexus impact --repo wlcodex _send_static_asset --direction upstream
```

Expected: report callers of static asset delivery. Risk should be low since we are only adding a new file.

- [ ] **Step 2: Write failing test for `base.css` route**

Add a test to `tests/test_worker_live_stream_server.py` that requests `/static/base.css` through the server test helper.

The test must assert:

```python
assert status_code == 200
assert "text/css" in content_type
assert "--bg-root" in response_body
assert "--color-link" in response_body
assert "--ease-default" in response_body
assert "focus-visible" in response_body
assert "prefers-reduced-motion" in response_body
```

- [ ] **Step 3: Run the focused test and verify RED**

Run:

```bash
.venv/bin/pytest tests/test_worker_live_stream_server.py -k "base_css" -vv
```

Expected: fail because `/static/base.css` does not exist yet.

- [ ] **Step 4: Create `base.css`**

Create `wlcodex/live_stream/static/base.css` with the following sections:

1. **CSS Reset**: `:root { color-scheme: dark; }`, `* { box-sizing: border-box; }`.
2. **Design Tokens**: All `--bg-*`, `--border-*`, `--text-*`, `--color-*`, `--btn-*`, `--duration-*`, `--ease-*`, `--font-*`, `--radius-*` custom properties from Spec 1.
3. **Body defaults**: `margin: 0; min-height: 100vh; font-family: var(--font-sans); background: var(--bg-root); color: var(--text-primary); -webkit-font-smoothing: antialiased;`.
4. **Circle buttons** (`.circle`): grid layout, 50×50px, border-radius 50%, border `var(--border-default)`, background `#202126`. Add `transition: background var(--duration-fast) ease, transform var(--duration-fast) ease`. Add `:hover { background: #2a2d35; }`, `:active { transform: scale(0.90); background: #343840; }`.
5. **Buttons** (`button`): min-height 44px, border-radius `var(--radius-md)`, background `var(--btn-primary-bg)`, color `var(--btn-primary-color)`, `font-weight: 760`. Add `transition: opacity var(--duration-fast) var(--ease-default), background var(--duration-fast) var(--ease-default)`. Add `:not(:disabled):hover { filter: brightness(0.92); }`, `:not(:disabled):active { transform: scale(0.97); transition-duration: 50ms; }`, `:disabled { opacity: .56; cursor: not-allowed; }`. Add `.secondary` and `.warn` variants with hover states. Add `.loading` pseudo-element spinner with `@keyframes btnSpin`.
6. **Inputs** (`input, textarea, select`): width 100%, border `var(--border-input)`, background `var(--bg-input)`, color `var(--text-primary)`, border-radius `var(--radius-sm)`. Add `:focus { outline: none; border-color: var(--color-link); box-shadow: 0 0 0 3px rgba(147, 197, 253, 0.15); }`.
7. **Empty state** (`.empty`): color `var(--text-muted)`, padding 24px 0, text-align center.
8. **Focus visible** (`*:focus-visible`): outline 2px solid `var(--color-link)`, outline-offset 2px.
9. **Reduced motion** (`@media (prefers-reduced-motion: reduce)`): set animation-duration and transition-duration to 0.01ms.

Do NOT include page-specific styles. Only shared, reusable rules.

- [ ] **Step 5: Run focused test and verify GREEN**

Run:

```bash
.venv/bin/pytest tests/test_worker_live_stream_server.py -k "base_css" -vv
```

Expected: pass.

- [ ] **Step 6: Run live stream regression**

Run:

```bash
.venv/bin/pytest tests/test_worker_live_stream_server.py tests/test_worker_live_stream_native_routes.py -q
```

Expected: all pass.

---

## Task 2: Link `base.css` from Council Review Page and Remove Duplicated CSS

**Implements:** Spec 1 (council review template deduplication).

**Files:**
- Modify: `wlcodex/live_stream/server.py` (L1707–L1977, `_council_review_page`)
- Test: `tests/test_worker_live_stream_native_routes.py`

- [ ] **Step 1: Run GitNexus impact**

Run:

```bash
npx gitnexus impact --repo wlcodex _council_review_page --direction upstream
```

Expected: identify route handler and tests.

- [ ] **Step 2: Write test asserting `base.css` link**

Add or update a council review page test to assert:

```python
assert '<link rel="stylesheet" href="/static/base.css">' in html
```

- [ ] **Step 3: Verify RED**

Run:

```bash
.venv/bin/pytest tests/test_worker_live_stream_native_routes.py -k "council_review" -vv
```

Expected: fail because the template does not link `base.css` yet.

- [ ] **Step 4: Modify `_council_review_page` template**

In the template string (L1707–L1977):

1. Add `<link rel="stylesheet" href="/static/base.css">` after `<title>`.
2. Remove these lines from the inline `<style>`:
   - `:root { color-scheme: dark; }` (now in `base.css`)
   - `* { box-sizing: border-box; }` (now in `base.css`)
   - `body { margin: 0; min-height: 100vh; font-family: ...; background: #050506; color: #f7f7f8; }` — replace with `body { background: #050506; }` (only the non-default background override)
   - `.circle { ... }` — keep only size override if it differs from `base.css` (46px vs 50px)
   - Generic `input, textarea, select { ... }` — remove; keep page-specific overrides only
   - Generic `button` disabled rule — remove (in `base.css`)

3. Keep all page-specific styles: `.panel`, `.stack`, `label`, `.row`, `.run`, `.muted`, `.seat-list`, `.results`, `.seat`, `.result`, `.badge`, `.summary`, `.session-link`, `.error`, `@media`.

- [ ] **Step 5: Verify GREEN**

Run:

```bash
.venv/bin/pytest tests/test_worker_live_stream_native_routes.py -k "council" -vv
```

Expected: pass.

- [ ] **Step 6: Visual spot-check**

Start the server with `--fake-backend` and load `/council` in a browser. Verify the page renders identically to the pre-change state. Focus an input field and verify the blue ring appears.

---

## Task 3: Link `base.css` from Council Seats Page

**Implements:** Spec 1 (council seats template).

**Files:**
- Modify: `wlcodex/live_stream/server.py` (L1980–L2123, `_council_seats_page`)
- Test: `tests/test_worker_live_stream_native_routes.py`

Follow the same pattern as Task 2:

- [ ] **Step 1: Run GitNexus impact on `_council_seats_page`**
- [ ] **Step 2: Add `<link>` tag, remove duplicated reset/body/circle/input/button CSS**
- [ ] **Step 3: Keep page-specific styles: `.toolbar`, `.seat-grid`, `.seat`, `.role`, `.mission`, `.switch`, `@media`**
- [ ] **Step 4: Run council tests and verify GREEN**

```bash
.venv/bin/pytest tests/test_worker_live_stream_native_routes.py -k "council_seats" -vv
```

---

## Task 4: Link `base.css` from Token Entry Page

**Implements:** Spec 1 (token entry template).

**Files:**
- Modify: `wlcodex/live_stream/server.py` (L2128–L2186, `_native_token_entry_page`)

- [ ] **Step 1: Add `<link>` tag**
- [ ] **Step 2: Remove duplicated CSS, keep page-specific layout: `body { display: grid; place-items: center; }`, `main`, `h1`, `p`, `form`, `.status`, and `input`/`button` overrides (larger border-radius 14px)**
- [ ] **Step 3: Run relevant tests**

```bash
.venv/bin/pytest tests/test_worker_live_stream_native_routes.py -q
```

---

## Task 5: Link `base.css` from Login Ticket Page

**Implements:** Spec 1 (login ticket template).

**Files:**
- Modify: `wlcodex/live_stream/server.py` (L2198–L2230, `_native_login_ticket_page`)

Note: This template uses f-string with `{{` escaping. Keep escaping correct.

- [ ] **Step 1: Add `<link>` tag**
- [ ] **Step 2: Remove duplicated CSS, keep page-specific layout**
- [ ] **Step 3: Run tests**

---

## Task 6: Link `base.css` from Native Codex Page and Add List Interactions

**Implements:** Spec 1 (native codex template), Spec 11 (session list interactions).

**Files:**
- Modify: `wlcodex/live_stream/server.py` (L2237–L2568, `_native_codex_page`)

- [ ] **Step 1: Run GitNexus impact on `_native_codex_page`**

```bash
npx gitnexus impact --repo wlcodex _native_codex_page --direction upstream
```

- [ ] **Step 2: Add `<link>` tag, remove duplicated reset/body/circle/input/button CSS**

- [ ] **Step 3: Keep page-specific styles: `.devices`, `.device-chip`, `.dot`, `.laptop`, `.nav-row`, `.project`, `.recent`, `.icon-folder`, `.icon-chat`, `.label`, `.section-title`, `.more-sessions`, `.time`, `.meta`, `.controls`**

- [ ] **Step 4: Add session list hover and active styles (Spec 11)**

Add these rules to the inline `<style>`:

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
.recent .time { transition: color var(--duration-fast) ease; }
.recent:hover .time { color: var(--text-secondary); }
```

- [ ] **Step 5: Run tests**

```bash
.venv/bin/pytest tests/test_worker_live_stream_native_routes.py -q
```

---

## Task 7: Link `base.css` from Native Index Page

**Implements:** Spec 1 (native index — update existing external CSS).

**Files:**
- Modify: `wlcodex/live_stream/static/native_index.css`
- Modify: `wlcodex/live_stream/server.py` (L1688–L1703, `_native_index_page`)

- [ ] **Step 1: Add `<link rel="stylesheet" href="/static/base.css">` before the existing `native_index.css` link**

- [ ] **Step 2: In `native_index.css`, remove:**
  - `html { color-scheme: dark; }`
  - `* { box-sizing: border-box; }`
  - `body { margin: 0; ... font-family: ...; background: #000; color: #f7f7f8; }`

Keep only: `body { padding: 28px; }`, `main`, `h1`, `.provider`, `.provider.council`, `.provider span`, `.provider small, .empty`.

- [ ] **Step 3: Run tests**

```bash
.venv/bin/pytest tests/test_worker_live_stream_native_routes.py -k "provider_index" -vv
```

---

## Task 8: Link `base.css` from Live Page and Add All Interaction Upgrades

**Implements:** Spec 1 (live page dedup), Spec 4 (typography), Spec 5 (popover transition), Spec 6 (typing indicator), Spec 7 (status pulse), Spec 8 (fold transition), Spec 9 (message entry), Spec 10 (pill state), Spec 13 (send status).

This is the largest task because the live page is ~1800 lines. Split into sub-steps.

**Files:**
- Modify: `wlcodex/live_stream/server.py` (L2577–L4391, `_live_page`)
- Test: `tests/test_worker_live_stream_server.py`

- [ ] **Step 1: Run GitNexus impact**

```bash
npx gitnexus impact --repo wlcodex _live_page --direction upstream
```

- [ ] **Step 2: Add `<link>` tag, remove duplicated CSS (Spec 1)**

Add `<link rel="stylesheet" href="/static/base.css">` after `<title>`.

Remove from inline `<style>`:
- `:root { color-scheme: dark; }`
- `* { box-sizing: border-box; }`
- `body { margin: 0; min-height: 100vh; font-family: ...; background: #000; color: #f7f7f8; }`
- Generic `button { ... }` and `button:disabled`, `button.secondary`, `button.warn` rules
- Generic `input { ... }` rule
- `.empty { ... }`

Keep all page-specific CSS.

- [ ] **Step 3: Run tests to verify no regression**

```bash
.venv/bin/pytest tests/test_worker_live_stream_server.py -q
```

- [ ] **Step 4: Apply transcript typography (Spec 4)**

In the live page inline `<style>`, modify:

- `.transcript-body`: add `letter-spacing: 0.01em`, change `line-height` from `1.62` to `1.68`.
- `.transcript-body code`: add `border: 1px solid rgba(255,255,255,0.06)`, change `color` to `#c4ccdb`, change `font` to `0.88em var(--font-mono)`.
- `.transcript-body pre`: change `background` to `#0c0e14`, `padding` to `14px 16px`, add `scrollbar-width: thin; scrollbar-color: #383c46 transparent`.
- `.transcript-body a`: add `transition: border-color 150ms ease`.
- Add `.transcript-body a:hover { border-bottom-color: rgba(147, 197, 253, .7); }`.
- `.transcript-item.user .transcript-body`: change `border-radius` to `20px 20px 4px 20px`, change `background` to `#1c2030`.

- [ ] **Step 5: Apply model popover transition (Spec 5)**

CSS: Remove `.model-popover[hidden]`. Add `.model-popover` transition properties. Add `.model-popover.closed` rule.

HTML: Change `hidden` attribute to `class="... closed"` on the popover div.

JS: Replace `modelPopover.hidden` checks/sets with `modelPopover.classList.contains("closed")` / `modelPopover.classList.toggle("closed", ...)`.

- [ ] **Step 6: Apply typing indicator upgrade (Spec 6)**

HTML: Replace single dot div with three-dot container.

CSS: Replace old `.composer-activity-dot` rules with `.composer-activity` container + `.composer-activity-dot` bounce + `@keyframes typingBounce`.

JS: Rename `composerActivityDot` references to `composerActivity`.

- [ ] **Step 7: Apply status pulse (Spec 7)**

CSS: Add transition to `.run-pulse`, add `animation: statusPulse` to `.run-state.busy .run-pulse`, add `@keyframes statusPulse`.

- [ ] **Step 8: Apply fold/expand transition (Spec 8)**

CSS: Replace `display: none` toggle on `.turn-fold-preview` and `.turn-fold-body` with `grid-template-rows` transition technique.

- [ ] **Step 9: Apply message entry animation (Spec 9)**

CSS: Add `.transcript-item` animation, `@keyframes messageEnter`, `.transcript-item.user` animation override, `@keyframes userMessageEnter`, `.transcript-item.no-animate`.

JS: Add `.no-animate` class to items rendered from history loads.

- [ ] **Step 10: Apply setting pill state (Spec 10)**

CSS: Add `border`, `transition`, `.modified`, `:hover` to `.setting-pill`.

JS: In `updateSettingSummary()`, toggle `.modified` on `modelSettingsButton` based on whether settings differ from defaults.

- [ ] **Step 11: Apply send status transition (Spec 13)**

CSS: Add `transition: color 300ms ease, opacity 300ms ease` to `.send-status`.

- [ ] **Step 12: Apply loading state to send button (Spec 3)**

JS: In `submitPrompt()`, add `continueButton.classList.add("loading")` before fetch, `continueButton.classList.remove("loading")` in `finally`.

- [ ] **Step 13: Run tests**

```bash
.venv/bin/pytest tests/test_worker_live_stream_server.py -q
```

Expected: all pass.

---

## Task 9: Link `base.css` from Legacy Live Page

**Implements:** Spec 1 (legacy live page).

**Files:**
- Modify: `wlcodex/live_stream/server.py` (L4395–L4539, `_legacy_live_page`)

- [ ] **Step 1: Add `<link>` tag**
- [ ] **Step 2: Remove duplicated body/button/input CSS, keep page-specific `.event`, `.meta`, `.controls`, `.row`, approval styles**
- [ ] **Step 3: Run tests**

```bash
.venv/bin/pytest tests/test_worker_live_stream_server.py -q
```

---

## Task 10: Final Regression and GitNexus Detect Changes

- [ ] **Step 1: Run full test suite**

```bash
.venv/bin/pytest -q
```

Expected: all pass.

- [ ] **Step 2: Run lint**

```bash
.venv/bin/python -m ruff check .
```

Expected: no new warnings.

- [ ] **Step 3: Run GitNexus detect changes**

```bash
npx gitnexus detect-changes --repo wlcodex
```

Expected: changes scoped to `server.py`, `base.css`, `native_index.css`, and test files. No unexpected symbol changes.

- [ ] **Step 4: Visual acceptance smoke**

Start the server and manually verify each page:

| Page | Route | Check |
|------|-------|-------|
| Native index | `/native` | Renders identically, base.css loads |
| Council review | `/council` | Input focus rings, button hover/active |
| Council seats | `/council/seats` | Input focus rings, button hover/active |
| Token entry | `/` | Input focus ring, button hover/active/press |
| Native codex | `/native/codex` | Session list hover, active indicator, circle button press |
| Live page | `/workers/{id}/live` | Typography, popover animation, typing dots, status pulse, fold transition, message entry, pill state, send status, send button loading |
| Legacy live | (fallback) | Basic styling preserved |
