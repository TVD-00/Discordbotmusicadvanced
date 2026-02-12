# Huong dan Cau hinh (`.env`)

File `.env` chứa các thông tin bảo mật và cấu hình quan trọng để bot hoạt động.

## 1. Cách setup nhanh
1. Copy file `.env.example` và đổi tên thành `.env`.
2. Mở file `.env` và điền token của bot vào dòng `DISCORD_TOKEN`.
3. Lưu file và khởi động bot.

## 2. Chi tiết các biến

### Discord (Bắt buộc)
- **DISCORD_TOKEN**: Token của bot, lấy tại [Discord Developer Portal](https://discord.com/developers/applications).
  - *Ví dụ*: `MTA...`
- **DEV_GUILD_ID**: (Tùy chọn) ID server test. Nếu điền, các lệnh slash sẽ cập nhật ngay lập tức trên server này (chỉ dùng khi đang code/test).

### Lavalink (Nhạc)
Đây là server xử lý luồng nhạc. Mặc định đã cấu hình sẵn server Public (Serenetia) nên bạn **không cần sửa** nếu không có server riêng.
- **LAVALINK_NODES_JSON**: (Khuyên dùng nếu chạy lâu) Danh sách nhiều node Lavalink để fallback khi 1 node chết.
  - Nếu đồng thời cấu hình `LAVALINK_HOST/PORT/PASSWORD/...`, bot sẽ ưu tiên node này làm **primary** và dùng `LAVALINK_NODES_JSON` làm **fallback**.
  - Nếu chỉ cấu hình `LAVALINK_NODES_JSON`, bot vẫn chạy bình thường với danh sách fallback.
  - Giá trị là JSON array, nên bọc bằng dấu nháy đơn trong `.env`.
  - *Ví dụ*:
    - `LAVALINK_NODES_JSON='[{"identifier":"main","uri":"https://lavalinkv4.serenetia.com:443","password":"https://dsc.gg/ajidevserver"},{"identifier":"backup-jirayu","uri":"https://lavalink.jirayu.net:443","password":"youshallnotpass"}]'`
  - Bạn có thể lấy thêm public node tại: https://lavalink-list.darrennathanael.com/
- **LAVALINK_HOST**: Địa chỉ server Lavalink (VD: `lavalinkv4.serenetia.com`).
- **LAVALINK_PORT**: Cổng kết nối (VD: `80` hoặc `2333`).
- **LAVALINK_PASSWORD**: Mật khẩu nối tới Lavalink.
- **LAVALINK_SECURE**: `1` nếu dùng SSL (https/wss), `0` nếu không.
- **LAVALINK_IDENTIFIER**: Tên định danh cho node này (để `main`).
- **WAVELINK_CACHE_CAPACITY**: Dung lượng cache (để trống hoặc `100`).

#### Lavalink nâng cao
- **LAVALINK_NODE_RETRIES**: Số lần retry khi node Lavalink không kết nối được.
  - `0` = thử 1 lần rồi bỏ (không treo startup)
  - Khuyên dùng `2` cho public node để tránh treo rất lâu khi node chết.

### Bot Behavior (Hành vi)
- **DEFAULT_VOLUME**: Âm lượng mặc định khi bot vào phòng (0-100).
- **IDLE_TIMEOUT_SECONDS**: Thời gian bot tự thoát nếu không phát nhạc (giây). `300` = 5 phút.
- **ANNOUNCE_NOWPLAYING**: `1` = Bật thông báo bài đang phát, `0` = Tắt.

### Storage (Lưu trữ)
- **DB_PATH**: Tên file database (SQLite). Nên để mặc định `bot.db`.

### Logging (Ghi log)
Dùng để theo dõi lỗi và hoạt động của bot.
- **LOG_LEVEL**: Mức độ chi tiết (`DEBUG`, `INFO`, `WARNING`, `ERROR`). Nên để `INFO`.
- **LOG_DIR**: Thư mục chứa file log.
- **LOG_FILE**: Tên file log.

### Optional Links (Link phụ)
- **SUPPORT_INVITE_URL**: Link mời vào server hỗ trợ của bạn (hiện khi gõ lệnh help/info).
- **VOTE_URL**: Link bình chọn cho bot (nếu có).
