# L3A Architecture Record

Tài liệu này ghi lại các quyết định thiết kế kiến trúc có thể kiểm chứng của hệ thống Multi-Agent điều tra khiếu nại thương mại điện tử (K4 L3A).

---

## 1. System overview

Quy trình xử lý một case khiếu nại tuân theo mô hình **Hierarchical Specialist Pipeline có kiểm chứng (Audited Pipeline)** kết hợp **Verifier là Cổng Kiểm Tra Bất Biến Tất Định (Deterministic Invariants Gate)**:

```text
Runner (CLI): Discover MCP tools & Validate case set một lần
  │
  ├── Per Case: CLI phát event "case_received"
  │
  ├── [Coordinator Agent]
  │     • Đọc input thực tế: case_id, customer_request, claimed_order_id, claims, policy_version
  │     • Lập kế hoạch điều tra (Task Execution Plan)
  │     • Phát event "task_assigned" cho Order Specialist
  │
  ├── [Order & Entity Specialist]
  │     • Quyền MCP: get_order, get_order_items, get_product_context, get_customer_history
  │     • Xác minh Order ID trước: Nếu không tìm thấy, dừng các nhánh phụ thuộc order đó
  │     • Trích xuất seller_ids, item_ids, customer_id
  │     • Phát "tool_result_consumed" (khi dùng evidence) & "handoff"
  │
  ├── [Parallel Domain Specialists] (Chạy song song có kiểm soát)
  │     ├─ [Payment Specialist]
  │     │    • Quyền MCP: get_order_payments, get_payment_timeline, get_refund_timeline
  │     │    • Code Python + Decimal: Tính toán tổng tiền, đợt trả góp, duplicate charges, refund status
  │     │    • Phát "tool_result_consumed" & "handoff"
  │     │
  │     └─ [Shipment Specialist]
  │          • Quyền MCP: get_shipment_summary, get_sellers
  │          • Code Python: So khớp timeline (ngày mua, hạn giao của seller, ngày giao thực tế)
  │          • Phân định trách nhiệm vận chuyển (Seller trễ hạn chuẩn bị vs Đơn vị logistics giao trễ)
  │          • Phát "tool_result_consumed" & "handoff"
  │
  ├── [Policy Specialist]
  │     • Quyền MCP: get_policy
  │     • Chỉ tra cứu điều khoản policy tương ứng với policy_version
  │     • Tổng hợp findings để đánh giá claim, tính dòng hoàn tiền (refund_lines)
  │     • Phát "policy_decided" & "handoff"
  │
  ├── [Reconciliation & Mapper]
  │     • Chuyển đổi toàn bộ findings nội bộ thành cấu trúc L3A Schema hợp lệ
  │     • Lọc bỏ 100% các trường nội bộ không thuộc L3A
  │
  ├── [Verifier Agent - Deterministic Invariants Gate]
  │     • Kiểm tra bất biến bằng Code Python (Không dùng LLM bỏ phiếu dữ kiện)
  │     • Kiểm tra khớp số tiền Decimal, schema L3A, entity scope, consistency
  │     • Hiệu chuẩn Confidence linh hoạt dựa trên bằng chứng & xung đột
  │     • Tối đa 1 lượt bổ sung evidence có mục tiêu nếu thiếu bằng chứng
  │     • Phát event "verification_completed"
  │
  └── Trả Output JSON hợp lệ cho CLI
        CLI validate contract → Ghi outputs/<case_id>.json → CLI phát "case_finalized"
```

---

## 2. Agent ownership

Toàn bộ agent sử dụng chung một phiên làm việc MCP và team API key. Nguyên tắc đặc quyền tối thiểu (Least Privilege) được thực thi ở tầng ứng dụng thông qua wrapper `ScopedEvidenceGateway`:

| Actor | Input | Trách nhiệm | Output / Handoff | Công cụ MCP được cấp phép (App-level) |
| --- | --- | --- | --- | --- |
| **`coordinator`** | Case JSON (`case_id`, `customer_request`, `claimed_order_id`, `claims`, `policy_version`) | Đọc input thực tế, trích xuất mã đơn và danh sách khiếu nại, lập kế hoạch và phân bổ nhiệm vụ. | `InvestigationPlan` (task list, entity targets) | *Không gọi MCP trực tiếp* (Chỉ điều phối) |
| **`order_specialist`** | `case_id`, `claimed_order_id`, `customer_unique_id` | **Xác minh Order ID trước tiên**. Trích xuất `item_ids`, `seller_ids`, trạng thái đơn (`canceled`, `unavailable`, `delivered`,...). Nếu không tìm thấy order, báo thiếu dữ liệu đối soát, không truyền candidate giả định cho các agent sau. | `OrderFinding` (thông tin đơn, danh sách thực thể, `evidence_refs`) | `get_order`<br>`get_order_items`<br>`get_product_context`<br>`get_customer_history` |
| **`payment_specialist`** | `case_id`, `order_id` (đã xác minh) | Kiểm tra số tiền thực thu, hình thức thanh toán, số kỳ trả góp, phát hiện trừ trùng (`duplicate_charge`) và tiến độ hoàn tiền (`refund_pending`/`refund_failed`). **Dùng Python Decimal để tính tiền**. | `PaymentFinding` (payment_references, tổng tiền đã trả, trạng thái hoàn tiền, `evidence_refs`) | `get_order_payments`<br>`get_payment_timeline`<br>`get_refund_timeline` |
| **`shipment_specialist`** | `case_id`, `order_id` (đã xác minh) | So sánh các mốc thời gian: ngày mua, `shipping_limit_date` của seller, ngày giao vận nhận hàng, ngày giao khách. Xác định lỗi giao trễ do người bán hay do đơn vị vận chuyển (`logistics_provider`). | `ShipmentFinding` (shipment_ids, ngày giao trễ, bên chịu trách nhiệm vận chuyển, `evidence_refs`) | `get_shipment_summary`<br>`get_sellers` |
| **`policy_specialist`** | `case_id`, `policy_version`, tổng hợp findings | Tra cứu điều khoản chính sách sàn. Đánh giá tính hợp lệ của từng claim (`claim_assessments`), xác định điều kiện hoàn tiền và lý do hoàn tiền (`reason_code`). | `DraftResolution` (primary_issue, claim_assessments, refund_lines, ranked_causes) | `get_policy` |
| **`verifier`** | `DraftResolution`, Evidence Ledger | Cổng kiểm tra chất lượng tất định (Deterministic Quality Gate): kiểm tra schema L3A, quan hệ logic, khớp tổng tiền hoàn Decimal, hiệu chuẩn confidence động. | Output JSON chính thức và phát event `verification_completed` | *Không gọi MCP* (Chỉ thẩm tra chéo) |

---

## 3. A2A protocol

* **Message Envelope nội bộ**: Giao tiếp giữa các tác tử sử dụng Python dataclass có định danh `case_id`, `sender`, `recipient`, `payload`, `evidence_refs`, và `timestamp`.
* **Correlation theo `case_id`**: Mọi tin nhắn, task và trace event đều mang `case_id`, tuyệt đối không dùng chung trạng thái giữa các case.
* **Pipeline đơn hướng (Acyclic DAG)**: Luồng chuyển tiếp chỉ đi theo thứ tự:
  $$\text{Coordinator} \longrightarrow \text{Order Specialist} \longrightarrow \text{Parallel Specialists} \longrightarrow \text{Policy Specialist} \longrightarrow \text{Verifier}$$
  Không cho phép vòng lặp ngược lại giữa các agent, loại trừ triệt để nguy cơ infinite loop.
* **Tối đa 1 lượt bổ sung có mục tiêu (Targeted Query)**: Nếu Verifier phát hiện thiếu bằng chứng cốt lõi, chỉ cho phép thực hiện tối đa 1 truy vấn bổ sung xác định, sau đó chốt kết quả ngay.
* **Timeout & Handoff**: Timeout mỗi cuộc gọi MCP là 30.0s. Tổng thời gian xử lý 1 case không vượt quá 120s.
* **Quy chuẩn Trace Observable**:
  * `cli.py` đã phát `case_received` ở đầu và `case_finalized` ở cuối mỗi case.
  * Trong `solve_case`, chỉ phát các sự kiện vòng đời trung gian: `task_assigned`, `tool_result_consumed`, `handoff`, `policy_decided`, `verification_completed`.
  * Chỉ ghi các quyết định/sự kiện quan sát được (`decision_code`, `actor`, `target`, `evidence_refs`). Không đưa prompt hoặc chain-of-thought vào trace.

---

## 4. Evidence lifecycle

* **Validate & Ingest**: Mọi response từ MCP Gateway đều được kiểm tra tính hợp lệ qua `Contracts.validate_evidence()`.
* **Evidence Ledger cô lập theo Case**: Mỗi case khởi tạo một `EvidenceLedger` riêng biệt lưu trữ ánh xạ `evidence_ref` $\rightarrow$ payload. Tuyệt đối không tái sử dụng `evidence_ref` giữa các case.
* **Ghi nhận sử dụng bằng chứng (`tool_result_consumed`)**: Chỉ emit sự kiện khi Specialist thực sự tiêu thụ và sử dụng evidence đó trong finding/kết luận của mình, không emit chỉ vì vừa nhận HTTP response.
* **Evidence Mapping & Provenance**: Toàn bộ `evidence_ref` trong `claim_assessments` phải là tập con của `evidence_refs` cấp cao nhất của output, và toàn bộ đều phải xuất hiện trong Evidence Ledger của case đó.

---

## 5. Failure policy

Hệ thống phân tách rạch ròi 3 lớp lỗi: Lỗi LLM, Lỗi MCP Gateway, và Lỗi Thực Thể / Dữ liệu.

| Failure | Retry? | Fallback | Trace event / Decision code |
| --- | :---: | --- | --- |
| **MCP Timeout (>30s)** | Có (Tối đa **1 lần**, backoff 1.0s) | Nếu vẫn timeout: ghi nhận thiếu bằng chứng, dừng nhánh phụ thuộc. Tuyệt đối không tự suy đoán dữ liệu. | `handoff` hoặc `policy_decided`<br>`decision_code="MCP_TIMEOUT_RECOVERED"` |
| **Entity Not Found (Order không tồn tại)** | Không | Phân biệt rõ: *Không tìm thấy* $\neq$ *Claim sai*. Kết luận `insufficient_evidence` hoặc `unsupported_claim` dựa trên bản chất yêu cầu của khách. | `policy_decided`<br>`decision_code="ORDER_NOT_FOUND"` |
| **Source Conflict (Khách khác hệ thống)** | Không | Chỉ ghi `data_conflicts` khi có 2 nguồn thực sự đối kháng. Chọn nguồn ưu tiên theo domain (hệ thống authoritative cho ngày giờ/tiền). | `policy_decided`<br>`decision_code="RESOLVE_DATA_CONFLICT"` |
| **LLM Call / JSON Parse Error** | Tự động qua `LLMClient` | Tự động chuyển cấp (Mô hình $\le 10\text{B}$ tham số): Primary (`gemini-1.5-flash-8b`) $\rightarrow$ Fallback 1 (`gemma-2-9b-it`) $\rightarrow$ Fallback 2 (`qwen2.5-7b-instruct`). Nếu cả 3 đều hỏng: fallback sang rule-based deterministic. | `handoff`<br>`decision_code="LLM_FALLBACK_APPLIED"` |

---

## 6. Verification invariants

Trước khi hàm `solve_case` hoàn tất và trả về kết quả, Verifier Agent (Code Python thuần) bắt buộc kiểm tra các điều kiện bất biến sau:

1. **Schema Compliance**: Output phải khớp 100% với `contracts/schemas/l3a-output-v2.schema.json`. Không chứa bất kỳ trường lạ nội bộ nào (`additionalProperties: false`).
2. **Entity Scope**: `order_ids`, `item_ids`, `seller_ids`, `payment_references`, `shipment_ids` phải có nguồn gốc từ MCP findings của case hiện tại.
3. **Evidence Ownership**: Toàn bộ `evidence_refs` trong output và trong `claim_assessments` phải tồn tại trong `EvidenceLedger` của case, không dùng evidence rỗng hoặc giả.
4. **Money Totals (Decimal Accuracy)**:
   * Tính toán bằng `decimal.Decimal`, làm tròn 2 chữ số thập phân (`ROUND_HALF_UP`).
   * $\sum (\text{refund\_lines}[i].\text{amount\_brl}) \equiv \text{recommended\_refund\_brl}$.
   * Nếu `case_status == "no_action"` $\implies \text{recommended\_refund\_brl} == 0$.
   * Nếu `recommended_refund_brl} > 0 \implies \text{case_status} == "action_required"$.
5. **Responsibility & Action Consistency**:
   * Nếu `primary_issue == "late_delivery_seller"` $\implies$ `responsible_parties` phải có `party_type == "seller"`.
   * Nếu `primary_issue == "late_delivery_logistics"` $\implies$ `responsible_parties` phải có `party_type == "logistics_provider"`.
   * Mảng `resolution_actions` không chứa phần tử trùng lặp (`uniqueItems: true`).
6. **Confidence Calibration**: Điểm `confidence` được tính toán động (không hard-code khoảng cố định) dựa trên độ bao phủ bằng chứng, mức độ xung đột dữ liệu và độ tin cậy của finding:
   $$\text{confidence} = \text{base\_confidence} \times (1 - 0.15 \times \text{has\_conflict}) \times \text{evidence\_completeness}$$

---

## 7. Reproducibility

* **Môi trường & Ngôn ngữ**: Python 3.12 / 3.11.
* **Quản lý Dependencies**: Pin chặt chẽ trong `pyproject.toml` (`google-genai>=1.0.0`, `openai>=1.0.0`, `httpx2>=2,<3`, `mcp>=2,<3`, `jsonschema>=4.25,<5`, `python-dotenv>=1.1,<2`).
* **Cấu hình Mô hình & Multi-Tier Resilience (Tuân thủ giới hạn $\le 10\text{B}$ tham số)**:
  * Primary Model: `gemini-1.5-flash-8b` (8B parameters, temperature: `0.1`, json_mode: `True`).
  * Fallback Model 1: `gemma-2-9b-it` (9B parameters) hoặc `llama-3.1-8b-instant` (8B parameters).
  * Fallback Model 2: `qwen2.5-7b-instruct` (7B parameters) hoặc `llama-3.2-3b-instruct` (3B parameters).
* **Giới hạn Tài nguyên**: Timeout MCP: `30.0s`, Timeout LLM: `60.0s`. Xử lý tuần tự từng case theo runner của CLI để đảm bảo thứ tự audit trail và kiểm soát rate limit.
* **Lệnh chạy chuẩn**:
  ```bash
  day09 validate-inputs
  day09 run
  day09 validate
  day09 package --output dist/submission.zip
  ```
