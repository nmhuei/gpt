# SOLVE_PLAYBOOK — Thư viện Luồng Giải (Solve Flows) theo Category

> **Quy tắc Bắt Buộc (Playbook-First Strategy) cho Mọi Agent:**
> 1. **Kiểm tra Playbook Đầu Tiên**: Khi nhận bất kỳ challenge mới nào, Agent PHẢI:
>    - Xác định đúng **Category** của bài (ví dụ: `Crypto/ZKP`, `Misc/Passwords`, `Crypto/Lattice`, `Web`, `Pwn`, `Reverse`).
>    - Mở file này và đọc toàn bộ các **Flows** đã có trong Category đó.
>    - **Ưu tiên áp dụng các Flow đã ghi nhận trước.**
> 2. **Chỉ nghĩ hướng mới khi các Flow cũ thất bại**: CHỈ KHI tất cả các Flow có sẵn trong Category đều không phù hợp hoặc đã kiểm thử thất bại (fail), Agent mới được phép sáng tạo / thử nghiệm hướng giải mới.
> 3. **Tự động Ghi nhận Flow Sau Khi Giải Thành Công**: Bất kỳ khi nào giải xong một bài mới, Agent PHẢI cập nhật ngay Flow giải (Lỗ hổng nhận diện, Các bước thực thi, Kỹ thuật/Mã nguồn cốt lõi) vào đúng Category trong file này.
> 4. **No-Spoiler Policy (Không tra Writeup, Khuyến khích Tra cứu Kỹ thuật & Tool)**: CẤM tìm kiếm writeup, flag hoặc bài giải có sẵn của đề bài. TUY NHIÊN, Agent ĐƯỢC PHÉP và NÊN tra cứu tài liệu kỹ thuật chuyên sâu (RFC, đặc tả thuật toán, paper phân tích mật mã học, mã nguồn thư viện C/Python) và các công cụ/solver liên quan (Z3 SMT, SageMath, fpylll, SAT solvers) để nắm vững bản chất kỹ thuật và tăng tốc độ giải tự lực.

---

## 📂 Danh mục Categories

- [1. Crypto / ZKP (Zero-Knowledge Proofs & Fiat-Shamir)](#1-crypto--zkp-zero-knowledge-proofs--fiat-shamir)
- [2. Misc / Modular Arithmetic & Password Constraints](#2-misc--modular-arithmetic--password-constraints)
- [3. Crypto / Lattices (HNP, CVP, SVP, LLL)](#3-crypto--lattices-hnp-cvp-svp-lll)
- [4. Crypto / RSA & Discrete Log](#4-crypto--rsa--discrete-log)
- [5. Web / API & Logic Flaws](#5-web--api--logic-flaws)
- [6. Pwn / Binary Exploitation](#6-pwn--binary-exploitation)
- [7. Reverse Engineering](#7-reverse-engineering)

---

## 1. Crypto / ZKP (Zero-Knowledge Proofs & Fiat-Shamir)

### 🔹 Flow ZKP-01: Fiat-Shamir Seed Refresh + Triệt tiêu Số Ngẫu nhiên Bí mật (Ephemeral Secret Elimination)
- **Bài toán mẫu đã giải:** `Let's Prove It Again` (CryptoHack - ZKP).
- **Dấu hiệu nhận biết:**
  - Giao thức Fiat-Shamir với $r = (v - c \cdot FLAG) \bmod (p - 1)$ hoặc biến thể Schnorr/Guillou-Quisquater.
  - Server cho phép `refresh(seed)` hoặc can thiệp vào seed của PRNG (`random.Random(nonce + seed)`).
  - Khóa bí mật $FLAG$ và số ngẫu nhiên $v$ được giữ cố định trong suốt session.
  - Thách thức $c$ được băm từ $t, y, g$ và một số ngẫu nhiên trong không gian nhỏ (ví dụ $z \in [2, 1024]$).
- **Các bước thực hiện chuẩn:**
  1. **Bước 1 (Đọc Nonce):** Lấy `nonce` từ server banner.
  2. **Bước 2 (Tái tạo Số nguyên tố $p$):** Sử dụng quyền `refresh(seed)` để gửi các seed tự chọn (e.g., `seedA`, `seedB`), từ đó tính toán chính xác giá trị các số nguyên tố $p_1, p_3$ trên máy cục bộ bằng hàm PRNG giống server.
  3. **Bước 3 (Xác định Thách thức $c$):** Với mỗi proof nhận được $(t, r, y)$, duyệt qua không gian nhỏ của $z$ để tìm $c$ duy nhất thỏa mãn phương trình xác minh:
     $$t \equiv g^r y^c \pmod p$$
  4. **Bước 4 (Triệt tiêu biến ngẫu nhiên $v$ qua phép trừ):**
     Do kích thước $|v - c \cdot FLAG| \ll p - 1$, phương trình modulo $p - 1$ thực chất là đẳng thức số nguyên chính xác trên $\mathbb{Z}$:
     $$v = r_1 - p_1 + 1 + c_1 \cdot FLAG = r_3 - p_3 + 1 + c_3 \cdot FLAG$$
     Suy ra $FLAG$ bằng phép chia số nguyên tuyệt đối:
     $$FLAG = \frac{(r_3 - p_3 + 1) - (r_1 - p_1 + 1)}{c_1 - c_3}$$
  5. **Bước 5 (Giải mã Nonce & Padding):** Undo phép XOR nonce và loại bỏ byte padding không in được để lấy flag.

---

## 2. Misc / Modular Arithmetic & Password Constraints

### 🔹 Flow MISC-01: Tràn số Nguyên 64-bit + Logarit 2-adic + Rút gọn Mạng LLL (Lattice Reduction)
- **Bài toán mẫu đã giải:** `Bruce Schneier's Password: Part 2` (CryptoHack - Misc/Passwords).
- **Dấu hiệu nhận biết:**
  - Ràng buộc mật khẩu chứa các tập ký tự (chữ hoa, chữ thường, số, dấu gạch dưới `\w*`).
  - Phép nhân tích mã ASCII bằng kiểu dữ liệu có độ rộng cố định (`np.array(map(ord, pw)).prod()` $\rightarrow$ tràn `np.int64` modulo $2^{64}$).
  - Tổng mã ASCII $\text{sum}(pw)$ phải là một số nguyên tố và bằng tích $\text{prod}(pw)$.
- **Các bước thực hiện chuẩn:**
  1. **Bước 1 (Thu hẹp Không gian bằng Tính Chẵn Lẻ):**
     - Tổng là số nguyên tố $\ge 48 \implies \text{sum}$ phải là số LẺ.
     - Vì $\text{prod} \equiv \text{sum} \pmod{2^{64}} \implies \text{prod}$ phải là số LẺ.
     - Tích là số lẻ $\implies$ **100% các ký tự trong mật khẩu bắt buộc phải có mã ASCII LẺ** (rút từ 63 ký tự xuống đúng 32 ký tự).
  2. **Bước 2 (Chuyển đổi Tích thành Tổng qua Logarit 2-adic):**
     - Nhóm nhân $(\mathbb{Z} / 2^{64}\mathbb{Z})^\times \cong \mathbb{Z}_2 \times \mathbb{Z}_{2^{62}}$ với phần tử sinh là $5$.
     - Tính logarit rời rạc 2-adic cơ số 5 (`dlog5`) cho toàn bộ 32 ký tự lẻ bằng thuật toán Hensel bit-by-bit trong 62 phép lặp:
       $$\sum a_i \log_5(x_i) \equiv \log_5(S_{\pm}) \pmod{2^{62}}$$
       $$\sum a_i c_i = S$$
  3. **Bước 3 (Xây dựng Ma trận Mạng Kannan's Embedding):**
     - Đặt $a_i = 1 + x_i$ để căn chỉnh vector nghiệm quanh 0.
     - Gán trọng số $W_{sum} = 2^{64}$ (đẳng thức nguyên tuyệt đối) và $W_{log} = 1$ (đồng dư modulo $2^{62}$).
  4. **Bước 4 (Chạy LLL trong SageMath):**
     - Thuật toán LLL tìm ra vector nghiệm nguyên $a_i \ge 0$ trong **0.05 giây**.
  5. **Bước 5 (Ghép chuỗi & Gửi Payload):**
     - Tạo chuỗi mật khẩu gồm các ký tự có số lần xuất hiện tương ứng $a_i$, verify kiểm tra đủ `\d`, `[A-Z]`, `[a-z]` và gửi server.

---

## 3. Crypto / Lattices (HNP, CVP, SVP, LLL)

### 🔹 Flow LAT-01: Hidden Number Problem (HNP) với ECDSA Lộ Bit Nonce
- **Dấu hiệu nhận biết:** ECDSA/Schnorr cho nhiều chữ ký $(r_i, s_i)$ với nonce $k_i$ bị thiên vị (biased), ví dụ $k_i$ luôn có vài bit đầu/cuối bằng 0 hoặc biết trước.
- **Các bước thực hiện chuẩn:**
  1. Đưa quan hệ chữ ký $s_i \equiv k_i^{-1} (h_i + r_i d) \pmod n$ về dạng:
     $$k_i \equiv t_i d + u_i \pmod n$$
  2. Xây dựng ma trận mạng Babai / Boneh-Venkatesan kích thước $(m+1) \times (m+1)$.
### 🔹 Flow LAT-02: Coupled LCG Hai Tầng (Mixed Moduli) qua Syzygy Matrix, Resultant Đa thức & Lattice Kernel
- **Bài toán mẫu đã giải:** `orbital-strike` (SekaiCTF - Crypto).
- **Dấu hiệu nhận biết:**
  - Hai bộ LCG lồng nhau: Inner LCG ($M_i = a M_{i-1} + b \pmod p$) và Outer LCG ($X_i = A X_{i-1} + M_i \pmod P$) với hai modulo nguyên tố khác nhau ($p \approx 2^{311}$, $P \approx 2^{256}$).
  - Đề bài cho chuỗi output ngoài $\text{orbit} = [X_1, \dots, X_m]$. Khóa AES bí mật $X = X_0$.
- **Các bước thực hiện chuẩn:**
  1. **Bước 1 (Tính Sai Phân D):** Lập vector sai phân $D_i = X_{i+1} - X_i$. Đặt $E_i = M_{i+1} - M_i$ thỏa $E_{i+1} \equiv a E_i \pmod p$.
  2. **Bước 2 (Tìm Short Syzygies bằng LLL):** Xây ma trận Hankel $H$ từ các lát cắt của $D$ (kích thước $3 \times 11$). Chạy LLL trên `H.right_kernel_matrix()` để lọc ra các vector ngắn (syzygies) triệt tiêu thành phần $D$.
  3. **Bước 3 (Khôi phục Modulus $p$ và Multiplier $a$ qua Resultant):**
     - Chuyển các syzygies thành đa thức $f_k(T) = \sum r_i T^i$.
     - Tính Resultant giữa các cặp đa thức: $\text{Res}(f_i, f_j) \equiv 0 \pmod p$.
     - Lấy $\gcd$ của các resultant nguyên, phân tích thừa số nguyên tố lớn để thu được $p$ (311-bit).
     - Tính $\gcd(f_1(T), f_2(T)) \pmod p$ trên $\mathbb{F}_p[T]$ để tìm nghiệm tuyến tính $T - a \implies a$.
  4. **Bước 4 (Khôi phục Sai phân $E$ và Modulus ngoài $P$):**
     - Thiết lập hệ phương trình nguyên cho 13 biến $E_i$ và 12 thương số $n_i$ thỏa $E_{i+1} - a E_i = p n_i$.
     - Chạy LLL trên kernel để tìm nghiệm ngắn duy nhất $E$.
     - Tính $\gcd$ các biểu thức nhân chéo $(D_i - E_i)D_{j-1} - (D_j - E_j)D_{i-1}$ để tách ra số nguyên tố $P$ (256-bit).
  5. **Bước 5 (Tính Multiplier $A$ & Khóa $X$):**
     $$A = (D_i - E_i) D_{i-1}^{-1} \pmod P$$
     $$X = (X_1 - (X_2 - A X_1 - E_1)) A^{-1} \pmod P$$
     Dùng $X$ giải mã AES-ECB thu được flag.

---

## 4. Crypto / RSA & Discrete Log

### 🔹 Flow RSA-01: Khảo sát Phân tích Thừa số Nhanh (Factordb -> Wiener/Boneh-Durfee -> Coppersmith)
- **Các bước thực hiện chuẩn:**
  1. Kiểm tra Factordb API xem $N$ đã có sẵn thừa số chưa.
  2. Nếu $e$ lớn ($d < N^{0.292}$): chạy Wiener / Boneh-Durfee.
  3. Nếu $e$ nhỏ ($e=3$): kiểm tra Hastad Broadcast hoặc tìm nghiệm nhỏ bằng `small_roots()`.
  4. Nếu $p, q$ gần nhau: Fermat factorization.

---

## 5. Web / API & Logic Flaws

*(Đang cập nhật sau các bài Web tiếp theo)*

---

## 6. Pwn / Binary Exploitation

*(Đang cập nhật sau các bài Pwn tiếp theo)*

---

## 7. Reverse Engineering

*(Đang cập nhật sau các bài Rev tiếp theo)*
