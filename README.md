# feishu-webhook

飞书机器人桥接服务——通过飞书消息驱动本地 Claude Code 执行任务。

## 功能

- **消息驱动**：在飞书中发消息，自动调用本地 Claude Code 执行并返回结果
- **会话持续**：同一项目自动续接上一次对话，支持多轮交互
- **多项目切换**：`@项目名` 前缀快速切换工作目录
- **安全模式**：`/safe` 前缀运行，工具调用需人工授权
- **图片处理**：发送图片自动保存本地，后续消息中引用路径让 Claude 处理

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置环境变量

复制环境变量模板并填入你的飞书应用凭证：

```bash
cp .env.example .env
vim .env
```

必须填写的变量：`FEISHU_APP_ID`、`FEISHU_APP_SECRET`、`FEISHU_ENCRYPT_KEY`、`FEISHU_VERIFICATION_TOKEN`

如需自定义项目映射，编辑 `config.py` 中的 `PROJECT_MAP`。

### 3. 启动服务

```bash
# 启动 bot
python bot.py

# 另开终端启动内网穿透
ngrok http 8000
```

将 ngrok 分配的 `https://xxx.ngrok-free.app/webhook` 填入飞书开放平台的事件订阅地址。

首次配置请参考 [飞书机器人配置指南](飞书机器人配置指南.md)。

## 使用方法

### 命令速查

| 命令 | 说明 |
|------|------|
| `帮我改 bot.py` | 直接发消息，自动模式执行 |
| `/safe 帮我改 bot.py` | 安全模式，操作需授权 |
| `@feishu 帮我改 bot.py` | 切换到 feishu 项目目录执行 |
| `/new` | 清除所有会话 |
| `/new @feishu` | 清除 feishu 项目会话 |
| `/session` | 查看当前活跃会话 |

### 多项目切换

在消息前加 `@项目名` 切换工作目录：

```
@feishu 帮我改 bot.py
@stock 分析今天的行情
```

可用项目在 `config.py` 的 `PROJECT_MAP` 中配置。

### 图片处理

直接给机器人发图片，会自动保存并返回文件路径，之后在文本消息中引用：

```
分析图片 /tmp/feishu_images/img_xxx.jpg
```

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `FEISHU_APP_ID` | - | 飞书应用 App ID |
| `FEISHU_APP_SECRET` | - | 飞书应用 App Secret |
| `FEISHU_ENCRYPT_KEY` | - | 事件订阅 Encrypt Key |
| `FEISHU_VERIFICATION_TOKEN` | - | 事件订阅 Verification Token |
| `CLAUDE_PROJECT_DIR` | 项目所在目录 | 默认工作目录 |
| `CLAUDE_MAX_TURNS` | `20` | Claude Code 最大执行轮次 |
| `FEISHU_ALLOWED_USERS` | 空 | 允许使用的 open_id，逗号分隔 |
| `FEISHU_BOT_PORT` | `8000` | 服务端口 |
| `SESSION_TTL_HOURS` | `24` | 会话过期时间（小时） |
| `SESSION_MAX_MESSAGES` | `50` | 每会话最大消息数 |
