# ##############################################################################
# MODULE: MAIN
# DESCRIPTION: Điểm khởi chạy chính của ứng dụng (Entry Point).
#              Chịu trách nhiệm load config, setup logging và chạy bot.
# ##############################################################################

from __future__ import annotations

import asyncio

from bot.bot import MusicBot
from bot.config import load_config
from bot.utils.logging import setup_logging


# ------------------------------------------------------------------------------
# Function: main
# Purpose: Hàm async chính để khởi tạo và chạy ứng dụng.
# ------------------------------------------------------------------------------
async def main() -> None:
    # 1. Load cấu hình từ biến môi trường (.env)
    config = load_config()
    
    # 2. Thiết lập hệ thống logging (file + console)
    setup_logging(config)

    # 3. Khởi tạo bot với config đã load
    bot = MusicBot(config)
    
    # 4. Chạy bot (Context Manager đảm bảo dọn dẹp tài nguyên khi đóng)
    async with bot:
        await bot.start(config.discord_token)


if __name__ == "__main__":
    asyncio.run(main())
