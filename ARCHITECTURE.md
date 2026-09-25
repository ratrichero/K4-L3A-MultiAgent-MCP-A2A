# L3A Architecture Record

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

Luồng từ `inputs/<case_id>.json` đến MCP, specialist, verifier, output và trace:

```text
inputs/<case_id>.json
        │
        ▼
   CLI (case loop) ── trace: case_received (coordinator)
        │
        ▼
   solve_case() ── Coordinator
        │  task_assigned / handoff
        ├──────────────┬──────────────┬──────────────┐
        ▼              ▼              ▼              ▼
   Order/Item     Payment        Shipment       Policy
   agent          agent          agent          agent
        │              │              │              │
        └────── MCP Evidence Gateway (allowlist theo actor) ──────┘
                        │
                        ▼  tool_result_consumed
                   Coordinator (aggregate)
                        │  handoff
                        ▼
                    Verifier
                        │  verification_completed
                        │  (+ policy_decided nếu có)
                        ▼
                   outputs/<case_id>.json
                        │
   CLI ── trace: case_finalized (coordinator)
                        │
                   traces/trace.jsonl
```

Một MCP session dùng chung cho cả run (`cli.py`); phân quyền tool được enforce **trong workflow** theo actor, không mở toàn bộ tool cho mọi agent.

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Output/handoff |
| --- | --- | --- | --- |
| Coordinator | `case` (message, ids, claims) | Parse case; lập kế hoạch specialist; gán task; gom kết quả; quyết định có cần thêm vòng; bàn giao verifier; ráp draft output | `task_assigned` → specialists; `handoff` → verifier (hoặc specialist tiếp theo) |
| Order/item | `case_id`, `order_id` / item ids từ case hoặc evidence trước | Lấy evidence order/item/product/seller liên quan đơn hàng; trích entity ids | `tool_result_consumed`; `handoff` → coordinator (kèm entity ids + evidence_refs) |
| Payment | `case_id`, payment refs / order_id | Lấy evidence payment/refund; đối chiếu số tiền, trạng thái thanh toán | `tool_result_consumed`; `handoff` → coordinator |
| Shipment | `case_id`, shipment/order ids | Lấy evidence shipment; timeline giao hàng, logistics | `tool_result_consumed`; `handoff` → coordinator |
| Policy | `case_id`, `primary_issue` / claim codes đã có | Lấy evidence policy áp dụng; hỗ trợ `policy_decided` | `tool_result_consumed`; `policy_decided`; `handoff` → coordinator |
| Verifier | Draft output + toàn bộ `evidence_refs` đã consume | Kiểm invariant (§6); hạ confidence / đánh dấu `insufficient_evidence` nếu thiếu; không gọi MCP trừ khi cần xác minh conflict đã ghi | `verification_completed`; trả output cuối cho coordinator finalize |

### Tool permissions (allowlist theo actor)

Phát hiện tool qua `gateway.list_tools()`; chỉ gọi tên nằm trong giao của discovery ∩ allowlist actor. Coordinator và Verifier **không** gọi MCP trong luồng happy-path.

| Actor | Domains MCP được phép | Tool pattern (ví dụ; lấy từ discovery) | Cấm |
| --- | --- | --- | --- |
| Coordinator | — | Không gọi MCP | Mọi tool |
| Order/item | `order`, `item`, `product`, `seller`, `customer` | `get_order`, tools item/product/seller/customer tương ứng | payment, shipment, refund, policy |
| Payment | `payment`, `refund` | tools payment/refund | order/item chi tiết (trừ khi cần `order_id` đã có sẵn từ handoff, không fetch lại order) |
| Shipment | `shipment` | tools shipment | payment, policy, order full-fetch không cần thiết |
| Policy | `policy` | tools policy | mọi domain giao dịch |
| Verifier | — (mặc định) | Không gọi MCP | Mọi tool; nếu bắt buộc re-check conflict thì chỉ tool đã dùng bởi specialist cùng case |

Nguyên tắc: least privilege — mỗi specialist chỉ domain mình sở hữu; không hard-code tên tool ngoài những gì discovery trả về.

## 3. A2A protocol

A2A được thể hiện bằng **observable trace events** (`TraceWriter.emit`), không dùng message bus riêng. Correlation luôn theo `case_id`.

### Message envelope (trace event)

| Field | Bắt buộc | Ý nghĩa |
| --- | --- | --- |
| `schema_version` | yes | `day09-trace-event-v1` |
| `event_id` | yes | `evt_...` unique |
| `case_id` | yes | Correlation toàn case |
| `event_type` | yes | Xem lifecycle bên dưới |
| `occurred_at` | yes | UTC ISO-8601 |
| `actor` | yes | Agent phát sự kiện |
| `target` | handoff / task_assigned | Agent nhận |
| `decision_code` | khi có quyết định | Mã quan sát được (không phải CoT) |
| `tool_name` / `evidence_refs` | khi consume tool | Liên kết provenance |
| `attributes` | tùy chọn | Metadata ngắn (vd. `hop`, `reason`) — không ghi prompt |

### Lifecycle handoff (thứ tự quan sát được)

Events bắt buộc cho điểm `workflow` (`scoring-policy-v2.json`):

`case_received` → `task_assigned` → (≥1) `handoff` → `verification_completed` → `case_finalized`

Luồng chi tiết trong một case:

1. **CLI** emit `case_received` (`actor=coordinator`).
2. Coordinator emit `task_assigned` (`target=<specialist>`, `decision_code` = lý do gán, vd. `NEED_ORDER_EVIDENCE`).
3. Specialist gọi MCP → validate evidence → emit `tool_result_consumed` (`tool_name`, `evidence_refs`).
4. Specialist emit `handoff` về `target=coordinator` (kèm refs / entity ids trong `attributes` hoặc state nội bộ; **không** dump raw CoT vào trace).
5. Coordinator có thể gán thêm specialist (lặp bước 2–4) hoặc `handoff` → `verifier`.
6. Policy agent (nếu chạy) emit `policy_decided` trước khi verifier xong.
7. Verifier emit `verification_completed` (`decision_code` = `PASS` / `FAIL_INVARIANT` / `DOWNGRADE_CONFIDENCE`).
8. **CLI** emit `case_finalized` sau khi ghi `outputs/<case_id>.json`.

### Điều kiện handoff

| Từ → Đến | Điều kiện |
| --- | --- |
| Coordinator → Specialist | Case còn thiếu evidence domain đó; hoặc claim/primary_issue gợi ý domain |
| Specialist → Coordinator | Tool success đã consume; hoặc not-found / exhausted retry (xem §5) với `decision_code` tương ứng |
| Coordinator → Verifier | Đủ evidence tối thiểu cho draft **hoặc** đã escalate `insufficient_evidence` / `needs_investigation` |
| Verifier → Coordinator | Verification xong (PASS hoặc đã chỉnh draft); không handoff vòng lại specialist trừ khi `decision_code=NEED_REFETCH` và còn ngân sách hop |

### Timeout và chống vòng lặp

| Cơ chế | Giá trị thiết kế |
| --- | --- |
| MCP HTTP timeout | `connect/write/pool=30s`, total `300s` (`mcp_gateway.connect_gateway`) |
| Max specialist hops / case | ≤ 8 `task_assigned` hoặc `handoff` nội bộ; vượt → dừng, draft với `insufficient_evidence` / `needs_investigation` |
| Max calls MCP / case | Giới hạn mềm theo efficiency audit (L3A trọng số 0); tránh gọi trùng cùng `(tool, args)` |
| Không vòng lặp | Cấm `A→B→A` cùng `decision_code` liên tiếp; mỗi cặp `(actor, target, decision_code)` chỉ một lần / case trừ khi `NEED_REFETCH` có `attributes.attempt` tăng |
| State | Orchestration state giữ trong process memory theo `case_id`; không tái dùng evidence/refs giữa các case |

## 4. Evidence lifecycle

1. Specialist chọn tool trong allowlist ∩ discovery; gọi `gateway.call(tool, case_id=..., **args)`.
2. Gateway: nếu `isError` → raise (xem retry §5); parse structured content; `Contracts.validate_evidence`.
3. Chỉ lưu `evidence_ref` / `data` / `domain` / `result_hash` từ response MCP — **không** tự tạo hoặc sửa `evidence_ref`.
4. Khi dùng evidence cho claim/output: emit `tool_result_consumed` với đúng `evidence_refs`.
5. Map refs vào `claim_assessments[].evidence_refs` và `evidence_refs` top-level của output; mọi ref trong output phải đã xuất hiện trong trace consume cùng `case_id`.
6. Evidence **không** tái sử dụng giữa case; không copy ref từ case khác.

## 5. Failure policy

Retry MCP chỉ áp dụng cho lỗi **thoáng qua** (timeout / transport / `isError` tạm thời). Lỗi nghiệp vụ (not found, schema invalid) **không** retry vô hạn và **không** bịa dữ liệu.

| Failure | Retry? | Fallback | Trace event/code |
| --- | --- | --- | --- |
| MCP timeout / transport | Có — tối đa **3** lần, backoff `0.5s → 1s → 2s`, cùng args (idempotent) | Sau hết lần: handoff coordinator với thiếu evidence; `primary_issue` có thể `insufficient_evidence`, `case_status=needs_investigation` | `handoff` `decision_code=MCP_TIMEOUT_EXHAUSTED`; optional `attributes.attempts` |
| MCP `isError` (5xx / transient) | Có — cùng giới hạn 3 + backoff | Như trên | `MCP_ERROR_EXHAUSTED` |
| Not found / empty domain | Không retry (hoặc **1** lần nếu tool khác cùng domain trong allowlist) | Ghi nhận thiếu; không invent entity/money | `tool_result_consumed` bỏ qua; `handoff` `decision_code=EVIDENCE_NOT_FOUND` |
| Source conflict (hai evidence mâu thuẫn) | Không retry cùng call | Ghi `data_conflicts[]`; chọn `selected_source` theo policy ưu tiên (vd. payment > customer message); hạ confidence | `handoff` / `policy_decided` `decision_code=SOURCE_CONFLICT` |
| Invalid specialist result (schema / thiếu field bắt buộc) | Không gọi MCP lại vì “cho đủ” | Verifier reject hoặc sửa draft xuống `needs_investigation` / `insufficient_evidence` | `verification_completed` `decision_code=INVALID_SPECIALIST_RESULT` |
| Evidence schema / `ContractError` | Không retry response hỏng | Coi như call thất bại nghiệp vụ; không đưa ref vào output | `handoff` `decision_code=EVIDENCE_INVALID` |

Quy tắc chung:

- Retry có giới hạn và **idempotent** (cùng `tool_name` + arguments).
- Missing evidence ≠ dữ liệu phỏng đoán; customer message không phải ground truth.
- Không emit `tool_result_consumed` cho call thất bại.

## 6. Verification invariants

Trước `verification_completed` / finalize, Verifier kiểm:

1. **Schema** — output khớp `l3a-output-v2`; `case_id` khớp input.
2. **Entity scope** — mọi id trong `affected_entities` xuất phát từ evidence đã consume hoặc ids có trong case input; không bịa id.
3. **Evidence ownership** — mọi `evidence_refs` thuộc MCP audit cùng team/run/case; không cross-case.
4. **Claim linkage** — mỗi claim có verdict phải có `evidence_refs` đã `tool_result_consumed`.
5. **Money totals** — `recommended_refund_brl` = tổng `refund_lines`; currency `BRL`.
6. **Responsibility / action** — `responsible_parties` và `resolution_actions` nhất quán với `primary_issue` / `case_status` (vd. `no_action` không kèm refund dương).
7. **Confidence bounds** — `confidence ∈ [0, 1]`; thiếu evidence → không inflate confidence; ưu tiên hạ khi conflict hoặc not-found.

Fail invariant → chỉnh draft hoặc `decision_code=FAIL_INVARIANT` trước khi coordinator/CLI finalize.

## 7. Reproducibility

| Hạng mục | Giá trị |
| --- | --- |
| Runtime | Python ≥ 3.11; deps pin qua `pyproject.toml` / lock khi có |
| Entry | `day09 run` từ root repo (sau `validate-inputs`) |
| Config | `.env`: `COMPETITION_API_URL`, `COMPETITION_TEAM_API_KEY`, `MCP_ENDPOINT` — **không** commit key |
| Concurrency | Case chạy tuần tự trong CLI; một MCP session / run |
| MCP timeout | 300s total; 30s connect/write/pool |
| Retry | max 3, exponential backoff 0.5–2s (§5) |
| Hop limit | ≤ 8 specialist assignments / case |
| Random seed | Không dùng sampling ngẫu nhiên cho quyết định nghiệp vụ; nếu LLM có temperature, ghi rõ trong PR/submit notes (không ghi API key) |
| Artifacts | `outputs/<case_id>.json`, `traces/trace.jsonl`; package: `day09 package` |

## 8. Schema Validation

Hệ thống sử dụng các schema chuẩn để đảm bảo tính hợp lệ của dữ liệu trong toàn bộ pipeline:

1. **`l3a-output-v2.schema.json`:**
   - Được sử dụng để validate output của mỗi case (`outputs/<case_id>.json`).
   - Output phải bao gồm các trường bắt buộc như `case_id`, `status`, `evidence_refs`, `claim_assessments`, v.v.

2. **`trace-event-v1.schema.json`:**
   - Được sử dụng để validate trace log (`traces/trace.jsonl`).
   - Mỗi sự kiện trace phải bao gồm `schema_version`, `event_id`, `case_id`, `event_type`, `occurred_at`, `actor`, v.v.

3. **`submission-manifest-v2.schema.json`:**
   - Được sử dụng để validate manifest khi đóng gói nộp bài (`submission_manifest.json`).
   - Manifest phải liệt kê đầy đủ các file output và trace log.

4. **`mcp-evidence-response-v1.schema.json`:**
   - Được sử dụng để validate phản hồi từ MCP Gateway.
   - Envelope phải bao gồm các trường như `evidence_id`, `content`, `metadata`, v.v.

### Quy trình Validation
- Mọi dữ liệu đầu ra (output, trace log, manifest) và phản hồi từ MCP Gateway đều được validate trước khi sử dụng hoặc ghi file.
- Validation được thực hiện bằng thư viện `jsonschema` trong Python.