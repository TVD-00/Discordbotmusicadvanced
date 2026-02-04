# ##############################################################################
# MODULE: CONSTANTS
# DESCRIPTION: Tập trung các hằng số và magic numbers dùng trong toàn bộ codebase.
#              Tránh hardcode giá trị, dễ maintain và điều chỉnh.
# ##############################################################################

from __future__ import annotations


# ==============================================================================
# TIMEOUT CONSTANTS
# ==============================================================================

# Thời gian chờ cho các thao tác network (giây)
LYRICS_API_TIMEOUT = 10           # Timeout cho API lyrics.ovh
SEARCH_TIMEOUT = 30               # Timeout cho tìm kiếm nhạc
VOICE_CONNECT_TIMEOUT = 12        # Timeout khi join voice
PLAYER_OP_TIMEOUT = 10            # Timeout cho thao tác player (play/filter)

# Thời gian delay (giây)
CONTROLLER_REFRESH_DELAY = 0.7    # Delay refresh controller sau track_end

# Thời gian seek (mili giây)
SEEK_STEP_MS = 10_000             # Bước nhảy seek +/- 10s


# ==============================================================================
# LIMIT CONSTANTS
# ==============================================================================

# Giới hạn số lượng
MAX_SEARCH_RESULTS = 10           # Số kết quả tìm kiếm hiển thị
MAX_QUEUE_DISPLAY = 10            # Số bài trong queue hiển thị
MAX_LIKED_DISPLAY = 10            # Số bài liked hiển thị
MAX_PLAYLIST_VIEW = 15            # Số bài trong playlist view
MAX_PLAYLIST_LIST = 20            # Số playlist hiển thị trong list
MAX_SAVE_QUEUE = 100              # Số bài tối đa khi lưu queue thành playlist
MAX_PLAYLIST_ADD = 50             # Số bài tối đa khi thêm từ playlist vào

# Cache
PLAYLIST_CACHE_TTL_SECONDS = 300  # Cache playlist trong 5 phút


# ==============================================================================
# VOLUME CONSTANTS
# ==============================================================================

VOLUME_STEP = 5                   # Bước tăng/giảm volume
VOLUME_MIN = 0                    # Volume tối thiểu
VOLUME_MAX = 100                  # Volume tối đa


# ==============================================================================
# RATE LIMITING CONSTANTS
# ==============================================================================

# Rate limiting đơn giản: số request tối đa trong khoảng thời gian
SEARCH_RATE_LIMIT_COUNT = 5       # Tối đa 5 lần search
SEARCH_RATE_LIMIT_WINDOW = 60     # Trong vòng 60 giây


# ==============================================================================
# TEXT LENGTH CONSTANTS
# ==============================================================================

MAX_LYRICS_LENGTH = 1900          # Độ dài tối đa lyrics gửi trực tiếp
MAX_EMBED_FIELD_VALUE = 50        # Độ dài tối đa cho SelectOption description
