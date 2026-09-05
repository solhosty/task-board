# Aludra redesign status

Implemented in this change:

- A Vite, React, TypeScript, Tailwind, and shadcn-compatible frontend, served by the existing Python application at `/` after production build. There is no separate `/ui` application route.
- Aludra semantic styling and source-owned shadcn-style Button, Select, Checkbox, Dialog, Menu, Tabs, Input, Textarea, and empty-state primitives.
- Projects, Activity, Connections, and Settings application surfaces.
- Four-stage task board with responsive list presentation, accessible dnd-kit drag and keyboard movement, per-stage task creation, task menus, search, attention inbox, pull-request filter, and saved board views.
- Task-level chronological conversation with one bottom-anchored composer, inline session boundaries, session evidence drawer, attempts and delivery inspector, and task-scoped settings.
- Separate `workflow_stage` and `board_position` storage, board save API, saved-view storage/API, and board summary data for session count, harness history, active harness, and integrity state.

Not completed because the current backend has no corresponding data/API contract:

- Durable task-message file/image storage and adapter delivery. The task UI exposes a single attachment trigger for both types and explains the missing backend rather than pretending uploads work.
- A draggable persisted global/project/task harness-order editor. The current global order endpoint works, but there is no scoped persistence model; Settings presents the real global order as read-only.
- One-harness-per-session enforcement during quota or availability failover. The current runner can still place a failover attempt in the same session; this needs a session transition change in execution code.
- Versioned handoff packs with the complete acceptance criteria, decisions, audit reference, and deterministic budget/usage counters. Existing sealed handoffs are rendered as the data currently supplied.
- Attachment-driven native harness deep links. Native continuation only appears when an adapter reports it, which the current API does not provide.
- The checked-in Playwright browser suite still needs to be run in an environment that permits localhost server binding. It covers real JSON bootstrap plus the rich board/task flow at desktop and phone widths.
