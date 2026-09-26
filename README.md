# Tropical Tidbits Auto Sync (GitHub Actions)

Bản Python của `trop.php`, chạy tự động **mỗi ngày 1 lần** trên GitHub Actions:

1. Lấy **tất cả pkg (thông số)** của các model `gfs, ecmwf, jma, ec-aifs, icon, gem` (region `wpac`).
2. Với từng pkg, tải toàn bộ ảnh, **thu nhỏ còn 80%**, **đóng dấu `dongdau.png`** ở góc phải dưới, lưu dạng **JPG (chất lượng 85)**.
3. Nếu ảnh đầu tiên trùng **và** số lượng ảnh bằng với dữ liệu đã lưu thì **bỏ qua** pkg đó (chỉ cập nhật `updateat`, giờ GMT+7).
4. Xóa ảnh của runtime cũ, lưu dữ liệu vào **MySQL** (giống bản PHP) hoặc vào `state.json` nếu bạn không dùng DB.
5. (Khuyến nghị) **Upload ảnh lên hosting qua FTP** vào thư mục `imgtrop`.

---

## 1. Cấu trúc thư mục

```
.
├── .github/
│   └── workflows/
│       └── tropical-sync.yml   # Lịch chạy tự động
├── tropical_sync.py            # Script chính
├── requirements.txt            # Thư viện Python
├── dongdau.png                 # (bạn tự thêm) logo đóng dấu, PNG nền trong suốt
├── .gitignore
└── README.md
```

> Thư mục `.github` phải nằm ở **thư mục gốc** của repo thì GitHub mới nhận workflow.

---

## 2. Đưa code lên GitHub

**Cách A: dùng giao diện web (không cần cài gì)**

1. Vào https://github.com/new, tạo repo mới (ví dụ `tropical-sync`). Nên chọn **Private**.
2. Bấm **"uploading an existing file"**, kéo thả **toàn bộ nội dung** thư mục này vào (kể cả thư mục `.github`).
   - Nếu trình duyệt không nhận thư mục ẩn `.github`: bấm **Add file → Create new file**, gõ tên
     `.github/workflows/tropical-sync.yml` rồi dán nội dung file yml vào.
3. Upload thêm file logo **`dongdau.png`** vào thư mục gốc (nếu muốn đóng dấu).
4. Bấm **Commit changes**.

**Cách B: dùng Git trên máy**

```bash
cd tropical-sync
git init
git add .
git commit -m "Init tropical sync"
git branch -M main
git remote add origin https://github.com/<ten-ban>/tropical-sync.git
git push -u origin main
```

---

## 3. Cấp quyền ghi cho workflow

Vào **Settings → Actions → General → Workflow permissions** → chọn **Read and write permissions** → **Save**.
(Cần quyền này để workflow commit `state.json` lại vào repo.)

---

## 4. Khai báo Secrets (mật khẩu DB / FTP)

Vào **Settings → Secrets and variables → Actions → tab Secrets → New repository secret**.

### 4.1. MySQL (tùy chọn, nên dùng nếu web của bạn đang đọc dữ liệu từ DB)

| Secret    | Ví dụ               | Ghi chú                     |
|-----------|---------------------|-----------------------------|
| `DB_HOST` | `123.45.67.89`      | IP/domain của MySQL server  |
| `DB_PORT` | `3306`              | Có thể bỏ trống             |
| `DB_USER` | `user_db`           |                             |
| `DB_PASS` | `matkhau`           |                             |
| `DB_NAME` | `ten_database`      |                             |

- Dùng chung 2 bảng `tropical_models` và `tropical_images` như bản PHP. Nếu chưa có, script **tự tạo**.
- **Quan trọng:** hosting phải cho phép **Remote MySQL**. Trong cPanel: **Remote MySQL → thêm `%`**
  (vì IP của GitHub Actions thay đổi liên tục).
- Nếu **không** khai báo `DB_HOST`, script dùng file `state.json` trong repo để nhớ trạng thái.

### 4.2. FTP (khuyến nghị để ảnh lên thẳng hosting)

| Secret     | Ví dụ                     | Ghi chú                                  |
|------------|---------------------------|------------------------------------------|
| `FTP_HOST` | `ftp.tenmien.com`         |                                          |
| `FTP_PORT` | `21`                      | Có thể bỏ trống                          |
| `FTP_USER` | `user@tenmien.com`        |                                          |
| `FTP_PASS` | `matkhau`                 |                                          |
| `FTP_DIR`  | `/public_html/imgtrop`    | Thư mục chứa ảnh trên hosting            |
| `FTP_TLS`  | `true`                    | `true` nếu hosting yêu cầu FTPS, không thì bỏ trống |

Ảnh mới được upload đè, ảnh runtime cũ được xóa trên hosting.

---

## 5. Biến tùy chọn (Variables)

**Settings → Secrets and variables → Actions → tab Variables → New repository variable**

| Variable              | Mặc định                             | Ý nghĩa                                                   |
|-----------------------|--------------------------------------|-----------------------------------------------------------|
| `MODELS`              | `gfs,ecmwf,jma,ec-aifs,icon,gem`     | Các model cần chạy                                        |
| `PKG_FILTER`          | *(trống = tất cả pkg)*               | Chỉ chạy một số pkg, ví dụ `mslp_pcpn,z500_vort`          |
| `THREADS`             | `8`                                  | Số luồng tải song song                                    |
| `REGION`              | `wpac`                               | Khu vực                                                   |
| `SAVE_IMAGES_TO_REPO` | `false`                              | `true` = commit ảnh vào repo (**không khuyến nghị**, xem lưu ý) |
| `UPLOAD_ARTIFACT`     | `false`                              | `true` = đóng gói ảnh thành file zip tải về được (lưu 3 ngày) |

---

## 6. Chạy

### Chạy tự động
Workflow chạy **mỗi ngày lúc 08:00 giờ Việt Nam** (`cron: "0 1 * * *"`, giờ UTC).
Muốn đổi giờ thì sửa dòng `cron` trong `.github/workflows/tropical-sync.yml`. Giờ UTC = giờ VN - 7, ví dụ:
- `"0 23 * * *"` → 06:00 sáng VN
- `"30 4 * * *"` → 11:30 trưa VN

### Chạy thủ công (nên chạy thử lần đầu)
1. Vào tab **Actions** → chọn **Tropical Tidbits Daily Sync** ở cột trái.
2. Bấm **Run workflow**. Có thể nhập:
   - `models`: ví dụ `gfs` để thử 1 model
   - `pkg_filter`: ví dụ `mslp_pcpn` để thử 1 pkg
   - `threads`: số luồng
3. Bấm vào lần chạy để xem log. Kết quả tổng hợp hiện ở trang **Summary**.

---

## 7. Chạy trên máy tính (tùy chọn)

Yêu cầu Python 3.10 trở lên.

```bash
pip install -r requirements.txt

# Linux / macOS
MODELS=gfs PKG_FILTER=mslp_pcpn python tropical_sync.py

# Windows PowerShell
$env:MODELS="gfs"; $env:PKG_FILTER="mslp_pcpn"; python tropical_sync.py
```

Ảnh được lưu ở thư mục `imgtrop/`. Nếu muốn dùng MySQL/FTP thì đặt thêm biến môi trường `DB_HOST`, `DB_USER`... như mục 4.

---

## 8. Lưu ý

- **Dung lượng:** mỗi model có khoảng 25-30 pkg, mỗi pkg khoảng 13-65 ảnh, mỗi ảnh khoảng 200 KB, nên mỗi ngày có thể lên tới vài trăm MB đến 1 GB.
  GitHub giới hạn repo khoảng 1-5 GB, vì vậy **đừng bật `SAVE_IMAGES_TO_REPO`** khi chạy tất cả pkg. Hãy dùng **FTP**.
- Mỗi lần chạy tối đa khoảng 6 tiếng (đã đặt `timeout-minutes: 350`). Nếu quá lâu thì tăng `THREADS` hoặc giảm số model/pkg.
- Repo **public** thì GitHub Actions miễn phí không giới hạn. Repo **private** được 2.000 phút/tháng miễn phí.
- GitHub sẽ **tạm dừng lịch chạy** nếu repo không có hoạt động nào trong 60 ngày. Khi đó vào tab Actions bấm **Enable workflow** lại.
  (Nếu không dùng DB, commit `state.json` hằng ngày cũng giúp repo luôn có hoạt động.)
- Lỗi `Can't connect to MySQL server`: kiểm tra Remote MySQL (mục 4.1) và firewall cổng 3306 của hosting.
- Lỗi FTP `530 Login incorrect`: kiểm tra lại user/pass. Nếu hosting bắt buộc FTPS thì đặt `FTP_TLS=true`.
