# Discord Music Bot

Một bot nghe nhạc Discord đơn giản, hiệu năng cao được viết bằng Python sử dụng thư viện `discord.py` và `wavelink`. Bot hỗ trợ phát nhạc chất lượng cao thông qua Lavalink node.

## Tính năng chính

- **Phát nhạc chất lượng cao**: Sử dụng Lavalink để stream nhạc ổn định.
- **Hàng chờ thông minh**: Quản lý hàng chờ bài hát dễ dàng.
- **Điều khiển đầy đủ**: Play, Pause, Skip, Stop, Volume, Loop.
- **Cơ sở dữ liệu**: Lưu trữ settings bằng SQLite (via `aiosqlite`).
- **Dễ dàng cấu hình**: Thiết lập qua file `.env`.

## Yêu cầu hệ thống

- Python 3.10 trở lên
- Một server Lavalink đang chạy (Project đã cấu hình sẵn server Public mặc định).

## Cài đặt và Chạy

### 1. Clone repository
```bash
git clone https://github.com/TVD-00/Discordbotmusicadvanced
cd bot-music
```

### 2. Cài đặt thư viện
Khuyên dùng môi trường ảo (Virtual Environment):
```bash
# Tạo venv
python -m venv .venv

# Kích hoạt venv (Windows)
.venv\Scripts\activate

# Kích hoạt venv (Linux/Mac)
source .venv/bin/activate
```

Cài đặt các gói phụ thuộc:
```bash
pip install -r requirements.txt
# Hoặc nếu dùng file pyproject.toml
pip install .
```

### 3. Cấu hình
Bạn cần tạo file `.env` từ file mẫu:
1. Copy file `.env.example` thành `.env`.
2. Mở file `.env` và điền **DISCORD_TOKEN**.
3. (Tùy chọn) Chỉnh sửa các thông số khác nếu cần.

> **Xem hướng dẫn chi tiết file cấu hình tại [ENV_GUIDE.md](ENV_GUIDE.md)**

### 4. Khởi chạy
```bash
python main.py
```

## Cấu trúc dự án

```
bot-music/
├── bot/                # Source code chính của bot
│   ├── commands/       # Các lệnh (Cogs)
│   ├── utils/          # Tiện ích bổ trợ
│   ├── config.py       # Xử lý cấu hình
│   └── bot.py          # Class bot chính
├── main.py             # File khởi chạy
├── .env.example        # File mẫu cấu hình
├── ENV_GUIDE.md        # Hướng dẫn cấu hình
└── pyproject.toml      # Quản lý dependencies
```

## Giấy phép
Dự án được phân phối dưới giấy phép [MIT License](LICENSE).
