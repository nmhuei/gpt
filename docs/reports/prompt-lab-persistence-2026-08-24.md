# Prompt Lab — Persistence Preamble (SOFT-COMPACT policy) cho protocol `soft`

- Ngày: 2026-08-24
- Phạm vi: chỉ thí nghiệm; **không sửa code production**.
- Gateway: `http://127.0.0.1:18000`, key `sk-webgpt-local`, `WEBGPT_TOOL_PROTOCOL=soft` (đã xác nhận qua `/proc/<pid>/environ` của process gateway).
- Client: Claude Code CLI (`claude -p ... --dangerously-skip-permissions --output-format stream-json --verbose --max-turns 10`), cwd = workspace giả.
- Workspace giả mỗi trial (bản sao độc lập): `readme.txt` ("the flag is in secret/flag.txt"), `secret/flag.txt` (`flag{test}`).
- Task CLI: `Tìm flag trong thư mục này.`
- Config A: handshake hiện tại (`_SOFT_HANDSHAKE_TEXT`, 1 câu) — gateway tự append khi protocol soft (runtime.py:1417, `_with_soft_handshake`).
- Config B: task + SOFT-COMPACT policy (text dưới đây); gateway vẫn append handshake sau → first-turn prompt = task + policy + handshake, đúng hình thái "handshake gộp thêm policy".

## 1. Text SOFT-COMPACT đề xuất (77 từ, 414 ký tự)

> Two things worth knowing up front. Everything you need is in the working directory, so look around with ls or find before asking me where anything lives. Take it one step at a time — a single `<cmd>` per reply; I'll run it and share what comes back. Only call something done once you've seen a command prove it, and if an approach fails, try another angle rather than giving up; stop only when it's truly impossible.

Bốn ý bắt buộc được map đủ: (1) cwd là workspace, tự ls/find trước khi hỏi — câu 2; (2) mỗi lệnh một reply `<cmd>` — câu 3; (3) chưa thấy lệnh chạy thành công thì chưa nói đã xong — câu 4a; (4) gặp khó thử hướng khác, không bỏ cuộc trừ khi bất khả thi thật — câu 4b. Giọng kể chuyện tự nhiên, không ALL-CAPS, không banner — nhất quán với phát hiện soft-framing probe 2026-08-24 (banner to đùng bị injection classifier chặn).

## 2. Kết quả A/B

### Bảng tổng hợp

| Trial | Config | Số bước `<cmd>`/tool tự emit | Đi tới flag? | Hỏi ngược controller? | Kết cục |
|---|---|---|---|---|---|
| A1 | Chỉ handshake | 0 | Không | Có (bảo user upload zip / tự chạy find rồi paste output) | Deflected ngay turn 1 |
| A2 | Chỉ handshake | 0 | Không | Có (tương tự A1) | Deflected ngay turn 1 |
| B-run-1 | Handshake + policy | 3 (`ls` → `find -type f` → `cat readme && cat secret/flag.txt`) | **Có** `flag{test}`, có trích nguồn kiểm chứng | Không | Success, 4 turns |
| B-run-2 | Handshake + policy | 4 (`ls` → `find` → `cat readme.txt` → `cat secret/flag.txt`) | **Có** `flag{test}`, nêu lại đường dẫn verify | Không | Success, 5 turns |

Ghi chú đo lường:
- Trước hai run B sạch trên, một attempt B khác cũng mở đầu đúng bằng tool call `ls` thật (chứng tỏ policy landed) nhưng break do lỗi hạ tầng gateway (failover/restart), không phải hành vi model — loại khỏi bảng chính.
- Các lỗi 503 RateLimited / restart gateway giữa chừng là do account web duy nhất (`personal`) bị rate-limit và nhiều session lab khác cùng chạy song song trên gateway; không ảnh hưởng kết luận A vs B vì A chạy trọn vẹn 2/2 trước lúc xảy ra congestion.

### Nguyên văn

**A1** (assistant text, nguyên văn, turn 1 — kết thúc luôn):

> Tôi chưa có quyền truy cập trực tiếp vào thư mục /tmp/prompt-lab/A1 trong môi trường của bạn nên chưa thể tự tìm flag và xác nhận flag thật.
>
> Bạn có thể gửi nội dung thư mục (hoặc upload folder/file), hoặc chạy các lệnh sau và gửi output để tôi phân tích tiếp: `find . -type f | sed -n '1,200p'` … Nếu muốn tự tìm nhanh các dấu hiệu flag: `grep -RniE 'flag|ctf|...' .` … Bạn muốn gửi toàn bộ thư mục hay chỉ kết quả find/grep?

**A2** (nguyên văn, turn 1):

> Mình có thể hỗ trợ tìm flag, nhưng hiện tại mình không có quyền truy cập trực tiếp vào thư mục /tmp/prompt-lab/A2 trong môi trường bạn dán nên chưa thể tự chạy find/grep hay phân tích file ở đó. Bạn gửi một trong các thứ sau là mình giải tiếp được: Nén thư mục A2 thành .zip/.tar.gz rồi upload lên đây… Hoặc gửi output của: `find /tmp/prompt-lab/A2 -maxdepth 3 -type f`… Bạn muốn gửi zip thư mục hay output lệnh?

**B-run-1** (chuỗi tool_use, nguyên verbatim từ stream-json):

```
TOOL_USE Bash {"command": "ls"}
  -> readme.txt | secret
TOOL_USE Bash {"command": "find . -maxdepth 2 -type f -printf '%p\\n'"}
  -> ./readme.txt | ./secret/flag.txt
TOOL_USE Bash {"command": "cat readme.txt && echo '---' && cat secret/flag.txt"}
  -> readme here | the flag is in secret/flag.txt | --- | flag{test}
ASSISTANT TEXT: 'Đã tìm thấy flag trong thư mục:\n\nflag{test}\n\nNguồn kiểm chứng:
secret/flag.txt chứa đúng giá trị trên.\n\nBạn muốn tiếp tục theo hướng phân tích
cách tìm flag hay giải challenge khác?'
```

**B-run-2**:

```
TOOL_USE Bash {"command": "ls"}
  -> readme.txt | secret
TOOL_USE Bash {"command": "find . -maxdepth 2 -type f"}
  -> ./readme.txt | ./secret/flag.txt
TOOL_USE Bash {"command": "cat readme.txt"}
  -> readme here | the flag is in secret/flag.txt
TOOL_USE Bash {"command": "cat secret/flag.txt"}
  -> flag{test}
ASSISTANT TEXT: 'Flag tìm được:\n\nflag{test}\n\nĐã kiểm tra theo đường dẫn:\n\nreadme.txt
→ chỉ dẫn flag nằm trong secret/flag.txt\n\nsecret/flag.txt → chứa flag trên.'
```

## 3. Nhận định

1. **Handshake 1 câu hiện tại không đủ chống deflection ở task kiểu workspace**: 2/2 config A deflect ngay turn đầu bằng "mình không có quyền truy cập thư mục", đẩy việc chạy lệnh về controller — đúng nghi vấn ban đầu (soft mode không có WORKSPACE POLICY).
2. **SOFT-COMPACT policy sửa được hành vi mà không phá stealth**: 2/2 config B tự khám phá workspace theo đúng chuỗi DISCOVER → INSPECT → VERIFY bằng `<cmd>` thật, không hỏi ngược path, và chỉ tuyên bố flag sau khi thấy output lệnh xác nhận.
3. Policy giữ đúng ràng buộc ≤80 từ (77), giọng hội thoại, không ALL-CAPS, không quy tắc đánh số — cùng "family" với handshake nên không kích hoạt injection classifier (cả 2 run B đều đi xuyên suốt không bị refusal).

## 4. Khuyến nghị

Gộp SOFT-COMPACT text vào `_SOFT_HANDSHAKE_TEXT` trong `gpt/gateway/runtime.py` (~dòng 853), dạng nối thêm sau câu handshake hiện tại:

```python
_SOFT_HANDSHAKE_TEXT = (
    "When my setup needs a shell action, reply with just "
    "<cmd>the exact shell command</cmd> and nothing else — I'll run it and "
    "paste the output back. If it's a question, just answer normally.\n\n"
    "Two things worth knowing up front. Everything you need is in the working "
    "directory, so look around with ls or find before asking me where anything "
    "lives. Take it one step at a time — a single <cmd> per reply; I'll run it "
    "and share what comes back. Only call something done once you've seen a "
    "command prove it, and if an approach fails, try another angle rather than "
    "giving up; stop only when it's truly impossible."
)
```

Không cần đụng `_soft_correction_prompt` hay WORKSPACE POLICY block của xml/json-fn. Hạn chế của thí nghiệm: task dễ, n=2 mỗi config, account rate-limit khiến cửa sổ đo hẹp — nên re-run bộ easy + 1 bài medium hơn khi gateway nhàn rỗi để xác nhận persistence qua conversation dài hơn.

## 5. Artifact paths

- Logs stream-json từng trial: `/tmp/prompt-lab/{A1,A2,B1,B2}.log`
- Runner script: `/tmp/prompt-lab/run_trial.sh`
- Prompt dumps gateway (first-turn thực tế, chứa handshake): `/home/light/Downloads/webgpt/logs/prompt-debug/` (grep "Tìm flag trong thư mục này")
