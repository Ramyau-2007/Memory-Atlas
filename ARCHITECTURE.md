# Memory Atlas architecture

Memory Atlas is a Python/FastAPI memory layer backed by SQLite, with a vanilla browser interface. It deliberately separates explicit database operations from model generation: the model adapter cannot invoke tools or write memory records.

## Components

| Proposed block | Module | Responsibility |
|---|---|---|
| Input Handler | `app/main.py` | Validates API bodies, verifies sessions, selects recent conversation messages, composes provider context, and returns additive transparency metadata. |
| Deployment safeguards | `app/security.py`, `render.yaml` | Adds bounded request bodies, process-local endpoint throttles and Groq action caps, security headers, cookie-mode same-origin checks, and a single-instance persistent SQLite hosting template. |
| Context Analyzer | `app/memory.py:needs_memory_analysis`, `validate_memory_analysis`, `decision_from_analysis`, `classify` | Gates likely personal statements and personal-memory questions; validates Groq structured interpretation and conservatively falls back to local rules. |
| Memory Manager | `app/memory.py` | Retention, temporary context expiry, retrieval, update, soft-delete, recovery, permanent deletion, and restore behavior. |
| Versioned Memory Store | `app/db.py` | Additive SQLite initialization for accounts, sessions, memories, versions, conversations, messages, temporary context, lifecycle events, invitations, and nullable account-disable state. |
| Private demo access | `app/invitations.py`, `app/demo_admin.py`, `app/auth.py` | Issues high-entropy one-time codes, stores only SHA-256 hashes, atomically expires/redeems/revokes codes, and disables or re-enables accounts without deleting their data. Operator actions are local CLI commands, not public API routes. |
| Memory Operations | `app/memory.py` | Parameterized, account-scoped memory lifecycle and version operations. |
| User Memory Panel | `static/index.html`, `static/app.js`, `static/style.css` | Responsive chat history (create, switch, rename, confirmed delete), memory archive, deleted-memory recovery, version dialog, Memory Universe, timeline, analytics, and settings. |
| Context Retrieval | `app/memory.py:retrieve`, `memory_search_fts` | SQLite FTS5/BM25 token candidates combined with lexical overlap and priority; optionally extended with validated Groq-generated search terms. SQL enforces account ownership and active/non-temporary status. This is hybrid lexical retrieval, not embeddings. |
| AI Agent | `app/agent.py` | Provider adapter for labeled deterministic development output, configured Ollama generation, GroqCloud chat completions, and gated structured memory analysis. |
| Memory Update | `app/main.py:/api/chat` | Optionally analyzes likely personal-memory messages before retrieval, then stores explicit validated candidates after response generation and places other messages in expiring temporary context. |
| Timeline and analytics | `app/memory.py:timeline`, `app/memory.py:analytics` | Computes data from version rows, newly recorded lifecycle events, and account-scoped memory records. |
| Transparency | `/api/chat` response and chat inspector | Returns provider/model, retrieved records, adapter input, character-based response-context estimates, retrieval time, generation time, and gated memory-analysis status/duration. Analysis request tokens are not included in response-context estimates. |

## Request flow

1. FastAPI validates the request and resolves the account from a hashed bearer-session record, or from a secure HTTP-only cookie when hosted cookie mode is enabled. Conversation list/create/load/rename/delete endpoints filter by this authenticated account.
2. The route verifies conversation ownership or creates a conversation, generates a short title from its first user message, reads up to four recent messages (not turns) from that conversation, then stores the new user message. Once the message count is at least eight, it rebuilds an extractive summary from up to the eight most recent user messages in that conversation.
3. For likely personal assertions/preferences and personal-memory questions, Groq receives one additional analysis request containing only the current message. Python validates exact keys, enums, ownership relevance, confidence, content/term limits, and sensitive-text rules. Unrecognized or failed analysis falls back to local classification and raw lexical retrieval. Other ordinary messages skip this call.
4. It retrieves up to five active durable memories using FTS5/BM25 token matches combined with lexical overlap and priority, and—when validated analysis supplied them—normalized search terms. Retrieval duration is measured around this operation. This is hybrid lexical retrieval; no embeddings are used.
5. `ModelAdapter` receives the current request, selected memories, and recent context. Ollama receives its prompt; Groq receives OpenAI-compatible chat messages. Both exact adapter inputs are returned to the authenticated caller for inspection. Normal Groq response input includes the current request, at most three top-ranked matching memories, and at most two recent messages; longer chats may also include an extractive summary of up to eight recent user messages. Memory interpretation, when triggered, sends the current message alone in an additional request. Deterministic mode is not an LLM: it uses the request and up to three retrieved memories and ignores recent turns.
6. The route stores the response, persists only explicit high-confidence personal assertions approved by validated analysis (or the local fallback), places other messages in expiring temporary context, and returns response-source, context, analysis status, approximate token, and timing metadata. Durable memories belong to the account rather than a conversation, so retrieval can surface them in any chat for the same user.

Conversation records and messages reuse the existing SQLite tables. Message rows are ordered by their IDs for display. Editing a user message regenerates against only earlier turns and prunes later turns after generation succeeds. Source-linked, untouched automatic memories from the pruned branch are soft-deleted; memories the user has edited are preserved. Deleting a conversation uses the existing foreign-key cascade to remove its messages; long-term memory rows remain separate. Titles are limited to 48 characters when generated, and users can rename them independently.

The context estimates use `ceil(characters / 4)` and are not tokenizer measurements. `generation_ms` measures the adapter call, including waiting on the local Ollama request if configured; it is not model-only compute time.

Assistant messages are parsed with the locally vendored Marked 18.0.14 build, then sanitized with DOMPurify 3.4.17 and a Markdown-only tag/attribute allowlist. User messages remain plain text. Scripts, event handlers, styles, images, and embedded media are not allowed in rendered assistant markup. Sanitization does not make model answers factual or trusted.

## Memory Universe relationships

The browser requests only active memories belonging to the current user. It creates a line between two nodes when their lowercased memory text/category terms share at least two distinct terms of four or more characters after a small stop-word filter. This is a transparent lexical heuristic, not a stored relationship or semantic/LLM inference. A node's displayed content and metadata come directly from the memory API. The list remains available as an accessible alternative to the SVG.

## Timeline data and additive initialization

`init_db()` uses additive tables, columns, indexes, and triggers; it does not drop, replace, or reseed existing database tables. This demo-access update adds an invitation table and nullable `users.disabled_at`; existing accounts default to enabled, and no account data is deleted. Before first startup against an existing hosted DB, take a consistent SQLite backup and verify restoration on a separate copy. The FTS5 search index is separately derived from active, non-temporary memories. Existing memory and version rows are not rewritten. A new `memory_events` table records lifecycle actions from its introduction onward; it stores identifiers/action/time, not memory content.

Genuine creation and edit timestamps are available from existing version rows, so the timeline derives those events from the version history. Previous deletion/recovery actions were not recorded and cannot be reconstructed. Permanent deletion removes memory contents and versions; an audit event with identifiers and action remains. Existing database behavior and user rows do not require manual reset or recreation.

Assistant replies are rendered with locally vendored Marked 18.0.14 and sanitized by DOMPurify 3.4.17 using a Markdown tag/attribute allowlist. The response prompt requests evidence-aware memory claims and states that live web search is unavailable.

## API ownership and safety

- Memory CRUD, deleted-memory recovery, versions, chat, timeline, and analytics require authentication.
- Database queries include the authenticated user ID when reading or changing account-owned records.
- Passwords use salted PBKDF2-SHA256 hashes. Session token hashes are stored in SQLite. Local development retains the bearer token flow; hosted cookie mode does not return the token in JSON and sets an HTTP-only cookie with same-site behavior.
- `DEMO_INVITATIONS_REQUIRED` protects account creation at the API layer. It defaults off in development and on in production; Render sets it explicitly on. Invitation code hashes are compared in constant time and single-use redemption is transactionally recorded. The operator CLI is the only invitation/account-administration surface. Disabled accounts cannot log in and cannot use extant sessions; disabling revokes sessions while leaving owned records in place.
- `app/security.py` caps request bodies, adds browser security headers, and rate-limits registration, login, chat sends, and message-edit regenerations using the shared chat window. Cookie-authenticated writes require a same-origin `Origin` header when cookie mode is enabled; cookie tokens are ignored when that mode is disabled. Groq chat actions have both per-user and service-wide daily caps.
- The daily usage and endpoint counters are process-local. They reset on restart and do not coordinate among instances, so they are only test-instance safeguards, not provider billing guarantees.
- The Render Blueprint uses a separate `/var/data/` SQLite path, a persistent disk, one service instance, no automatic deployment, and a production start command. It does not contain the Groq secret; the host dashboard must supply it.
- In production, invitation-only registration is enabled by default unless `DEMO_INVITATIONS_REQUIRED` is explicitly set to `false`. The Render Blueprint explicitly enables it. `python -m app.demo_admin invite`, `revoke-invite`, `disable-user`, and `enable-user` operate locally against the configured DB path. There is no public admin API; the operator must protect terminal/shell and DB access. Disabling a tester revokes all sessions and prevents future login without deleting conversations or memories.
- Model input is untrusted data. The model has no database tools; only backend code performs memory operations.
- With Groq selected, likely memory-related messages may be sent in an additional analysis request (current message only). The normal response request includes the current message, selected memory text, and selected recent message text. No database, account identifier, or unrelated user's content is sent. This gated second completion increases latency and rate-limit use. Review Groq's current retention terms; this is not a zero-disclosure mode.
- Soft deletion can be reversed. Permanent deletion requires a prior soft-delete state and the exact confirmation phrase.
- `GET /api/health` reports the configured provider/model, not whether the Ollama service is currently reachable.

## Known limitations

The current UI has static rendering-contract tests but no browser-driven end-to-end test or screenshot review. The timeline cannot show lifecycle changes from before the event table existed. Analytics count stored records and are not model-quality or production performance metrics. FTS5 and model-assisted search-term expansion can still miss paraphrases or return imperfect terms; retrieval is lexical, not vector semantic retrieval. Memory extraction depends on a language model's interpretation and conservative validation, while deterministic/Ollama modes retain local phrase-based behavior. Groq free-tier limits, model availability, regional eligibility, and data handling can change; live Groq generation was not exercised by mocked tests. Request throttles and hosted-model daily caps are process-local and reset on restart; proxy-header trust must be checked against the chosen host. The POC still lacks account recovery, encryption at rest, a cluster-wide quota, and production incident-response procedures. Local development keeps its bearer token in local storage; the separate hosted mode uses an HTTP-only cookie but has not undergone an external security audit.
