# Memory V2

This document describes the target memory architecture for Boss as a long-lived personal agent. It is intentionally incremental: the current system already has persisted facts, indexed projects, file metadata, conversation history, permission memory, and resumable runs. Memory V2 builds on that instead of replacing it.

## Goals

- Keep context relevant across long-running use without flooding every prompt.
- Distill durable information into stable memory instead of relying on raw chat history.
- Separate user memory from project memory so personal context and workspace context do not blur together.
- Make memory injection and consolidation observable locally.
- Keep all persistence local-first and compatible with the existing SQLite knowledge store and JSON conversation history.

## Layers

### 1. Working Memory

Working memory is the short-lived context assembled for the current turn.

It should include only the minimum useful state:

- recent conversation turns
- current agent or surface state
- active permission interruption state
- active project context when a request is clearly project-scoped
- high-value recent tool results that matter for the next step

Working memory should be rebuilt each turn, not treated as a durable store.

### 2. Episodic Summaries

Episodic summaries are condensed records of a conversation slice or work session.

They should capture:

- user goal
- important decisions
- unresolved follow-up work
- artifacts produced
- project or environment assumptions

These summaries should be cheaper to inject than raw history and should become the default bridge across long conversations or restarts.

### 3. Long-Term Memory

Long-term memory stores durable facts about the user and their world.

Examples:

- preferences
- recurring workflows
- stable device or environment facts
- long-lived personal notes the user expects Boss to remember

The existing `facts` table is the base for this layer. Future expansion can add confidence, last-confirmed time, or decay metadata without invalidating existing rows.

### 4. Project Memory

Project memory stores durable information tied to a repo or directory.

Examples:

- project summary
- architecture notes
- open workstreams
- important commands
- known verification steps
- indexed files and scanner metadata

The existing `projects` and `file_index` tables already provide the foundation. Future project notes and summaries should be keyed by normalized project path so current rows remain usable.

## Injection Strategy

Memory should be injected by selection, not by dumping everything into every prompt.

Target read path per turn:

1. Detect whether the turn is personal, project-scoped, or operational.
2. Gather candidate memories from:
   - recent turns
   - episodic summaries for the session
   - durable user facts
   - active project memory
3. Rank candidates by direct relevance to the user request.
4. Inject only a compact bundle into the active agent.
5. Log what was injected, from which source, and how many items were selected.

Injection should stay explicit. A good future shape is a structured context bundle with sections such as `user_memory`, `project_memory`, and `session_summary` rather than one large free-form blob.

## Consolidation Strategy

Consolidation is the process that turns transient activity into durable memory.

It should happen at natural boundaries:

- after a significant task is completed
- when a session gets long enough that raw history is no longer efficient
- when the agent learns a stable preference or durable project fact
- when a manual memory write is requested or clearly warranted

Consolidation outputs should be split by purpose:

- episodic summary for the session or task
- durable fact for user memory
- project note or project summary update

Every consolidation step should be logged locally as a memory distillation event so it is possible to understand why a fact exists.

## Storage Model

Memory V2 should continue to use local disk under `~/.boss/` by default.

Recommended storage evolution:

- keep `knowledge.db` as the main structured memory store
- keep existing `facts`, `projects`, and `file_index` tables readable as-is
- add new tables only when the read path is clear enough to justify them
- keep conversation history and resumable runs in their current formats unless a migration is needed

Reasonable future additions:

- `session_summaries`
- `project_notes`
- `memory_events`
- `consolidation_jobs`

## Observability

Memory behavior should be inspectable locally.

At minimum, local logs should cover:

- session start or continuation
- agent changes
- tool calls
- permission interruptions and decisions
- memory injection events
- memory distillation events

This is necessary because stronger memory without visibility becomes very hard to debug.

## Safety And Quality Rules

- Do not store obviously transient or low-value details by default.
- Prefer stable user facts over speculative inferences.
- Keep personal memory and project memory separate.
- Require scoped, understandable writes for agent-created durable memory.
- Avoid duplicating the same fact under multiple categories unless there is a clear retrieval need.

## Incremental Rollout

### Phase 1

- config-driven memory and observability paths
- structured local event logging
- richer `/api/system/status`

### Phase 2

- memory injection builder for user facts, project facts, and session summaries
- lightweight summarization for long sessions

### Phase 3

- project-level summaries and note distillation
- context ranking and budget management

### Phase 4

- review surfaces for episodic summaries and durable memory edits
- consolidation policies and decay / reconfirmation rules