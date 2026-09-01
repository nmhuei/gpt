# MED-BATCH4 PREP — 5 bài medium ~100đ chuẩn bị sẵn (2026-08-25)

**Bối cảnh:** chuẩn bị offline cho batch CTF kế tiếp — KHÔNG bắn turn, KHÔNG đụng gateway :18000, KHÔNG chạy pytest suite chính, KHÔNG mark `used_at` (việc đó thuộc trọng tài khi giải thật). Chỉ chạy picker + đọc/kiểm tra artifact.

## Lệnh pick đã chạy nguyên văn

```bash
# lần 1 — list chính thức (NEEDS_REMOTE bị loại mặc định)
.venv/bin/python scripts/pick_ctf_challenge.py --used-file scripts/.ctf_used_challenges.json --output ~/Downloads/med-batch4-pick.json

# lần 2 — chạy lại với --include-remote chỉ để LIỆT KÊ các bài bị loại NEEDS_REMOTE
/home/light/GitHub/gpt/.venv/bin/python /home/light/GitHub/gpt/scripts/pick_ctf_challenge.py --used-file /home/light/GitHub/gpt/scripts/.ctf_used_challenges.json --include-remote --output ~/Downloads/med-batch4-pick-all.json
```

**Stats:** scanned=499 · accepted=173 · rejected=326 (not_solvable=166, unclear_statement=128, already_solved(flag.txt)=28, needs_human_review=4) · used_filtered_out=12 · **needs_remote_filtered_out=8** · final=153.

## Bảng 5 bài chọn

| # | Bài | Category | Điểm | Path | Đề tóm tắt | Artifact check | Sơ độ |
|---|-----|----------|------|------|-----------|----------------|-------|
| 1 | OneVoice | mobile (mới, chưa giải bao giờ) | 100 | `/home/light/Workspace/CTF/BrunnerCTF_2026_-_Global/Mobile/OneVoice` | App nội bộ `OneVoice` bắt nhân viên dùng wording tuần duyệt verbatim; có tin đồn thông báo sắp tới — tìm message draft đã soạn sẵn trong app | `OneVoice.apk` 4.6MB, `classes.dex` 8.5MB thuần Java/Kotlin (không native lib) → jadx/apktool static analysis là đủ; sạch REDACTED | Medium (label Medium, 190 solves) — dễ nhất batch |
| 2 | Shredded Recipe | crypto | 100 | `/home/light/Workspace/CTF/BrunnerCTF_2026_-_Global/Crypto/Shredded_recipe` | Công thức bị shred; flag 54 byte chia 3 share x=f[0::3], y=f[1::3], z=f[2::3]; cho p (512-bit prime) + 1 phương trình d = ax+by+cz mod p | `source.py` + `output.txt` (p,a,b,c,d) đầy đủ; placeholder `brunner{???…}` trong source chỉ là mask, ciphertext thật nằm trong output.txt → giải được local bằng LLL/lattice trên coset 2D + ràng buộc ASCII | Hard theo label nhưng dạng kinh điển hidden-number/lattice (~medium-thard thực tế); 250 solves |
| 3 | The Missing Recipe | forensics | 100 | `/home/light/Workspace/CTF/BrunnerCTF_2026_-_Global/Forensics/The_Missing_Recipe` | Ảnh "không phù hợp" + công thức mật mất khỏi network; SOC có PCAP full kỳ sự cố; tái dựng cuộc tấn công và lấy lại flag | `the-missing-recipe.pcap` 41MB; tshark/capinfos có sẵn trên máy; scan sạch REDACTED; flag = dữ liệu exfil trong pcap (kèm ảnh nhiễu để lọc bỏ) | Medium (label Medium, 187 solves) |
| 4 | Alternative Channel | misc/stego | 100 | `/home/light/Workspace/CTF/BrunnerCTF_2026_-_Global/Misc/Alternative_channel` | Kênh SSTV chính bị lẫn tín hiệu lạ; tìm nội dung đang được truyền trộm. Flag = content tìm được, tự bọc `brunner{}` | `alternative_channel.png` 320×256 RGB (10KB) — đúng kích thước khung SSTV; decode offline bằng numpy/PIL (lưu ý: `.venv` chưa có Pillow — cần `pip install pillow` hoặc decode thuần Python); sạch REDACTED | Medium (label Medium, tag Stego, 60 solves — ít solves nhất batch nên khó đoán hơn chút) |
| 5 | Reorg | rev | 100 | `/home/light/Workspace/CTF/BrunnerCTF_2026_-_Global/Reversing/Reorg` | "Org chart phẳng 2 lần": request không route theo department mà department do manager sở hữu tại từng bước escalation, mỗi manager review một metric khác nhau; tìm escalation path được sign-off tới đỉnh. README ghi chú nên giải `Roadmap` trước — ta đã giải Roadmap (batch 2) | `default.conf` (330 dòng) + 5 conf `reorg-{bands,ledger,legal,ops,sales}.conf` (ledger 1522 dòng) — nginx map FSM thuần, cùng họ kỹ thuật với Roadmap đã verify bằng referee pure-Python parse; sạch REDACTED | Hard theo label; lợi thế: ta đã có kinh nghiệm + referee script pattern từ Roadmap |

**Đa category:** 5 bài = 5 category phân biệt (mobile/crypto/forensics/misc/rev), mobile là category chưa từng giải. **Ưu tiên pwn/web:** xem phần loại bên dưới — KHÔNG có bài pwn/web nào trong thư viện hiện giải được offline (flag redacted hoặc cần instance remote), nên 5 chỗ còn lại dàn đều category.

## NEEDS_REMOTE bị picker loại (8) — để unmark/tham khảo thủ công sau nếu có instance

| Path | Category | Điểm | Lý do |
|------|----------|------|-------|
| `/home/light/Workspace/CTF/2026_haruulzangi_CTF/web/Likeness` | web | 957 | connection_info_null(web) |
| `/home/light/Workspace/CTF/2026_haruulzangi_CTF/web/My_Avatar` | web | 906 | connection_info_null(web) |
| `/home/light/Workspace/CTF/BrunnerCTF_2026_-_Global/Web/Technical_Debt` | web | 130 | connection_info_null(web) |
| `/home/light/Workspace/CTF/CTF_Competition/web/Likeness` | web | 957 | connection_info_null(web) (trùng haruulzangi) |
| `/home/light/Workspace/CTF/CTF_Competition/web/My_Avatar` | web | 906 | connection_info_null(web) (trùng haruulzangi) |
| `/home/light/Workspace/CTF/N1PH_RSxTCTF/Pwn/TurboCalc` | pwn | 835 | remote_hint_in_statement('nc 13.203.69.239 31004') |
| `/home/light/Workspace/CTF/N1PH_RSxTCTF/Web/PingBox` | web | 266 | connection_info_null(web) |
| `/home/light/Workspace/CTF/grudo/Pwn/Red Tide Terminal` | pwn | — | redacted_flag_in_source(m8wroubk.js: contains REDACTED) |

## Loại thêm ở tầng prep (picker không thấy — blind spot: REDACTED nằm BÊN TRONG zip/tar, `_scannable_texts` skip file nhị phân)

- **Pwn BrunnerCTF ~100đ tất cả đều chết local:** Locked_Out, Brunner_Stocks, Pure_Notes, Mindbreaker (100đ), Guessing_game (125đ) — zip đều chứa `flag.txt` = `brunner{REDACTED}` (binary đọc ./flag.txt runtime → exploit local chỉ in ra REDACTED, không có flag thật). Boot2Root cũng vậy: Bink_Ink_(User), BrunRouter (flag_user/root.txt REDACTED); Misc: Functional_Budget, Git_gud (REDACTED); Onboarding HRBot (REDACTED). Crypto TriKDF_Enterprise (100đ): FLAG + MASTER_SECRET đều REDACTED, challenge tương tác input() kiểu service, không ship ciphertext → cần remote.
- **Pwn Z0d1ak** Salvage_Protocol(128)/rapture(135)/House_XIII(147): binary+libc+ld kiểu remote service, không ship flag → không verify offline.
- **Pwn grudo** Clockwork Vault / House Of Mirage (1000đ, label medium): binary in flag từ flag.txt local không ship → chỉ demo được exploit, không có flag thật.
- **PTIT Pwn/Web** (baby-heap-V2 100đ…): `DynamicContainer` — Dockerfile/compose, flag inject lúc chạy container → cần instance.
- **grudo Web Wrodle**: cần tạo instance OpenVPN (link drive) → remote.

Kết luận pyn/web: toàn bộ ~100đ pwn/web trong thư viện hiện **không** satisfy "artifact local giải được" → tạm gác, chờ library mới hoặc chế độ có instance.

## Dự phòng (reserve, nếu bài nào trong 5 hỏng khi bắn thật)

- π-crypt_0.57 (crypto 100đ Hard): bake.py + baked_pie.txt + unbaked_pi.txt — static, sạch.
- Secret_Storage (crypto 100đ Hard): Flask app + vault-export json — static khả thi nhưng nhiều bộ phận.
- Lockdown-mode (rev 100đ Hard): `recovered.rbf` FPGA bitstream — novel, rủi ro cao hơn Reorg.
- Magic_or_not / Half_Baked (misc 100đ Easy): static, sạch — fallback dễ.

## Ghi chú vận hành

- KHÔNG mark used_at cho cả 5 bài — `scripts/.ctf_used_challenges.json` giữ nguyên (12 entry cũ).
- Extract kiểm tra nằm ở `/home/light/Downloads/med-batch4-check/` (user tự dọn; zip gốc trong workspace không đổi).
- Picker JSON: `~/Downloads/med-batch4-pick.json` (final list) và `~/Downloads/med-batch4-pick-all.json` (--include-remote, để đối chiếu NEEDS_REMOTE).
- Tooling: tshark/capinfos OK cho forensics; `.venv` thiếu Pillow (cần cho stego SSTV nếu đi đường image-decode).
- Khi gateway khỏe: bắn trực tiếp 5 bài theo thứ tự đề xuất OneVoice → Missing_Recipe → Alternative_Channel → Shredded → Reorg (dễ→khó), trọng tài mark used_at sau khi giải thật.
