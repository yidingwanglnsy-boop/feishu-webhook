import os

# 飞书应用凭证 - 从飞书开放平台获取，也可通过 .env 文件配置
# 路径：开发者后台 → 你的应用 → 凭证与基础信息
APP_ID = os.getenv("FEISHU_APP_ID", "")
APP_SECRET = os.getenv("FEISHU_APP_SECRET", "")

# 事件订阅验证 - 从飞书开放平台获取，也可通过 .env 文件配置
# 路径：开发者后台 → 你的应用 → 事件订阅
ENCRYPT_KEY = os.getenv("FEISHU_ENCRYPT_KEY", "")
VERIFICATION_TOKEN = os.getenv("FEISHU_VERIFICATION_TOKEN", "")

# Claude Code 项目目录 — 越具体越好，目录越大扫描越慢
# 可通过环境变量覆盖，或在飞书消息中用 @项目名 指定（如 @feishu 帮我改bot.py）
CLAUDE_PROJECT_DIR = os.getenv("CLAUDE_PROJECT_DIR", os.path.dirname(os.path.abspath(__file__)))

# 项目名到目录的映射，用于飞书消息中 @项目名 快速切换
PROJECT_MAP = {
    # "feishu": "/home/user/projects/feishu",
    # "stock": "/home/user/projects/stock",
}

# Claude Code 最大执行轮次，限制工具调用次数以防超时
CLAUDE_MAX_TURNS = int(os.getenv("CLAUDE_MAX_TURNS", "20"))

# 允许使用机器人的飞书用户 open_id 白名单
# 留空则允许所有人使用（不推荐）
# 启动后给机器人发消息可在日志中看到你的 open_id
ALLOWED_USERS = os.getenv("FEISHU_ALLOWED_USERS", "").split(",") if os.getenv("FEISHU_ALLOWED_USERS") else []

# 服务端口
PORT = int(os.getenv("FEISHU_BOT_PORT", "8000"))

# 会话管理
SESSION_STORE_PATH = os.getenv("SESSION_STORE_PATH", os.path.join(os.path.dirname(__file__), "sessions.json"))
SESSION_TTL_HOURS = int(os.getenv("SESSION_TTL_HOURS", "24"))
SESSION_MAX_MESSAGES = int(os.getenv("SESSION_MAX_MESSAGES", "50"))
