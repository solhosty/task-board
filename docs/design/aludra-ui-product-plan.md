# Aludra UI product and migration plan

Status: design candidate, not approved for implementation.

## Product model

- A project contains tasks. The project board is the primary navigation surface.
- A task owns one continuous user conversation, delivery state, and task-level policy.
- A task contains sessions. A session belongs to exactly one harness and model.
- A session contains attempts. Retries and verification remain nested unless they need human attention.
- A normal boundary creates a fresh session on the same harness. Quota or unavailability may create a fresh session on another eligible harness.
- Workflow stage is user-editable organization. Runtime status, session status, verification, approval, and pull-request state remain separate system facts.

## Frontend architecture decision

Replace the current static `index.html` + imperative `app.js` frontend with a React and TypeScript application. Use Vite for the initial build unless packaging constraints discovered during implementation require a different bundler.

Adopt shadcn/ui as source-owned shared components, backed by Radix primitives and Tailwind design tokens. Aludra styling lives in semantic CSS variables rather than direct shadcn defaults. Components are copied into the repository and become part of the product codebase; this avoids a runtime dependency on a hosted design system.

The shared UI layer starts with:

- Button, IconButton, Badge, Avatar/AgentMark, Tooltip, Separator
- Tabs, DropdownMenu, ContextMenu, Select, Command, Popover
- Dialog, AlertDialog, Sheet, Drawer
- Input, Textarea, Checkbox, Switch, FormField
- Card, EmptyState, Skeleton, Toast
- TaskCard, HarnessStack, AttentionBadge, PullRequestBadge
- SessionCard, AttemptRow, TimelineEvent, HandoffSummary, IntegrityStatus
- SettingsScope, InheritedValue, ConnectionCard

Use dnd-kit for accessible board dragging, keyboard movement, collision handling, and persisted ordering. Do not build drag-and-drop directly from HTML drag events in production.

## Aludra design system

Core brand tokens come from the supplied Signal Gold assets:

- Obsidian `#0B0D12`: typography and dense operational detail; not a default full-height navigation background
- Ivory `#F5F3EE`: primary canvas
- Slate `#777E8C`: secondary text
- Signal Blue `#5367FF`: primary action and active execution
- Signal Gold `#F3C969`: attention, review, and the Aludra signal

Semantic tokens must cover canvas, panel, elevated panel, border, muted text, primary action, focus ring, success, warning, danger, harness identity, and diff states. All interactive controls use shared components; no unstyled browser-default inputs remain.

Interaction character: use a dense, content-first operational feel inspired by PostHog’s product ergonomics, not a visual copy. Keep layouts crisp and direct, use low-radius surfaces and readable activity streams, and reserve the elevated Signal Blue treatment for the single committed action in a context. Gold is restricted to the Aludra mark and small review/attention signals; it is never a page, lane, card, or dialog surface.

## Information architecture

Primary rail:

- Projects
- Activity
- Connections
- Settings

Project views:

- All tasks
- Needs me (derived from approval, permission, verification, integrity, and delivery states; not assignment)
- Pull requests
- User-created saved views

Task view:

- Stable chronological conversation with the composer anchored at the bottom
- Compact session-boundary and failover events inline with messages
- Overview, Sessions, and Delivery inspector tabs
- Attachments accepted at task-message level and passed to the active harness through an adapter-safe attachment contract
- Attempts, raw logs, native resume commands, handoff packs, and integrity evidence disclosed progressively

## Data that exists now

- Task messages can identify their session and attempt.
- Task sessions store session number, harness, model, status, close reason, sealed handoff JSON, baseline JSON, and integrity status/details.
- Attempts expose harness/model, status, logs, diffs, verification, and native Codex resume metadata when present.
- Task detail can return messages, sessions, attempts, pull requests, blockers, connection action, and lease state.
- GitHub PR state, Coder workspace/lease state, and current harness availability are available enough for the designed detail surfaces.

## API and schema work needed for the design

- Add `workflow_stage` and `board_position`; do not overload execution status for drag-and-drop organization.
- Add board summary fields for session count, harness history, active harness, attention reason, integrity state, and PR state so cards do not fetch every task detail.
- Make quota failover seal the current session and open a new session for the new harness. The current implementation can record a second harness attempt in the same session, which conflicts with the intended one-harness-per-session model.
- Add session start reason, optional provider usage estimates, deterministic budget counters, and explicit transition kind.
- Evolve handoff JSON into a versioned contract with goal, acceptance criteria, decisions, assumptions, Git baseline, changed files, completed work, verification evidence, blockers, next action, and audit reference.
- Add task-message attachments with stable metadata, storage reference, MIME type, size, source, and adapter delivery status.
- Add saved board views and per-view visible fields, grouping, sorting, filters, and density.
- Add an aggregate activity endpoint if Activity must span projects without loading every task.

## Current implementation constraints confirmed in the code

- The adapter registry includes Codex, Claude Code, Droid, and OpenCode, while the current persistent remote-runner chooser explicitly supports Codex and Claude Code. The visual order editor can represent the broader installed-adapter model, but the shipped remote flow must not imply it can dispatch Droid or OpenCode yet.
- The existing `chain_position` gives a global eligible-harness ordering. Project and task-scoped order need an explicit persistence model; they cannot be inferred from the current preferred-harness field.
- Normal session rollover already keeps the established harness and `task_prompt` scopes transcript context to the active session plus a previous handoff. A quota/availability failover still needs a distinct new-session transition rather than changing the harness on the existing session.
- Codex run metadata may expose a native resume command. No generic native-session action should appear for other harnesses unless their adapter actually reports a reliable continuation target.

## Migration sequence

1. Foundation: add a separate React/TypeScript entry point, Aludra tokens, shadcn primitives, routing shell, API client, query/cache layer, and component tests. Keep Flask as the backend and static asset host.
2. Read-only parity: implement Projects, Activity, Connections, and scoped Settings against current endpoints. Ship behind a frontend feature flag while the existing UI remains available.
3. Task board: add board-summary API fields, TaskCard primitives, saved views, column add/edit actions, and dnd-kit interactions. Persist stage/order only after the schema migration exists.
4. Task conversation: render current messages and attempt metadata first, then add the sessions inspector and compact boundary events. Preserve a single task-level composer.
5. Session contract: enforce one harness per session, explicit transition kinds, structured handoff versions, integrity evidence, and usage/budget fields. Keep multi-session behavior behind the task-level setting and default it to current behavior.
6. Attachments and native handoff: add attachment storage/API/adapter delivery, then harness deep links or native resume actions only where a runner exposes a reliable target.
7. Cutover: reach functional parity, migrate remaining settings/forms, run accessibility and responsive QA, make React default, then remove the legacy static UI in a later cleanup change.

Each slice should be independently releasable and should not require the session backend rewrite to be complete before users benefit from the new shell and component system.

## React component contract

Build the new frontend with TypeScript, shadcn-compatible source-owned primitives, and explicit domain components rather than a generic component-library skin.

Foundation primitives:

- `Button` (`primary`, `secondary`, `quiet`, `destructive`), `IconButton`, `Menu`, `Dialog`, `Drawer`, `Toast`, `Tooltip`
- `Select`, `Checkbox`, `RadioGroup`, `Tabs`, `SegmentedControl`, `Badge`, `AvatarStack`, `EmptyState`
- Form field wrappers with labels, help, validation, inherited-value/source labels, keyboard navigation, and visible focus states

Domain components:

- `AppShell`, `ProjectRail`, `ProjectHeader`, `AttentionIndicator`
- `TaskBoard`, `BoardLane`, `TaskCard`, `TaskCardMenu`, `BoardViewEditor`, `SavedViewCard`
- `TaskTimeline`, `TimelineMessage`, `SessionBoundary`, `FailoverEvent`, `TaskComposer`, `AttachmentPicker`
- `TaskDetailsDrawer`, `SessionList`, `SessionDetail`, `AttemptHistory`, `HandoffEvidence`, `DeliveryPanel`
- `HarnessMark`, `HarnessStack`, `HarnessOrderEditor`, `ConnectionCard`, `ConnectionState`
- `ScopedSettings`, `ScopeSwitcher`, `SettingRow`, `InheritedValue`, `PresetBudgetSelect`

Layout contracts:

- Small screens: task list rather than a compressed kanban; mobile navigation drawer; task details occupy a full-screen drawer.
- Medium screens: two board lanes; large screens: three only when four lanes cannot maintain a usable card width; full desktop: four lanes.
- No primary UI uses horizontal scrolling as a responsive fallback. Content wraps or reflows; only scrollable long-form transcript/history content is intentional.
- The application header and task composer are sticky. No other sticky region may compete with them.

State contracts:

- A task card consumes a summary DTO only. It never fetches messages, session history, or logs just to render a board.
- A harness mark is rendered from an adapter identity and availability state, never placeholder initials.
- A session boundary declares `transition_kind`: normal rollover, quota failover, unavailable failover, manual fresh session, or state-review pause.
- A native resume/deep-link action is absent unless its specific adapter returned a valid target.

## Approval gates before implementation

- Every visible navigation item and major CTA is represented by an interactive prototype state.
- Board density, card content, column behavior, empty states, drag/drop, and add/edit flows are approved.
- Task conversation, multi-session hierarchy, handoff disclosure, integrity mismatch, human approval, quota failover, and file attachment flows are approved.
- Global, project, and task setting inheritance is approved with exact ownership of every field.
- API/schema gap list is accepted and split into backend prerequisites versus frontend-only work.
- React migration boundaries, feature-flag strategy, component inventory, accessibility target, and legacy cutover plan are accepted.

## Design decisions locked by the prototype pass

- The product uses a clean white / faint blue-gray shell. There is no dark navigation rail and no gold surface treatment. Signal Blue is the committed-action color; Gold is limited to the Aludra mark and a small review-state indicator.
- The board has four canonical workflow stages: Planned, Running, Needs review, and Done. Board organization is separate from execution state and must persist as its own stage/position model.
- “Needs you” is an attention inbox, not another generic task list. It exposes the specific human decision and keeps the primary decision action aligned at the bottom of each item.
- Harness selection is a visible, draggable ordered list of eligible adapters. It is not a prose dropdown. The first eligible adapter starts work; failover policy is an independent setting.
- Task conversations are chronological operational timelines: messages, session boundaries, failover events, and verification state share one ordering. The composer is task-level and stays anchored at the bottom.
- Multiple sessions are progressive disclosure: cards show a compact harness stack and key state; task detail shows session summaries; a session drawer exposes transition, continuation brief, attempts, integrity evidence, and an adapter-specific native continuation only when available.
- Settings use Global → Project → Task override with per-field source labeling. Context budgets are bounded presets.
- Both new-task and task-message attachment flows begin with one attachment trigger, then let the user choose file or image.

## Prototype QA evidence

- Static scripts parse successfully; the prototype has no duplicate static DOM IDs outside intentionally re-rendered template fragments.
- Board layout was visually checked at 320px, 1024px, 1280px, and 1440px. The responsive model is list-first at phone width, two lanes at tablet width, then three only in the constrained desktop interval before returning to four lanes.
- The 320px pass confirmed the header, task CTA, tabs, list cards, and long card content remain visible without horizontal spill. The 1024px pass confirmed two spacious lanes. The 1440px pass confirmed the four-stage board is stable.
- The implementation must repeat this check for every route, overlay, and dynamic data state with automated viewport tests; the prototype check is design evidence, not a substitute for production test coverage.

## Prototype feedback backlog — September 4

### P0 — visual language and responsive structure

- Replace the generic dark-sidebar/light-gray SaaS shell. Use clean white and faint blue-gray planes, with Obsidian reserved for typography and dense operational detail and Signal Blue for committed actions. Signal Gold is attention-only: never use it as a shell, lane, or task-card wash. Do not copy PostHog.
- Use the liked elevated Signal Blue primary CTA treatment consistently for one committed action in each context. Secondary, navigation, utility, and destructive actions must remain visually distinct.
- Remove browser-default selects, checkboxes, and radios. Implement shared shadcn-style Select, Checkbox, RadioGroup, Button, IconButton, and Menu primitives.
- Repair responsive layouts before approval: no narrow description rails, no destructive word wrapping, bottom-align decision-card actions, preserve touch targets, and make the mobile shell intentional rather than a compressed desktop screen.
- Treat overflow as a release blocker: at every supported viewport, the shell, board, cards, settings, drawers, dialogs, tabs, harness-order rows, and composer must have no accidental horizontal scrolling, clipping, or obscured controls. Intentional vertical scrolling is allowed; horizontal scrolling is not a fallback for primary content.
- Keep the top application header sticky. Keep the task composer anchored; do not create competing sticky regions.

### P0 — harness, sessions, and task operations

- Replace “starting harness” modes with a user-editable, draggable harness order. The first eligible available harness begins work; quota/unavailability uses the next eligible harness under a separately configured failover-confirmation policy.
- Use recognizable harness/company marks across cards, order settings, task history, connection status, and task detail. Current supported adapters are Codex, Claude Code, Droid, and OpenCode; only show an adapter in the active order when it is installed, enabled, and eligible.
- Make sessions discoverable and drillable: Task → sessions → attempts. Each session must open a drawer/page with transition type, attempts/run history, continuation brief, integrity evidence, workspace baseline, and native resume/deep link only when real data exists.
- Make multi-harness task-card stacks reflect actual harness history rather than placeholder initials; include active harness, current session count, PR status, and a human-decision indicator.

### P1 — board and views

- Board columns retain a usable minimum height when empty. Per-stage add controls and per-card quick actions must be deliberate and aligned.
- List view needs visible row separation, consistent spacing, and the same task signals as the board.
- Edit View must let people choose shown and hidden statuses/stages, restore hidden stages, change card emphasis, and eventually support view-level grouping, fields, and density.
- Change “Needs you” into a notification-style attention indicator with a clear count and an explainable derived state.

### P1 — settings, connections, and attachments

- Every setting must show scope and inheritance: Global, Project, then Task override. Use a single-field vertical settings layout, not two narrow columns.
- Session context budget is a bounded preset Select; no arbitrary limits.
- Attachment composition uses one attachment trigger, followed by a menu for file or image. Persisting and delivery to adapters remains backend work.
- Connections need realistic lifecycle states: connected/ready, pending setup, quota cooldown, re-auth required, unavailable, disabled, and unconfigured, each with a relevant next action.

### P2 — interaction completion before implementation

- Finalize interactive states for My focus/Needs you, Pull requests, Add view/Edit view, Connections, Settings, Activity, task inspector, session drawer, attachment menu, and native-session actions.
- Validate the prototype against the actual current schemas and explicitly label future-only controls. Avoid presenting unsupported behavior as shipped capability.
