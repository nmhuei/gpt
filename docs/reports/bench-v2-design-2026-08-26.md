# Bench V2 Design — 2026-08-26

Trạng thái: DESIGN (chưa implement). Phạm vi: thiết kế E2E-BENCH v2 cho pipeline
Claude Code CLI → webgpt gateway, dựa trên harness v1 đã PASS
(docs/reports/e2e-bench-2026-08-25.md). Văn bản này đủ chi tiết để một agent
khác implement không phải đoán: layout, schema task.json, assertion, mutation,
turn budget, ma trận chống gian lận.

---

## 1. Tổng quan harness v1 (đã đọc, giữ nguyên làm nền)

V1 gồm 2 lớp:

- **Runner** `scripts/run_practical_bench.py`: với mỗi task trong
  `benchmarks/practical/tasks/<t>/` — copy `fixture/` → workspace mới trong
  `$WEBGPT_BENCH_RUN_ROOT/<ts>/<t>/`, chạy `claude -p <prompt từ task.json>
  --dangerously-skip-permissions --print --verbose --output-format stream-json`
  với `ANTHROPIC_BASE_URL` trỏ gateway, rồi gọi grader, ghi `results.json`.
  PASS-workspace xoá, FAIL giữ lại.
- **Grader** `benchmarks/practical/grade.py`, 5 pha, tất cả trong tmp copy:
  1a. baseline fixture + visible tests phải đúng kỳ vọng (đỏ với bugfix/feature,
      xanh với refactor);
  1b. baseline fixture + hidden grader suite phải ĐỎ;
  2. required_files hiện diện;
  3. workspace copy + inject hidden suite (`_grader/grader_check_<task>.py`)
     phải xanh toàn bộ, đạt `min_visible_tests_passed` /
     `min_grader_tests_passed`;
  4. structural_checks (regex trên source);
  5. mutation checks: pytest plugin tự sinh (`WB_MUTATION`) stomp attr mục tiêu
     trên mọi module của package trong bản COPY; suite phải FAIL trên mutant
     ("binds") — nếu vẫn pass thì suite là trivially-passing → overall FAIL.
- Task spec `task.json`: id/title/baselinepytest_expect/entry_package/
  required_files/min_visible_tests_passed/min_grader_tests_passed/
  structural_checks/mutations[{id,kind,attr}]/prompt.
- Selfcheck: `selfcheck/<task>-solved/` chấm phải PASS, pristine phải FAIL từng
  check đúng hướng (xem selfcheck.log).
- Benchmark "dựng project từ đầu" cũ: `scripts/e2e_project_benchmark.py`
  (assertion inline, sandbox snapshot chống escape cwd).

**Điểm yếu V2 cần khắc phục**: bài v1 đều là 1 package nhỏ 1–2 hàm, không có
bằng chứng quá trình (TDD/git/regression-red), không chống hardcode output, không
có bài đa module/tích hợp/log-forensics, mutation chỉ stomp function-level.

---

## 2. Nguyên tắc thiết kế V2

1. **Objective-only grading** — grader chỉ tin exit code, junitxml, AST, hash
   diff, byte-equality; không tin prose của model.
2. **Red→green bằng chứng cơ học** — mọi bài yêu cầu thay đổi hành vi phải có
   trạng thái đỏ đo được trước, xanh đo được sau (visible test, hidden suite,
   hoặc regression test do model viết nhưng bị ép chứng minh đỏ trên pristine).
3. **Mutation binding bắt buộc** — mọi bài ≥2 mutants; combined suite phải kill
   100%; riêng bài TDD/property thêm *test-strength check*: suite do model viết
   (không tính hidden) phải tự kill ≥K/M mutants.
4. **Chống hardcode** — corpus held-out sinh tại thời điểm chấm từ seed bí mật +
   reference implementation nhúng trong grader suite.
5. **Determinism** — clock/sleep/transport inject được; không dùng hypothesis
   (tránh phụ thuộc PyPI lúc chấm); generator property-test ship kèm fixture,
   seed cố định.
6. **Diff confinement** — hash-lock các vùng cấm (tests/, incident/, docs/),
   snapshot sandbox cha trước/sau để bắt escape cwd (tái dùng ý tưởng
   `detect_escaped_paths`).
7. **Resource discipline** — fixture <100KB, corpus sinh thủ tục, grader tmp qua
   `tempfile.TemporaryDirectory` (KB–MB là an toàn trên tmpfs); bench-run root
   như cũ (`~/.local/share/webgpt/bench-run`); KHÔNG đổ file lớn vào /tmp.

---

## 3. Layout thư mục V2

```
benchmarks/practical/
├── grade.py                        # mở rộng engine (mục 5), backward-compat v1
├── grader_suites/
│   ├── grader_check_bugfix.py      # v1, giữ nguyên
│   ├── ...
│   ├── grader_check_v2_bugfix_multi.py
│   ├── grader_check_v2_tdd_tokenbucket.py
│   ├── grader_check_v2_refactor_property.py
│   ├── grader_check_v2_log_debug.py        # chứa thêm reference replay harness
│   └── grader_check_v2_api_integration.py   # chứa thêm mock-server spawn helper
├── tasks/
│   ├── bugfix|feature|refactor/            # v1, giữ nguyên
│   ├── v2_bugfix_multi/
│   │   ├── fixture/orderkit/{__init__,models,pricing,ledger}.py
│   │   ├── fixture/tests/test_totals.py
│   │   └── task.json
│   ├── v2_tdd_tokenbucket/
│   │   ├── fixture/ratelimit/{__init__,counter,bucket}.py
│   │   ├── fixture/tests/test_counter_window.py
│   │   └── task.json                       # fixture SHIP git repo đã init + initial commit
│   ├── v2_refactor_property/
│   │   ├── fixture/csvnorm/{__init__,ingest,export,backfill}.py
│   │   ├── fixture/tools/gen_rows.py       # generator deterministic
│   │   ├── fixture/golden/{input.csv,expected.jsonl}
│   │   └── task.json
│   ├── v2_log_debug/
│   │   ├── fixture/urlwatch/{__init__,checker,alerts,state,transport}.py
│   │   ├── fixture/incident/{app.log,metrics.txt,timeline.md}
│   │   └── task.json
│   └── v2_api_integration/
│       ├── fixture/feedclient/{__init__,client,retry,pager,errors,cli}.py
│       ├── fixture/mockserver/{server.py,scenarios.py}
│       ├── fixture/docs/API.md
│       ├── fixture/tests/test_unit_errors.py
│       └── task.json
└── selfcheck/
    ├── v2_*-solved/…               # cây lời giải chuẩn để selfcheck PASS
    └── selfcheck-v2.log
```

Runner: đổi `TASKS` tuple thành glob động `sorted(p.parent.name for p in
TASKS_DIR.glob("*/task.json"))` để v1+v2 cùng chạy; per-task `timeout_s` đọc từ
task.json (override CLI default).

---

## 4. Format task.json V2 (schema mở rộng)

Giữ toàn bộ field v1, bổ sung:

| Field | Kiểu | Ý nghĩa / cách grade.py xử lý |
|---|---|---|
| `timeout_s` | int | claude timeout per-task (runner đọc, override --timeout) |
| `allowed_globs` | [str] | diff confinement: chỉ file khớp globs này được khác pristine (vd `orderkit/**`); ignore `__pycache__/,.pytest_cache/,.git/**` |
| `locked_paths` | [str] | ngược lại: đường dẫn phải byte-giống pristine (vd `tests/test_totals.py`, `incident/app.log`) |
| `model_regression_check` | {path, must_fail_on_fixture: true, min_asserts: int} | grader chạy file này trên WS copy (phải xanh) VÀ trên pristine fixture copy (phải ĐỎ, rc≠0); đếm `assert` ≥ min |
| `git_history` | {min_commits, require_ordered_commits: [{message_prefix, paths_glob}]} | parse `git log --format=%s --name-only`: số commit, và commit nào chỉ đụng glob nào theo thứ tự |
| `test_strength` | {suite_glob, mutants_ref: [mutation.id], min_kill} | chạy CHỈ suite khớp glob (khônginject hidden) trên từng mutant workspace copy; số mutant bị kill ≥ min_kill |
| `heldout_corpus` | {cmd, args, compare:"bytes"} | grader tự sinh input tại thời điểm chấm (seed bí mật), expected tính từ reference impl nhúng trong grader suite, so bytes với output CLI của ws |
| `integration` | {module, spawn_cmd, ready_path, wall_budget_s, cases_ref} | grader spawn subprocess mock server (port trắng), poll ready, chạy hidden suite tích hợp, tổng wall ≤ budget |
| `entry_cli` | [str] | lệnh CLI để grader chạy khi cần (vd `python -m csvnorm --in X --out Y`) |

Ví dụ đầy đủ (v2_tdd_tokenbucket):

```json
{
  "id": "v2_tdd_tokenbucket",
  "version": 2,
  "title": "TDD-implement TokenBucket in ratelimit.bucket per SPEC",
  "language": "en",
  "timeout_s": 1500,
  "baseline": {"pytest_expect": "fail", "grader_suite_expect": "fail"},
  "entry_package": "ratelimit",
  "required_files": ["ratelimit/bucket.py", "tests/test_token_bucket.py"],
  "min_visible_tests_passed": 10,
  "min_grader_tests_passed": 16,
  "allowed_globs": ["ratelimit/**", "tests/test_token_bucket.py"],
  "locked_paths": ["ratelimit/counter.py", "tests/test_counter_window.py",
                   "tests/test_counter_window.py"],
  "git_history": {
    "min_commits": 3,
    "require_ordered_commits": [
      {"message_prefix": "[tdd] tests", "paths_glob": "tests/**"},
      {"message_prefix": "[tdd] impl",  "paths_glob": "ratelimit/bucket.py"}
    ]
  },
  "test_strength": {
    "suite_glob": "tests/test_token_bucket.py",
    "mutants_ref": ["drain_on_deny", "no_cap_write",
                     "partial_consume", "over_capacity_consume"],
    "min_kill": 3
  },
  "structural_checks": [
    {"file": "ratelimit/bucket.py",
     "regex": "def try_consume\\(|def available\\("}
  ],
  "mutations": [
    {"id": "drain_on_deny",         "kind": "v2tb:drain_on_deny",
     "attr": "try_consume"},
    {"id": "no_cap_write",          "kind": "v2tb:no_cap_write",
     "attr": "_refill"},
    {"id": "partial_consume",       "kind": "v2tb:partial_consume",
     "attr": "try_consume"},
    {"id": "over_capacity_consume", "kind": "v2tb:over_capacity_consume",
     "attr": "try_consume"}
  ],
  "prompt": "<đề bài, xem §5.2>"
}
```

Mutation plugin: giữ kiến trúc `WB_MUTATION` + registry `_FACTORIES` keyed
`"task:kind"`; BỔ SUNG khả năng stomp method trên CLASS (walk modules tìm class
chứa attr — phục vụ v2_api_integration stomp `RetryPolicy.should_retry`). Toàn bộ
factory mới liệt kê kèm mỗi scenario dưới đây.

---

## 5. Năm scenario V2

### 5.1 `v2_bugfix_multi` — bugfix đỏ→xanh, bug đa模块 (hướng a)

**Scaffold** (package `orderkit`, toàn bộ tiền tệ là INT cents — cấm float):

- `models.py`: `LineItem(sku, qty:int, unit_price_cents:int)`,
  `Order(items)`; helper `subtotal_cents(order)`.
- `pricing.py` — SEED BUG (compound + round từng bước):
  ```python
  def apply_discounts(order, discounts):
      total = sum(li.qty * li.unit_price_cents for li in order.items)
      for d in discounts:
          if d.kind == "pct":
              total = round(total * (100 - d.basis_points / 100) / 100)  # BUG
          elif d.kind == "flat":
              total -= d.cents
      return max(total, 0)
  ```
- `ledger.py` — SEED BUG 2 (first-fit thay largest-remainder):
  ```python
  def allocate_cents(total_cents, weights):
      s = sum(weights)
      shares = [total_cents * w // s for w in weights]   # BUG: remainder về line đầu
      shares[0] += total_cents - sum(shares)
      return shares
  ```
- `tests/test_totals.py`: 8 test, 3 ĐỎ (2 end-to-end pricing, 1 allocation).
- README mục "Pricing contract" ghi đúng hợp đồng (nguồn sự thật cho model).

**Hợp đồng đúng (pin trong docstring + hidden suite)**:
pct discounts cộng dồn basis_points, cap 10 000; nhân MỘT lần với FLOOR nguyên:
`total = (subtotal * (10000 - bp)) // 10000`; flat trừ SAU pct; clamp ≥0.
`allocate_cents`: largest remainder, tie-break theo (-fractional, index);
case phân biệt: `allocate_cents(10, [1, 2]) == [3, 7]`.

**Prompt (verbatim)**:
> The project in this workspace is `orderkit`, an order-pricing library that
> works exclusively in INTEGER cents. Production reports: discounted baskets are
> sometimes billed 1 cent too high, and nightly reconciliation flags allocation
> drift when weights differ. Two failing tests in `tests/test_totals.py` encode
> the reported symptoms; the rest of the suite is green. TASK: find and fix BOTH
> root causes so ALL tests pass. The intended contracts are documented in
> README.md ("Pricing contract") and in the docstrings of
> `orderkit/pricing.apply_discounts` and `orderkit/ledger.allocate_cents`.
> Rules: do NOT edit or delete any test; do NOT rename files/packages/public
> functions; keep all arithmetic integer-exact (no float). Verify with
> `python -m pytest` inside the workspace.

**Assertion khách quan**: required_files · baseline visible ĐỎ (3 fail) ·
hidden suite ≥14 case parametrized green (gồm: hai-pct stacking
subtotal=100000,bp=2000+3000 → 50 000 [compound-bug cho 95 060]; boundary
subtotal=101,bp=5000 → 50 [half-up ra 51]; flat-after-pct order 1000−10%−500flat
→ 400; cap bp>10 000 → free; `allocate_cents(10,[1,2])==[3,7]`; sum(allocation)
== total; empty order → 0) · locked_paths hash (tests/) · allowed_globs
(`orderkit/**`).

**Mutation checks**: `m_compound_pct` (reintroduce multiplicative stacking),
`m_half_up_round` (floor→half-up), `m_first_fit_alloc` (largest-remainder→
first-fit). Combined suite phải kill cả 3.

**Turn budget**: ~20–28 tool turns, `timeout_s` 1200.

**Cheat-risks**: sửa/xoá test đỏ → locked_paths hash bắt; hardcode kết quả vài
case → hidden suite 14 case + held-out không có (không cần, mọi giá trị suy từ
hợp đồng); "sửa" bằng monkeypatch trong conftest → conftest nằm ngoài
allowed_globs → diff-confine bắt; mutation half-up bắt fix "đúng đại".

### 5.2 `v2_tdd_tokenbucket` — feature qua TDD (hướng b)

**Scaffold** (package `ratelimit`; fixture là GIT REPO: init + initial commit
chứa pristine tree + `git config user.email/name` local):

- `counter.py`: FixedWindowCounter hoàn chỉnh + `tests/test_counter_window.py`
  xanh (hash-lock cả hai).
- `bucket.py`: stub `TokenBucket` raise NotImplementedError, SPEC đầy đủ trong
  docstring.
- Model phải TỰ tạo `tests/test_token_bucket.py`.

**SPEC TokenBucket(capacity:int>0, refill_per_sec:float>0,
clock=time.monotonic, sleep=time.sleep)** — pin CHỖ quan sát được (lưu ý: lazy
linear refill + clamp-at-read là path-independent nên KHÔNG pin việc advance
`last` khi bị từ chối — không thể phân biệt, tránh chấm oan):

- tokens khởi full; refill lazy `min(capacity, tokens + elapsed*rate)`,
  clamp-at-WRITE (field nội bộ không bao giờ > capacity);
- `try_consume(n=1)`: n>capacity → False, không đổi state; available≥n → True
  trừ n; ngược lại → False, KHÔNG trừ phần thiếu (cấm partial);
- `available()` trả số token hiện tại (float, đã clamp);
- mặc định dùng time.monotonic/time.sleep nhưng mọi test inject fake.

**TDD protocol (ép bằng git_history)**: ≥3 commits; commit `[tdd] tests` CHỈ
đụng `tests/**` (test đỏ lúc đó — grader kiểm chứng bằng cách checkout commit đó
và chạy pytest: phải rc≠0); sau đó commit `[tdd] impl` đụng
`ratelimit/bucket.py`; cây cuối xanh.

**Prompt (verbatim)**:
> This workspace contains `ratelimit`, a rate-limiting library. The fixed-window
> counter is implemented and tested; `ratelimit/bucket.py::TokenBucket` is a
> stub. Implement it using STRICT TDD, verified mechanically: (1) FIRST write
> `tests/test_token_bucket.py` covering AT LEAST 10 distinct behaviors from the
> SPEC in the bucket docstring (full-capacity start, deny-without-drain,
> n>capacity guard, lazy refill, capacity clamp, fractional refill accumulation,
> injected clock/sleep usage, ...), run pytest to see them FAIL, and commit ONLY
> those tests with message starting `[tdd] tests`. (2) Then implement
> TokenBucket until the whole suite is green and commit with message starting
> `[tdd] impl`. Do not modify `ratelimit/counter.py` or
> `tests/test_counter_window.py`. The harness inspects git history, runs your
> test file alone against mutated implementations (your tests must catch them),
> and runs its own hidden timeline suite. Use `git config user.email/name`
> locally if needed.

**Assertion khách quan**: git_history (min 3 commits, thứ tự tests→impl,
commit tests checkout ra phải ĐỎ) · visible ≥10 test model + counter tests xanh ·
hidden suite 16 case FakeClock (timeline int-ms) green · test_strength: file test
của model TỰ kill ≥3/4 mutant · structural: `try_consume(`/`available(` tồn tại.

**Mutation checks**: `drain_on_deny` (bị từ chối → zero hoá tokens),
`no_cap_write` (thiếu min(capacity,·) khi ghi → `available()` > capacity sau idle
dài — quan sát được nhờ clamp-at-write), `partial_consume` (True + trừ phần có),
`over_capacity_consume` (n>capacity → True + drain). Combined kill 4/4.

**Turn budget**: ~30–40 turns, timeout 1500.

**Cheat-risks**: viết test sau code rồi commit gộp → git_history + checkout-red
bắt; test vô nghĩa (assert True) → test_strength bắt (mutant sống sót); copy
hidden-style timeline nhưng hardcode expected sai SPEC → hidden suite bắt;
đụng counter.py → locked_paths.

### 5.3 `v2_refactor_property` — refactor giữ hành vi + property test (hướng c)

**Scaffold** (package `csvnorm`; KHÔNG có dependency ngoài):

- Ba bản sao gần-identical của row-normalizer: `ingest.normalize_row_ingest`,
  `export.normalize_row_export`, `backfill.normalize_row_backfill` (trim,
  NFC-normalize, lowercase keys, `""→None`, date ISO passthrough).
- `tools/gen_rows.py`: `--seed S --rows N` in CSV ra stdout, deterministic.
- `golden/input.csv` + `golden/expected.jsonl` (canonical JSON:
  `json.dumps(sort_keys=True, ensure_ascii=False, separators=(',',':'))`),
  sinh từ seed 1..5.
- Entry CLI: `python -m csvnorm --in FILE.csv --out OUT.jsonl`.

**Task**: extract `csvnorm/core.py` (`normalize_key`, `normalize_value`,
`normalize_row`) làm single source of truth; cả 3 module import từ `.core`; hành
vi byte-identical; THÊM `tests/test_properties.py` dùng `tools/gen_rows.py`
(seed 1..5 × 100 rows) assert: P1 idempotence `normalize_row(normalize_row(r))
== normalize_row(r)`; P2 mọi key khớp `^[a-z0-9_]+$`; P3 canonical JSON ổn định
2 lần chạy; P4 `""→None`; P5 sentinel set (precomposed 'é', combining accent,
`"  x "`→`"x"`, `""`→None).

**Prompt (verbatim)**:
> `csvnorm` has THREE near-identical copies of its row normalizer spread across
> `ingest.py`, `export.py` and `backfill.py`. TASK: (1) Refactor so the single
> implementation lives in a NEW module `csvnorm/core.py` exporting
> `normalize_key`, `normalize_value`, `normalize_row`; all three existing
> modules must route through it; behavior must be BYTE-identical —
> `python -m csvnorm --in golden/input.csv --out out.jsonl` must reproduce
> `golden/expected.jsonl` exactly. (2) Add `tests/test_properties.py` that uses
> `tools/gen_rows.py` (seeds 1..5, 100 rows each) to verify the properties P1–P5
> listed in the docstrings of the duplicated functions. Do not edit/delete
> existing tests (there are none besides smoke), do not change the public names
> of the three entry functions, and do not touch `golden/` or `tools/`. The
> grader also replays a HELD-OUT corpus you have never seen; only a faithful
> refactor passes it.

**Assertion khách quan**: golden byte-equality · held-out corpus: grader sinh
input seed **777** lúc chấm, expected tính từ REFERENCE `core.py` NHÚNG trong
grader suite (chống hardcode tuyệt đối) · structural regex import `.util`-style
trên cả 3 module (như v1 refactor) · test_properties tồn tại, xanh, và TỰ kill
≥2/3 mutant (test_strength) · hidden parity suite (text/stats-style ba-way
parity như v1) green.

**Mutation checks**: `r1_drop_nfc` (bắt bởi P5), `r2_drop_strip` (P5),
`r3_empty_stays_empty` (P4/P5). Combined kill 3/3.

**Turn budget**: ~35–48 turns, timeout 1800.

**Cheat-risks**: hardcode golden output → held-out seed 777 + reference-embed
bắt; giữ dupe ẩn + chỉ export wrapper → structural regex + mutation load-bearing
(v1 pattern) bắt; property test bỏ sentinel → test_strength bắt (r1 sống sót);
đụng golden/tools → locked_paths.

### 5.4 `v2_log_debug` — debug từ production-log, tái tạo root cause (hướng d)

**Scaffold** (package `urlwatch`; visible tests ĐỎ = 0 — bug chỉ lộ khi race):

- `checker.py`: WorkerPool queue-based; `_on_timeout` RESUBMIT task mà không có
  generation/in-flight guard → attempt cũ hoàn tất sau retry → alert trùng
  (RC1).
- `transport.py`: HttpTransport error path tạo session rồi early-return KHÔNG
  close (RC2); protocol `Transport`; hỗ trợ inject fake.
- `alerts.py`: AlertSink với dedupe window; `state.py`: sqlite state (test dùng
  tmp file).
- `incident/app.log` (~300 dòng JSON structured: event alert_sent lặp 6× cùng
  incident_id trong 1 window; session_open không có session_close tăng dần; 1
  traceback trỏ path retry) + `metrics.txt` (gauge in-flight leo thang) +
  `timeline.md`.

**Prompt (verbatim)**:
> Production incident 042 hit our link-health service `urlwatch`. Evidence is in
> `incident/` (structured app log, metrics snapshot, ops timeline). Symptoms:
> duplicate alert storms for one host within a single dedupe window, and
> monotonic growth of open sessions. TASK: (1) Read the evidence and reproduce
> the root cause(s) locally. There are TWO independent defects; fixing only one
> leaves symptoms. (2) Fix both defects MINIMALLY — do not disable retries or
> change the alert policy. (3) Add `tests/test_regression_incident_042.py`
> containing at least 2 assert statements that FAIL on the unfixed code and PASS
> after your fix (the harness runs this file against a pristine copy of the
> fixture to confirm it is genuinely red there). All existing tests must stay
> untouched and green. Do not modify anything under `incident/`.

**Assertion khách quan** (điểm NOVEL của V2):
- C1 `model_regression_check`: file regression tồn tại, ≥2 assert; chạy trên WS
  → xanh; chạy trên PRISTINE fixture copy → ĐỔNG rc≠0. Đây là bằng chứng cơ học
  model tìm đúng cause chứ không shotgun (nếu fix sai hướng, regression test sẽ
  không đỏ trên pristine).
- C2 hidden replay suite: FakeClock + scripted FakeTransport tái diễn đúng kịch
  bản log: đúng 1 alert/incident-window; số session_open == session_close;
  queue drained lúc shutdown; không task nào thực thi 2 lần (multiset execution
  log); GIỮ retry policy: 503 → đúng 1 retry rồi success.
- C3 diff confinement: `tests/**` hash-lock TRỪ file regression mới;
  `incident/**` hash-lock.
- C4 mutations: `w1_remove_inflight_guard` → replay duplicate-alert assertion
  fail; `w2_leak_session_again` → session-count assertion fail.

**Turn budget**: ~38–52 turns, timeout 2100 (đọc log nặng nhất nhóm).

**Cheat-risks**: tắt hẳn retry/alert để hết symptom → C2 có assertion giữ chính
sách retry + healthy-path → fail; regression test tautology → C1 red-on-pristine
bắt (tautology không đỏ trên pristine); xoá regression test sau khi pass →
presence check; sửa log cho "khớp chuyện" → incident/ hash-lock; fix trúng 1/2
cause → C2 còn assertion phía kia → fail.

### 5.5 `v2_api_integration` — client đa file + mock server (hướng e)

**Scaffold**:

```
feedclient/  __init__.py  errors.py (taxonomy stubs)
             client.py (FeedClient skeleton, NotImplementedError)
             retry.py (RetryPolicy stub)  pager.py (iterate_items stub)
             cli.py  (argparse pull --since TS --out FILE)
mockserver/  server.py   # stdlib ThreadingHTTPServer; env SCENARIO chọn kịch bản
             scenarios.py # happy | paginated | ratelimited | flaky500 |
                          # unauthorized | badjson
docs/API.md              # hợp đồng đầy đủ
tests/test_unit_errors.py # xanh, hash-lock
```

Contract (docs/API.md, nguồn sự thật): `GET /items?cursor&limit` →
`{"items":[{id,published_at,title}], "next_cursor": str|null}`, server ép
limit≤100 (limit>100 → 400 INVALID_LIMIT); auth header `X-API-Key`, sai → 401
UNAUTHORIZED; 429 kèm `Retry-After: <int>`; `POST /subscribe` bắt buộc header
`Idempotency-Key`, thiếu → 409; endpoint chẩn đoán test-only
`GET /__debug/stats`. Kịch bản `paginated`: 250 items trải nhiều trang + 1 item
overlap giữa 2 trang liền nhau (ép de-dup theo id).

Client yêu cầu: `FeedClient(base_url, api_key, transport=None,
sleep=time.sleep, max_retries=3)` — retry 5xx backoff `0.1 * 2^k` qua INJECTED
sleep; 429 honor Retry-After qua injected sleep; KHÔNG BAO GIỜ retry 4xx (trừ
429); `iterate_items(client, since)` đi theo next_cursor tới null, yield đúng
thứ tự server, de-dup id xuyên trang; taxonomy lỗi `errors.py`: FeedError(base),
AuthError, RateLimited(after_s), InvalidResponse, TransientError.

**Prompt (verbatim)**:
> Implement the `feedclient` package against the local mock server described in
> `docs/API.md` (the ONLY API documentation; trust it over any guess). Fill in
> `errors.py`, `retry.py` (class RetryPolicy), `pager.py` (`iterate_items`) and
> `client.py` (`FeedClient`). Requirements: typed error hierarchy; retry ONLY
> transient failures (network, 5xx up to max_retries with exponential backoff
> 0.1*2^k seconds) and 429 honoring the Retry-After header — never retry other
> 4xx; pagination must follow next_cursor to exhaustion and de-duplicate item
> ids across page boundaries (the paginated scenario includes an overlap item).
> Everything must go through the INJECTED clock dependencies (`sleep` argument):
> calling real time.sleep in client code will blow the grader's wall-clock
> budget and fail. Keep every public signature listed in the skeletons' docstrings.
> You may run `mockserver/server.py` yourself (env SCENARIO=<name>, port as
> argv[1]) to develop. Existing tests must stay green.

**Assertion khách quan** (hidden integration suite spawn mock server port trắng,
poll `/healthz`, client luôn inject fake sleep-recorder):
- happy: count/order đúng; paginated: 250 items duy nhất (overlap id xuất hiện
  ĐÚNG 1 lần);
- ratelimited: sleep được gọi với ĐÚNG int(Retry-After) (vd 30) và tổng wall
  <5s (chứng minh không dùng time.sleep thật);
- flaky500 (500×2 rồi 200): thành công sau đúng 2 retry; chuỗi sleep ==
  [0.1, 0.2];
- unauthorized: AuthError raise VÀ `/__debug/stats` cho tổng request == 1
  (không retry 401);
- badjson → InvalidResponse mang status; limit>100 → 400 surfaced, không retry;
  POST thiếu Idempotency-Key → lỗi rõ ràng, không âm thầm gửi 2 lần.
Unit-visible + structural (signature AST: tên hàm/class + tham số) như v1.

**Mutation checks**: `a1_retry_disabled` (flaky500 fail), `a2_fixed_sleep` (bỏ
qua giá trị Retry-After → ratelimited assert fail), `a3_cursor_drop` (dừng trang
1 → paginated fail), `a4_retry_on_401` (request-counter != 1 → unauthorized
fail). Combined kill 4/4.

**Turn budget**: ~48–62 turns, timeout 2400 (phức tạp nhất nhóm).

**Cheat-risks**: dùng time.sleep thật → wall-budget assert fail; sniff
port/path để special-case mock server → không khả thi (port ephemeral mỗi lần
chấm, kịch bản chỉ khác hành vi server); fetch limit=100000 gộp một trang →
server cap 100 + 250 items + next_cursor ép phân trang thật; retry mọi thứ →
a4 + request-counter bắt; hardcode response → server sinh payload động theo
scenario env.

---

## 6. Chấm điểm & pass/fail

- **Gate giữ BINARY** như v1 (chống threshold-gaming): verdict PASS ⇔ TẤT CẢ
  checks ok (baseline 2 chiều + required_files + main-suite-green + structural
  + mutations + mọi extension check của schema §4). Exit 0/1, runner ghi
  results.json.
- **Subscore báo cáo** (informational, không gate), ghi vào results.json:
  correctness 40% (main suite + golden/held-out/integration cases) ·
  anti-cheat-bindings 25% (toàn bộ mutation + test_strength) · process 20%
  (git_history, regression-red, diff-confinement) · robustness 15%
  (wall-budget, determinism reruns ×2 cho integration).
- Grader timeout mỗi pha pytest 240s giữ nguyên; integration wall-budget mặc
  định 60s/suite.

## 7. Ma trận gian lận tổng hợp (cross-scenario)

| Cheat | Cơ chế bắt |
|---|---|
| Sửa/xoá test có sẵn | locked_paths hash-diff |
| Patch ngoài vùng cho phép | allowed_globs + sandbox snapshot trước/sau |
| Test/assert vô nghĩa | mutation binding + test_strength + (S4) red-on-pristine |
| Hardcode output/corpus | held-out seed bí mật + reference nhúng trong grader |
| Gian lận git history (TDD giả) | parse log + checkout commit-tests chạy lại phải đỏ |
| Che symptom bằng cách phá feature (tắt retry…) | assertion bảo toàn chính sách trong replay suite |
| Sleep thật phá ngân sách | injected-sleep recorder + wall_budget_s |
| Trivially-pass trên mutant | phase 5 mutation (kế thừa v1), mở rộng method-level |

## 8. Kế hoạch implement (cho agent thi công)

1. Mở rộng grade.py: engine generic đọc schema §4 (phase G6 diff-confinement,
   G7 model-regression red-on-pristine, G8 git-history, G9 test_strength,
   G10 held-out, G11 integration-spawn); MUTATION_PLUGIN thêm class-method
   stomp + factory mới cho từng `task:kind` ở §5.
2. Runner: TASKS glob động + per-task timeout_s; dry-run in thêm budget.
3. Author fixture + task.json + hidden suites theo §5; mỗi scenario PHẢI có
   selfcheck solved-tree PASS và pristine FAIL từng check đúng hướng, log vào
   `selfcheck/selfcheck-v2.log` (chuẩn v1).
4. Smoke: `--dry-run` toàn bộ; mock-gateway run xác nhận pipeline sạch (kỳ vọng
   assertion FAIL vì mock backend không sửa file — đúng như guide PRACTICAL_CLI_BENCH.md).
5. Rủi ro kỹ thuật lớn nhất: mutation method-level cho S5 và độ ổn định
   spawn/teardown mock server trong grader — làm G11 cuối cùng, sau khi 4
   scenario đầu selfcheck xanh.

## 9. Rủi ro thiết kế còn lại (chấp nhận có chủ ý)

- S2/S3 có thể có lời giải "đúng máy, khác văn mẫu" — mọi assertion đều hành vi-
  based (exit/junit/bytes), chấp nhận đa dạng style.
- S4 phụ thuộc chất lượng fixture log: nếu log quá lộ cause, bài mất giá trị —
  khi author fixture phải tự review "đủ manh mối nhưng không nói mốc".
- Turn budget là ước lượng từ data point v1 (build-from-scratch = 50 turn/7m16s);
  hiệu chỉnh sau 1 live run pilot mỗi scenario trước khi đóng băng.
