import json
import subprocess
import threading
import logging
import os
import time
import uuid

from fastapi import FastAPI, Request
from lark_oapi import Client, EventDispatcherHandler
from lark_oapi.api.im.v1 import *

import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("feishu-bot")


class SessionStore:
    """线程安全的文件会话存储，绑定 open_id+cwd → Claude session_id"""

    def __init__(self, path: str, ttl_hours: int, max_messages: int):
        self.path = path
        self.ttl_hours = ttl_hours
        self.max_messages = max_messages
        self._lock = threading.Lock()
        self._cache = {}
        self._load()
        self.cleanup_expired()

    def _load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, 'r') as f:
                    self._cache = json.load(f)
            except (json.JSONDecodeError, IOError):
                self._cache = {}
        else:
            self._cache = {}

    def _save(self):
        tmp_path = self.path + ".tmp"
        with open(tmp_path, 'w') as f:
            json.dump(self._cache, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, self.path)

    def get(self, open_id: str, cwd: str) -> dict | None:
        with self._lock:
            user_sessions = self._cache.get(open_id, {})
            session_data = user_sessions.get(cwd)
            if not session_data:
                return None

            now = time.time()
            if now - session_data.get("last_used", 0) > self.ttl_hours * 3600:
                del user_sessions[cwd]
                if not user_sessions:
                    del self._cache[open_id]
                self._save()
                return None

            if session_data.get("message_count", 0) >= self.max_messages:
                del user_sessions[cwd]
                if not user_sessions:
                    del self._cache[open_id]
                self._save()
                return None

            sid = session_data.get("session_id", "")
            try:
                uuid.UUID(sid)
            except ValueError:
                del user_sessions[cwd]
                if not user_sessions:
                    del self._cache[open_id]
                self._save()
                return None

            return session_data

    def set(self, open_id: str, cwd: str, session_id: str):
        with self._lock:
            if open_id not in self._cache:
                self._cache[open_id] = {}

            now = time.time()
            existing = self._cache[open_id].get(cwd)
            if existing:
                existing["session_id"] = session_id
                existing["last_used"] = now
                existing["message_count"] = existing.get("message_count", 0) + 1
            else:
                self._cache[open_id][cwd] = {
                    "session_id": session_id,
                    "cwd": cwd,
                    "created_at": now,
                    "last_used": now,
                    "message_count": 1,
                }
            self._save()

    def increment_message_count(self, open_id: str, cwd: str):
        with self._lock:
            user_sessions = self._cache.get(open_id, {})
            session_data = user_sessions.get(cwd)
            if session_data:
                session_data["last_used"] = time.time()
                session_data["message_count"] = session_data.get("message_count", 0) + 1
                self._save()

    def remove(self, open_id: str, cwd: str = None):
        with self._lock:
            if open_id in self._cache:
                if cwd:
                    self._cache[open_id].pop(cwd, None)
                    if not self._cache[open_id]:
                        del self._cache[open_id]
                else:
                    del self._cache[open_id]
                self._save()

    def get_user_info(self, open_id: str) -> dict:
        with self._lock:
            user_sessions = self._cache.get(open_id, {})
            now = time.time()
            result = {}
            for cwd, data in user_sessions.items():
                age_hours = (now - data.get("created_at", now)) / 3600
                idle_hours = (now - data.get("last_used", now)) / 3600
                result[cwd] = {
                    "session_id": data["session_id"][:8] + "...",
                    "messages": data.get("message_count", 0),
                    "age_hours": round(age_hours, 1),
                    "idle_hours": round(idle_hours, 1),
                }
            return result

    def cleanup_expired(self):
        with self._lock:
            now = time.time()
            for open_id in list(self._cache.keys()):
                for cwd in list(self._cache[open_id].keys()):
                    data = self._cache[open_id][cwd]
                    if now - data.get("last_used", 0) > self.ttl_hours * 3600:
                        del self._cache[open_id][cwd]
                if not self._cache.get(open_id):
                    del self._cache[open_id]
            self._save()


session_store = SessionStore(
    path=config.SESSION_STORE_PATH,
    ttl_hours=config.SESSION_TTL_HOURS,
    max_messages=config.SESSION_MAX_MESSAGES,
)

# 每用户锁，防止同一用户并发 resume 同一个 session
_user_locks: dict[str, threading.Lock] = {}
_user_locks_lock = threading.Lock()


def _get_user_lock(user_id: str) -> threading.Lock:
    with _user_locks_lock:
        if user_id not in _user_locks:
            _user_locks[user_id] = threading.Lock()
        return _user_locks[user_id]


app = FastAPI()

# 飞书客户端
client = Client.builder() \
    .app_id(config.APP_ID) \
    .app_secret(config.APP_SECRET) \
    .build()

# 事件处理器（自动解密 + 验签）
event_handler = EventDispatcherHandler.builder(
    encrypt_key=config.ENCRYPT_KEY,
    verification_token=config.VERIFICATION_TOKEN,
).register_p2_im_message_receive_v1(
    lambda data: on_message_receive(data)
).build()


def parse_project_dir(text: str) -> tuple[str, str]:
    """从消息中解析 @项目名 前缀，返回 (实际cwd, 清理后的prompt)
    例如 '@feishu 帮我改bot.py' → ('/home/wyd/daily/feishu', '帮我改bot.py')
    """
    import re
    match = re.match(r'^@(\S+)\s+', text)
    if match:
        project_name = match.group(1)
        if project_name in config.PROJECT_MAP:
            prompt = text[match.end():]
            return config.PROJECT_MAP[project_name], prompt
    return config.CLAUDE_PROJECT_DIR, text


def _run_claude(prompt: str, cwd: str, chat_id: str, session_id: str = None,
                skip_permissions: bool = True, _is_retry: bool = False) -> tuple[str, str]:
    """调用 Claude Code 并返回 (结果文本, session_id)"""
    cwd = cwd or config.CLAUDE_PROJECT_DIR
    mode = "auto" if skip_permissions else "safe"
    log.info(f"Calling Claude Code ({mode}) with prompt: {prompt[:100]}... (cwd={cwd}, session={session_id})")
    try:
        cmd = [
            "claude",
            "-p",
            "--output-format", "stream-json",
            "--verbose",
            "--max-turns", str(config.CLAUDE_MAX_TURNS),
        ]
        if skip_permissions:
            cmd.append("--dangerously-skip-permissions")
        if session_id:
            cmd.extend(["--resume", session_id])

        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=cwd,
        )
        proc.stdin.write(prompt)
        proc.stdin.close()

        result_text = ""
        tool_uses = []
        all_denials = []
        captured_session_id = None
        resume_failed = False
        last_progress_time = 0

        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)

                # 捕获 session_id
                if data.get("type") == "system" and data.get("subtype") == "init":
                    captured_session_id = data.get("session_id")
                    continue

                if data.get("type") == "assistant":
                    content = data.get("message", {}).get("content", [])
                    for c in content:
                        if c.get("type") == "tool_use":
                            tool_uses.append(f"🔧 {c.get('name','')}: {json.dumps(c.get('input',{}), ensure_ascii=False)[:80]}")
                        elif c.get("type") == "text":
                            result_text = c.get("text", "")
                elif data.get("type") == "result":
                    captured_session_id = data.get("session_id", captured_session_id)
                    result_text = data.get("result", result_text)
                    all_denials = data.get("permission_denials", [])
                    if data.get("subtype") == "error_during_execution":
                        errors = data.get("errors", [])
                        if any("No conversation found" in str(e) for e in errors):
                            resume_failed = True
            except json.JSONDecodeError:
                continue

            # 每 30 秒发送一次进度通知
            now = time.time()
            if chat_id and now - last_progress_time >= 30:
                progress_hint = ""
                if tool_uses:
                    last_tool = tool_uses[-1]
                    progress_hint = f"\n最近操作: {last_tool[:60]}"
                mode_label = "安全模式" if not skip_permissions else ""
                send_message(chat_id, f"⏳ 仍在执行中{mode_label}...{progress_hint}")
                last_progress_time = now

        # resume 失败则重试（不带 session）
        if resume_failed and not _is_retry:
            log.warning(f"Session {session_id} not found, retrying without resume")
            return _run_claude(prompt, cwd, chat_id, session_id=None,
                              skip_permissions=skip_permissions, _is_retry=True)

        # 等待进程结束
        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

        error = proc.stderr.read().strip() if proc.stderr else ""

        # 组装回复
        parts = []
        if tool_uses:
            parts.append("📋 执行的操作:\n" + "\n".join(tool_uses[:10]))
        if result_text:
            parts.append(result_text)
        if all_denials and not skip_permissions:
            denial_msgs = []
            for d in all_denials:
                denial_msgs.append(f"- {d.get('tool_name','?')}: {json.dumps(d.get('tool_input',{}), ensure_ascii=False)[:100]}")
            parts.append("⚠️ 以下操作因未授权被拒绝:\n" + "\n".join(denial_msgs))
            parts.append("💡 提示: 使用不带 /safe 的消息发送，可自动批准所有操作")

        if parts:
            return "\n\n".join(parts), captured_session_id

        if error:
            return f"执行出错:\n{error[:2000]}", captured_session_id
        return "Claude Code 未返回任何内容", captured_session_id
    except Exception as e:
        return f"调用 Claude Code 失败: {str(e)}", None


def call_claude(prompt: str, cwd: str = None, chat_id: str = None, session_id: str = None) -> tuple[str, str]:
    """调用 Claude Code（自动模式），返回 (结果文本, session_id)"""
    return _run_claude(prompt, cwd, chat_id, session_id, skip_permissions=True)


def call_claude_safe(prompt: str, cwd: str = None, chat_id: str = None, session_id: str = None) -> tuple[str, str]:
    """调用 Claude Code（安全模式），返回 (结果文本, session_id)"""
    return _run_claude(prompt, cwd, chat_id, session_id, skip_permissions=False)


def send_message(chat_id: str, text: str):
    """发送消息到飞书，超长文本自动分段"""
    max_len = 4000
    if len(text) <= max_len:
        _do_send_message(chat_id, text)
        return

    parts = []
    while text:
        if len(text) <= max_len:
            parts.append(text)
            break
        split_at = text.rfind("\n", 0, max_len)
        if split_at == -1:
            split_at = max_len
        parts.append(text[:split_at])
        text = text[split_at:].lstrip("\n")

    for i, part in enumerate(parts):
        header = f"[{i+1}/{len(parts)}]\n" if len(parts) > 1 else ""
        _do_send_message(chat_id, header + part)


def _do_send_message(chat_id: str, text: str):
    """实际发送消息到飞书"""
    try:
        content = json.dumps({"text": text})
        request = CreateMessageRequest.builder() \
            .receive_id_type("chat_id") \
            .request_body(CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("text")
                .content(content)
                .build()) \
            .build()

        response = client.im.v1.message.create(request)
        if not response.success():
            log.error(f"发送消息失败: code={response.code}, msg={response.msg}")
        else:
            log.info(f"发送消息成功: chat_id={chat_id}")
    except Exception as e:
        log.error(f"发送消息异常: {e}")


def on_message_receive(event: P2ImMessageReceiveV1) -> None:
    """处理接收到的飞书消息"""
    try:
        message = event.event.message
        sender = event.event.sender
        sender_id = sender.sender_id

        chat_id = message.chat_id
        user_id = sender_id.open_id
        msg_type = message.message_type

        log.info(f"Message from user={user_id}, chat={chat_id}, type={msg_type}")

        # 解析消息文本
        text = ""
        if msg_type == "text":
            try:
                content = json.loads(message.content)
                text = content.get("text", "").strip()
            except json.JSONDecodeError:
                pass

            if not text:
                return

            log.info(f"User open_id: {user_id}")

            # 判断是否安全模式
            safe_mode = text.startswith("/safe ")
            if safe_mode:
                text = text[6:].strip()

            # 异步处理文本消息
            thread = threading.Thread(
                target=handle_user_message,
                args=(chat_id, user_id, text, safe_mode),
            )
            thread.daemon = True
            thread.start()

        elif msg_type == "image":
            log.info(f"User open_id: {user_id}")
            thread = threading.Thread(
                target=handle_image_message,
                args=(chat_id, user_id, message),
            )
            thread.daemon = True
            thread.start()

        else:
            log.info(f"Unsupported message type: {msg_type}")

    except Exception as e:
        log.error(f"处理消息异常: {e}", exc_info=True)


def handle_user_message(chat_id: str, user_id: str, text: str, safe_mode: bool = False):
    """处理用户消息"""
    # 检查用户白名单
    if config.ALLOWED_USERS and user_id not in config.ALLOWED_USERS:
        send_message(chat_id, "抱歉，你没有使用权限。请在 config.py 中添加你的 open_id。")
        return

    # 处理命令（在 parse_project_dir 之前）
    stripped = text.strip()
    if stripped == "/new":
        session_store.remove(user_id)
        send_message(chat_id, "✅ 已清除所有会话，下次消息将开始新对话。")
        return
    if stripped.startswith("/new "):
        project_name = stripped[5:].strip().lstrip("@")
        if project_name in config.PROJECT_MAP:
            session_store.remove(user_id, config.PROJECT_MAP[project_name])
            send_message(chat_id, f"✅ 已清除 {project_name} 项目会话。")
        else:
            send_message(chat_id, f"未知项目名: {project_name}\n可用项目: {', '.join(config.PROJECT_MAP.keys())}")
        return
    if stripped == "/session":
        info = session_store.get_user_info(user_id)
        if not info:
            send_message(chat_id, "当前没有活跃会话。发送任意消息即可开始新对话。")
            return
        lines = ["📋 当前活跃会话:"]
        for cwd, data in info.items():
            lines.append(f"  {cwd}: session={data['session_id']} 消息数={data['messages']} 空闲={data['idle_hours']}h")
        lines.append(f"\nTTL: {config.SESSION_TTL_HOURS}h | 上限: {config.SESSION_MAX_MESSAGES} 条 | /new 清除")
        send_message(chat_id, "\n".join(lines))
        return

    # 解析 @项目名 前缀，缩小 cwd 范围
    cwd, text = parse_project_dir(text)

    # 获取用户锁，防止并发 resume
    user_lock = _get_user_lock(user_id)
    if not user_lock.acquire(timeout=300):
        send_message(chat_id, "❌ 你有一条消息正在执行中，请等它完成后再发新消息。")
        return

    try:
        # 查找已有会话
        existing_session = session_store.get(user_id, cwd)
        session_id = existing_session["session_id"] if existing_session else None

        mode_label = "🔒 安全模式" if safe_mode else "⚡ 自动模式"
        cwd_label = f"📁 {cwd}" if cwd != config.CLAUDE_PROJECT_DIR else ""
        session_label = f"📝 继续会话 {session_id[:8]}..." if session_id else "🆕 新会话"

        # 先回复"正在执行"
        header_parts = [f"⏳ {mode_label} 正在执行: {text[:50]}..."]
        if cwd_label:
            header_parts.append(cwd_label)
        header_parts.append(session_label)
        header_parts.append("请稍候，这可能需要几分钟。")
        send_message(chat_id, "\n".join(header_parts))

        # 调用 Claude Code
        if safe_mode:
            result, new_session_id = call_claude_safe(text, cwd=cwd, chat_id=chat_id, session_id=session_id)
        else:
            result, new_session_id = call_claude(text, cwd=cwd, chat_id=chat_id, session_id=session_id)

        # 更新会话存储
        if new_session_id:
            if session_id and session_id == new_session_id:
                session_store.increment_message_count(user_id, cwd)
            else:
                session_store.set(user_id, cwd, new_session_id)

        # 发送结果
        send_message(chat_id, result)
    finally:
        user_lock.release()


def handle_image_message(chat_id: str, user_id: str, message):
    """处理图片消息：下载保存到本地，回复保存路径"""
    # 检查用户白名单
    if config.ALLOWED_USERS and user_id not in config.ALLOWED_USERS:
        send_message(chat_id, "抱歉，你没有使用权限。")
        return

    try:
        content = json.loads(message.content)
        image_key = content.get("image_key", "")
        if not image_key:
            send_message(chat_id, "❌ 无法获取图片信息")
            return

        # 下载图片
        send_message(chat_id, "📥 正在下载图片...")

        req = GetImageRequest.builder().image_key(image_key).build()
        resp = client.im.v1.image.get_image(req)

        if not resp.success():
            send_message(chat_id, f"❌ 图片下载失败: {resp.msg}")
            return

        # 保存到临时目录
        save_dir = "/tmp/feishu_images"
        os.makedirs(save_dir, exist_ok=True)
        save_path = os.path.join(save_dir, f"{image_key}.jpg")

        with open(save_path, "wb") as f:
            f.write(resp.raw.content)

        log.info(f"Image saved to {save_path}")
        send_message(chat_id, f"✅ 图片已保存\n路径: {save_path}\n\n你可以后续消息中引用这个路径让 Claude Code 处理，例如:\n「分析图片 {save_path}」")

    except Exception as e:
        log.error(f"处理图片异常: {e}", exc_info=True)
        send_message(chat_id, f"❌ 处理图片失败: {str(e)}")


@app.post("/webhook")
async def webhook(request: Request):
    """处理飞书事件推送"""
    body = await request.body()
    body_str = body.decode("utf-8")
    headers_dict = dict(request.headers)

    log.info(f"Received webhook: {body_str[:300]}")

    try:
        # 手动处理 URL 验证握手
        try:
            data = json.loads(body_str)
            if data.get("type") == "url_verification":
                challenge = data.get("challenge", "")
                log.info(f"URL verification challenge: {challenge}")
                return {"challenge": challenge}
        except json.JSONDecodeError:
            pass

        # 使用 lark-oapi SDK 处理事件（自动解密 + 验签）
        from lark_oapi.core.model.raw_request import RawRequest
        req = RawRequest()
        # FastAPI headers key 全小写，SDK 期望 X-Lark-xxx 格式，需要转换
        normalized_headers = {}
        for k, v in headers_dict.items():
            parts = k.split('-')
            normalized = '-'.join(p.capitalize() for p in parts)
            normalized_headers[normalized] = v
        req.headers = normalized_headers
        req.body = body
        req.uri = "/webhook"
        resp = event_handler.do(req)
        log.info(f"Event handler response: code={resp.code if hasattr(resp, 'code') else 'N/A'}")

    except Exception as e:
        log.error(f"Webhook处理异常: {e}", exc_info=True)

    return {"code": 0}


@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    log.info(f"Starting Feishu Bot on port {config.PORT}")
    log.info(f"Claude Code project dir: {config.CLAUDE_PROJECT_DIR}")
    log.info(f"Claude Code max turns: {config.CLAUDE_MAX_TURNS}")
    log.info(f"Project map: {list(config.PROJECT_MAP.keys())}")
    log.info(f"Allowed users: {config.ALLOWED_USERS or 'all'}")
    log.info(f"Session store: {config.SESSION_STORE_PATH} (TTL={config.SESSION_TTL_HOURS}h, max={config.SESSION_MAX_MESSAGES})")

    uvicorn.run(app, host="0.0.0.0", port=config.PORT)
