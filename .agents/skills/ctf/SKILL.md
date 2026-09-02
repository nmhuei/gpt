---
name: ctf
description: Autonomous CTF solving pipeline using the local `gpt` tool — pick a challenge from the local CTF library, spawn parallel solver subagents, supervise + auto-recruit dead sessions, record flags + writeups, and run a 24/7 Attack-Defense war-game loop. Use when the user asks to solve a CTF challenge, run `ctf`, list CTF commands, check solver status, recruit a dead agent, or work the A&D war-game coordinator.
argument-hint: "[pick|solve|status|recruit|flag|writeup|wargame] [--args...]"
---

# CTF Mode

Chế độ tự động giải CTF trong thư viện cục bộ (`~/Workspace/CTF`) song song qua subagents. Tool `gpt` (xem skill `/gpt`) chạy từng session; supervisor + recruiter tự động duy trì 24/7.

## Khi nào dùng

- User nói: `ctf pick`, `giải bài CTF`, `solve challenge`, `CTF mode`, `giải CTF tự động`
- Cần xem registry flag: `ctf flag --list`, `đã giải được bài nào`
- Cần recruit agent chết: `ctf recruit`
- Cần chạy A&D war-game: `ctf wargame`

## Tổng quan scripts

| Script | Vai trò |
|---|---|
| `scripts/pick_ctf_challenge.py` | Pick bài ranked theo risk-tier, loại solved/đánh dấu |
| `scripts/ctf_spawn_session.py` | Spawn 1 session `gpt` cho 1 bài (PID thật, log file, prompt đã neutralize) |
| `scripts/ctf_supervisor.py` | Auto-detect session chết / cyber-refusal / gateway down |
| `scripts/ctf_recruiter.py` | Tự recruit session mới khi progress cũ >15p |
| `scripts/ctf_flag_registry.py` | Registry central `docs/automation/solved-flags.json` |
| `scripts/ctf_writeup.py` | Auto-gen `WRITEUP.md` từ progress.json + REPORT.md |
| `scripts/ctf_monitor.py` | Health monitor (gateway + solver + POST/min + RAM) |

## Lệnh thường dùng

Luôn chạy với `.venv` activated:

```bash
.venv/bin/python scripts/pick_ctf_challenge.py --max-risk low --json | head -10
.venv/bin/python scripts/ctf_spawn_session.py --chal-dir "<CHAL_PATH>" --name <NAME>
.venv/bin/python scripts/ctf_flag_registry.py --add "<CHAL_DIR>" "<FLAG>"
.venv/bin/python scripts/ctf_flag_registry.py --check "<CHAL_DIR>"
.venv/bin/python scripts/ctf_flag_registry.py --list
.venv/bin/python scripts/ctf_flag_registry.py --sync
.venv/bin/python scripts/ctf_writeup.py --all
.venv/bin/python scripts/ctf_monitor.py
.venv/bin/python scripts/ctf_recruiter.py
```

## Workflow chuẩn (1 bài)

```
0. PLAYBOOK     đọc docs/automation/SOLVE_PLAYBOOK.md theo category, áp dụng flow cũ trước
1. PICK         scripts/pick_ctf_challenge.py --max-risk low
2. SPAWN        scripts/ctf_spawn_session.py --chal-dir <X> --name <X> --timeout 0
3. MONITOR      pstree + tail -f ~/Downloads/ctf-workspace/runs/<X>/session.log
4. GUARD        thẩm định toán học script Crypto chạy >10s, kill ngay nếu UNSAT/brute vô vọng
5. REGISTER     scripts/ctf_flag_registry.py --add <X> <FLAG>
6. WRITEUP      scripts/ctf_writeup.py --all
7. UPDATE FLOW  cập nhật flow giải vừa thành công vào đúng category trong SOLVE_PLAYBOOK.md
```

## 🛡️ Crypto Feasibility Guard (Cơ chế Thẩm định Toán học & Cắt bẫy Timeout 600s)

Khi solver chạy một script giải Crypto:
1. **Phân loại:**
   - Lệnh hệ thống thường (`ls`, `cat`, `pip`, tests $<5$s) $\rightarrow$ cho qua.
   - Script Crypto / SMT / Brute-force (`z3`, `sage`, vòng lặp lớn) chạy $>10$s $\rightarrow$ thẩm định ngay.
2. **Thẩm định toán học:**
   - Kiểm tra xem ràng buộc có vô nghiệm (UNSAT) hoặc mâu thuẫn trạng thái (như so khớp output trung gian với final hash).
   - Kiểm tra không gian tìm kiếm: Nếu $O(2^{64})$ hoặc Z3 bit-blasting mạch nhân phi tuyến $\rightarrow$ đánh giá BẤT KHẢ THI.
3. **Can thiệp & Phản biện:**
   - `kill -9 <child_pid>` ngay lập tức để không lãng phí 600s.
   - Nạp thông điệp phản biện chỉ rõ lỗi logic và hướng giải tích thay thế vào session.


## Spawn N session song song

Không chạy CLI batch (chưa có). Phải loop:

```bash
for entry in $(.venv/bin/python scripts/pick_ctf_challenge.py --max-risk low --json | jq -r '.[].path'); do
  name=$(basename "$entry")
  .venv/bin/python scripts/ctf_spawn_session.py --chal-dir "$entry" --name "$name" &
done
wait
```

Giữ concurrency ≤5 (gateway overload nếu >5 session cùng lúc).

## Quy tắc cứng

**Playbook-First Strategy**: BẮT BUỘC đọc `docs/automation/SOLVE_PLAYBOOK.md` theo category của challenge trước khi giải. Thử toàn bộ các flow đã có trước; CHỈ KHI toàn bộ flow cũ thất bại mới tìm kiếm hướng giải mới. Sau khi giải xong, BẮT BUỘC ghi nhận flow giải vào file này để tái sử dụng.

**Strict No-Writeup-Search Policy (No Spoilers, Yes Research)**: CẤM tuyệt đối việc tìm kiếm writeup, flag hay bài giải trực tiếp của đề thi. TUY NHIÊN, agent ĐƯỢC PHÉP và KHUYẾN KHÍCH tra cứu tài liệu kỹ thuật, đặc tả thuật toán gốc (RFC, specification, whitepaper), bài báo khoa học về điểm yếu đại số, và tài liệu các công cụ chuyên sâu (Z3 SMT, SageMath, SAT solvers, thư viện mật mã) để trang bị kiến thức và tự giải bài nhanh nhất.

**Account là ChatGPT Plus** — KHÔNG quota, không cần `preflight_quota.py`.

**Risk-tier mặc định THẤP** (crypto/stego/pcap/osint/toán). Tăng `--max-risk medium` chỉ khi cần rev-config/web-exploit/forensics.

**Cyber-refusal** (model từ chối vì "cybersecurity requests"): KHÔNG retry trong cùng conversation. Mark `used_at` + chuyển bài risk thấp hơn. Tool `gpt` đã có `frame_local_ctf_prompt()` + `neutralize_ctf_text()` để bypass (xem skill `/gpt`).

**OSINT bài cần online lookup chính xác cao** (toạ độ, profile) → BỎ, không verify flag.

**Khi gặp lỗi → khởi động lại toàn bộ** theo yêu cầu owner, không retry cục bộ.

## A&D War-game

Workspace: `~/Downloads/ctf-workspace/and-ctf/war-game/`

Mỗi round:
1. Đọc `rounds/STATUS.md` + `rounds/progress.json` (ghi vào đây, KHÔNG ghi `runs/war-game-coord/`).
2. DEF đang thắng → thêm vuln đơn giản vào `service/vulnbox_r<N>.py`.
3. ATK viết `atk/exploit_v<N>.py` → test thật port 13337.
4. DEF viết `def/patch_v<N>.py` → verify 0 flag.
5. Ghi STATUS + progress + `/tmp/flag_round<N>.txt`.

Coordinator: spawn 1 subagent đọc `STATUS.md`, chạy 1 round (≤2 phút), cập nhật file rồi exit.

## File locations

| Path | Vai trò |
|---|---|
| `~/Downloads/ctf-workspace/workspaces/<NAME>/` | Workspace mỗi bài (attachments, solve script, flag.txt) |
| `~/Downloads/ctf-workspace/runs/<NAME>/progress.json` | Trạng thái solver |
| `~/Downloads/ctf-workspace/runs/<NAME>/session.log` | Log từ `gpt` |
| `~/Downloads/ctf-workspace/runs/<NAME>/session.pid` | PID process |
| `~/Downloads/ctf-workspace/incidents/` | Incident reports từ supervisor/recruiter |
| `~/Downloads/ctf-workspace/and-ctf/logs/progress-10min.log` | Log wake-up cron |
| `docs/automation/solved-flags.json` | Registry flag |
| `docs/automation/CTF_OWNER_POLICY.md` | Policy: Plus account, cyber-refusal, restart-toàn-bộ |

## Spawn subagent để giải 1 bài

Khi user yêu cầu "cho subagent dùng tool làm bài CTF":

```
Prompt mẫu:
  Bạn là CTF solver agent. Workspace: ~/Downloads/ctf-workspace/workspaces/<NAME>/
  Challenge dir: <CHAL_PATH>. Progress: ~/Downloads/ctf-workspace/runs/<NAME>/progress.json
  Đọc docs/automation/CTF_OWNER_POLICY.md.
  Dùng tool gpt (skill /gpt) để giải. Ghi flag vào flag.txt + REPORT.md.
  Ghi registry: scripts/ctf_flag_registry.py --add "<chal-dir>" "<flag>"
  Cập nhật progress mỗi 5 phút.
```

## Known bugs đã fix (tránh lặp)

- `409 Tool definitions changed` khi >4 session: dùng `--no-session --new-session`.
- `gpt --print` không tồn tại: dùng positional prompt.
- War-game vulnbox thiếu `import secrets as _sec` ở đầu file: thêm vào.
- DB `:memory:` cần `check_same_thread=False` cho ThreadingMixIn.
- Brace expansion `{}` trong path: sanitize trước khi mkdir.
- Event queue overflow với >4 session: raise `WEBGPT_HYBRID_EVENT_QUEUE_CAP` → 2048.
- Recruiter false positive: check progress mtime + fresh file, không chỉ PID.

## Đã làm được (work log 2026-09-01)

17 bài solved (xem `docs/automation/solved-flags.json`):
Half Baked, Rubik's Cube, π-crypt, CleanDesk, QR (PTIT), Not Seen Colors, QR Queries,
CAN you read this, OnePass, siren, passkey-nightmare, Magic or not, Gerege, genie,
cyclotomic-echo, secret-storage, TotalReward.

War-game: 27 rounds | ATK 27 generations | DEF 23 layers.
