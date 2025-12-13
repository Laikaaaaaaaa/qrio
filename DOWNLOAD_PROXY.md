# Giữ đúng tên file khi quét (tải về ngay)

Vấn đề bạn thấy (tên file tải về là chuỗi ký tự lạ) xảy ra vì các host trung gian (ví dụ `bashupload.com`) thường:
- Đặt URL theo token ngẫu nhiên để tránh trùng tên
- Không trả header `Content-Disposition` kèm `filename=...`

Khi đó trình duyệt sẽ lấy tên file theo phần cuối của URL (token), nên không còn tên gốc.

## Cách fix đúng (miễn phí) — dùng Cloudflare Worker làm “download proxy”

Worker sẽ:
- Lấy file từ link trung gian
- Trả về lại cho trình duyệt với `Content-Disposition: attachment` + **đúng tên file gốc**

### 1) Tạo Worker
1. Vào Cloudflare Dashboard → **Workers & Pages**
2. Create Worker
3. Copy nội dung từ file [cf-worker-download-proxy.js](cf-worker-download-proxy.js) vào
4. Deploy

Sau khi deploy bạn sẽ có URL kiểu:
- `https://your-worker.your-subdomain.workers.dev/`

### 2) Cấu hình app.py dùng Worker
Backend đã hỗ trợ sẵn biến môi trường:
- `FILE_QR_DOWNLOAD_PROXY`

Ví dụ (PowerShell):

```powershell
$env:FILE_QR_DOWNLOAD_PROXY = "https://your-worker.your-subdomain.workers.dev/"
python app.py
```

Hoặc set vĩnh viễn (PowerShell):

```powershell
setx FILE_QR_DOWNLOAD_PROXY "https://your-worker.your-subdomain.workers.dev/"
```

Sau đó mở terminal mới và chạy lại `python app.py`.

### 3) Kết quả
Khi upload file ở QR kiểu File:
- Server sẽ upload lên host trung gian (bashupload)
- Nhưng URL trả về để nhét vào QR sẽ là link Worker (proxy)
- Quét QR → tải xuống ngay **và đúng tên file**

## Không dùng Worker thì sao?
Nếu không dùng Worker, bạn **không thể** ép “đúng tên file gốc” một cách chắc chắn vì header/tên file do bên thứ ba quyết định.
