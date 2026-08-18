# Software Architecture Specification
Multi-Agent WhatsApp Training Assistant • Version 1.1
Status: Approved architecture after iterative discovery; includes decision traceability appendix. This document is normative for MVP implementation unless superseded by an Architecture Decision Record (ADR).
Scope: WhatsApp-first assistant in Python/LangGraph for natural-language and audio workout logging, training analysis, recommendations, workout programs, long-term training preferences and curated RAG knowledge.
Privacy boundary: the product does not persist health/medical information, diagnoses, symptoms, injuries, medications or medical documents. Optional body measurements are supported as a dedicated domain.

---

## Normative language
MUST indicates an implementation requirement. SHOULD indicates the preferred implementation with an allowed justified exception. MAY indicates an optional capability.

## Architecture status

| Area | Decision |
| --- | --- |
| Architecture style | Modular monolith codebase with independently scalable container/worker entrypoints. |
| Workflow runtime | Static LangGraph MainGraph; ExecutionPlan is a dynamic DAG represented as data. |
| System of record | PostgreSQL for domain state; Object Storage for original knowledge artifacts; Qdrant is a rebuildable derived index. |
| Messaging | RabbitMQ at-least-once + idempotent consumers + transactional outbox. |
| Ephemeral coordination | Redis for debounce, caches, rate limiting, locks and expiration hints. |
| Observability | Datadog for application/infrastructure; Langfuse for LLM/agent/prompt/RAG/evaluation. |
| Deployment | Docker; managed infrastructure where practical; no Kubernetes in MVP. |


\newpage


# 1. Executive Summary
The system receives WhatsApp text or audio, persists the inbound message, batches fragmented user input with a Redis-backed debounce window, and routes an InputBatch to a partitioned RabbitMQ workflow queue. A static LangGraph MainGraph interprets the batch, plans a dynamic task DAG, executes deterministic subgraphs and specialized LLM agents, persists domain side effects through application services, normalizes all user-facing output through a ResponseNormalizerAgent, and stores outbound messages plus outbox events before acknowledging the workflow message.
The design deliberately separates probabilistic reasoning from deterministic domain behavior. LLMs extract, classify, plan, analyze and recommend; they do not execute arbitrary SQL, directly mutate databases, calculate authoritative metrics, choose infrastructure identifiers or bypass validation. PostgreSQL transactions, validators, domain services, idempotency constraints and outbox publishing remain authoritative.
```text
WhatsApp -> FastAPI -> PostgreSQL -> RabbitMQ -> Redis Debounce -> InputBatch
                                              |
                                              v
                                      workflow partition
                                              |
                                              v
                                      Static MainGraph
                                              |
                         +--------------------+--------------------+
                         |                    |                    |
                    Subgraphs             Agents             Services
                         |                    |                    |
                         +--------------------+--------------------+
                                              |
                                              v
                                      DomainResult[]
                                              |
                                      ResponseNormalizer
                                              |
                                       ResponseGuard
                                              |
                             OutboundMessage + Transactional Outbox
                                              |
                                              v
                                      WhatsAppDispatcher
```

# 2. Goals and Non-Goals

## 2.1 Goals
- Log workouts from natural language text and externally transcribed audio.
- Support fragmented WhatsApp messages that together describe one exercise or set sequence.
- Normalize exercises against a canonical catalog and learned user aliases.
- Track strength, distance, timed, mixed and mobility activities from the schema level.
- Maintain automatic training sessions with inactivity-based close and robust expiration fallback.
- Analyze historical training using deterministic metrics plus an interpretive Analysis Agent.
- Provide evidence-grounded recommendations and structured workout-program drafts.
- Store durable training preferences/goals/equipment memories while rejecting health/medical information from memory.
- Scale MVP from roughly 100 to 10,000 users with asynchronous workers and horizontal scaling.
- Provide reproducible prompt/model/RAG versions, evaluation gates and full operational observability.

## 2.2 Non-Goals for MVP
- No Kubernetes requirement.
- No full event sourcing; normal domain tables remain mutable/soft-deletable while audit/domain events are append-only.
- No retroactive workout logging through conversational workout logger.
- No arbitrary SQL tools exposed to agents.
- No sparse retrieval in the initial RAG implementation; architecture remains ready for it.
- No persistence of health/medical data or medical documents.
- No mandatory body-weight capture for bodyweight exercises.
- No automatic activation of generated workout programs without explicit user approval.

# 3. Core Architecture Principles
1. Probabilistic components propose; deterministic domain services validate and commit.
2. PostgreSQL is authoritative for structured user, workout, program, memory and operational state.
3. LLM output is always structured when it affects downstream logic.
4. Agents receive least-privilege tools and scoped context; no generic production SQL tool exists.
5. All user-visible text passes through ResponseNormalizerAgent and then ResponseGuard.
6. At-least-once messaging is accepted; all business side effects must be idempotent.
7. A static MainGraph orchestrates a dynamic task DAG represented as data.
8. Conversation, LangGraph thread, training session and message batch are distinct concepts.
9. Qdrant is a derived search index and can be rebuilt from authoritative content.
10. Observability and evaluation metadata are versioned alongside prompts, agents, models, graph and retrieval profiles.

# 4. C4-Style System Context
```text
[WhatsApp User]
      |
      v
[Meta/WhatsApp Platform]
      |
      v
[Training Assistant System]
      |
      +--> [LLM Provider(s)]
      +--> [Speech-to-Text Provider]
      +--> [Datadog]
      +--> [Langfuse]
      +--> [Managed Data Infrastructure]
             - PostgreSQL
             - RabbitMQ
             - Redis
             - Qdrant
             - Object Storage
```
WhatsApp is an external identity/channel only. The internal identity is a UUID-backed User. The architecture MUST permit future channels such as Telegram, web or mobile without redefining domain identity.

# 5. Container and Process Architecture

| Process/Container | Primary responsibility |
| --- | --- |
| api | FastAPI webhook ingress, provider verification, identity resolution, message persistence, health/readiness. |
| message-aggregator | Consumes raw inbound references, manages Redis debounce, creates MessageBatch, routes to workflow partition. |
| workflow-worker | Consumes partitioned InputBatch and runs MainGraph/LangGraph task handlers. |
| outbox-publisher | Publishes pending outbox rows to RabbitMQ with publisher confirmation. |
| whatsapp-dispatcher | Sends persisted outbound messages in response-group sequence; updates delivery state. |
| memory-worker | Async memory candidate extraction and policy validation. |
| analytics-worker | Async session analytics, planned-vs-performed and progression evaluation. |
| knowledge-worker | Knowledge ingest, chunking, embedding, Qdrant indexing/reindex. |
| session-expiration-worker | Closes expired training sessions after PostgreSQL validation. |

These are deployment roles, not separate repositories. MVP SHOULD use one codebase and usually one application image with different entrypoints.

# 6. Project Structure
```text
src/app/
├── api/
├── domain/
│   ├── users/
│   ├── exercises/
│   ├── training/
│   ├── programs/
│   ├── memory/
│   └── knowledge/
├── application/
│   ├── commands/
│   ├── queries/
│   ├── services/
│   └── ports/
├── agents/
│   ├── intent_router/
│   ├── planner/
│   ├── workout_extractor/
│   ├── analysis/
│   ├── recommendation/
│   ├── workout_program/
│   ├── memory/
│   └── response_normalizer/
├── graphs/
│   ├── main/
│   ├── workout_logging/
│   ├── correction/
│   └── exercise_resolver/
├── analytics/
├── rag/
├── infrastructure/
│   ├── postgres/
│   ├── rabbitmq/
│   ├── redis/
│   ├── qdrant/
│   ├── whatsapp/
│   ├── llm/
│   ├── speech_to_text/
│   ├── langfuse/
│   └── datadog/
├── workers/
├── observability/
├── security/
└── config/

tests/
├── unit/
├── domain/
├── application/
├── integration/
├── contract/
├── graph/
├── agents/
├── rag/
├── evals/
└── e2e/
```

# 7. Identity, Conversations, Sessions and Messages

## 7.1 Identity
users.id MUST be the internal identity. External identities are stored in user_identifiers with provider + external_id uniqueness. WhatsApp uses BSUID. BSUID SHOULD be protected as ciphertext plus an HMAC lookup value when application-level encryption is enabled.
```text
users(id UUID, locale, timezone, status, created_at, updated_at)
user_identifiers(user_id, provider, external_id_ciphertext, external_id_lookup_hmac,
                 UNIQUE(provider, external_id_lookup_hmac))
```

## 7.2 Conversation vs training session
- **Conversation: **A conversational window. It rotates after a large configurable inactivity period and maps to LangGraph thread_id.
- **TrainingSession: **A domain workout session. It starts automatically when workout logging begins and closes after configurable inactivity.
- **MessageBatch: **A short debounce aggregation of one or more inbound messages used as one workflow input.
- **LangGraph thread: **Checkpoint namespace for the conversation; thread_id = conversation_id.
TrainingSession is never derived from LangGraph checkpoint lifetime. A conversation can contain questions, analyses and several interaction types beyond a physical training session.

## 7.3 No retroactive workout logger
Workout logging represents activity performed in the current session. A phrase such as “yesterday I ran 5 km” MUST NOT create a historical training session through the normal logger. Retrospective import, if later desired, requires a separate explicit product workflow.

# 8. Inbound WhatsApp and Debounce Flow
```text
POST /webhooks/whatsapp
  -> verify provider event
  -> resolve/create internal user
  -> deduplicate provider + external_message_id
  -> persist messages row
  -> publish message.received reference
  -> return HTTP success

MessageAggregationWorker
  -> debounce:{user_id}:{conversation_id}
  -> sliding window = 3s
  -> absolute max batch window = 10s
  -> generation counter prevents stale timer flush
  -> persist message_batch + ordered items
  -> emit InputBatchReady
```
The webhook MUST NOT wait for STT, debounce, LangGraph, LLM or response generation. Raw inbound data MUST be persisted before the system relies on Redis state.

# 9. RabbitMQ Architecture

## 9.1 Exchanges

| Exchange | Purpose |
| --- | --- |
| whatsapp.inbound | Inbound channel events/references. |
| workflow | Partitioned primary workflow execution. |
| domain.events | Published append-only domain events for independent consumers. |
| background | Async internal work such as memory extraction, analytics and knowledge ingest. |
| whatsapp.outbound | Persisted outbound-message dispatch. |


## 9.2 Workflow partitions
```text
NUMBER_OF_PARTITIONS = 32
partition = stable_hash(user_id) % NUMBER_OF_PARTITIONS
queues: workflow.00 ... workflow.31
Single Active Consumer per partition
initial prefetch = 1
```
The hash implementation MUST be stable across processes and deployments. Python built-in hash() MUST NOT be used as the persistent partition contract.

## 9.3 Delivery semantics
- RabbitMQ delivery is at-least-once.
- Consumers MUST be idempotent.
- Workflow messages are ACKed only after domain effects and required outbound/outbox persistence reach a safe state.
- Retry MUST use delayed/retry queues rather than sleeping inside consumers.
- Critical worker classes MUST have DLQs and replay tooling preserving original idempotency keys.

## 9.4 Retry tiers
```text
main queue -> retry 5s -> main -> retry 30s -> main -> retry 5m -> main -> DLQ
(actual delays and max attempts are typed configuration)
```

# 10. Redis Architecture

| Use | Authoritative? | Notes |
| --- | --- | --- |
| Debounce | No | Recoverable from persisted messages; TTL required. |
| Analytics cache | No | Invalidated by domain events and versioned keys. |
| Rate limits | No | Per-user and provider/model limits. |
| Distributed locks | No | Additional guard only; never replacement for DB constraints/idempotency. |
| Session-expiration hints | No | PostgreSQL MUST be checked before closing a session. |
| LLM concurrency limiter | No | Distributed semaphore/limit when needed. |

```text
Key convention:
{namespace}:{version}:{entity}:{identifier}:{purpose}

Examples:
debounce:v1:user:{user_id}:conversation:{conversation_id}
analytics:v1:user:{user_id}:exercise:{exercise_id}:{params_hash}:{data_version}
session:v1:user:{user_id}:expiration
rate:v1:user:{user_id}
```

# 11. LangGraph Runtime

## 11.1 Static graph, dynamic plan
MainGraph MUST be statically defined and compiled. ExecutionPlanner produces a dynamic ExecutionPlan DAG that is stored as graph data. The system MUST NOT construct a unique StateGraph topology for every user message.
```text
START
  -> load_base_context
  -> resolve_pending_workflow
  -> normalize_input
  -> intent_router
  -> execution_planner
  -> task_scheduler
       -> fan-out READY tasks via Send/worker dispatch
       -> task-result reducer
       -> repeat scheduler until terminal
  -> collect_results
  -> response_normalizer
  -> response_guard
  -> persist_outbound + outbox
  -> END
```

## 11.2 Execution task contract
```text
ExecutionTask {
  id
  task_type
  depends_on[]            # each dependency has a policy
  result_visibility       # USER_VISIBLE | INTERNAL
  payload
  status                  # PENDING|READY|RUNNING|COMPLETED|FAILED|WAITING_FOR_USER|SKIPPED
}

DependencyPolicy = REQUIRE_SUCCESS | ALLOW_PARTIAL | OPTIONAL
```
Parallelism is derived from dependencies. The plan SHOULD NOT redundantly store can_run_parallel.

## 11.3 Task handlers
The execution engine uses a TaskHandler registry. A handler may internally be a deterministic service, a LangGraph subgraph or a tool-calling/ReAct agent. MainGraph should not care about the internal implementation.
```text
TASK_HANDLERS = {
  LOG_WORKOUT: WorkoutLoggingSubgraph,
  CORRECTION: CorrectionSubgraph,
  TRAINING_ANALYSIS: TrainingAnalysisAgent,
  RECOMMENDATION: RecommendationAgent,
  WORKOUT_PROGRAM: WorkoutProgramAgent,
  CONVERSATION: ConversationAgent,
  ...
}
```

## 11.4 Graph state
```text
MainGraphState {
  workflow_execution_id
  graph_version
  trace_id
  correlation_id
  user_id
  conversation_id
  thread_id
  input_batch_id
  normalized_input
  intents[]
  execution_plan
  task_results{task_id -> TaskExecutionResult}
  pending_interrupt
  response_input
  outbound_messages[]
  workflow_errors[]
}
```
Graph state MUST contain working state and references, not replicate full database records or the complete user history.

## 11.5 Persistence and interrupts
- Production uses a persistent PostgreSQL-backed LangGraph checkpointer.
- Checkpoint storage SHOULD be isolated from domain tables by schema/database boundary.
- thread_id = conversation_id.
- pending_clarifications is also persisted as searchable operational state outside checkpoint internals.
- Nodes SHOULD interrupt before dependent side effects. Writes before an interrupt must be idempotent if unavoidable.
- workflow_executions and execution_tasks remain application-level operational tables distinct from checkpoint storage.

# 12. Agent and Component Catalog

| Component | Type | Responsibility | Mode |
| --- | --- | --- | --- |
| IntentRouter | Structured LLM | Classify one or multiple intents; no side effects. | No ReAct |
| ExecutionPlanner | Structured LLM | Produce task DAG/dependencies/visibility. | No ReAct |
| WorkoutExtractor | Structured LLM | Extract activities/sets from input. | No ReAct |
| ExerciseResolverSubgraph | Controlled workflow | Canonical resolution hierarchy + optional LLM fallback. | No ReAct |
| Clarification workflow | Policy + optional LLM | Interrupt/resume missing/ambiguous inputs. | No ReAct |
| CorrectionSubgraph | Controlled workflow | Resolve semantic target and issue validated correction commands. | No ReAct |
| TrainingAnalysisAgent | Tool-calling agent | Interpret deterministic metrics/history using read-only tools. | ReAct/tool loop |
| RecommendationAgent | Tool-calling agent | Evidence-grounded recommendations. | ReAct/tool loop |
| WorkoutProgramAgent | Tool-calling agent | Create/modify structured program proposals. | ReAct/tool loop |
| MemoryCandidateExtractor | Structured LLM | Extract long-term training memory candidates asynchronously. | No ReAct |
| ResponseNormalizerAgent | Structured LLM | Transform DomainResult(s) into ordered WhatsApp messages. | No ReAct |
| Recommendation/Program Critic | Conditional LLM | Semantic validation only when needed. | No direct writes |


# 13. Least-Privilege Tooling
Agents MUST NOT receive generic SQL or database clients. Tools expose bounded domain queries/services. user_id and authorization context SHOULD be injected by runtime rather than supplied by model arguments.
```text
Analysis tools (read only):
- get_exercise_history
- get_recent_training_sessions
- get_training_frequency
- get_current_program
- get_planned_vs_performed
- calculate_exercise_progression
- calculate_volume_metrics
- calculate_rpe_trend
- get_personal_records
- get_session_metrics

Recommendation tools:
- get_user_profile
- get_training_preferences
- get_current_program
- get_current_prescription
- get_recent_training_metrics
- get_exercise_history
- search_training_knowledge
- search_exercise_knowledge
- search_exercises
- get_exercise_substitutions
- evaluate_progression_rule
```
Domain writes follow: LLM structured output -> command/result -> application service -> validation -> transaction -> PostgreSQL. An LLM MUST NEVER issue raw UPDATE/INSERT/DELETE.

# 14. Workout Logging Domain

## 14.1 Activity model

| Activity type | Core validation |
| --- | --- |
| STRENGTH | Repetitions are essential. Load is optional. |
| DISTANCE_ACTIVITY | Distance and/or duration accepted; pace/speed derived deterministically when possible. |
| TIMED_ACTIVITY | Duration-centric activity. |
| MIXED_ACTIVITY | Combined dimensions as defined by ActivitySchemaRegistry. |
| MOBILITY | Activity-specific minimal schema. |
| OTHER | Conservative schema; avoid invented values. |

ActivitySchemaRegistry and ActivityValidator are deterministic. Validation output: VALID, VALID_WITH_WARNINGS, MISSING_ESSENTIAL_DATA or INVALID.

## 14.2 Strength semantics
- A strength set MUST contain repetitions. “Bench press 80 kg” requires clarification for reps.
- Bodyweight exercise does not require current body weight.
- Additional external weight on bodyweight movements uses BODYWEIGHT_PLUS; assistance uses BODYWEIGHT_MINUS.
- For dumbbells, a reported “20 kg” defaults to PER_IMPLEMENT semantics when the exercise/equipment model supports it.
- Load modes: TOTAL, PER_SIDE, PER_IMPLEMENT, BODYWEIGHT, BODYWEIGHT_PLUS, BODYWEIGHT_MINUS.
- Set types: WARMUP, WORKING, BACKOFF, DROP_SET, FAILURE, AMRAP, OTHER.
- Supersets/trisets/circuits/complexes use explicit exercise-group entities.

## 14.3 Effort
The user-facing normalized effort is RPE. Input may be explicit RPE, RIR or natural-language effort. EffortNormalizer combines deterministic parsing/mappings with a small LLM classifier fallback. Raw effort, normalized RPE, normalization method and version are persisted. If effort applies to the activity rather than a specific set, the system MUST NOT invent the same set-level RPE for every set.

## 14.4 Fragment inheritance
Context may be inherited within a coherent workout block, e.g. “80 kg: 10, 9, 8”. Provenance MUST distinguish EXPLICIT vs INHERITED values. Consecutive same-exercise messages append sets to the same SessionExercise; returning to the same exercise after another creates a new exercise block.

# 15. Workout Logging Subgraph
```text
InputBatch
 -> build_workout_input
 -> WorkoutExtractor
 -> schema validation
 -> ExerciseResolverSubgraph
      high confidence -> continue
      medium -> LLM-assisted resolver
      low/ambiguous -> Clarification interrupt
 -> normalize units/effort
 -> ActivityValidator
      missing essential -> Clarification interrupt
 -> TrainingSessionManager
 -> WorkoutCommandBuilder
 -> WorkoutApplicationService
 -> PostgreSQL transaction
      domain rows
      entity_sources
      audit_events
      domain_events
      outbox_events
 -> WorkoutLoggedResult
```
For a multi-exercise request, already resolved/valid activities MAY be persisted while ambiguous semantic items remain pending. All valid activities included in one LogWorkoutCommand are atomic in one database transaction.

# 16. Exercise Catalog and Resolver
Exercises use a global canonical catalog. exercise_aliases supports global aliases and user-specific learned aliases. Resolver order is normative:
1. Exact user alias.
2. Exact global alias.
3. Canonical exercise name.
4. Normalized lexical/fuzzy match.
5. Vector semantic match.
6. LLM-assisted resolution.
7. User clarification.
```text
ExerciseResolution {
  raw_name
  exercise_id
  canonical_name
  method  # USER_ALIAS|GLOBAL_ALIAS|CANONICAL|FUZZY|VECTOR|LLM|USER_CONFIRMED
  confidence
  candidates[]
  requires_clarification
}
```
Exercise relations include VARIATION_OF, SUBSTITUTE_FOR, SIMILAR_MOVEMENT, PROGRESSION_OF and REGRESSION_OF. Exercise-to-muscle roles include PRIMARY, SECONDARY and STABILIZER.

# 17. Correction and Undo
- Natural corrections target only the current training session, plus a configurable correction grace period immediately after automatic finish.
- “Last set” resolves deterministically to the latest valid non-deleted set in the allowed session context.
- Implicit recent-entity references may resolve without clarification only at high confidence.
- High confidence executes; medium may use LLM-assisted ranking; low/multiple candidates trigger clarification.
- One message may contain several correction operations.
- Resolved valid operations from one coherent request commit atomically; ambiguous operations are excluded before the command.
- Undo supports the last reversible CREATE, UPDATE or DELETE as a compensating domain mutation.
- Deleting a whole SessionExercise soft-deletes its sets in one domain operation and emits an event describing affected children.
```text
CorrectionExtractor -> CorrectionIntent[] -> EntityReferenceResolver
 -> CorrectionValidator -> CorrectionCommandBuilder -> WorkoutApplicationService
 -> optimistic concurrency (expected_version) -> transaction -> CorrectionResult
```
The LLM emits semantic references, not arbitrary database IDs. Every mutable correction target SHOULD carry a version integer for optimistic concurrency.

# 18. Training Sessions
A TrainingSession starts automatically when logging occurs and no valid active session exists. It tracks start_at, last_activity_at and finish_at. The timeout is configurable and is independent from conversation timeout.
Expiration uses a background SessionExpirationWorker plus lazy fallback on the next workout input. Redis may provide expiration hints, but PostgreSQL last_activity_at is authoritative before closing a session.

# 19. Analysis Architecture
TrainingAnalyticsService is deterministic and authoritative for metrics. TrainingAnalysisAgent interprets those metrics. Analysis does not always lead to Recommendation; it may independently produce a USER_VISIBLE result that flows directly to ResponseNormalizer.
```text
ExerciseSets / Sessions
      -> TrainingAnalyticsService
         - volume
         - set/repetition counts
         - load progression
         - RPE trend
         - estimated 1RM
         - frequency/adherence
         - planned vs performed
         - pace/speed/duration
         - personal records
      -> TrainingAnalysisAgent (read-only tool loop)
      -> TrainingAnalysisResult
      -> ResultCollector -> ResponseNormalizer
```
Metric calculations SHOULD carry metric_name, metric_version, calculation_window and calculated_at. Raw reported load is preserved separately from any effective load used by a metric formula.

# 20. Recommendation Architecture
RecommendationAgent is independent from TrainingAnalysisAgent. ExecutionPlanner establishes dependencies only when needed. Analysis may be INTERNAL evidence for a recommendation or separately USER_VISIBLE.
```text
Example 1: "How has my bench progressed?"
ANALYSIS -> ResponseNormalizer

Example 2: "What can replace bench if I only have dumbbells?"
RECOMMENDATION -> Validator -> ResponseNormalizer

Example 3: "Am I progressing, and should I increase load?"
ANALYSIS -> RECOMMENDATION
both results visible or analysis internal depending planner result_visibility
```
RecommendationResult MUST carry evidence references, user-context references, analysis references, confidence, assumptions and limitations. Agents SHOULD communicate through structured results rather than call each other arbitrarily as tools.

## 20.1 Recommendation validation
- Layer 1 deterministic validator checks catalog existence, equipment availability, ranges, avoided exercises, program references and progression-rule structure.
- Layer 2 semantic critic runs only when complexity warrants it and checks contradiction, relevance, evidence support and internal coherence.
- Retry is controlled and bounded; no infinite self-critique loop.

# 21. Workout Programs
WorkoutProgramAgent creates or modifies a structured proposal using read-only tools. It never activates a program directly. Every generated program is first persisted as DRAFT after deterministic and optional semantic validation.
```text
Program lifecycle:
DRAFT --explicit user approval--> ACTIVE
ACTIVE --new program activation--> ARCHIVED

Modification:
ACTIVE v3 -> clone -> DRAFT v4 -> validate -> explicit approval -> ACTIVE v4
Session already started on v3 remains linked to v3.
```

## 21.1 Prescription model
```text
WorkoutProgram
  -> WorkoutDay[]
      -> ExercisePrescription[]
          target_sets
          min_repetitions / max_repetitions
          target_load?              # optional
          target_rpe?
          rest_seconds?
          tempo?
          warmups?
          ProgressionRule?
          substitutions[]
          prescription_group?        # superset/circuit etc.
```
Programs support fixed and adaptive prescriptions. Recommended next load can evolve operationally without mutating the canonical program version.

## 21.2 Progression
ProgressionRule is structured with rule_type, conditions, actions and natural-language description. LLM may propose a rule; deterministic ProgressionEngine evaluates it. When a rule is satisfied, ExercisePrescriptionState may update next_target_load/last_evaluation without rewriting the program version. The suggestion is not mandatory for the user.

## 21.3 Workout-day flexibility
WorkoutDay.order_index is recommended sequence, not an enforced order. NextWorkoutResolver may suggest the next day based on active program and recent execution, but the user may choose another day. Workout logging works with no active program at all.

# 22. Long-Term Memory and User Profile

## 22.1 Structured profile first
Long-term context SHOULD be structured in PostgreSQL whenever possible: training goals, experience level, weekly frequency, equipment, training preferences, exercise preferences/avoidances and training routine. Qdrant is not the primary store for these facts.

## 22.2 Async memory pipeline
```text
Interaction completes
 -> background MemoryCandidateRequested
 -> MemoryWorker
 -> MemoryCandidateExtractorAgent
 -> MemoryPolicyValidator
 -> persist or reject

If a preference in the current message is needed for the current request,
the current workflow uses it directly without waiting for background persistence.
```

## 22.3 Memory lifecycle
Memory statuses: ACTIVE, SUPERSEDED, DELETED, EXPIRED. Explicit contradictory new information supersedes the previous active memory. User requests such as “what do you remember about me?” and “forget X” are supported.

## 22.4 Health-data exclusion
MemoryPolicy MUST reject diagnoses, symptoms, injuries, medications, medical history, health status and medical documents. The system is not a health-record store. Optional body measurements are persisted through user_measurements rather than free-form long-term memory.

# 23. Body Measurements
Optional measurement types may include BODY_WEIGHT, HEIGHT, WAIST and BODY_FAT_ESTIMATE. The product MUST NOT require a measurement to log workouts. Measurement history is structured, access-controlled and excluded from unrelated agent context.

# 24. RAG and Knowledge Architecture

## 24.1 Source of truth
PostgreSQL KnowledgeDocument metadata plus Object Storage original content are authoritative. Qdrant contains derived chunks/vectors and MUST be rebuildable.

## 24.2 Collections
```text
training_knowledge   # global curated exercise/training knowledge
user_knowledge       # reserved for permitted user-specific non-health knowledge
                     # medical/health documents are explicitly prohibited
```
MVP should avoid one collection per user or dozens of collections by document type. Use payload metadata and indexes. User-specific retrieval MUST enforce user_id filters.

## 24.3 Knowledge document and metadata
```text
KnowledgeDocument {
  document_id
  document_type
  title
  source_type / source_reference / author
  language
  version
  status             # ACTIVE|SUPERSEDED|DISABLED|DELETED
  trust_level        # HIGH|MEDIUM|LOW
  checksum
  storage_reference
  published_at
  ingested_at
  supersedes_document_id
}
```

## 24.4 Chunking
- Exercise knowledge uses semantic/logical sections rather than blind fixed-size chunks.
- Technical documents use structure-aware hierarchical chunking.
- Workout templates use logical blocks.
- Parent-child retrieval is supported: small chunks may retrieve a larger parent context.
- Overlap is configurable and used only where beneficial; it is not a universal rule.

## 24.5 Retrieval
```text
MVP query pipeline:
RetrievalQueryBuilder
 -> metadata filters
 -> dense embedding search in Qdrant
 -> top N candidates
 -> replaceable Reranker
 -> top K RetrievedEvidence
 -> agent context

Future-compatible:
dense + sparse -> fusion -> rerank
```
Agents access RAG only through domain tools such as search_training_knowledge or search_exercise_knowledge. They never receive raw Qdrant clients or choose arbitrary physical collections.

## 24.6 Ingestion and versioning
```text
Source -> Loader -> ContentNormalizer -> Metadata -> Document Registry
 -> Chunker -> Enrichment -> EmbeddingService -> QdrantIndexer -> Validation -> ACTIVE

Idempotency dimensions:
document_id + document_version + chunking_profile_version + embedding_version
```
Document versions are immutable. New content creates a new version; prior ACTIVE version becomes SUPERSEDED after successful indexing. Embedding migrations SHOULD use a controlled blue/green index or equivalent alias strategy.

# 25. Response Architecture
No domain agent writes final WhatsApp prose. Every USER_VISIBLE DomainResult goes through ResultCollector -> ResponseNormalizerAgent -> ResponseGuard.
```text
NormalizedResponse {
  messages: OutboundMessage[]   # one or multiple messages, ordered
}

OutboundMessage {
  id
  response_group_id
  sequence
  text
  type
  user_id
  conversation_id
  correlation_id
  trace_id
}
```
ResponseNormalizer applies persona, language, terminology, verbosity and message splitting. It MUST NOT alter facts. ResponseGuard compares verifiable facts against DomainResult(s); if normalizer fails, deterministic templates provide a fallback for critical acknowledgements such as workout registration.
Every successful workout registration MUST produce a confirmation. Split-message sequence MUST be persisted, and WhatsAppDispatcher sends sequence N only after the preceding message of the same response group has reached its dispatch-safe state.

# 26. PostgreSQL Domain Model
The schema below is logical; exact column types/index choices belong in migrations. Important domain entities use soft deletion where applicable. audit_events and domain_events are append-only.

| Aggregate / area | Tables |
| --- | --- |
| Identity | users, user_identifiers, user_profiles, user_preferences, user_available_equipment |
| Conversation | conversations, messages, message_batches, message_batch_items, pending_clarifications |
| Training | training_sessions, session_exercises, exercise_sets, exercise_groups, entity_sources |
| Exercise catalog | exercises, exercise_aliases, muscles, exercise_muscles, equipment, exercise_equipment, exercise_relations |
| Programs | workout_programs, workout_days, exercise_prescriptions, prescription_substitutions, progression_rules, exercise_prescription_state, prescription_groups |
| Measurements | user_measurements |
| Memory | memories |
| Knowledge | knowledge_documents, knowledge_chunks metadata if retained relationally |
| Workflow ops | workflow_executions, execution_tasks, processed_operations |
| Events | domain_events, audit_events, outbox_events |
| Outbound | outbound_messages / messages with outbound direction depending final physical schema |


## 26.1 ERD logical relationships
```text
User 1---* UserIdentifier
User 1---* Conversation 1---* Message
Conversation 1---* MessageBatch 1---* MessageBatchItem *---1 Message

User 1---* TrainingSession
TrainingSession 1---* SessionExercise
SessionExercise *---1 Exercise
SessionExercise 1---* ExerciseSet
TrainingSession 0..1---1 WorkoutProgramVersion
SessionExercise 0..1---1 ExercisePrescription

WorkoutProgram 1---* WorkoutDay
WorkoutDay 1---* ExercisePrescription *---1 Exercise
ExercisePrescription 0..1---1 ProgressionRule
ExercisePrescription 0..1---1 ExercisePrescriptionState

Exercise *---* Muscle      via ExerciseMuscle(role)
Exercise *---* Equipment   via ExerciseEquipment
Exercise *---* Exercise    via ExerciseRelation
Exercise 1---* ExerciseAlias

User 1---* Memory
User 1---* UserMeasurement

DomainEntity *---* Message/Batch via EntitySource
Domain mutation -> DomainEvent + AuditEvent + OutboxEvent (same transaction)
```

## 26.2 Provenance
entity_sources MUST allow a created/changed entity to reference one or more source messages or a source batch with source_role. This supports debugging, correction resolution, replay and eval-case generation.

# 27. Transactional Outbox and Domain Events
```text
BEGIN
  mutate domain rows
  append audit_event when applicable
  append domain_event
  insert outbox_event(PENDING)
COMMIT

OutboxPublisher:
  SELECT ... FOR UPDATE SKIP LOCKED
  publish to RabbitMQ
  await publisher confirmation
  mark PUBLISHED
```
Duplicate publication remains possible around failures; consumers MUST use event_id/operation_id uniqueness. The architecture is event-driven where useful but is not full event sourcing.

## 27.1 Event envelope
```text
DomainEventEnvelope {
  event_id
  event_type
  event_version
  aggregate_type
  aggregate_id
  user_id
  trace_id
  correlation_id
  causation_id
  payload
  occurred_at
}
```

# 28. Idempotency and Concurrency

| Layer | Key / mechanism |
| --- | --- |
| Inbound message | UNIQUE(provider, external_message_id) |
| Message batch | batch_id / deterministic membership safeguards |
| Workflow | workflow_execution_id + persisted status/checkpoint |
| Domain command | operation_id / processed_operations + domain uniqueness |
| Mutable corrections | expected_version optimistic concurrency |
| Domain event | event_id unique |
| Outbound message | outbound_message_id + response_group sequence |
| Knowledge ingest | document/version/chunk-profile/embedding-version key |

Exactly-once transport is not assumed. The target property is exactly-once business effect where practical through idempotency and transactions.

# 29. Error Handling and Failure Boundaries
Errors are classified as RETRYABLE, NON_RETRYABLE or UNKNOWN. Nodes should be sized around meaningful retry/checkpoint boundaries. Avoid nodes that combine extraction, multiple external tools, several writes and final delivery in one step.
- Partial task failure MUST NOT automatically roll back unrelated successfully committed tasks.
- If Analysis fails but workout logging succeeded, workflow may end PARTIAL_SUCCESS and preserve the logged workout.
- A task with REQUIRE_SUCCESS dependency becomes SKIPPED when its required predecessor fails.
- If workout is saved and outbound generation fails, retry MUST NOT re-create workout rows.
- If outbound messages were persisted and worker crashes before ACK, redelivery MUST resolve to the existing workflow/outbound state.

# 30. Observability

## 30.1 Datadog - application and infrastructure
Datadog is the primary application/infrastructure observability platform.
- FastAPI request rate, latency and errors.
- Distributed application traces and logs.
- RabbitMQ queue depth, oldest-message age, retry/DLQ volume and per-partition health.
- PostgreSQL, Redis, Qdrant and container/service health.
- Worker utilization, CPU, memory, exceptions and SLO/alert dashboards.
- CI/CD and test telemetry where useful.

## 30.2 Langfuse - AI observability
- LLM calls, prompts, model/profile versions, tokens, cost and latency.
- Agent iterations and tool calls.
- RAG queries, filters, candidates, retrieval/reranking scores and evidence.
- Datasets, experiments, eval scores and production feedback signals.

## 30.3 Correlation
```text
Shared metadata:
trace_id
correlation_id
workflow_execution_id
task_id (when applicable)
conversation_id / thread_id (pseudonymized/internal only)
graph_version
agent_version
prompt_version
model_profile_version
retrieval_profile_version
```
TelemetryRedactor MUST run before telemetry leaves the application. PII and protected measurements are not suitable as tags. Raw content policy is environment- and component-specific: FULL, REDACTED, METADATA_ONLY or DISABLED.

# 31. Prompt, Model and Agent Governance
- Prompts are centrally versioned and referenced by key + environment label (development/staging/production).
- Published prompt versions are immutable; modifications create a new version.
- agent_version is distinct from prompt_version because toolsets, middleware, schemas, budgets and routing also affect behavior.
- Model Registry defines replaceable profiles such as fast, extraction and reasoning; traces record profile version and physical provider/model.
- Each ReAct/tool-calling agent has explicit max iterations, max tool calls, time budget, token budget and optional cost budget.
- FeatureFlagProvider supports controlled rollout of agent/model/prompt/RAG candidates.

# 32. Evaluation Framework

## 32.1 Levels

| Level | Examples |
| --- | --- |
| Component | IntentRouter, WorkoutExtractor, ExerciseResolver, MemoryCandidateExtractor. |
| Workflow | WorkoutLoggingSubgraph, CorrectionSubgraph, Planner + task DAG. |
| End-to-end | Inbound message(s) -> expected side effects + expected response characteristics. |


## 32.2 Critical metrics

| Component | Representative metrics |
| --- | --- |
| IntentRouter | multi-label precision/recall/F1 |
| Planner | task precision/recall, dependency accuracy, visibility accuracy, unnecessary-task rate |
| WorkoutExtractor | field accuracy, set count, load/reps/RPE/load-mode exactness |
| ExerciseResolver | Top-1, Top-K recall, clarification precision, false auto-resolution rate |
| Correction | target accuracy, field/value accuracy, wrong-mutation rate |
| Analysis | metric factuality, appropriate evidence/tool use |
| Recommendation | constraint adherence, evidence use, relevance, unsupported claims |
| ResponseNormalizer | fact preservation, message ordering/splitting |
| Memory | precision, false-persistence rate, contradiction handling |
| RAG | Recall@K, Precision@K, MRR, nDCG, groundedness/evidence utilization |


## 32.3 CI/CD evaluation tiers
```text
PR:
  unit/domain/schema tests + fast graph tests + small golden eval subset

main/merge:
  integration tests + broader component evals + RAG retrieval evals

release/staging:
  full regression + workflow/E2E + recommendation/program evals
  + RAG end-to-end + LLM-judge where needed + cost/latency gates

nightly/scheduled:
  expensive challenge sets, model comparisons, shadow candidates,
  large RAG and long trajectory experiments
```
Production corrections/rejections/undos MAY become candidate eval cases only after human review. A carefully reviewed GOLDEN dataset acts as a release-regression set. Response fact-preservation is a strict gate for verifiable workout facts.

# 33. Security and Privacy

## 33.1 Data boundary
The product persists training data and optional body measurements. It intentionally does not persist diagnoses, symptoms, injuries, medications, medical history, health status or medical documents. If such content appears in conversation, memory extraction MUST reject it and logging/telemetry minimization policies should avoid retaining it beyond the minimum operational need defined by retention policy.

## 33.2 Protection
- TLS/protected transport for external and internal connections where supported.
- Encryption at rest for PostgreSQL, backups, Object Storage and Qdrant through infrastructure controls.
- Selective application-level encryption for high-risk identifiers/fields rather than indiscriminate per-column encryption.
- BSUID lookup via keyed HMAC with encrypted original value when application-level protection is enabled.
- SecretsProvider abstraction; no secrets in source, prompts, images or logs.
- Service-specific database roles, credentials and infrastructure permissions.
- Agent ContextPolicy controls which data categories each agent can receive.
- PrivacyDeletionWorkflow coordinates deletion/anonymization across PostgreSQL, Qdrant, Object Storage, checkpoints and caches.
- Retention is category-specific; raw messages, workout history, audit records, traces, DLQ and backups do not share a universal TTL.
- Administrative exports, deletion, manual replay/correction, role changes and privileged access generate audit records.

## 33.3 Privacy incident operations
Security incident workflow covers detection, containment, affected-data identification, evidence preservation, legal/privacy assessment, required communication, remediation and postmortem. Provider/subprocessor registry SHOULD document data categories, purpose, region, retention configuration and contractual/security controls for Meta/WhatsApp, LLM providers, STT, Datadog, Langfuse and cloud infrastructure.

# 34. Configuration and Secrets
Typed settings MUST validate at startup. Operational values are externalized from domain code.
```text
ApplicationSettings
  environment
  postgres
  rabbitmq
  redis
  qdrant
  workflow
    partitions = 32
    debounce_window_seconds = 3
    max_batch_window_seconds = 10
    conversation_timeout
    training_session_timeout
    correction_grace_period
  llm
    model profiles
    timeouts
    agent budgets
  rag
    top_n
    top_k
    retrieval_profile versions
  observability
  security
```
Non-secret configuration and secrets are separate. Secrets are resolved through SecretsProvider and service-specific permissions.

# 35. API Contracts

## 35.1 Public ingress
```text
POST /webhooks/whatsapp
- authenticate/verify provider event
- parse supported message types
- identity resolution
- message deduplication/persistence
- enqueue inbound reference
- fast success response
```

## 35.2 Operational endpoints
```text
GET /health   # process liveness; lightweight
GET /ready    # readiness; verifies critical dependencies appropriate to process

Future admin APIs should be separately authenticated/authorized and SHOULD NOT share
public webhook authentication assumptions.
```
Internal module communication SHOULD use in-process Python calls when components share a process and RabbitMQ for asynchronous boundaries. Do not introduce HTTP microservice calls solely to mimic microservices inside the modular monolith.

# 36. Database Migrations
SQLAlchemy plus Alembic is the PostgreSQL persistence stack. Domain code should depend on repository/application ports rather than ORM details. Migrations SHOULD be backward-compatible using expand/backfill/contract steps when schema changes cannot be performed atomically without deployment coupling.

# 37. Deployment

## 37.1 MVP
- Docker containers.
- Managed PostgreSQL/Redis/RabbitMQ/object storage where practical.
- Qdrant managed or containerized according to environment.
- No Kubernetes requirement.
- One repository/codebase; independent process/container scaling.
- Vendor-neutral architecture document. Cloud-specific mapping belongs in a deployment-reference appendix/ADR.

## 37.2 Local development
```text
docker compose up
  postgres
  rabbitmq
  redis
  qdrant
  api
  required workers

Optional local/self-hosted observability components according to developer setup.
```

## 37.3 Scaling
API replicas, workflow workers, background workers, knowledge workers, outbox publishers and dispatchers can scale independently. Workflow scaling observes the 32 partition/SAC contract. Signals SHOULD include queue depth, oldest message age, workflow latency, CPU/memory and provider/LLM concurrency rather than queue depth alone.

## 37.4 Graceful shutdown
Workers stop accepting new messages, finish or checkpoint the current safe unit when possible, ACK completed work, release resources and exit. Correctness still relies on idempotency/checkpoints for abrupt crashes.

# 38. Testing Strategy

| Test class | Focus |
| --- | --- |
| Unit | Validators, progression engine, partition hashing, debounce logic, reducers, retry policies, command builders. |
| Domain | Aggregate invariants and business rules without infrastructure. |
| Integration | Real ephemeral PostgreSQL/RabbitMQ/Redis/Qdrant; transactions, outbox, filtering, retries, expiration. |
| Contract | WhatsApp parser, STT provider, LLM structured schemas, Rabbit event envelope, Qdrant payloads. |
| Graph | Planner DAG, task dependencies, fan-out/fan-in, interrupts/resume, result visibility. |
| Agent/Eval | Dataset-based quality/trajectory evaluation. |
| E2E | Inbound messages through persisted expected side effects and outbound payload. |
| Failure injection | Crash after commit, redelivery, duplicate outbox publish, LLM/Qdrant timeout, Redis loss, WhatsApp failure. |

Integration tests SHOULD use ephemeral containers/Testcontainers-style infrastructure rather than replacing all persistence and messaging behavior with mocks. Unit tests may use fakes/mocks. Fake providers for LLM, STT, WhatsApp and embeddings enable deterministic local tests.

# 39. Sequence Diagrams

## 39.1 Workout log
```text
User -> WhatsApp -> API: "Bench 80kg, 10 9 8, hard"
API -> PostgreSQL: persist Message
API -> RabbitMQ: message.received
API -> WhatsApp: webhook ACK
Aggregator -> Redis: debounce
Aggregator -> PostgreSQL: MessageBatch
Aggregator -> workflow.N: InputBatchReady
Workflow -> IntentRouter: LOG_WORKOUT
Workflow -> WorkoutLoggingSubgraph
WorkoutExtractor -> ExerciseResolver -> EffortNormalizer -> Validator
ApplicationService -> PostgreSQL: transaction sets + sources + events + outbox
ResultCollector -> ResponseNormalizer -> ResponseGuard
Workflow -> PostgreSQL: OutboundMessage + outbox
Workflow -> RabbitMQ: ACK
OutboxPublisher -> whatsapp.outbound
Dispatcher -> WhatsApp: confirmation
```

## 39.2 Clarification
```text
User -> "Bench 80kg"
Extractor -> Validator: missing repetitions
Subgraph -> interrupt(ClarificationSpec)
Checkpoint + pending_clarifications -> PostgreSQL
Workflow status -> WAITING_FOR_USER

User -> "8 reps"
PendingWorkflowResolver -> classifies ANSWER
LangGraph Command(resume=...)
Validator -> ApplicationService -> persist
ResponseNormalizer -> confirmation
```

## 39.3 Analysis plus recommendation
```text
User: "Am I progressing on bench and should I increase load?"
Planner DAG:
  A TrainingAnalysis (USER_VISIBLE or INTERNAL)
  B Recommendation depends on A REQUIRE_SUCCESS

A -> analytics read tools -> TrainingAnalysisResult
B -> receives structured AnalysisResult + profile + RAG evidence -> RecommendationResult
Validator -> ResultCollector -> ResponseNormalizer -> messages[]
```

# 40. Main Contracts

## 40.1 DomainResult pattern
```text
DomainResult (discriminated union)
- WorkoutLoggedResult
- CorrectionResult
- TrainingAnalysisResult
- RecommendationResult
- WorkoutProgramDraftResult
- ProgramActivationResult
- ConversationResult
- Error/PartialResult

Every result that can become user-visible carries structured facts rather than final prose.
```

## 40.2 ClarificationSpec
```text
ClarificationSpec {
  clarification_id
  reason
  original_task_id
  missing_fields[]
  ambiguous_entities[]
  expected_response_schema
}
```

## 40.3 Retrieval evidence
```text
RetrievedEvidence {
  evidence_id
  document_id
  chunk_id
  source
  content
  retrieval_score?
  reranker_score?
  metadata
}
```

## 40.4 Recommendation result
```text
RecommendationResult {
  recommendations[]
  evidence[]
  user_context_refs[]
  analysis_refs[]
  confidence
  assumptions[]
  limitations[]
}
```

# 41. Implementation Roadmap

## Phase 0 - Foundation
1. Create repository/package boundaries, typed settings, Docker Compose and CI skeleton.
2. Provision PostgreSQL, RabbitMQ, Redis and Qdrant locally.
3. Implement SQLAlchemy/Alembic base, IDs, timestamps, repositories and transaction helpers.
4. Implement trace/correlation context, Datadog hooks and Langfuse client abstraction.
5. Implement event envelope, transactional outbox and OutboxPublisher.

## Phase 1 - Inbound and workout logger MVP
1. WhatsApp webhook + identity + message deduplication.
2. RabbitMQ inbound exchange and Redis debounce/message batches.
3. Canonical exercise catalog and deterministic resolver stages.
4. Training session manager, strength activity model, ActivitySchemaRegistry and validator.
5. WorkoutExtractor structured LLM, EffortNormalizer and WorkoutLoggingSubgraph.
6. ResponseNormalizer + deterministic fallback + outbound outbox/dispatcher.
7. Core golden evals for intent/extraction/resolution/fact preservation.

## Phase 2 - LangGraph orchestration and correction
1. Static MainGraph, ExecutionPlan, scheduler, task-result reducer and persistent checkpointer.
2. workflow_executions/execution_tasks/pending_clarifications.
3. Interrupt/resume and PendingWorkflowResolver.
4. CorrectionSubgraph, optimistic concurrency, undo and provenance.
5. Partitioned workflow queues and operational DLQ/replay.

## Phase 3 - Analytics, analysis and recommendation
1. TrainingAnalyticsService and metric versions.
2. TrainingAnalysisAgent with read-only tools.
3. RecommendationAgent + deterministic validator + conditional critic.
4. Async post-session analytics/progression and cache invalidation.
5. Expanded eval harness with trajectory/cost/latency gates.

## Phase 4 - RAG
1. KnowledgeDocument registry + Object Storage adapter.
2. Chunking profiles, EmbeddingProvider, Qdrant indexing and payload indexes.
3. KnowledgeRetrievalService dense + metadata filter + reranker.
4. Evidence propagation into recommendations and RAG eval datasets.
5. Blue/green reindex capability and retrieval-profile versioning.

## Phase 5 - Workout programs and memory
1. Program domain, DRAFT/ACTIVE/ARCHIVED lifecycle and versioning.
2. WorkoutProgramAgent, ProgramAnalyticsService, validators and activation flow.
3. ProgressionRule + deterministic ProgressionEngine + prescription operational state.
4. Async MemoryCandidateExtractor + strict MemoryPolicy including health-data rejection.
5. “What do you remember?” / forget-memory commands.

## Phase 6 - Hardening
1. Privacy deletion and retention workers.
2. Security roles/secrets encryption and telemetry redaction hardening.
3. Failure-injection suites, load tests and hot-partition metrics.
4. Staging release gates, shadow-capable evaluation contracts and SLO dashboards.
5. Cloud-specific deployment reference and production runbooks.

# 42. Initial Definition of Done for MVP
- Text and audio-transcript inputs can log strength workouts through WhatsApp.
- Fragmented messages batch correctly and preserve original-message provenance.
- Essential missing data triggers resumable clarification.
- Exercise resolution uses canonical IDs and avoids low-confidence silent matches.
- Current-session corrections and undo work idempotently.
- Training sessions auto-close robustly.
- Analysis and recommendation operate independently or with explicit DAG dependency.
- All user-facing output passes ResponseNormalizer and fact-preservation guard.
- RabbitMQ redelivery does not duplicate workout side effects or outbound confirmations.
- Datadog and Langfuse traces correlate by workflow/correlation identifiers.
- Golden eval gates run in CI/CD.
- No health/medical information is persisted as long-term memory or supported user document content.

# 43. Architectural Decision Records to Create

| ADR | Decision |
| --- | --- |
| ADR-001 | Modular monolith with distributed worker entrypoints. |
| ADR-002 | PostgreSQL as domain source of truth; Qdrant derived index. |
| ADR-003 | RabbitMQ at-least-once + idempotency + transactional outbox. |
| ADR-004 | 32 stable-hash workflow partitions + Single Active Consumer. |
| ADR-005 | Static LangGraph MainGraph with ExecutionPlan DAG as data. |
| ADR-006 | ResponseNormalizer is mandatory final prose boundary. |
| ADR-007 | Structured SQL profile/history separate from semantic RAG. |
| ADR-008 | Workout programs are immutable versions with draft activation. |
| ADR-009 | Datadog + Langfuse split observability responsibilities. |
| ADR-010 | Health/medical information is outside persisted product scope. |


# 44. Open Implementation Parameters (Configuration, Not Architecture)
The following are intentionally not hard-coded architectural decisions and must be calibrated through load tests/evals/product policy:
- Training-session inactivity timeout.
- Conversation rotation timeout.
- Correction grace period duration.
- Per-agent max iterations/tool calls/time/token/cost budgets.
- Retry delays and maximum attempts.
- RAG retrieve_top_n and return_top_k.
- Embedding and reranker provider/model.
- Specific LLM providers/models for each model profile.
- Data-retention periods by category.
- Release-gate tolerances and latency/cost SLOs.
- Cloud provider and managed service mappings.

# 45. Final Architecture Summary
The implementation should begin as a modular Python application whose correctness does not depend on an LLM behaving deterministically. LLMs are bounded by typed schemas, scoped tools, deterministic validators and application services. PostgreSQL transactions and the outbox own business consistency; RabbitMQ owns asynchronous delivery; Redis owns ephemeral coordination; LangGraph owns resumable orchestration within an interaction; Qdrant owns semantic retrieval over rebuildable knowledge indexes; Datadog owns application/infrastructure observability; Langfuse owns AI observability and evaluation.
The most important implementation invariant is that probabilistic reasoning can fail, retry or be replaced without corrupting structured workout state. The second invariant is that every user-facing answer is derived from explicit DomainResult objects and cannot silently mutate their facts. The third is that every asynchronous path tolerates duplicate delivery through idempotent business effects.
This specification is the implementation baseline. Changes to the principles above should be recorded as ADRs rather than introduced implicitly in code.

# 46. Decision Traceability Matrix — Q1 to Q160
This appendix traces the interview decisions to implementation impact. **CONFIRMED** means the numbered mapping is recoverable from the retained decision record. **RECONSTRUCTED** applies only to Q24–Q70: the approved decisions are preserved in the architecture, but the original numbered question wording is not retained, so the topic-to-number mapping is reconstructed rather than presented as an exact transcript. The normative architecture sections remain authoritative if any wording differs.
| Q | Final decision | Implementation consequence | Traceability |
|---:|---|---|---|
| Q1 | “80 kg, 10, 9, 8” represents three sets. | Extractor expands one coherent strength statement into three ExerciseSet records. | CONFIRMED |
| Q2 | One message may contain multiple exercises. | WorkoutExtractionResult supports multiple activities/exercises per InputBatch. | CONFIRMED |
| Q3 | Normalize perceived effort and always communicate normalized RPE. | Persist raw effort plus normalized RPE/method/version; ResponseNormalizer exposes RPE. | CONFIRMED |
| Q4 | Ask only for essential missing data; optional fields remain null. | ActivitySchemaRegistry/Validator distinguishes essential vs optional fields and triggers clarification only for essential gaps. | CONFIRMED |
| Q5 | Training-session inactivity timeout is configurable. | TrainingSessionManager and expiration worker use typed configuration, independent from conversation timeout. | CONFIRMED |
| Q6 | Fragmented messages may compose one workout item; use debounce. | Redis sliding debounce batches fragments before LangGraph processing. | CONFIRMED |
| Q7 | Corrections are supported in the MVP. | Dedicated CorrectionSubgraph, provenance, optimistic concurrency and undo are part of the core design. | CONFIRMED |
| Q8 | Workout plans use a rich/full prescription model. | Programs include days, prescriptions, ranges, RPE, rest, progression, substitutions and grouping. | CONFIRMED |
| Q9 | Store the proposed onboarding/profile training fields. | Structured profile contains goals, experience, frequency, equipment, preferences, avoidances and related training context. | CONFIRMED |
| Q10 | MVP target scale is roughly 100–10,000 users. | Async workers, partitioned RabbitMQ processing and horizontal container scaling are sized for this range. | CONFIRMED |
| Q11 | Debounce uses 3 s sliding window and 10 s absolute maximum batch window. | Redis batch state resets the short deadline but cannot exceed MAX_BATCH_WINDOW. | CONFIRMED |
| Q12 | Confirm every successful workout registration. | WorkoutLoggedResult always flows to a confirmation via ResponseNormalizer or deterministic fallback. | CONFIRMED |
| Q13 | Use an external Speech-to-Text API initially. | SpeechToTextProvider abstracts provider choice; transcription is not implemented as local model infrastructure in MVP. | CONFIRMED |
| Q14 | Use model routing by agent/task profile. | Model Registry exposes fast/extraction/reasoning profiles with independent versions. | CONFIRMED |
| Q15 | Use Redis + RabbitMQ, with distinct responsibilities. | RabbitMQ owns durable async transport; Redis owns ephemeral coordination/cache/debounce. | CONFIRMED |
| Q16 | Use Qdrant as vector database. | KnowledgeRetrievalService and exercise semantic fallback target Qdrant behind abstractions. | CONFIRMED |
| Q17 | Generated workout programs start as drafts and require user confirmation. | WorkoutProgram lifecycle is DRAFT → explicit approval → ACTIVE; previous active version is archived. | CONFIRMED |
| Q18 | Architecture is partially event-driven, not full event sourcing. | Domain tables remain current state; append-only domain/audit events and outbox drive asynchronous reactions. | CONFIRMED |
| Q19 | An LLM may identify long-term memory candidates, but does not directly persist them. | MemoryCandidateExtractor → MemoryPolicyValidator → repository; memory extraction runs asynchronously. | CONFIRMED |
| Q20 | Separate global semantic RAG from structured user context/history. | PostgreSQL supplies profile/history/program state; Qdrant supplies semantic knowledge evidence. | CONFIRMED |
| Q21 | WhatsApp external identity uses BSUID; internal identity remains UUID. | user_identifiers maps provider identity to internal User and supports future channels. | CONFIRMED |
| Q22 | Use a canonical exercise catalog plus global and user-learned aliases. | ExerciseResolver maps natural language to canonical IDs and can learn user-specific aliases. | CONFIRMED |
| Q23 | Use 32 workflow partitions with Single Active Consumer. | stable_hash(user_id)%32 preserves per-user order while allowing cross-partition parallelism. | CONFIRMED |
| Q24 | RabbitMQ processing uses at-least-once delivery with idempotent consumers. | Business effects rely on operation IDs/unique constraints rather than exactly-once transport. | RECONSTRUCTED |
| Q25 | Use Transactional Outbox for reliable event/outbound publication. | Domain mutation, domain event and outbox row commit atomically; publisher sends after commit. | RECONSTRUCTED |
| Q26 | LLMs never write databases directly. | Structured LLM output is converted into domain commands handled by application services and transactions. | RECONSTRUCTED |
| Q27 | Load context lazily instead of injecting full user history into every request. | Main flow loads identity/session/pending state; handlers fetch only task-specific context. | RECONSTRUCTED |
| Q28 | Multi-intent workflows preserve successful independent tasks on partial failure. | Workflow can end PARTIAL_SUCCESS; no global rollback of unrelated committed work. | RECONSTRUCTED |
| Q29 | A pending clarification does not block unrelated new intents. | PendingWorkflowResolver classifies incoming input as clarification answer, new intent or cancellation. | RECONSTRUCTED |
| Q30 | Rotate conversations after configurable long inactivity. | A Conversation maps to a LangGraph thread and is distinct from TrainingSession. | RECONSTRUCTED |
| Q31 | Training-session expiration uses background expiration plus lazy fallback. | SessionExpirationWorker closes stale sessions; next workout input also validates stale active sessions. | RECONSTRUCTED |
| Q32 | Normal workout logger does not create retroactive sessions. | performed_at is tied to current interaction/session; retrospective import requires a separate future workflow. | RECONSTRUCTED |
| Q33 | Structured user profile/history belongs in PostgreSQL, not embeddings by default. | RAG is reserved for semantically appropriate knowledge/documents; structured facts remain queryable domain data. | RECONSTRUCTED |
| Q34 | User-specific semantic knowledge shares a collection with tenant filtering rather than collection-per-user. | Qdrant user_knowledge uses mandatory user_id payload filters. | RECONSTRUCTED |
| Q35 | Long-term memories have lifecycle ACTIVE/SUPERSEDED/DELETED/EXPIRED. | Contradictory explicit new information supersedes prior active memory rather than silently overwriting provenance. | RECONSTRUCTED |
| Q36 | Memory is normally stored silently, but users can inspect or forget remembered information. | Product supports “what do you remember?” and “forget X” workflows. | RECONSTRUCTED |
| Q37 | Long-term memory extraction runs asynchronously after the user response. | Memory processing does not increase WhatsApp response latency; current-message preferences remain usable immediately. | RECONSTRUCTED |
| Q38 | Use ReAct/tool loops selectively, not for every LLM component. | Analysis, Recommendation and WorkoutProgram are agentic; routers/extractors/normalizers stay structured calls. | RECONSTRUCTED |
| Q39 | Agents use least-privilege domain tools and never a generic SQL tool. | Tools inject user/authorization context and expose bounded read/query operations. | RECONSTRUCTED |
| Q40 | User profile is a structured first-class domain model. | Goals, experience, frequency, equipment and preferences are directly queryable and versionable. | RECONSTRUCTED |
| Q41 | Exercise resolution follows a staged deterministic-to-probabilistic hierarchy. | User alias → global alias → canonical → fuzzy → vector → LLM → clarification. | RECONSTRUCTED |
| Q42 | Exercise resolution is confidence-routed. | High confidence continues, medium can invoke assisted resolution, low/ambiguous asks the user. | RECONSTRUCTED |
| Q43 | Exercise-to-muscle relations record role. | exercise_muscles distinguishes PRIMARY, SECONDARY and STABILIZER. | RECONSTRUCTED |
| Q44 | Exercise catalog supports semantic relations between exercises. | Relations include VARIATION_OF, SUBSTITUTE_FOR, SIMILAR_MOVEMENT, PROGRESSION_OF and REGRESSION_OF. | RECONSTRUCTED |
| Q45 | Support multiple activity classes at schema level from MVP. | Activity types include STRENGTH, DISTANCE_ACTIVITY, TIMED_ACTIVITY, MIXED_ACTIVITY, MOBILITY and OTHER. | RECONSTRUCTED |
| Q46 | Strength sets require repetitions; load is optional. | “Supino 80 kg” triggers clarification for reps; “10 flexões” is valid. | RECONSTRUCTED |
| Q47 | Distance activities accept distance, duration or both. | Pace/speed are derived deterministically where sufficient data exists. | RECONSTRUCTED |
| Q48 | Bodyweight exercises do not require the user’s body weight. | External assistance/load is recorded separately with BODYWEIGHT_PLUS/MINUS semantics. | RECONSTRUCTED |
| Q49 | Dumbbell load defaults to per-implement semantics. | A reported 20 kg means 20 kg per dumbbell/implement unless explicit context says otherwise. | RECONSTRUCTED |
| Q50 | Warm-up and other set roles are explicit. | Set types include WARMUP, WORKING, BACKOFF, DROP_SET, FAILURE, AMRAP and OTHER. | RECONSTRUCTED |
| Q51 | Supersets/trisets/circuits are explicit domain groups. | exercise_groups/prescription_groups represent grouped execution rather than encoding it in notes. | RECONSTRUCTED |
| Q52 | Derived metrics are deterministic services, not LLM calculations. | Volume, pace, speed, estimated 1RM and related metrics are versioned code paths. | RECONSTRUCTED |
| Q53 | Primary ReAct candidates are RecommendationAgent, TrainingAnalysisAgent and WorkoutProgramAgent. | Agentic tool loops are reserved for tasks that need iterative evidence/tool selection. | RECONSTRUCTED |
| Q54 | Allow contextual inheritance across coherent fragmented sets. | Explicit/inherited provenance is stored, e.g. one stated load applied to several rep counts. | RECONSTRUCTED |
| Q55 | Workout extraction receives a small active-session summary. | Recent exercise/set context resolves phrases such as “agora 85 por 6” without loading full history. | RECONSTRUCTED |
| Q56 | In a mixed-validity multi-exercise request, persist resolved valid activities and clarify ambiguous ones. | Semantic ambiguous items are excluded before command construction; valid work is not discarded. | RECONSTRUCTED |
| Q57 | All valid activities in one LogWorkoutCommand commit atomically. | One coherent logging command maps to one domain transaction. | RECONSTRUCTED |
| Q58 | Consecutive sets of the same exercise share a SessionExercise block; returning later creates a new block. | exercise_block_index preserves workout order and repeated exercise blocks. | RECONSTRUCTED |
| Q59 | All user-facing prose passes through ResponseNormalizerAgent. | Domain agents emit structured DomainResult objects and do not directly write WhatsApp text. | RECONSTRUCTED |
| Q60 | Message splitting belongs to ResponseNormalizerAgent. | NormalizedResponse contains ordered OutboundMessage[] and may emit one or several WhatsApp messages. | RECONSTRUCTED |
| Q61 | Successful registrations always produce confirmation. | Confirmation is an invariant regardless of whether the response is one or multiple messages. | RECONSTRUCTED |
| Q62 | ResponseGuard follows ResponseNormalizer and validates facts/invariants. | Normalizer cannot silently alter exercise, load, repetitions, RPE or other verifiable domain facts. | RECONSTRUCTED |
| Q63 | Use deterministic response templates when the response LLM fails. | Critical acknowledgements remain available even during LLM/provider failure. | RECONSTRUCTED |
| Q64 | Long-term memory is typed and policy-controlled. | Training preferences, goals, equipment and routine-like facts are candidates; unsupported/sensitive categories are rejected. | RECONSTRUCTED |
| Q65 | Onboarding/profile captures the agreed comprehensive training fields. | Recommendation/program context can be constructed from structured fields instead of free-form memory. | RECONSTRUCTED |
| Q66 | Workout programs are versioned and never silently mutate the active version. | Changes clone active version to a new DRAFT and require approval before activation. | RECONSTRUCTED |
| Q67 | Exercise prescriptions support rich structured targets. | Sets, rep ranges, optional load, RPE/RIR, rest, tempo, warmups, substitutions and notes are explicit. | RECONSTRUCTED |
| Q68 | ProgressionRule is separate from ExercisePrescription. | LLM may propose structured rules; deterministic ProgressionEngine evaluates conditions/actions. | RECONSTRUCTED |
| Q69 | Performed exercises can link back to planned prescriptions. | SessionExercise.prescription_id enables planned-vs-performed analysis. | RECONSTRUCTED |
| Q70 | Correction is a controlled workflow rather than an unrestricted ReAct mutation agent. | Semantic extraction/ranking may use LLMs, but resolution, validation and writes remain bounded. | RECONSTRUCTED |
| Q71 | Corrections apply only to the current training session. | Natural correction does not search arbitrary historical workouts. | CONFIRMED |
| Q72 | Allow a configurable correction grace period after session finish. | Recently auto-finished session remains temporarily correctable. | CONFIRMED |
| Q73 | “Última série” resolves deterministically to the latest valid non-deleted set. | No clarification is required when this reference is unambiguous in the allowed session. | CONFIRMED |
| Q74 | Implicit recent-entity references may resolve automatically at high confidence. | Phrases such as “era inclinado, não reto” can target the immediate recent entity. | CONFIRMED |
| Q75 | Correction resolution uses confidence routing. | High executes; medium uses assisted ranking; low/multiple candidates clarify. | CONFIRMED |
| Q76 | CorrectionResolver is not ReAct. | Use deterministic/contextual pipeline + LLM ranking fallback + clarification. | CONFIRMED |
| Q77 | One message may correct multiple records. | CorrectionIntent supports multiple operations, e.g. first set 10 reps and second 9. | CONFIRMED |
| Q78 | Resolved valid operations from one coherent correction request commit atomically. | Ambiguous operations are excluded before the transaction. | CONFIRMED |
| Q79 | Undo option B: undo the last reversible CREATE/UPDATE/DELETE. | Undo is implemented as a compensating domain operation/event; audit history remains append-only. | CONFIRMED |
| Q80 | Deleting a whole exercise soft-deletes its SessionExercise and child sets in one operation. | Domain event records affected children; deletion is explicit, not merely hidden by parent filtering. | CONFIRMED |
| Q81 | Analysis and Recommendation are separate; each may flow directly to ResponseNormalizer. | ExecutionPlanner introduces a dependency only when the user request requires recommendation to consume analysis. | CONFIRMED |
| Q82 | TrainingAnalysisAgent is read-only. | It cannot modify training, profile, program or memory state. | CONFIRMED |
| Q83 | Analytics strategy is hybrid. | Simple metrics are on-demand/cacheable; expensive/frequent metrics may later be materialized/precomputed. | CONFIRMED |
| Q84 | RAG architecture is hybrid-ready, but MVP uses dense + metadata filters + reranking. | Sparse retrieval/fusion remains a compatible later extension. | CONFIRMED |
| Q85 | Important Analysis/Recommendation conclusions carry evidence references. | Traces can link conclusions to metrics, history and RAG chunks. | CONFIRMED |
| Q86 | Agents access RAG only through bounded retrieval tools/services. | No agent receives raw Qdrant client/collection control. | CONFIRMED |
| Q87 | Recommendations pass deterministic validation and optional semantic critic. | Constraints are code-validated first; semantic critic runs only for complex cases. | CONFIRMED |
| Q88 | Every agentic loop has explicit budgets. | Max iterations/tool calls plus time/token/cost limits are configurable and eval-calibrated. | CONFIRMED |
| Q89 | Agents do not arbitrarily call other agents. | ExecutionPlanner creates explicit DAG dependencies and agents exchange structured results. | CONFIRMED |
| Q90 | Persist only product-relevant recommendation/analysis outputs as domain state. | Routine agent results remain ephemeral/traced; accepted programs/progression actions become domain entities when needed. | CONFIRMED |
| Q91 | WorkoutProgramAgent is separate from RecommendationAgent. | Point recommendations and structured program authoring have distinct contracts and toolsets. | CONFIRMED |
| Q92 | WorkoutProgramAgent is a ReAct/tool-calling agent. | It builds a structured proposal using read-only context/tools and cannot persist directly. | CONFIRMED |
| Q93 | Prescriptions support optional target load plus RPE, rep range and progression rules. | Both fixed and adaptive prescriptions are representable. | CONFIRMED |
| Q94 | System may suggest a next load automatically but does not force it. | User can log any performed load; suggestion does not overwrite canonical prescription. | CONFIRMED |
| Q95 | Progression uses option C: update operational next-target state, not the canonical program version. | ExercisePrescriptionState carries next_target_load/last evaluation separately from immutable program version. | CONFIRMED |
| Q96 | Workout-day order is recommended, not rigid. | User may choose another day; session links to the selected WorkoutDay. | CONFIRMED |
| Q97 | NextWorkoutResolver only suggests the next workout. | No automatic forced selection/activation of a workout day. | CONFIRMED |
| Q98 | Users can train without an active program. | TrainingSession.workout_program_id/workout_day_id may be null. | CONFIRMED |
| Q99 | Changing a program during an active session does not rewrite that session’s version. | Modification creates a new DRAFT; current session remains linked to the version it started with. | CONFIRMED |
| Q100 | TrainingSessionFinished triggers async analytics/progression processing. | Planned-vs-performed and next-target state can update in background without mandatory user notification. | CONFIRMED |
| Q101 | PostgreSQL + Object Storage are authoritative knowledge sources; Qdrant is derived. | Vector indexes can be destroyed/rebuilt without losing original knowledge. | CONFIRMED |
| Q102 | Use training_knowledge and user_knowledge collections, not collection-per-user/type. | Payload metadata and tenant filters provide logical separation. | CONFIRMED |
| Q103 | Chunking strategy depends on document type. | Exercises use semantic sections; technical docs hierarchical chunks; templates logical blocks. | CONFIRMED |
| Q104 | Support parent/child retrieval. | Small precise retrieval chunks can expand to larger parent context for generation. | CONFIRMED |
| Q105 | Embedding provider is abstracted and versioned. | Model/provider/dimension changes can reindex without agent changes. | CONFIRMED |
| Q106 | Do not implement sparse retrieval in MVP. | Dense + filter + rerank ships first while interfaces remain hybrid-compatible. | CONFIRMED |
| Q107 | Include a replaceable Reranker interface from MVP. | Retrieve top-N then rerank to top-K; values are configuration/eval-driven. | CONFIRMED |
| Q108 | Knowledge carries source/provenance/trust metadata. | Retrieval policies can filter/weight by source, type, version and trust level. | CONFIRMED |
| Q109 | Documents are immutable versions and reindex/migration is controlled. | Old version becomes SUPERSEDED; embedding migrations use blue/green or equivalent alias strategy. | CONFIRMED |
| Q110 | Relevant RAG changes require evaluation before release. | Embedding/chunking/reranker/retrieval changes compare candidate against production baseline. | CONFIRMED |
| Q111 | Use RabbitMQ exchanges separated by responsibility. | whatsapp.inbound, workflow, domain.events, background and whatsapp.outbound have distinct routing purposes. | CONFIRMED |
| Q112 | Persist inbound messages before enqueueing async work. | Webhook success is based on durable message capture before Redis/workflow processing. | CONFIRMED |
| Q113 | Redis debounce uses generation/version to prevent stale timer flush. | Old debounce timers are ignored when a newer generation exists. | CONFIRMED |
| Q114 | Implement 32 physical workflow queues with stable hashing and SAC. | Ordering is explicit infrastructure behavior, not a distributed-lock approximation. | CONFIRMED |
| Q115 | Initial workflow consumer prefetch is 1. | Simplifies strict ordered processing; benchmark may justify a later increase. | CONFIRMED |
| Q116 | Retries are asynchronous through retry/delay queues. | Workers do not sleep while holding a message/consumer slot. | CONFIRMED |
| Q117 | Critical worker classes have DLQ plus inspect/replay/discard tooling. | Replay preserves original idempotency keys to avoid duplicate business effects. | CONFIRMED |
| Q118 | Persist workflow_executions and execution_tasks in addition to LangGraph checkpoints. | Checkpoint state and operational/business execution status remain separate concerns. | CONFIRMED |
| Q119 | Outbound WhatsApp delivery also uses persisted message + Outbox. | Agent workflow does not call WhatsApp API directly; dispatcher owns provider retries. | CONFIRMED |
| Q120 | Split response ordering is persisted by response_group_id + sequence. | Failed later message retries do not resend already-delivered earlier messages. | CONFIRMED |
| Q121 | MainGraph is static. | ExecutionPlan is dynamic data; topology is not rebuilt per request. | CONFIRMED |
| Q122 | Task scheduler uses fan-out/fan-in for READY tasks. | Dynamic parallelism is derived from dependencies and merged with task-result reducers. | CONFIRMED |
| Q123 | thread_id equals conversation_id. | Conversation windows are LangGraph persistence namespaces; TrainingSession remains distinct. | CONFIRMED |
| Q124 | Production LangGraph checkpointer is persistent in PostgreSQL. | Checkpoint tables are isolated from domain tables by schema/database boundary when practical. | CONFIRMED |
| Q125 | Persist pending clarification outside checkpoint internals as operational state. | Incoming message routing can efficiently discover waiting workflows. | CONFIRMED |
| Q126 | Interrupt before dependent side effects whenever possible. | Resumed nodes avoid duplicate writes; unavoidable earlier writes must be idempotent. | CONFIRMED |
| Q127 | Execution dependencies carry policies REQUIRE_SUCCESS/ALLOW_PARTIAL/OPTIONAL. | Failed required predecessors cause dependent tasks to be SKIPPED rather than blindly executed. | CONFIRMED |
| Q128 | Persist task status outside GraphState. | execution_tasks supports monitoring, debugging and replay independently from computational snapshots. | CONFIRMED |
| Q129 | Use subgraphs only for meaningful workflow boundaries. | WorkoutLogging/Correction/ExerciseResolver are subgraphs; agent handlers need not become subgraphs by default. | CONFIRMED |
| Q130 | ACK workflow message only after safe outbound/outbox persistence. | No need to wait for actual WhatsApp provider delivery before ACK. | CONFIRMED |
| Q131 | Create one main trace per InputBatch interaction. | Derived background jobs use their own traces linked through correlation_id. | CONFIRMED |
| Q132 | Trace all behavior-affecting versions. | graph, agent, prompt, model-profile and retrieval-profile versions are recorded. | CONFIRMED |
| Q133 | Evaluation exists at component, workflow and end-to-end levels. | Failures can be localized rather than judged only from final prose. | CONFIRMED |
| Q134 | Maintain a reviewed golden regression dataset. | Critical components must pass stable representative cases before release. | CONFIRMED |
| Q135 | Production corrections/failures may become eval cases only after review. | No automatic ingestion of arbitrary user conversations into evaluation datasets. | CONFIRMED |
| Q136 | Response fact preservation is a strict gate. | Normalizer changes to exercise/load/reps/RPE/dates or other verifiable facts fail validation. | CONFIRMED |
| Q137 | Evaluate ReAct trajectories as well as final answers. | Tool usage, loops and inefficiency are monitored without requiring one unique valid trajectory. | CONFIRMED |
| Q138 | Run applicable eval gates in CI/CD with tiered cost. | PR fast evals; broader main; full staging/release; expensive nightly/shadow experiments. | CONFIRMED |
| Q139 | Cost and latency are evaluation metrics alongside quality. | Candidate selection considers tokens, cost, p50/p95, tool count and iterations. | CONFIRMED |
| Q140 | Architecture supports shadow testing of candidate AI components. | Production response remains authoritative while candidate output is evaluated without user impact. | CONFIRMED |
| Q141 | Do not persist user health information or similar medical documents; only optional body measurements are allowed. | MemoryPolicy rejects diagnoses, symptoms, injuries, medications, medical history/status and medical documents; measurements use dedicated structured domain. | CONFIRMED |
| Q142 | Use transport encryption, storage encryption, and selective application-level field encryption. | Avoid indiscriminate per-column crypto while protecting higher-risk identifiers/fields. | CONFIRMED |
| Q143 | Protect BSUID as encrypted value plus keyed HMAC lookup. | Exact identity lookup remains possible without storing a directly searchable plaintext external identifier. | CONFIRMED |
| Q144 | Use a SecretsProvider abstraction. | Secrets stay outside source/prompts/logs/images and can map to the chosen cloud secrets manager. | CONFIRMED |
| Q145 | Apply least privilege between infrastructure services as well as agents. | Service-specific DB roles, credentials and permissions limit blast radius. | CONFIRMED |
| Q146 | Redact telemetry in the application before Datadog/Langfuse. | TelemetryRedactor masks/removes/hashes protected content instead of relying only on vendor-side filters. | CONFIRMED |
| Q147 | Each agent has a ContextPolicy/data-minimization boundary. | Intent/extraction agents receive small context; recommendation/program agents receive only relevant profile/history/evidence. | CONFIRMED |
| Q148 | Model a PrivacyDeletionWorkflow from the start. | Deletion/anonymization coordinates PostgreSQL, Qdrant, object storage, checkpoints and caches. | CONFIRMED |
| Q149 | Retention is category-specific, not one global TTL. | Raw messages, traces, audit, workout history, DLQ and backups have independent policies. | CONFIRMED |
| Q150 | Maintain an explicit security/privacy incident workflow. | Detection, containment, affected-data assessment, evidence, communications, remediation and postmortem are operational requirements. | CONFIRMED |
| Q151 | Use one modular-monolith codebase with separately scalable worker entrypoints. | Shared domain/contracts remain in one repository while API/workers run as independent processes/containers. | CONFIRMED |
| Q152 | Use Docker and managed infrastructure where practical; no Kubernetes in MVP. | Operational complexity is kept proportional to 100–10k-user target; orchestration can evolve later. | CONFIRMED |
| Q153 | FastAPI is ingress/API only and does not run heavy LangGraph work inside webhook requests. | Persist + enqueue + fast webhook response; workers execute LLM/RAG/workflows asynchronously. | CONFIRMED |
| Q154 | Use typed externalized configuration. | Operational values are grouped in validated settings rather than scattered constants. | CONFIRMED |
| Q155 | Use SQLAlchemy + Alembic for PostgreSQL persistence/migrations. | Domain layers depend on ports/repositories; schema evolution is explicit and backward-compatible when needed. | CONFIRMED |
| Q156 | Include a FeatureFlagProvider abstraction. | AI model/prompt/agent/RAG rollouts can be controlled without hard-coding vendor choice. | CONFIRMED |
| Q157 | Local development uses Docker Compose. | PostgreSQL, RabbitMQ, Redis, Qdrant and required app services can be started reproducibly. | CONFIRMED |
| Q158 | Integration tests use ephemeral real infrastructure/Testcontainers-style dependencies. | Database/messaging/vector behaviors are tested against real services rather than mocked away. | CONFIRMED |
| Q159 | CI/CD uses tiered test + eval gates. | PR, main, staging/release and nightly stages trade off speed, cost and coverage. | CONFIRMED |
| Q160 | Keep the main deployment architecture vendor-neutral. | Cloud-specific service mappings belong in a later deployment appendix/ADR rather than the core design. | CONFIRMED |

# 47. Detailed Decision Records
These records preserve the rationale and consequences of the most structural decisions. They complement, rather than replace, the normative architecture sections and the ADR list.

## DEC-001 — Probabilistic reasoning vs deterministic authority
**Decision/rationale.** LLMs classify, extract, plan, analyze and recommend, but application services, validators, transactions and unique constraints own business correctness.

**Implementation implications.** Allows model/provider changes and retries without corrupting structured workout state.

**Interview origin.** Q4, Q26, Q52, Q82, Q87.

## DEC-002 — Static LangGraph MainGraph with ExecutionPlan as data
**Decision/rationale.** A stable graph executes a dynamic task DAG instead of constructing a new StateGraph per request.

**Implementation implications.** Improves checkpointing, tracing, replay, testing and graph-version governance.

**Interview origin.** Q121-Q129.

## DEC-003 — Analysis and Recommendation are independent
**Decision/rationale.** Analysis may return directly to ResponseNormalizer; Recommendation depends on Analysis only when the planner declares it.

**Implementation implications.** Avoids unnecessary agent cost and keeps user intent semantics explicit.

**Interview origin.** Q81-Q90.

## DEC-004 — Mandatory ResponseNormalizer boundary
**Decision/rationale.** Every user-visible DomainResult passes through ResponseNormalizer and ResponseGuard; domain agents never write final WhatsApp prose.

**Implementation implications.** Centralizes persona/message splitting while enforcing fact preservation.

**Interview origin.** Q59-Q63, Q120, Q136.

## DEC-005 — At-least-once messaging + idempotent business effects
**Decision/rationale.** RabbitMQ delivery can duplicate; commands/events/outbound messages carry stable idempotency keys and DB constraints.

**Implementation implications.** Avoids fragile exactly-once assumptions and supports crash/retry recovery.

**Interview origin.** Q24-Q25, Q111-Q120, Q130.

## DEC-006 — 32 stable-hash workflow partitions + SAC
**Decision/rationale.** All interactions for one user hash to one of 32 workflow queues with a Single Active Consumer.

**Implementation implications.** Preserves per-user order while enabling parallelism across users; partition count is scalable.

**Interview origin.** Q23, Q114-Q115.

## DEC-007 — PostgreSQL structured truth vs Qdrant semantic index
**Decision/rationale.** User/profile/workout/program state is relational; Qdrant is a rebuildable retrieval index over authoritative knowledge.

**Implementation implications.** Prevents semantic search from becoming a source of truth and enables safe re-embedding.

**Interview origin.** Q20, Q33-Q34, Q101-Q109.

## DEC-008 — Workout programs are immutable versions
**Decision/rationale.** Program changes create a new DRAFT, explicit approval activates it, and current sessions keep their starting version.

**Implementation implications.** Preserves reproducibility and planned-vs-performed history.

**Interview origin.** Q17, Q66-Q69, Q91-Q100.

## DEC-009 — Corrections are bounded current-session operations
**Decision/rationale.** CorrectionResolver targets current/recently-finished session, uses confidence routing and applies idempotent versioned commands.

**Implementation implications.** Limits dangerous broad mutation and makes undo/audit deterministic.

**Interview origin.** Q70-Q80.

## DEC-010 — Debounce fragmented WhatsApp messages before planning
**Decision/rationale.** Redis groups fragments with 3s sliding / 10s maximum window and generation protection.

**Implementation implications.** Improves extraction quality without blocking webhook and avoids stale timer races.

**Interview origin.** Q6, Q11, Q113.

## DEC-011 — Long-term memory is structured, async and policy-controlled
**Decision/rationale.** LLM extracts candidates after response; policy validates/supersedes/rejects and PostgreSQL stores approved training context.

**Implementation implications.** Keeps latency low and prevents LLM from directly mutating durable memory.

**Interview origin.** Q19, Q35-Q37, Q64-Q65, Q141.

## DEC-012 — Health information is outside persisted product scope
**Decision/rationale.** Diagnoses, symptoms, injuries, medications, medical history/status and medical documents are rejected from durable memory/document storage; only optional body measurements are supported.

**Implementation implications.** Reduces privacy surface and keeps the product focused on training tracking rather than health records.

**Interview origin.** Q141, architecture clarification before Q151.

## DEC-013 — Datadog and Langfuse have separate observability domains
**Decision/rationale.** Datadog owns app/infrastructure telemetry; Langfuse owns LLM/agent/prompt/RAG/evaluation traces, correlated by shared IDs.

**Implementation implications.** Makes operational incidents and AI-quality regressions independently diagnosable.

**Interview origin.** Q131-Q140 plus observability clarification.

## DEC-014 — Evaluation gates are part of CI/CD
**Decision/rationale.** Fast deterministic/golden evals run on PR; broader component evals on main; full quality/cost/latency gates on staging/release; expensive suites run scheduled.

**Implementation implications.** Treats AI behavior changes as software/model releases rather than informal prompt edits.

**Interview origin.** Q110, Q133-Q140, Q159.

## DEC-015 — Modular monolith codebase, distributed runtime roles
**Decision/rationale.** One Python repository contains bounded modules while API, workflow, outbox and background workers deploy/scale independently.

**Implementation implications.** Avoids premature microservice overhead while retaining process-level scalability and clear boundaries.

**Interview origin.** Q151-Q160.

# 48. Traceability Maintenance Rule
Future architectural changes MUST be captured as ADRs and linked back to the affected section and, when applicable, the original interview decision. If the exact historical transcript for Q24–Q70 becomes available, the RECONSTRUCTED rows should be replaced with exact wording without changing the already-approved normative decisions unless an ADR explicitly supersedes them.
