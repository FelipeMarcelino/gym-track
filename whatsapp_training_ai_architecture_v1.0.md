# Software Architecture Specification
Multi-Agent WhatsApp Training Assistant • Version 1.0
Status: Approved architecture after iterative discovery. This document is normative for MVP implementation unless superseded by an Architecture Decision Record (ADR).
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