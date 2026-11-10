# DESIGN.md — invoice-agent

Direction **C "Case File"** (phase 3.5, decided 2026-11-10; rationale in `docs/decisions.md`).
Reference canvas (private): https://claude.ai/artifact/KzzVogMqmcH6MgwDzg9pFP. The boards to build from are "C · Case File — Inbox" and "C · Case File — Review".

## Thesis
Each invoice is a *case*, read top to bottom like a short report: what was proposed, the evidence, how the action was set, what the agent thinks, your decision, the audit chain. Narrative over density; one column of reading width; the PDF is a side drawer, not the main stage.

## Palette
| Token | Hex | Use |
|---|---|---|
| `ground` | `#FFFFFF` | page background |
| `tint` | `#F4F5F9` | cards at rest, closed cases, audit stamps |
| `ink` | `#14161F` | text |
| `muted` | `#545A6D` | secondary text (≥ 4.5:1 on ground and tint) |
| `rule` | `#E2E4EC` | borders, dividers |
| `accent` | `#3346D3` | links, focus ring, audit chain dots |
| `approve` / `approve-bg` | `#2450C8` / `#E7ECFB` | Approve action |
| `clarify` / `clarify-bg` | `#9A4D00` / `#FBEBD9` | Clarify action, warning banners (text `#5E2F00`) |
| `block` / `block-bg` | `#B42318` / `#FBE4E1` | Block action, suspicious evidence |

Action colours are never the only signal: every badge also carries its word (Block / Clarify / Approve).

## Type
- Display / headings: **Space Grotesk** 600 — 40/1.1 (page title, verdict headline), 22 (section), 17–20 (case title).
- Body: **Public Sans** 400/500/600 — 16/1.55; small 14–15.
- Data (IDs, IBANs, amounts, hashes, timestamps): **JetBrains Mono** 400/500 — 13–14.
- All three are SIL OFL 1.1 (Google Fonts), free for this use.

## Layout and spacing
- Spacing scale (px): 4, 8, 12, 16, 24, 32, 48, 72. Section gap 48.
- Reading measure: `max-width: 760px` for the case column; inbox list `max-width: 1040px`.
- Radius 12px (cards, buttons, inputs); pills 999px.
- Touch targets ≥ 44px (decision buttons 48px). Fluid flex-wrap layouts, no horizontal page scroll; data tables, if any, live inside `overflow-x: auto`.

## Signature elements
1. **Case card** (inbox): action pill · vendor + mono ID · one-sentence plain-language summary · total + status. Closed cases sit on `tint`.
2. **Evidence pair**: "On the invoice" (on `block-bg`) next to "In our vendor record" (on `tint`), plus the quoted suspicious text with its rule ID.
3. **Gate line**: `Rule checks: X → Agent: Y → Proposed: max(X, Y)`, with the sentence "The agent can raise the action but never lower it below what the rules require."
4. **Audit chain**: vertical timeline of stamps (actor · event · time · `#hash ← #prev`), the pending reviewer step greyed until decided.

## Motion
| Token | Value | Applies to |
|---|---|---|
| `dur-fast` | 120ms | hover/press colour, focus ring |
| `dur-base` | 180ms | decision button selected state, drawer toggle |
| `dur-slow` | 260ms | new audit stamp appearing (opacity + 8px translate) |
| `ease-out` | `cubic-bezier(0.22, 1, 0.36, 1)` | everything entering or changing |
| `ease-in-out` | `cubic-bezier(0.65, 0, 0.35, 1)` | drawer slide only |

Never animated: the inbox list on load, the verdict headline, amounts, the waking/budget banners. Nothing scales from 0. `prefers-reduced-motion`: durations drop to 0 except colour changes.

## States that every screen must show
- Server waking up (Render free tier, ~30 s): status banner, content stays readable.
- Demo budget used up: uploads paused until 00:00 UTC, existing cases fully reviewable.
- Empty inbox, extraction failed, loading a case.

## Copy rules
Plain language first, rule IDs second. No model names in the UI. Every screen states that the data is synthetic.
