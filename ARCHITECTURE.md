# L3A Architecture Record

Tài liệu này mô tả các quyết định có thể kiểm chứng. Trace chỉ chứa sự kiện và mã
quyết định quan sát được, không chứa prompt bí mật hoặc chain-of-thought.

## 1. System overview

```text
inputs/<case_id>.json
        |
   Coordinator ---- MCP tool discovery
        |
        +-- Order/item specialist --+
        +-- Payment specialist ------+-- Evidence ledger -- Verifier -- Output
        +-- Shipment specialist -----+          |              |
        +-- Policy specialist -------+          +---- trace ----+
```

Coordinator chỉ chuyển entity identifiers có trong case hoặc evidence đã được Gateway
trả về. Specialist không nhận quyền ghi trực tiếp output. Kết luận cuối cùng được tạo
bằng quy tắc xác định và phải qua verifier. Thông tin khách hàng chỉ là claim, không
phải ground truth.

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Tool/domain được phép | Output/handoff |
| --- | --- | --- | --- | --- |
| Coordinator | Case, tool schemas | Phân công, giữ correlation `case_id` | Không gọi data tool | Task envelope |
| Order/item | Order/item/seller/customer IDs | Trạng thái order, item, seller, customer history, giá và SLA gửi hàng | `order`, `item`, `product`, `seller`, `customer` | Evidence records |
| Payment | Order/payment refs | Thanh toán, duplicate, refund | `payment`, `refund` | Evidence records |
| Shipment | Order/shipment IDs | Mốc giao hàng và trách nhiệm logistics | `shipment` | Evidence records |
| Policy | Primary issue sơ bộ | Chính sách áp dụng cho issue | `policy` | Evidence records |
| Verifier | Draft output + ledger | Scope, provenance, totals và claim linkage | Không gọi tool | Verified output |

Tool được route từ metadata discovery. Response có domain ngoài quyền của specialist bị
từ chối, kể cả response đó có schema hợp lệ.

## 3. A2A protocol

Message envelope nội bộ gồm `case_id`, actor, target, decision code và thuộc tính đếm;
không truyền nội dung suy luận riêng. Mỗi specialist có đúng một lượt điều tra cho mỗi
case và kết thúc bằng `handoff`, nên không có vòng lặp. Thứ tự observable:

1. `case_received`;
2. `task_assigned`;
3. zero hoặc nhiều `tool_result_consumed`;
4. `handoff`;
5. `policy_decided` khi có policy evidence;
6. `verification_completed`;
7. `case_finalized`.

## 4. Evidence lifecycle

1. Gateway discovery cung cấp tên và JSON input schema; workflow không tự đoán tham số.
2. Mọi call truyền `case_id` từ case hiện tại. Data tool không có entity selector sẽ
   không được gọi để tránh bulk/cross-scope query.
3. Gateway validate toàn bộ envelope theo `mcp-evidence-response-v1`.
4. Ledger ghi nguyên `evidence_ref`, `result_hash`, domain và data. Ref không được sinh,
   chuẩn hóa hoặc sửa đổi phía client.
5. Ngay khi specialist thực sự đọc response, trace ghi `tool_result_consumed` với đúng
   actor, tool và ref.
6. Output chỉ lấy ref từ ledger hiện tại và chỉ chọn domain liên quan tới kết luận.
7. Verifier và submission validator kiểm tra output-to-trace linkage và cấm một ref xuất
   hiện ở nhiều case. Server audit vẫn là nguồn provenance độc lập cuối cùng.

## 5. Failure policy

| Failure | Retry? | Fallback | Trace/behavior |
| --- | --- | --- | --- |
| MCP timeout/transient error | Không tự retry trong một run | Bỏ tool, giảm evidence | Handoff ghi số failures |
| Not found | Không | Tiếp tục bằng evidence còn lại | Handoff ghi số failures |
| 401/403 hoặc scope violation | Không | Không tạo output | Fail run ngay lập tức |
| Source conflict | Không tự chọn dữ liệu customer | Ưu tiên authoritative MCP | `data_conflicts` khi rule nhận diện được |
| Invalid MCP envelope/domain | Không | Không tạo output | Contract/domain error |
| Invalid specialist result | Không | Không tạo output | Verifier error |

Không biến missing evidence thành dữ liệu phỏng đoán. Nếu không có evidence hợp lệ,
output là `insufficient_evidence`, confidence 0 và không có evidence ref giả.

## 6. Verification invariants

- `case_id` không đổi từ input đến mọi MCP call, trace và output;
- mọi output ref thuộc ledger của đúng case và đã có `tool_result_consumed`;
- claim refs là tập con của top-level refs;
- specialist chỉ tiêu thụ domain được cấp quyền;
- refund lines cộng đúng `recommended_refund_brl`;
- tiền tệ là `BRL`, confidence nằm trong `[0, 1]` và output pass public JSON Schema;
- trace có đủ lifecycle, đúng receive/finalize ordering và không dùng ref chéo case.

## 7. Reproducibility

- Python 3.11+, dependency ranges được khai báo trong `pyproject.toml`;
- workflow không dùng model, random seed hoặc clock để ra quyết định nghiệp vụ;
- xử lý case tuần tự theo thứ tự trong `case-set.json`;
- tool discovery được cache trong một MCP session;
- chạy: `day09 run`, kiểm tra: `pytest -q`, `ruff check src tests`, `day09 validate`;
- API key chỉ đọc từ `.env`, không ghi vào output, trace hoặc package.
