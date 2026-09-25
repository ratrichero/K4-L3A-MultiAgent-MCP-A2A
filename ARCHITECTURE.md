# L3A Architecture Record

## 1. System overview

Hệ thống sử dụng Python async state-machine để xử lý từng khiếu nại.

```text
Input case
    ↓
Coordinator
    ↓
Order/Item ─ Payment ─ Shipment
    ↓
Policy Agent
    ↓
Verifier
    ↓
Output JSON + Trace JSONL
```

Quy trình:

1. Coordinator nhận case và xác định các nhóm dữ liệu cần kiểm tra.
2. Coordinator giao nhiệm vụ cho specialist agent.
3. Specialist gọi đúng MCP tool được cấp quyền.
4. Policy Agent đối chiếu dữ liệu với chính sách.
5. Verifier kiểm tra bằng chứng, số tiền, trách nhiệm và schema.
6. Hệ thống tạo output và trace.

Thông tin khách hàng cung cấp chỉ được xem là claim, không phải ground truth.

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Tool được phép dùng | Output/handoff |
| --- | --- | --- | --- | --- |
| Coordinator | Case đầu vào | Phân tích yêu cầu, lấy entity ID, giao nhiệm vụ | Không gọi MCP trực tiếp | Nhiệm vụ cho specialist |
| Order/Item Agent | Order ID, item ID | Kiểm tra trạng thái đơn, sản phẩm, người bán và lịch sử khách hàng | `get_order`, `get_order_items`, `get_product_context`, `get_sellers`, `get_customer_history` | Order evidence |
| Payment Agent | Order ID, payment reference | Kiểm tra thanh toán, giao dịch trùng và hoàn tiền | `get_order_payments`, `get_payment_timeline`, `get_refund_timeline` | Payment evidence |
| Shipment Agent | Order ID, shipment ID | Kiểm tra giao hàng, thời gian và trạng thái vận chuyển | `get_shipment_summary` | Shipment evidence |
| Policy Agent | Evidence đã thu thập | Đối chiếu sự việc với chính sách | `get_policy` | Policy decision |
| Verifier | Kết quả các agent | Kiểm tra tính nhất quán và quyết định cuối | Không gọi MCP | Validated output |

Mỗi agent chỉ được sử dụng tool thuộc trách nhiệm của mình.

## 3. A2A protocol

Message nội bộ giữa các agent có dạng:

```python
{
    "case_id": "L3A_CASE_001",
    "source": "coordinator",
    "target": "payment-agent",
    "task": "verify_payment",
    "entity_ids": [],
    "evidence_refs": [],
    "attempt": 1,
}
```

Quy tắc:

- Mọi message phải chứa đúng `case_id`.
- Không chuyển evidence giữa hai case khác nhau.
- Coordinator chỉ handoff tới agent cần thiết.
- Mỗi nhiệm vụ có tối đa 2 lần thử.
- Agent không được tự giao việc ngược lại vô hạn.
- Sau khi specialist hoàn thành, kết quả được trả về Coordinator.
- Verifier là bước cuối trước khi tạo output.

Trace chỉ ghi sự kiện quan sát được, không ghi prompt hoặc nội dung suy luận riêng.

## 4. Evidence lifecycle

1. Specialist gọi MCP với đúng `case_id`.
2. `EvidenceGateway` kiểm tra response theo `mcp-evidence-response-v1`.
3. Hệ thống giữ nguyên `evidence_ref` do Gateway trả về.
4. Evidence được lưu trong state riêng của case hiện tại.
5. Khi evidence được dùng, hệ thống ghi sự kiện `tool_result_consumed`.
6. Verifier liên kết evidence với claim và quyết định tương ứng.
7. Chỉ evidence thực sự hỗ trợ kết luận mới được đưa vào output.

Không tự tạo, sửa hoặc tái sử dụng `evidence_ref`.

## 5. Failure policy

| Failure | Retry | Fallback | Trace event / decision code |
| --- | --- | --- | --- |
| MCP timeout | Tối đa 2 lần | Chuyển sang thiếu bằng chứng | `handoff / MCP_RETRY_EXHAUSTED` |
| Không tìm thấy dữ liệu | Không retry nếu kết quả rõ ràng | `insufficient_evidence` | `handoff / EVIDENCE_NOT_FOUND` |
| Nguồn dữ liệu mâu thuẫn | Không tự chọn tùy ý | Ghi vào `data_conflicts` | `verification_completed / SOURCE_CONFLICT` |
| Specialist trả kết quả sai | Kiểm tra lại 1 lần | Từ chối kết quả lỗi | `verification_completed / INVALID_SPECIALIST_RESULT` |
| Policy không đủ | Không suy đoán | `needs_investigation` | `policy_decided / POLICY_INSUFFICIENT` |

Không chuyển missing evidence thành dữ liệu phỏng đoán.

## 6. Verification invariants

Trước khi finalize, Verifier kiểm tra:

- `case_id` của input, evidence, trace và output phải giống nhau.
- Mọi `evidence_ref` phải do MCP Gateway trả về.
- Evidence phải thuộc case hiện tại.
- Mỗi kết luận phải có evidence hỗ trợ.
- Các entity ID không được tự tạo.
- Tổng `refund_lines.amount_brl` phải khớp `recommended_refund_brl`.
- Tiền tệ phải là `BRL`.
- `confidence` phải nằm trong khoảng 0 đến 1.
- Trách nhiệm và `resolution_actions` phải nhất quán.
- Output không chứa field ngoài schema.
- Nếu thiếu bằng chứng, kết quả phải là `insufficient_evidence` hoặc `needs_investigation`.

`day09 validate` được dùng để kiểm tra JSON Schema lần cuối.

## 7. Reproducibility

- Python: 3.11 trở lên.
- Dependencies: quản lý trong `pyproject.toml`.
- Workflow: Python async state-machine.
- Concurrency ban đầu: xử lý case tuần tự để tránh trộn evidence.
- Random seed: không sử dụng cho baseline.
- API key chỉ lưu trong `.env`.
- Không ghi API key vào source, trace hoặc output.

Lệnh chạy:

```bash
python -m pytest -q
day09 mcp-tools
day09 run
day09 validate
day09 package --output dist/submission.zip
```