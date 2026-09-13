# Secure service isolation, API contracts, and MCP for a two-sleeve trading execution layer

**Bottom line (4 lines)**
1. A hard boundary between the research layer and the execution layer should be **one-way artifact drop + a signed HTTPS control API**, never shared code; correctness comes from **versioned JSON contracts, idempotency keys, and a consumer-side inbox** that turn at-least-once delivery into effectively-once *effects*.
2. The execution API should authenticate the research/LLM tier with **HMAC-SHA256 signed requests** over a canonical string (method, path, body hash, timestamp, nonce) with a **300 s replay window**, keys held in the **OS keystore** (Windows DPAPI/WinCred, Linux systemd credentials) - while the **Alpaca secret never leaves the execution process**.
3. MCP's current revision (**2026-07-28**) makes the server an **OAuth 2.1 resource server** with **RFC 8707 resource indicators** and **forbids token passthrough**; the LLM should see a **read-only + propose-only** tool surface where every mutating tool needs human approval behind a deterministic gate.
4. Every 2025-2026 MCP incident that matters here (GitHub MCP private-repo exfiltration, `mcp-remote` CVE-2025-6514, MCP Inspector CVE-2025-49596) failed on a **trust boundary, not on crypto** - so the boundary must be structural: the LLM never holds broker credentials and never touches an order path.

---

## Findings

### A. Service boundary, versioned contracts, delivery semantics

1. **Two independently shippable services with no shared code** must exchange only explicit artifacts or messages; the canonical reliability pattern is a **transactional outbox**: write the business change and the outbound event in one local transaction, then publish via a separate relay. The relay can publish **more than once** (crash between publish and record), so **consumers must be idempotent**.[21]
2. The matching inbound pattern is the **idempotent consumer / inbox**: store processed message IDs in a `PROCESSED_MESSAGES` table whose primary key is `(subscriberId, messageID)`; a duplicate `INSERT` fails and the transaction rolls back, so the message is ignored. This is the standard way to get *effectively-once* behaviour from **at-least-once** delivery.[22]
3. **Exactly-once is a system-level effect, not a transport guarantee.** Stripe's idempotency design is the API-level proof: the server stores the first request's status code and body under the key and returns the same result to every retry - **including `500` errors**. Keys are client-generated (Stripe suggests UUIDv4-class entropy), up to 255 chars, and pruned after **at least 24 hours**; a key reused with *different* parameters errors.[17]
4. **Idempotency keys belong on mutating requests only.** Stripe accepts them on all `POST`s and states that sending them on `GET`/`DELETE` has no effect because those are idempotent by definition - a good rule for the execution API: `POST /signals`, `POST /orders/propose` take `Idempotency-Key`; reads do not.[17]
5. **Webhook/event ingress must be signature-verified against the raw body.** Stripe requires the *unmodified* raw request body plus the signature header plus the endpoint's signing secret; any framework rewrite of the body breaks verification, and handlers must return `2xx` before doing slow work.[18] This is directly reusable for a research-to-execution event drop where the sender may retry.
6. **Schema evolution should be additive-only under an explicit compatibility mode.** In Confluent Schema Registry terms, `BACKWARD` = new consumers can read old data, `FORWARD` = old consumers can read new data, `FULL` = both; the default is `BACKWARD`, and `*_TRANSITIVE` checks all prior versions rather than just the last one.[23]
7. **JSON Schema is the weakest of the three formats for compatibility** because it does not itself define compatibility; Confluent's rules differ by `compatibilityPolicy` (lenient/strict) and by content model (`additionalProperties: true` = open vs `false` = closed). For a **closed** model, backward-compatible changes are essentially only *removing optional fields* and *adding union/oneof variants* - which is why an open envelope plus explicit enum validation is the safer default for this system.[23]
8. **Unknown-field policy must be stated, not assumed.** Protobuf/Avro tolerate additive change via skip-unknown/defaults; a JSON consumer that uses `additionalProperties: false` will *break* on any additive field. Recommendation: envelope open (`additionalProperties: true`, ignore unknown), but **enums closed** - an unknown `decision` or `trade_permission` value must be **rejected, never coerced**.[23]
9. **Version negotiation must be per-request and fail-closed.** MCP's own versioning is the reference implementation: versions are `YYYY-MM-DD`, incremented **only on backwards-incompatible change**; each request carries the version it uses, the server accepts/rejects **each request independently**, and an unsupported version gets an explicit `UnsupportedProtocolVersionError` listing supported versions.[2][5]
10. **Consumer-side rejection semantics should be explicit and terminal.** MCP distinguishes *protocol errors* (malformed request, unknown tool - the caller cannot fix them) from *tool execution errors* (`isError: true`, actionable, retryable-with-changed-input), and tells clients to validate results before passing them on.[3][4] For the trading boundary: schema-invalid or expired decisions go to a dead-letter/quarantine path with a machine-readable reason; they are never partially applied.
11. **Servers must validate all tool inputs, enforce access control, rate-limit invocations, and sanitize outputs** - stated as `MUST` in the MCP tools spec, with clients `SHOULD` prompting for confirmation on sensitive operations and logging tool usage for audit.[3][4]
12. **Steady-state flow between sleeves should be artifacts, not RPC.** The existing `reports/<TICKER>_<ts>/research_decision.json` + `run_card.json` drop is already the right primitive: it is retryable, replayable, and gives a natural idempotency key (`run_id`), whereas a synchronous call couples the research LLM's latency and failure modes into the order path.

### B. Securing an API that can move money

13. **A shared secret must never be usable without a freshness proof.** AWS SigV4 is the reference recipe: canonical request = `METHOD` + newline + `CanonicalURI` + newline + `CanonicalQueryString` + newline + `CanonicalHeaders` + newline + `SignedHeaders` + newline + `HashedPayload`, then a *string to sign* containing algorithm, timestamp, credential scope, and the canonical-request hash; the signing key is derived by chained HMAC-SHA256, and the signature is recomputed by the server.[19]
14. **Replay protection = timestamp window + nonce cache + signature.** The timestamp bounds the replay to a small window (commonly ~5 min), the nonce is stored with a TTL equal to the window and accepted exactly once, and the nonce+timestamp must be **inside the signed material** or an attacker can rewrite them.[19][18]
15. **Constant-time comparison is mandatory**; use `hmac.compare_digest` (Python) so an attacker cannot recover the MAC byte-by-byte through timing.
16. **Do not invent authentication.** OWASP API2:2023 warns that OAuth is not authentication and neither are API keys, and that a service is vulnerable if other services can reach it unauthenticated, if tokens are weak/predictable, or if JWTs are accepted with `alg: none` or without expiry validation.[12]
17. **Client-credentials OAuth with short-lived tokens is the right shape for machine-to-machine**, and Alpaca itself demonstrates it: broker-partner tokens are valid for **15 minutes** (`expires_in: 899`), obtained via `grant_type=client_credentials` at the token endpoint, with the ability to use `private_key_jwt` (RFC 7523) so the private key never leaves custody. Alpaca explicitly says *do not* request a new token per API call, and that a **live key cannot be used against the paper API or vice versa**.[20]
18. **Alpaca's legacy scheme is a bearer-style key pair**: `APCA-API-KEY-ID` + `APCA-API-SECRET-KEY` headers (or HTTP Basic). There is **no per-endpoint scope**: possession of the pair is full trading authority on that account, which is precisely why the pair must be confined to the execution process.[20]
19. **IP allow-listing and TLS are necessary but not sufficient.** They reduce the blast radius of a leaked key and block opportunistic abuse, but they do not stop a compromised research host on the allow-list; the decisive control is that the research host holds a credential that **cannot trade** (a signed-request key the execution API maps to a *proposal-only* role).
20. **Rate limiting and abuse controls have a standard wire contract now.** `draft-ietf-httpapi-ratelimit-headers-11` defines `RateLimit-Policy` (quota policy) and `RateLimit` (quota currently available), aimed at letting clients throttle themselves; note the draft explicitly says these headers are **not** an authorization mechanism and do not imply an SLA.[28]
21. **Unbounded consumption is a money bug, not just a DoS bug.** OWASP API4:2023 lists missing execution timeouts, memory caps, process/fd caps, max upload size, **operations per request**, page-size limits, and **third-party spending limits** as the vulnerability class, and cites a real example where a missing cost allowance moved a monthly bill from **$13 to $8k**.[13] For a broker API that charges per call/asset, a per-day call budget is a hard requirement.
22. **Function-level authorization must deny by default.** OWASP API5:2023 requires deny-all enforcement with explicit grants per role/function, and warns against assuming an endpoint is administrative from its URL path.[14] The execution API should therefore have **separate route groups** (`/read/*`, `/propose/*`, `/execute/*`) with an explicit grant table, not a path-prefix heuristic.

### C. MCP as of 2026

23. **The current MCP revision is 2026-07-28**; version identifiers are `YYYY-MM-DD` and are incremented only for backwards-incompatible changes, with `Draft`/`Current`/`Final` states and a documented **12-month (or 90-day expedited) deprecation window**.[2]
24. **2026-07-28 is a structural change, not an incremental one.** Elicitation moved to **Multi Round-Trip Requests (MRTR)**: instead of a server-initiated `elicitation/create` on a long-lived stream, the server returns `resultType: "input_required"` / `InputRequiredResult`, and the client **retries the original call with `inputResponses`** (and a different JSON-RPC `id`). Tasks moved out of core into the `io.modelcontextprotocol/tasks` extension (`tasks/get`, `tasks/update`); Roots, Sampling and Logging are **deprecated**.[1][3]
25. **Transports are stdio and Streamable HTTP** (SSE-style streaming *inside* Streamable HTTP), replacing the old HTTP+SSE transport. Streamable HTTP has explicit security requirements: **validate `Origin` on every connection** (DNS-rebinding defence), bind local servers to **127.0.0.1** not `0.0.0.0`, and authenticate all connections.[5]
26. **Session semantics are a security surface.** A session ID returned in `Mcp-Session-Id` must be globally unique and cryptographically secure (e.g. UUID/JWT/hash), requests without it get `400`, requests with a dead one get `404` (client must re-initialize), and `DELETE` may be refused with `405`.[5]
27. **MCP authorization is OAuth 2.1 with strict audience rules.** The server is an **OAuth 2.1 resource server**; it **MUST** implement OAuth 2.0 Protected Resource Metadata (RFC 9728), clients **MUST** implement **Resource Indicators (RFC 8707)** and send the `resource` parameter in both authorization and token requests using the server's canonical URI, and the server **MUST validate that tokens were issued specifically for it**.[6][7][26][27]
28. **Token passthrough is explicitly forbidden.** MCP servers **MUST NOT accept or transit any tokens** not issued for them; the spec's stated risks are security-control circumvention, audit-trail corruption, trust-boundary violation, and data exfiltration via a stolen token used as a proxy.[6][8]
29. **The spec's security page enumerates the attacks you must design against**: confused deputy (static client ID + dynamic registration + consent cookie lets an attacker skip consent), token passthrough, SSRF during OAuth discovery (block private ranges `10/8`, `172.16/12`, `192.168/16`, `127/8`, `169.254/16`, `fc00::/7`, `fe80::/10`; require HTTPS; validate redirects; use an egress proxy), session hijacking (never authenticate *from* a session; bind sessions to identity as `<user_id>:<session_id>`), and local server compromise (consent dialogs must show the exact command; sandbox the server; use stdio or an authenticated IPC socket).[8]
30. **Tool safety is stated bluntly by the spec**: tools are **arbitrary code execution**, tool *descriptions/annotations are untrusted* unless from a trusted server, applications **SHOULD** always keep a human in the loop able to deny invocations, and clients **MUST** treat annotations as untrusted.[1][3]
31. **The tool primitive is: `name`, `title`, `description`, JSON-Schema `inputSchema`, optional `outputSchema`, and `annotations`.** Servers **MUST** return structured results conforming to `outputSchema` when declared, and clients **SHOULD** validate structured results against it - this is the hook that lets the execution layer *schema-check* everything the LLM asks for.[3][4]
32. **The four behavioural hints are `readOnlyHint` (default false), `destructiveHint` (default true, only meaningful when read-only is false), `idempotentHint` (default false), `openWorldHint` (default true).** They are *hints*, not enforcement; the server-side allow-list is what actually enforces least privilege.[9]
33. **Resources are application-controlled, prompts are user-controlled, tools are model-controlled** - three different trust levels that should map to three different approval policies in our stack: the LLM may *call* tools, but resources/context it reads are attacker-influenceable input.[38]
34. **The current spec recommends deterministic tool ordering, `tools/list` results that may vary by the authorization presented on the request, and per-request authorization** - credentials are per-request input, not connection state.[3]

### D. Designing a safe trading MCP surface, and what 2025-2026 incidents teach

35. **The GitHub MCP exploit (May 2025) is the archetype.** An attacker files a GitHub Issue on a *public* repo; the victim asks the agent to review issues; the injected text coerces the agent to read **private repositories** and exfiltrate content into a **public PR**. Invariant Labs stated this is **not a flaw in the GitHub MCP server code** but an architectural trust-model failure with no server-side patch.[33][34]
36. **Reported MCP CVEs confirm the pattern is in the plumbing, not the model**: `mcp-remote` **CVE-2025-6514** (CVSS 3.1 **9.6**, CWE-78 OS command injection via a crafted `authorization_endpoint` URL, fixed in **v0.1.16**)[31]; MCP Inspector **CVE-2025-49596** (CVSS 4.0 **9.4**, critical RCE because the Inspector proxy had **no authentication** between client and proxy, allowing unauthenticated stdio command launch, fixed in **0.14.1**)[32].
37. **The three incident classes map to three controls**: untrusted-content-to-tool-flow (GitHub) -> per-session dataflow/policy constraints and no cross-repo authority[33]; untrusted-server-driven client compromise (`mcp-remote`, Inspector) -> pin versions, never auto-install servers, authenticate the local proxy, bind to loopback[31][32]; tool-poisoning via metadata -> treat descriptions as untrusted and never let a tool description change policy.[1]
38. **"LLM proposes, deterministic gate disposes" is the OWASP-endorsed architecture.** LLM06:2025 (Excessive Agency) names the root causes as excessive functionality, excessive permissions, and excessive autonomy, and the mitigations are: minimise extensions, minimise extension functionality, avoid open-ended extensions ("run a shell command", "fetch a URL"), minimise permissions, execute in the user's context, require user approval for high-impact actions, and **complete mediation** - enforce authorization in the *downstream* system, never rely on the LLM to decide if an action is allowed.[16]
39. **Prompt injection cannot be "prompted away."** OWASP LLM01:2025 says fool-proof prevention is unclear, notes that RAG and fine-tuning do not fix it, and prescribes: constrain model behaviour, **define and validate expected output formats with deterministic code**, input/output filtering, least privilege with tokens handled *in code*, human approval for high-risk actions, and segregation of untrusted content.[15]
40. **The strongest structural defences are published and measurable.** CaMeL extracts control and data flow from the trusted query so untrusted data cannot alter program flow, and uses capabilities to block exfiltration; it solved **77% of AgentDojo tasks with provable security vs 84% undefended** - i.e. a real security gain at a measured utility cost.[35]
41. **The "lethal trifecta" is the risk test to apply before adding any tool**: private data access + exposure to untrusted content + ability to communicate externally. Any tool set that combines all three is exploitable, and Willison argues guardrail products advertising "95% of attacks" are a failing grade for security.[34]
42. **The design-patterns paper states the invariant**: "once an LLM agent has ingested untrusted input, it must be constrained so that it is impossible for that input to trigger any consequential actions."[36] Concretely: news/filing text may inform a *score*, but may never appear in a field that the order path consumes as an instruction or identifier.
43. **News/filings must be data, never instruction.** The order path should accept only typed fields (ticker, side, qty/weight, order type, limit, TIF) that are re-validated against the *deterministic* risk gate; free text is never forwarded, and any LLM-produced rationale lives in an audit-only column.
44. **Dual control is the last line**: for an action that can move money, require (a) an LLM proposal, (b) a deterministic gate verdict, and (c) a human approval token - three independent gates, no single agent able to complete the flow. This is the concrete reading of "require user approval" + "complete mediation" + "human in the loop".[16][1]

### E. Operational isolation

45. **Separate processes and separate environments are the baseline.** Each service gets its own repo, its own virtualenv/interpreter (`py -3.12`), its own pinned dependency set, and its own `.env`/credential file - and CI must build, lint, and test each repo independently so a green suite in one repo never implies anything about the other. This is the engineering implementation of the trust boundary above, and it is what makes the "no shared imports" rule enforceable by CI rather than by discipline.
46. **Pin and hash dependencies.** pip's hash-checking mode requires hashes for *all* requirements and *all* transitive dependencies and requires them to be pinned; `--require-hashes` is all-or-nothing, and `--only-binary :all:` removes source-distribution build execution. Recommended algorithm is SHA-256.[30]
47. **Produce an SBOM.** CISA frames the SBOM as a nested inventory of components, with **2026-updated minimum elements** and a parallel **AI SBOM minimum elements** guidance; treat SBOM generation as part of the release pipeline rather than a one-off.[29]
48. **Keep secrets out of the repo and out of the environment where possible.** Windows: DPAPI (`CurrentUser` scope) or Credential Manager for local secrets; Linux services: systemd credentials (`LoadCredentialEncrypted=` / `systemd-creds`, read from `$CREDENTIALS_DIRECTORY`) rather than `Environment=` lines in unit files. Never log secrets or interpolate them into errors.
49. **Rotate keys on a bounded cryptoperiod.** NIST SP 800-57 Part 1 Rev. 5 sets cryptoperiods by key type and risk (symmetric authentication/data-encryption keys commonly <= 2 years originator usage, with recipient usage bounded beyond that); a signed-request key for the boundary should be rotated well inside that - see the table.[37]
50. **Copy only the secrets each service needs.** The execution process holds the broker key and the boundary signing key; the research process holds an LLM/vendor key and a boundary signing key that maps to a *proposal-only* role - and nothing else. This is "minimise extension permissions" applied to services.[16]

---

## Numbers to design against

| Parameter | Recommended value | Why / source |
|---|---|---|
| Contract versioning scheme | `schema_version` semver `MAJOR.MINOR.PATCH`; `MAJOR` bump = breaking; additive changes bump `MINOR` only | Mirrors MCP's date-versioning rule of incrementing only on breaking change [2] |
| Compatibility mode | `BACKWARD_TRANSITIVE` (new consumer reads every prior producer) | Confluent default is BACKWARD; transitive covers replayed historical artifacts [23] |
| Envelope unknown fields | Ignore (`additionalProperties: true`) | Additive-only evolution requires tolerance [23] |
| Enums | Closed; unknown value = reject, never coerce | Backward-compat in a closed model is narrow; fail-closed for `decision`/`trade_permission` [23] |
| Rejected-message handling | Dead-letter path + reason code; no partial apply | Protocol vs execution error distinction in MCP tools spec [3][4] |
| Idempotency key | UUIDv4; `Idempotency-Key` header; scope = producer role + endpoint + key | Stripe: high-entropy, <=255 chars [17] |
| Idempotency retention | >= 24 h (use 48 h to cover manual retries) | Stripe prunes at >= 24 h [17] |
| Retry policy | Exponential backoff, bounded attempts, same idempotency key each attempt | Outbox relay may publish more than once; consumer must dedupe [21][22] |
| Inbox dedupe key | `(subscriber_id, message_id)` primary key | Idempotent Consumer pattern [22] |
| Request signature algorithm | HMAC-SHA256 (do not use SHA-1/MD5) | SigV4 uses HMAC-SHA256; pip excludes weak hashes [19][30] |
| Canonical string | `METHOD` + `\n` + `PATH` + `\n` + `SHA256(body)` + `\n` + `X-Timestamp` + `\n` + `X-Nonce` + `\n` + `X-Key-Id` | SigV4 canonical request shape [19] |
| Signature headers | `X-Key-Id`, `X-Timestamp`, `X-Nonce`, `X-Signature` | Authenticated-header pattern [19] |
| Timestamp format | RFC 3339 UTC or Unix epoch seconds; clock sync via NTP | SigV4 uses ISO-8601 UTC [19] |
| Replay window | 300 s (+/-150 s skew tolerance) | Common ~5 min window [19] |
| Nonce | 128-bit random, hex; single-use; cache TTL 600 s (2x window) | Nonce must be inside signed material [19] |
| MAC comparison | Constant-time (`hmac.compare_digest`) | Timing side channel |
| Boundary auth token (OAuth path) | client_credentials; access token TTL <= 900 s; never new token per call | Alpaca: `expires_in: 899` [20] |
| mTLS (optional second factor) | TLS client cert bound to the token (`cnf`/thumbprint) | RFC 8705 certificate-bound tokens [25] |
| TLS | 1.3; HTTPS everywhere; loopback exception only in dev | OAuth 2.1 / RFC 9700 [24] |
| Rate limit (read) | e.g. 60 req/min per key; return `RateLimit` + `RateLimit-Policy` | IETF ratelimit-headers draft [28] |
| Rate limit (mutating) | e.g. 10 req/min per key; separate, stricter bucket | OWASP API4 fine-tune per endpoint [13] |
| Broker call budget | Hard per-day cap + billing alerts | OWASP API4 spending-limit guidance; $13 -> $8k case [13] |
| Max payload | 1 MiB request, 64 KiB per field, bounded array lengths | OWASP API4 parameter limits [13] |
| Timeouts | Request 10 s; broker call 10 s; tool call 30 s | OWASP API4 execution timeouts; MCP client timeouts [13][3] |
| IP allow-list | Execution API ingress restricted to the research host + VPN; loopback-bound MCP for local use | MCP bind-to-127.0.0.1 [5] |
| Key rotation | Boundary signing keys <= 12 months; broker keys >= quarterly or on staff/tooling change | NIST SP 800-57 cryptoperiods (symmetric auth keys <= 2 y OUP) [37] |
| Key storage | Win: DPAPI CurrentUser or WinCred; Linux: systemd `LoadCredentialEncrypted`/`systemd-creds` | Never repo/.env/logs |
| Dependency installs | `--require-hashes` + `--only-binary :all:`; SHA-256 | pip hash-checking all-or-nothing [30] |
| SBOM | Generate per release; CycloneDX/SPDX; 2026 minimum elements | CISA SBOM programme [29] |
| MCP spec pin | `2026-07-28`; pin SDK versions; no auto-install of servers | Version negotiation + local-compromise mitigations [2][8] |
| MCP transport | stdio for local tools; Streamable HTTP with `Origin` validation + auth for remote | Transport security requirements [5] |
| Human approval TTL | e.g. 15 min; single-use approval token bound to the proposal hash | Elicitation/MRTR round-trips are per-request [1] |

## Contested or unproven

- **"Exactly-once delivery" as a transport property is not available.** The reliable claim (Stripe, microservices.io) is *effectively-once effects* via idempotency + dedupe; any vendor promising transport-level exactly-once for a cross-service boundary should be read as at-least-once plus dedupe.[17][21][22]
- **Prompt-injection defence efficacy numbers are preliminary.** CaMeL's 77% vs 84% AgentDojo result is a single benchmark with a specific task distribution; it is evidence that structural defences are viable, not a guarantee of production safety. OWASP LLM01 still says fool-proof prevention is unknown.[35][15]
- **Tool annotations are advisory.** `readOnlyHint` etc. are hints and are explicitly untrusted per the spec; any system that relies on them for safety without a server-side allow-list is unsafe.[9][1]
- **The exact wording of the MCP security page moves between revisions.** The security guidance I cite ([8]) is served on the `2025-11-25` docs path while the current spec is `2026-07-28`; the normative authorization requirements in [7] are the current ones, but the attack catalogue's URL/version can shift, so re-pin the citation at implementation time.[2][8]
- **The 24-hour idempotency retention is Stripe-specific**, not a law; 24 h is a floor chosen for safety, and a trading day plus overnight open/close boundaries should drive the real value (I would use 48-72 h).
- **Vendor claims of "guardrails catch 95% of attacks" are contested** by design: in security a 5% miss rate on an order path is not acceptable, and multiple 2025 disclosures (GitHub MCP, M365 Copilot, GitLab Duo) defeated deployed defences.[34][33]
- **Whether the two sleeves should share one broker account is an open design question.** Alpaca credentials are account-wide with no per-strategy scoping [20]; if both sleeves share one account, the *only* separation is in the execution process's own bookkeeping, so allocation (70/30) must be enforced by the house gate and reconciled against broker positions, not by the broker.
- **I did not find a public, peer-reviewed study isolating the security ROI of MCP-specific allow-lists**; the strongest evidence is incident post-mortems and the spec's own normative requirements, which is why I marked the incident-based findings as REPORTED rather than PROVEN.[8][33]

## Implications for our system

1. **Make the boundary artifact-first, API-second.** Keep `reports/<TICKER>_<ts>/*.json` as the durable hand-off and add a thin, signed HTTPS control API for *status/queries/proposals*; the execution daemon should never import anything from `TradingAgents` and CI should fail if a cross-repo import appears.
2. **Add an inbox table to `signald`.** `PROCESSED_MESSAGES(producer_id, message_id)` as the primary key, written in the same transaction that records the signal; this is the single change that makes redelivery and restart safe.[22]
3. **Wrap the existing decision in a versioned envelope** (`contract`, `schema_version`, `idempotency_key`, `produced_at`, `producer{service,git_sha,run_id}`, `expires_at`) and validate it on ingest against a JSON Schema; reject rather than coerce, and route rejects to a quarantine directory with a machine-readable reason.
4. **Split the risk verdict into two orthogonal fields** — `opportunity_score` (valuation/attractiveness, producer-owned) and `trade_permission` + `binding_gate` (house-risk-owned) — so "no BUY because unattractive" and "no BUY because risk forbids" can never be conflated. The execution layer should be the only component allowed to set `trade_permission`.
5. **Replace any shared secret in `.env` with a signed request scheme** (`X-Key-Id`/`X-Timestamp`/`X-Nonce`/`X-Signature`, HMAC-SHA256, 300 s window) and store keys in DPAPI/WinCred (Windows) or systemd credentials (Linux); the research/LLM process must receive a key whose role maps to *propose-only*.
6. **Never let the LLM tier hold Alpaca credentials.** The broker key pair lives only in the execution process's keystore; this is the single highest-leverage control given that Alpaca keys have no per-endpoint scope.[20]
7. **Expose MCP as read-only + propose-only.** `get_*` tools are read-only; `simulate_order` is side-effect-free; `propose_order` writes only a pending proposal; `submit_order`/`cancel_order` are **disabled by default** in the MCP surface and, when enabled, require a human approval token plus the deterministic gate verdict.
8. **Treat every tool description and every news/filing string as untrusted input.** Schema-validate tool I/O against `outputSchema` and never pass free text into an order field; keep LLM rationale in an audit-only field.[3][15][36]
9. **Add a hash-chained audit record per tool call and per API request** (fields in the boundary spec below) and make the audit ledger the system of record for "who proposed, who approved, what gate blocked".
10. **Adopt the lethal-trifecta test as a gate on new tools**: before adding any MCP tool, check whether the tool set as a whole gives the agent (a) private data, (b) untrusted content, and (c) external communication; if all three, redesign rather than add guardrails.[34]

---

## Boundary specification (copyable)

Everything here is traceable to the official sources cited in brackets; treat the bracketed refs as the normative anchors, not the prose.

### 1. The two services

| | Service A: `tradingagents` (research / LLM) | Service B: `TradingExecution` (order path) |
|---|---|---|
| Owns | LLM calls, vendor data keys, `reports/**` artifacts | Broker credentials, risk gate, signals, ledger |
| Talks via | artifact drop (write), signed HTTPS `GET/POST /v1/...` (read/propose) | artifact read + signed HTTPS API |
| Credential it holds | LLM/vendor keys + boundary key with role `propose` | Broker key pair + boundary key with role `execute` |
| Must never hold | broker key pair [20] | LLM/vendor API keys |

No shared Python package. CI in each repo runs lint/tests independently; a cross-repo import is a build failure.

### 2. Contract envelope (both directions)

Producer -> execution: `ResearchDecisionV1` (derived from the existing `research_decision.json` + `run_card.json`).
Execution -> producer/UI: `ExecutionSignalV1`.

```json
{
  "contract": "research_decision",
  "schema_version": "1.0.0",
  "idempotency_key": "<uuidv4>",
  "produced_at": "2026-09-12T13:45:02Z",
  "expires_at": "2026-09-12T20:00:00Z",
  "producer": { "service": "tradingagents", "git_sha": "<sha>", "run_id": "<uuidv4>" },
  "subject": { "ticker": "AAPL", "as_of": "2026-09-12", "horizon_days": 10 },
  "decision": "BUY|HOLD|SELL|AVOID",
  "opportunity_score": 0.0,
  "confidence": 0.0,
  "target_weight": 0.0,
  "rationale_ref": "reports/AAPL_20260912T134502/research_decision.json",
  "trace": { "artifact_sha256": "<hex>" }
}
```

Rules:
- **Versioning**: `schema_version` is semver. `MAJOR` = breaking; `MINOR` = additive; `PATCH` = docs/fix. Consumers must accept any `MINOR`/`PATCH` within a supported `MAJOR`, and must **reject unknown `MAJOR`** with a machine-readable error listing supported majors (MCP's `UnsupportedProtocolVersionError` shape) [2].
- **Unknown fields**: ignored at the envelope level (`additionalProperties: true`) [23].
- **Enums closed**: unknown `decision` -> reject, never coerce [23].
- **Additive-only** within a major: new optional fields only; never repurpose or retype an existing field; never make an existing field required [23].
- **Rejection semantics**: schema-invalid, expired (`expires_at < now`), signature-invalid, or duplicate-with-different-body -> `4xx`/dead-letter with `{ "reason_code": "...", "detail": "..." }`. Never partially apply [3][4].
- **Expiry** is mandatory: an analysis produced before the market open must not be actionable after the close.

### 3. Auth recipe for the execution API

Request signing (all non-GET and all mutating endpoints; recommended for GET too):

```
CanonicalString =
  METHOD            + "\n" +
  PATH              + "\n" +
  SHA256_HEX(body)  + "\n" +
  X-Timestamp       + "\n" +
  X-Nonce           + "\n" +
  X-Key-Id

X-Signature = HEX( HMAC-SHA256( secret, CanonicalString ) )
```

| Item | Value |
|---|---|
| Headers | `X-Key-Id`, `X-Timestamp`, `X-Nonce`, `X-Signature` |
| Algorithm | HMAC-SHA256, hex-encoded, constant-time verify (`hmac.compare_digest`) |
| Timestamp | RFC 3339 UTC (or epoch seconds); NTP-synced host clock |
| Replay window | 300 s (reject outside `[-150 s, +150 s]`); **nonce must be inside the signed string** |
| Nonce store | single-use, TTL 600 s, keyed `(key_id, nonce)` |
| Verify order | timestamp window -> nonce unused -> HMAC equal -> replay nonce as used (in one transaction) |
| Failure response | generic `401` (do not disclose which check failed) |
| Path binding | signature covers `PATH`; body signature covers `SHA256(body)` so a valid MAC cannot be replayed onto another request |

Basis: AWS SigV4 canonical-request + string-to-sign structure and key derivation [19]; Stripe's raw-body signature verification rule [18].

Authorization:
- Even on success, the **role attached to `X-Key-Id` is checked** (deny-by-default; explicit grants per route group `/v1/read`, `/v1/propose`, `/v1/execute`) [14].
- For machine-to-machine where OAuth is used instead, use `grant_type=client_credentials`, access-token TTL <= 900 s, never one token per call, and prefer `private_key_jwt` so the signing key never leaves custody [20]; validate audience per RFC 8707 if tokens flow through MCP [26].
- Optional second factor: mTLS client certificate bound to the token (RFC 8705 `cnf`/thumbprint), rejecting token theft without the private key [25].
- OAuth/redirect flows must follow RFC 9700 (exact redirect-URI match, PKCE, no token in query string) [24].

Key storage and rotation:
- Windows: DPAPI `CurrentUser` scope, or Windows Credential Manager; Linux: systemd `LoadCredentialEncrypted=` / `systemd-creds` read from `$CREDENTIALS_DIRECTORY`. Never in git, never in a checked-in `.env`, never logged.
- Rotation: boundary signing keys every <= 12 months (well inside NIST's <= 2-year cryptoperiod for symmetric authentication keys [37]); two keys valid during overlap; `X-Key-Id` makes rotation a config change, not a redeploy.
- Broker keys: rotate at least quarterly and immediately after any staff/tooling change; Alpaca keys are account-wide with no scoping, so rotation is the only containment [20].

Abuse controls:
- Per-key rate limits, with mutating endpoints on a stricter bucket; advertise `RateLimit` / `RateLimit-Policy` [28].
- Hard timeouts and payload caps (see table) [13].
- Hard per-day broker-call budget and billing alerts [13].
- IP allow-list on `/v1/execute` only (prefer not to allow-list `/v1/read` from dynamic research hosts).

### 4. MCP tool surface (least-privilege verbs)

Exposed over MCP only after auth (OAuth 2.1 resource server; `resource` indicator = the server's canonical URI; token audience validated; **no token passthrough**) [7][8]. Tool names are verbs; descriptions are treated as untrusted by clients [1][3].

| Tool | Class | `readOnlyHint` | `destructiveHint` | `idempotentHint` | `openWorldHint` | Approval required |
|---|---|---|---|---|---|---|
| `get_account_state` | read | true | n/a | true | false | no |
| `get_positions` | read | true | n/a | true | false | no |
| `get_open_orders` | read | true | n/a | true | false | no |
| `get_signal_latest` | read | true | n/a | true | false | no |
| `get_risk_state` | read | true | n/a | true | false | no |
| `get_sleeve_allocation` | read | true | n/a | true | false | no |
| `simulate_order` | dry-run (no side effects) | true | false | true | false | no |
| `propose_order` | write-intent (pending proposal only) | false | false | false | false | no (creates proposal; cannot fill) |
| `submit_order` | mutating | false | true | false | true | **yes** + deterministic gate verdict |
| `cancel_order` | mutating | false | true | true | true | **yes** |
| `halt_trading` | safety / kill-switch | false | false | true | false | yes (but always permitted) |
| `set_sleeve_allocation` | config mutation | false | true | true | false | **yes** + human |

Rules that make the table real:
- **Read-only by default**: only the `get_*` and `simulate_*` tools are enabled on a fresh server; the mutating tools are behind an explicit config flag, off in the shipped default [16].
- **`simulate_order` is mandatory** and must return the full gate evaluation (verdict, binding gate, projected post-trade CVaR/drawdown/weights) without touching the broker — it is the LLM's only way to "test" an idea [16][36].
- **No credentials in tool arguments**: tools accept only domain data; the broker key is read from the server's keystore inside the server process. The current spec's warning against marking sensitive parameters with `x-mcp-header` reinforces that secrets must not transit as parameters at all [3].
- **Two-phase mutate**: `propose_order` writes an immutable proposal row keyed by `proposal_id` and payload hash; `submit_order(proposal_id, approval_id)` must match the hash — an LLM cannot mutate a proposal after approval. This is the dual-control construction [16][1].
- **Per-tool allow-list + scopes**: `tools/list` may return only the tools the caller's scopes permit (credentials are per-request input, not connection state) [3].
- **Deterministic gate disposes**: `submit_order` only proceeds if the house risk gate returns `ALLOW`; the gate reads the same risk machinery (CVaR, drawdown, correlation stress, vol regime, knife guard, sizing, market regime) that governs the Value-Dip sleeve, so both sleeves share one verdict source.
- **Server hardening**: bind local servers to `127.0.0.1`; validate `Origin`; authenticate every connection; use stdio or an authenticated IPC socket for local tools; pin SDK and tool versions (CVE-2025-6514 and CVE-2025-49596 were both "install/run an untrusted component" failures) [5][8][31][32].
- **Lethal-trifecta check** before adding any tool: if the set combines private data + untrusted content + external communication, do not ship it [34].

### 5. Audit fields (every tool call and every boundary request)

Hash-chained ledger row (append-only; `hash = SHA256(prev_hash + canonical(row_without_hash))`):

| Field | Type | Meaning |
|---|---|---|
| `event_id` | uuid4 | primary key |
| `ts_utc` | RFC 3339 | server clock, UTC |
| `channel` | enum | `file_drop` \| `http_api` \| `mcp_tool` \| `webhook` |
| `actor_id` | string | `X-Key-Id` / OAuth sub / `human:<name>` |
| `actor_role` | enum | `propose` \| `execute` \| `approve` \| `read` |
| `action` | string | endpoint or tool name |
| `args_hash` | sha256 hex | hash of canonical args (never raw secrets) |
| `idempotency_key` | uuid4? | as supplied |
| `signature_verified` | bool | request-signature outcome |
| `policy_id` | string | allow-list / grant rule applied |
| `gate_verdict` | enum? | `ALLOW` \| `BLOCKED` (mutating tools only) |
| `binding_gate` | enum? | `cvar` \| `drawdown` \| `correlation` \| `vol_regime` \| `knife_guard` \| `sizing` \| `market_regime` \| `sleeve_capital` |
| `approval_id` | uuid4? | human approval token, single-use, bound to `args_hash` |
| `outcome` | enum | `applied` \| `rejected` \| `error` \| `deduped` |
| `reason_code` | string? | machine-readable on non-apply |
| `broker_order_id` | string? | only when a real order was sent |
| `prev_hash` / `hash` | hex | hash chain |

Minimum requirements: log **who proposed, who approved, which gate bound, and whether a dedupe occurred**; keep the LLM's free-text rationale in a separate, audit-only field that the order path never reads [3][4][16].

---

## Sources

1. Model Context Protocol — Specification (current revision 2026-07-28) — https://modelcontextprotocol.io/specification/2026-07-28 (accessed 2026-09-12)
2. MCP — Versioning (2026-07-28 docs) — https://modelcontextprotocol.io/docs/2026-07-28/learn/versioning (accessed 2026-09-12)
3. MCP — Server Features: Tools (spec 2026-07-28) — https://modelcontextprotocol.io/specification/2026-07-28/server/tools (accessed 2026-09-12)
4. MCP — Server Features: Tools (spec 2025-06-18) — https://modelcontextprotocol.io/specification/2025-06-18/server/tools (accessed 2026-09-12)
5. MCP — Base Protocol: Transports (spec 2025-06-18) — https://modelcontextprotocol.io/specification/2025-06-18/basic/transports (accessed 2026-09-12)
6. MCP — Authorization (spec 2025-06-18) — https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization (accessed 2026-09-12)
7. MCP — Authorization (spec 2026-07-28) — https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization (accessed 2026-09-12)
8. MCP — Security Best Practices (docs) — https://modelcontextprotocol.io/docs/2025-11-25/tutorials/security/security_best_practices (accessed 2026-09-12)
9. MCP — schema.ts, `ToolAnnotations` (`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`) — https://github.com/modelcontextprotocol/specification/blob/main/schema/2025-06-18/schema.ts (accessed 2026-09-12)
10. OWASP — API Security Top 10 – 2023 — https://owasp.org/API-Security/editions/2023/en/0x11-t10/ (2023)
11. OWASP — API1:2023 Broken Object Level Authorization — https://owasp.org/API-Security/editions/2023/en/0xa1-broken-object-level-authorization/ (2023)
12. OWASP — API2:2023 Broken Authentication — https://owasp.org/API-Security/editions/2023/en/0xa2-broken-authentication/ (2023)
13. OWASP — API4:2023 Unrestricted Resource Consumption — https://owasp.org/API-Security/editions/2023/en/0xa4-unrestricted-resource-consumption/ (2023)
14. OWASP — API5:2023 Broken Function Level Authorization — https://owasp.org/API-Security/editions/2023/en/0xa5-broken-function-level-authorization/ (2023)
15. OWASP GenAI — LLM01:2025 Prompt Injection — https://genai.owasp.org/llmrisk/llm01-prompt-injection/ (2025-04-17)
16. OWASP GenAI — LLM06:2025 Excessive Agency — https://genai.owasp.org/llmrisk/llm062025-excessive-agency/ (2025-05-05)
17. Stripe — Idempotent requests — https://docs.stripe.com/api/idempotent_requests (accessed 2026-09-12)
18. Stripe — Webhooks / signature verification — https://docs.stripe.com/webhooks (accessed 2026-09-12)
19. AWS — Create a signed AWS API request (SigV4) — https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_sigv-create-signed-request.html (accessed 2026-09-12)
20. Alpaca — Authentication — https://docs.alpaca.markets/us/docs/authentication (updated 2025-12-01)
21. Microservices.io (Chris Richardson) — Pattern: Transactional outbox — https://microservices.io/patterns/data/transactional-outbox.html (accessed 2026-09-12)
22. Microservices.io — Pattern: Idempotent Consumer — https://microservices.io/patterns/communication-style/idempotent-consumer.html (accessed 2026-09-12)
23. Confluent — Schema Evolution and Compatibility for Schema Registry — https://docs.confluent.io/platform/current/schema-registry/fundamentals/schema-evolution.html (accessed 2026-09-12)
24. RFC 9700 — Best Current Practice for OAuth 2.0 Security (BCP 240) — https://www.rfc-editor.org/rfc/rfc9700 (January 2025)
25. RFC 8705 — OAuth 2.0 Mutual-TLS Client Authentication and Certificate-Bound Access Tokens — https://www.rfc-editor.org/rfc/rfc8705 (February 2020)
26. RFC 8707 — Resource Indicators for OAuth 2.0 — https://www.rfc-editor.org/rfc/rfc8707 (February 2020)
27. RFC 9728 — OAuth 2.0 Protected Resource Metadata — https://datatracker.ietf.org/doc/html/rfc9728 (2025)
28. IETF — RateLimit header fields for HTTP, draft-ietf-httpapi-ratelimit-headers-11 — https://datatracker.ietf.org/doc/draft-ietf-httpapi-ratelimit-headers/ (2026-05-23)
29. CISA — Software Bill of Materials (SBOM) — https://www.cisa.gov/topics/information-communications-technology-supply-chain-security/sbom (accessed 2026-09-12)
30. pip — Secure installs (hash-checking mode) — https://pip.pypa.io/en/stable/topics/secure-installs/ (accessed 2026-09-12)
31. NVD — CVE-2025-6514 (`mcp-remote` OS command injection) — https://nvd.nist.gov/vuln/detail/CVE-2025-6514 (published 2025-07-09)
32. GitHub Security Advisory GHSA-7f8r-222p-6f5g / CVE-2025-49596 (MCP Inspector proxy RCE) — https://github.com/modelcontextprotocol/inspector/security/advisories/GHSA-7f8r-222p-6f5g (2025-06-13)
33. Invariant Labs — GitHub MCP Exploited: Accessing private repositories via MCP — https://invariantlabs.ai/blog/mcp-github-vulnerability (2025-05-26)
34. Simon Willison — The lethal trifecta for AI agents: private data, untrusted content, and external communication — https://simonwillison.net/2025/Jun/16/the-lethal-trifecta/ (2025-06-16)
35. Debenedetti et al. — Defeating Prompt Injections by Design (CaMeL), arXiv:2503.18813 — https://arxiv.org/abs/2503.18813 (2025-03-24)
36. Beurer-Kellner et al. — Design Patterns for Securing LLM Agents against Prompt Injections, arXiv:2506.08837 — https://arxiv.org/abs/2506.08837 (2025-06-10)
37. NIST — SP 800-57 Part 1 Rev. 5, Recommendation for Key Management — https://csrc.nist.gov/pubs/sp/800/57/pt1/r5/final (2020)
38. MCP — Understanding MCP servers (tools vs resources vs prompts) — https://modelcontextprotocol.io/docs/2026-07-28/learn/server-concepts (accessed 2026-09-12)
