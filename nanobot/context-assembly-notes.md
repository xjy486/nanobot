# nanobot 上下文组装链路笔记

> 本文档按时间顺序梳理一条用户消息从进入系统到最终发给 LLM provider 的完整上下文组装流程，并摘录核心代码。

---

## 1. 顶层数据流

```
[Channel: 飞书/钉钉/CLI/Slack/...]
    └─> InboundMessage  (bus.publish_inbound)
            │
            ▼
   AgentLoop.run()              loop.py:652
       ├─ 命令路由 (/stop, slash 等)
       ├─ pending_queue 中途注入判定
       └─ asyncio.create_task(_dispatch)
            │
            ▼
   AgentLoop._dispatch()        loop.py:727
       ├─ session_lock (每会话串行)
       ├─ 并发信号量
       └─ pending Queue 注册 (mid-turn injection)
            │
            ▼
   AgentLoop._process_message() loop.py:861
       1. _refresh_provider_snapshot
       2. extract_documents (抽出 doc/image)
       3. session = sessions.get_or_create(key)
       4. _restore_runtime_checkpoint / _restore_pending_user_turn (崩溃恢复)
       5. auto_compact.prepare_session => (session, summary) 摘要注入
       6. 命令路由 (slash command)
       7. consolidator.maybe_consolidate_by_tokens (token 超预算才归档)
       8. _set_tool_context (给 tool 注入 channel/chat_id/session_key)
       9. session.get_history(max_messages, max_tokens, include_timestamps)
      10. pending_ask_id 分支 OR ContextBuilder.build_messages(...)
      11. _save_turn (持久化 user 消息)
      12. AgentLoop._run_agent_loop
            │
            ▼
   AgentRunner.run(AgentRunSpec) runner.py:231
       loop iteration:
         - _drop_orphan_tool_results
         - _backfill_missing_tool_results
         - _microcompact (老旧工具结果摘要)
         - _apply_tool_result_budget
         - _snip_history (token 超预算硬切)
         - _drop_orphan_tool_results (再次)
         - _backfill_missing_tool_results (再次)
         => messages_for_model
       _request_model => provider.chat_with_retry(messages, tools)
            │
            ▼
   AnthropicProvider.chat()     anthropic_provider.py:540
       _build_kwargs:
         _sanitize_empty_content
         _convert_messages          (OpenAI shape -> Anthropic Messages API)
         _convert_tools             (function tools schema)
         _apply_cache_control       (prompt caching markers)
         thinking / temperature 处理
       => self._client.messages.create(**kwargs)
```

---

## 2. 入口与预处理

### 2.1 真实入口

- **统一异步循环**: [`AgentLoop.run()`](nanobot/agent/loop.py#L652)
  - 从 `MessageBus.consume_inbound()` 拉取 `InboundMessage`（每 1s 超时一次，期间触发 `auto_compact.check_expired`）。
- **直接调用入口**（供 CLI/HTTP 直接调用）: [`AgentLoop.process_direct()`](nanobot/agent/loop.py#L1333)
- **InboundMessage** 数据形状: [`nanobot/bus/events.py:8`](nanobot/bus/events.py#L8)
  - 字段: `channel, sender_id, chat_id, content, media[], metadata{}, session_key_override`
  - 属性: `session_key = session_key_override or f"{channel}:{chat_id}"`

### 2.2 会话级并发控制

每个 `session_key` 对应:

- `_session_locks[key]` — 同一 session 串行 ([`loop.py:732`](nanobot/agent/loop.py#L732))
- `_active_tasks[key]` — 已在跑的 task 列表（支持 `/stop`）
- `_pending_queues[key]` — 中途消息注入队列（[`loop.py:737`](nanobot/agent/loop.py#L737）)

**中途注入**: 在一个 session 的某次回合处理过程中又来了同一 session 的新消息 → 直接放入 `pending_queues[key]`，而**不**新建 task。这是 mid-turn injection 的关键。

**统一 session 模式**: `_unified_session=True` 时所有 channel/chat 都用 `UNIFIED_SESSION_KEY = "unified:default"`（[`loop.py:63`](nanobot/agent/loop.py#L63)、[`loop.py:496`](nanobot/agent/loop.py#L496)）。

### 2.3 `_process_message` 完整职责链

`_process_message` 是**编排器（orchestrator）**，上下文拼装只是其中一环。完整链路如下（[`loop.py:861`](nanobot/agent/loop.py#L861)）：

```
_process_message(msg)
  │
  ├─ 1. 前置：刷新 & 媒体提取
  │     ├─ _refresh_provider_snapshot()           刷新 provider/config 快照
  │     └─ extract_documents(content, media)       分离文档文本与图片 base64
  │
  ├─ 2. 会话恢复 & 维护
  │     ├─ sessions.get_or_create(key)             取/创建 session
  │     ├─ _restore_runtime_checkpoint(session)    崩溃恢复：把上次 crash 时未完成的
  │     │                                           assistant + tool results 还原到 messages
  │     ├─ _restore_pending_user_turn(session)     只持久化了 user 但没 assistant 回复的 →
  │     │                                          补一条中断标记
  │     ├─ auto_compact.prepare_session(session)   空闲会话归档摘要注入，
  │     │                                          返回 (session, pending_summary)
  │     └─ consolidator.maybe_consolidate_by_tokens(token 超预算才沿 user-turn 切 chunk
  │         session, session_summary=pending)       → LLM 摘要 → 写 history.jsonl
  │
  ├─ 3. 命令拦截（slash command）
  │     └─ commands.dispatch(ctx)                  命中就 return OutboundMessage，后续全跳过
  │
  ├─ 4. 上下文拼装  ← 重点
  │     ├─ _set_tool_context(channel, chat_id, ...)给工具注入 channel/chat_id/session_key
  │     ├─ session.get_history(max_messages,        取历史消息（三层裁剪 + 边界修复）
  │     │                       max_tokens, ...)
  │     └─ 分支判断:
  │         ├─ pending_ask_user? → ask_user_tool_result_messages(...)
  │         │                     用户回复作为 tool_result 喂回，不是普通 user 消息
  │         └─ 否则 → context.build_messages(
  │                        history=history,
  │                        current_message=msg.content,
  │                        session_summary=pending,
  │                        media=..., channel=..., chat_id=...)
  │                    → [system, ...history, user_with_runtime_ctx]
  │
  ├─ 5. 提前持久化 user 消息（防 crash 丢失）
  │     └─ session.add_message("user", text)
  │        _mark_pending_user_turn(session)
  │        sessions.save(session)
  │
  ├─ 6. 执行 Agent 循环
  │     └─ _run_agent_loop(initial_messages,         → AgentRunner.run(AgentRunSpec(
  │                         on_progress,                    initial_messages,
  │                         on_stream,                      tools=self.tools,
  │                         on_stream_end,                  model=self.model,
  │                         session,                        hook=...,
  │                         pending_queue))                 injection_callback=_drain_pending))
  │
  └─ 7. 后置处理
        ├─ _save_turn(session, all_msgs, skip)      把本轮对话增量写入 session（跳过已持久化的）
        ├─ session.enforce_file_cap()               消息数 > 2000 → 归档溢出部分
        ├─ _clear_pending_user_turn(session)        清除 pending 标记
        ├─ _clear_runtime_checkpoint(session)       清除 runtime checkpoint
        ├─ sessions.save(session)                   持久化 session
        ├─ _schedule_background(                    后台触发记忆整合
        │   consolidator.maybe_consolidate_by_tokens)
        ├─ ask_user 按钮处理                         如果 stop_reason == "ask_user" → 生成按钮
        └─ 返回 OutboundMessage(channel, chat_id, content, buttons, metadata)
```

**一句话总结**：`_process_message` 做的是把"上下文拼装"作为输入，喂给 runner 执行 ReAct 循环，再把结果持久化并返回 OutboundMessage 到消息总线。上下文拼装只是第 4 步，真正负责拼装的是 `ContextBuilder`。

### 2.4 进入 `_process_message` 前的预处理

```python
# loop.py:861
async def _process_message(self, msg: InboundMessage, ...) -> OutboundMessage | None:
    self._refresh_provider_snapshot()
    if msg.channel == "system":
        # subagent 回调走 system 分支
        ...
    # 崩溃恢复：把上一次跑到一半但 crash 的 in-flight assistant + tool results 塞回 session.messages
    if self._restore_runtime_checkpoint(session):
        self.sessions.save(session)
    # 只持久化了 user 但没 assistant 回复的会话，补一句中断标记
    if self._restore_pending_user_turn(session):
        self.sessions.save(session)
    # 空闲会话归档摘要注入
    session, pending = self.auto_compact.prepare_session(session, key)
    # Token 超预算时，沿 user-turn 边界切 chunk -> LLM 摘要 -> history.jsonl
    await self.consolidator.maybe_consolidate_by_tokens(session, session_summary=pending)
```

---

## 3. system prompt 是怎么拼起来的

入口: [`ContextBuilder.build_system_prompt()`](nanobot/agent/context.py#L31)

**七大段**（用 `\n\n---\n\n` 拼接）：

```python
# context.py:31
def build_system_prompt(self, skill_names=None, channel=None) -> str:
    parts = [self._get_identity(channel=channel)]

    bootstrap = self._load_bootstrap_files()
    if bootstrap:
        parts.append(bootstrap)

    memory = self.memory.get_memory_context()
    if memory and not self._is_template_content(self.memory.read_memory(), "memory/MEMORY.md"):
        parts.append(f"# Memory\n\n{memory}")

    always_skills = self.skills.get_always_skills()
    if always_skills:
        always_content = self.skills.load_skills_for_context(always_skills)
        if always_content:
            parts.append(f"# Active Skills\n\n{always_content}")

    skills_summary = self.skills.build_skills_summary(exclude=set(always_skills))
    if skills_summary:
        parts.append(render_template("agent/skills_section.md", skills_summary=skills_summary))

    entries = self.memory.read_unprocessed_history(since_cursor=self.memory.get_last_dream_cursor())
    if entries:
        capped = entries[-self._MAX_RECENT_HISTORY:]
        history_text = "\n".join(f"- [{e['timestamp']}] {e['content']}" for e in capped)
        history_text = truncate_text(history_text, self._MAX_HISTORY_CHARS)
        parts.append("# Recent History\n\n" + history_text)

    return "\n\n---\n\n".join(parts)
```

| 段                  | 来源                                                                                          | 关键文件                                        |
| ------------------- | --------------------------------------------------------------------------------------------- | ----------------------------------------------- |
| 1. Identity         | `templates/agent/identity.md` 渲染，带 OS/Python 版本、`platform_policy.md`、channel 格式提示 | [context.py:68](nanobot/agent/context.py#L68)   |
| 2. Bootstrap files  | workspace 下的 `AGENTS.md, SOUL.md, USER.md, TOOLS.md`（用户可自定义）                        | [context.py:109](nanobot/agent/context.py#L109) |
| 3. Long-term Memory | `<workspace>/memory/MEMORY.md`（若不是模板原样）                                              | [memory.py:221](nanobot/agent/memory.py#L221)   |
| 4. Always-on skills | frontmatter 里 `nanobot.always: true` 且依赖满足的 SKILL.md 全文                              | [skills.py](nanobot/agent/skills.py)            |
| 5. Skills summary   | 其余 skills 的一行简介 + 路径（指引模型用 `read_file` 渐进加载）                              | `templates/agent/skills_section.md`             |
| 6. Recent History   | `memory/history.jsonl` 中 `last_dream_cursor` 之后的 ≤50 条 / ≤32k 字符                       | [context.py:57](nanobot/agent/context.py#L57)   |

> 注意：**tool 定义不在 system prompt 里**，而是通过 `chat()` 的 `tools=` 参数单独传入（[`runner.py:609`](nanobot/agent/runner.py#L609)），格式由 provider 转换。

---

## 4. messages 列表是怎么交织起来的

入口: [`ContextBuilder.build_messages()`](nanobot/agent/context.py#L132)

```python
# context.py:132
def build_messages(self, history, current_message, skill_names=None, media=None,
                   channel=None, chat_id=None, current_role="user", session_summary=None):
    runtime_ctx = self._build_runtime_context(channel, chat_id, self.timezone, session_summary=session_summary)
    user_content = self._build_user_content(current_message, media)

    # 合并 runtime context 和 user content 为单条 user message
    # 避免连续同 role 导致 provider 拒绝
    if isinstance(user_content, str):
        merged = f"{runtime_ctx}\n\n{user_content}"
    else:
        merged = [{"type": "text", "text": runtime_ctx}] + user_content

    messages = [
        {"role": "system", "content": self.build_system_prompt(skill_names, channel=channel)},
        *history,
    ]
    # 如果最后一条 role 相同，合并 content
    if messages[-1].get("role") == current_role:
        last = dict(messages[-1])
        last["content"] = self._merge_message_content(last.get("content"), merged)
        messages[-1] = last
        return messages
    messages.append({"role": current_role, "content": merged})
    return messages
```

### 4.1 `build_messages` 核心逻辑解析

`build_messages` 是上下文组装的核心方法，职责是把 system prompt、历史消息、当前输入拼成一条合法的 messages 列表准备喂给 LLM。方法签名：

```
build_messages(
    history: list[dict],          # 来自 session.get_history()，已裁剪
    current_message: str,         # 当前用户消息（可能为空字符串）
    current_role: str = "user",   # 当前消息的角色
    session_summary: str = None,  # 空闲归档摘要
    channel: str = None,
    chat_id: str = None,
    ...
) -> list[dict]
```

方法分四步执行：

**第一步：构建 `runtime_ctx`（运行时上下文）**

调用 `_build_runtime_context(channel, chat_id, timezone, session_summary)`，产出用 `<runtime_context>` 标签包裹的元数据块（当前时间、channel、chat_id、可选 `[Resumed Session]` 摘要）。详见 §4.2。

**第二步：构建 `user_content`（用户内容块）**

调用 `_build_user_content(current_message, media)`。若有图片附件，转为 base64 `image_url` block 列表；否则就是纯文本字符串。详见 §4.3。

**第三步：合并 `runtime_ctx` 与 `user_content`**

```python
# 都是字符串：直接拼接
if isinstance(user_content, str):
    merged = f"{runtime_ctx}\n\n{user_content}"
# user_content 是图片 block 列表：runtime_ctx 作为第一个 text block 插入
else:
    merged = [{"type": "text", "text": runtime_ctx}] + user_content
```

这是**第一次合并**——把运行时元数据和用户消息合为一条。目的是避免连续出现两条 user-role 消息（一条 runtime_ctx、一条用户正文），因为 Anthropic API 禁止连续同 role。

**第四步：拼装最终 messages 并做角色冲突检测**

```python
messages = [
    {"role": "system", "content": self.build_system_prompt(...)},
    *history,
]
# 关键判断：最后一条历史消息的角色 == 当前消息的角色？
if messages[-1].get("role") == current_role:
    # 冲突 → 合并内容到上一条，不新增消息
    last = dict(messages[-1])
    last["content"] = self._merge_message_content(last.get("content"), merged)
    messages[-1] = last
    return messages
# 不冲突 → 正常追加
messages.append({"role": current_role, "content": merged})
return messages
```

这是**第二次合并**（边界合并）。它只检查 history 的最后一条是否与 `current_role` 冲突，如果冲突就把内容合并，避免形成连续同 role。

不同场景下的行为：

| 场景                         | `current_role` | history 末尾 role                  | 行为                                          |
| ---------------------------- | -------------- | ---------------------------------- | --------------------------------------------- |
| 用户发新消息                 | `"user"`       | `"assistant"`                      | 不冲突 → 追加新 user 消息                     |
| `pending_ask_user`           | `"user"`       | `"user"`（上一条也是 tool_result） | 冲突 → 合并到上一条 user                      |
| subagent 结果回传            | `"assistant"`  | `"assistant"`（子 agent 结果）     | 冲突 → 把 runtime_ctx 合并到子 agent 消息末尾 |
| `_try_drain_injections` 注入 | `"user"`       | `"user"`                           | 冲突 → 合并注入消息到上一条 user              |

> **注意**：这里的角色合并只是**边界合并**（只检查 `messages[-1]`）。历史内部可能已经存在连续同 role 消息（例如子 agent 结果持久化后，`session.messages` 中形成 `assistant → assistant`），`build_messages` 不处理这种内部连续。最终由 Provider 层的 `_merge_consecutive` 做全面扫描合并（见 §7 的 `_convert_messages` 和 §8.2.3）。

### 4.2 `runtime_ctx`（运行时元数据）

```python
# context.py:82
@staticmethod
def _build_runtime_context(channel, chat_id, timezone=None, session_summary=None) -> str:
    lines = [f"Current Time: {current_time_str(timezone)}"]
    if channel and chat_id:
        lines += [f"Channel: {channel}", f"Chat ID: {chat_id}"]
    if session_summary:
        lines += ["", "[Resumed Session]", session_summary]
    return ContextBuilder._RUNTIME_CONTEXT_TAG + "\n" + "\n".join(lines) + "\n" + ContextBuilder._RUNTIME_CONTEXT_END
```

这个块**只在喂给 LLM 那一刻拼接**，保存到 session 时由 [`_save_turn`](nanobot/agent/loop.py#L1181) 剥掉（搜索 `_RUNTIME_CONTEXT_TAG` 并删除），不污染历史。

### 4.3 `current_message` + `media`

```python
# context.py:165
def _build_user_content(self, text: str, media: list[str] | None):
    if not media:
        return text
    images = []
    for path in media:
        raw = Path(path).read_bytes()
        mime = detect_image_mime(raw) or mimetypes.guess_type(path)[0]
        if mime and mime.startswith("image/"):
            b64 = base64.b64encode(raw).decode()
            images.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"},
                "_meta": {"path": str(path)},
            })
    return images + [{"type": "text", "text": text}]
```

### 4.4 history 来自哪里

[`Session.get_history()`](nanobot/session/manager.py#L73) — 分两阶段，5 步处理：

**阶段 A：消息数裁剪 + 边界修复（不改内容）**

```python
# session/manager.py:73
def get_history(self, max_messages=120, *, max_tokens=0, include_timestamps=False):
    unconsolidated = self.messages[self.last_consolidated:]      # 1. 排除已归档
    max_messages = max_messages if max_messages > 0 else 120
    sliced = unconsolidated[-max_messages:]                      # 2. 消息数硬上限（取尾）

    # 3. 对齐到第一个 user（若前一条是 _channel_delivery=True 也保留）
    for i, message in enumerate(sliced):
        if message.get("role") == "user":
            start = i
            if i > 0 and sliced[i - 1].get("_channel_delivery"):
                start = i - 1
            sliced = sliced[start:]
            break

    # 4. 跳过开头孤儿 tool 消息（没有前置 assistant tool_call 的 tool 结果）
    start = find_legal_message_start(sliced)
    if start:
        sliced = sliced[start:]
```

**阶段 B：构建输出 + token 预算裁剪**

```python
    # 5. 正序遍历构建 out 列表（加 image 占位符 + 时间戳）
    out = []
    for message in sliced:                                # 正序，只做字段转换
        content = message.get("content", "")
        media = message.get("media")
        if isinstance(media, list) and media and isinstance(content, str):
            breadcrumbs = "\n".join(image_placeholder_text(p) for p in media)
            content = f"{content}\n{breadcrumbs}" if content else breadcrumbs
        if include_timestamps:
            content = self._annotate_message_time(message, content)
        out.append({"role": message["role"], "content": content, ...})

    # 6. 真正的 token 预算裁剪：从尾往前累加，超预算丢弃
    if max_tokens > 0 and out:
        kept = []
        used = 0
        for message in reversed(out):                     # ← 倒序，从尾到头
            tokens = estimate_message_tokens(message)
            if kept and used + tokens > max_tokens:
                break                                     # 超预算就停
            kept.append(message)
            used += tokens
        kept.reverse()                                    # 再正回来

        # 对齐到第一个 user（token 裁剪可能切出无 user 的 assistant 尾巴）
        first_user = next((i for i, m in enumerate(kept)
                          if m.get("role") == "user"), None)
        if first_user is not None:
            kept = kept[first_user:]
        # 边界修复：跳过开头孤儿 tool 消息
        start = find_legal_message_start(kept)
        if start:
            kept = kept[start:]
        return kept

    return out
```

> **关键点**：步骤 5 的正向循环只是做字段转换（image 占位符、时间戳），不是裁剪。真正的 token 裁剪在步骤 6，**倒序遍历** `reversed(out)`，从尾部往前保留，逼近 `max_tokens` 就停。这样做是为了保留**最近的消息**（尾部 = 最新），丢弃的是较早的历史。

调用方 `_replay_token_budget` = `context_window_tokens - max_output - 1024`，默认下限 `context_window_tokens // 2`。

每条 history 消息会经过 [`_annotate_message_time`](nanobot/session/manager.py#L36) 在 user / `_channel_delivery=True` 的 assistant 内容前加 `[Message Time: <ISO>]\n`。

### 4.5 特殊分支

- **`pending_ask_user`**: 上次有未关闭的 `ask_user` tool_call → 用户回复被当成 tool result 喂回（[`tools/ask.py:95`](nanobot/agent/tools/ask.py#L95)）。
- **subagent 回调** (`channel == "system"`): 结果作为 `assistant`（`injected_event="subagent_result"`）持久化到 session，然后以 `current_role="assistant"`、`current_message=""` 走 `build_messages`，让主模型对结果做后续处理（[`loop.py:1208`](nanobot/agent/loop.py#L1208)）。由于会产生连续 assistant 消息，由 `build_messages` 边界合并 + Provider 层 `_merge_consecutive` 全面合并兜底。详见 §8.2。

---

## 5. Memory 模块在哪些点注入

### 5.1 `MemoryStore`（纯 IO 层）

- 文件: `memory/MEMORY.md`、`memory/history.jsonl`、`SOUL.md`、`USER.md`
- 支持从老的 `HISTORY.md` 自动迁移到 `history.jsonl`
- `append_history()` 按 cursor 自增，`_HISTORY_ENTRY_HARD_CAP=64_000`
- **GitStore** 跟踪 `SOUL.md/USER.md/MEMORY.md`，`Dream` 完成后会 `auto_commit`

注入 system prompt 的两条路径（已在 §3 说明）：

- **MEMORY.md** 整体作为长期记忆区
- **history.jsonl** 中 dream cursor 之后的条目 → "Recent History" 区

### 5.2 `Consolidator`（轻量在线归档）

- 入口: `maybe_consolidate_by_tokens(session, session_summary)`（[`memory.py:589`](nanobot/agent/memory.py#L589)）
- **触发条件**: `estimate_session_prompt_tokens() >= _input_token_budget`（`context_window_tokens - max_completion_tokens - 1024`）
- **目标**: 压到 `budget * consolidation_ratio`（默认 0.5）
- **方式**: 沿 user-turn 边界切 chunk → LLM 摘要 → `append_history(summary)` 写到 `history.jsonl`，推进 `session.last_consolidated`
- 摘要写到 `session.metadata["_last_summary"]`，下次 `auto_compact.prepare_session` 注入到 runtime_ctx

### 5.3 `AutoCompact`（空闲会话归档）

- 时机: `AgentLoop.run()` 每次 1s 超时 → `auto_compact.check_expired`（[`loop.py:662`](nanobot/agent/loop.py#L662)）
- 仅当 `session_ttl_minutes > 0` 且 `now - updated_at >= ttl` 且 session 当前**无活跃任务**（不在 `_pending_queues` 中）
- 行为: 把 `tail` 切成 `archive_msgs + kept_msgs`（最近 8 条 legal suffix），LLM 摘要写 `history.jsonl`，kept 留下，摘要塞 `metadata["_last_summary"]`
- 下次该 session 进入 `_process_message` 时，`auto_compact.prepare_session` 把 `_last_summary` 取出，通过 `build_messages(session_summary=...)` 注入到 runtime_ctx 的 `[Resumed Session]` 段

### 5.4 `Dream`（后台周期性记忆整理）

- 由 cron 触发，**两阶段** LLM 调用：
  - **Phase 1**: plain LLM call（无 tools），输入 = 最近 `history.jsonl` + MEMORY.md/SOUL.md/USER.md（MEMORY.md 经 git blame 加老化标注）+ system 模板 `templates/agent/dream_phase1.md`，产出 analysis 文本。
  - **Phase 2**: 走 AgentRunner，装备 `read_file/edit_file/write_file` tools，模板 `agent/dream_phase2.md`，可对 MEMORY.md/SOUL.md/USER.md 做精确局部编辑、可创建 skills。
- 完成后 `set_last_dream_cursor`（让 Recent History 段不再重复包含已被消化的条目）+ git auto_commit。

---

## 6. Runner 内部对 messages 的"最后一道加工"

[`AgentRunner.run()`](nanobot/agent/runner.py#L231) — 每个 iteration 之前对 `messages` 做 6 步加工，产出 `messages_for_model`（**不改原** `messages`）：

```python
# runner.py:246
for iteration in range(spec.max_iterations):
    messages_for_model = self._drop_orphan_tool_results(messages)
    messages_for_model = self._backfill_missing_tool_results(messages_for_model)
    messages_for_model = self._microcompact(messages_for_model)
    messages_for_model = self._apply_tool_result_budget(spec, messages_for_model)
    messages_for_model = self._snip_history(spec, messages_for_model)
    # Snipping 可能造成新孤儿，再清理一次
    messages_for_model = self._drop_orphan_tool_results(messages_for_model)
    messages_for_model = self._backfill_missing_tool_results(messages_for_model)

    response = await self._request_model(spec, messages_for_model, ...)
```

### 6.1 `_drop_orphan_tool_results`

删除前面没有匹配 assistant `tool_call` 的 tool 消息。

```python
# runner.py:911
@staticmethod
def _drop_orphan_tool_results(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    declared: set[str] = set()
    updated: list[dict[str, Any]] | None = None
    for idx, msg in enumerate(messages):
        role = msg.get("role")
        if role == "assistant":
            for tc in msg.get("tool_calls") or []:
                if isinstance(tc, dict) and tc.get("id"):
                    declared.add(str(tc["id"]))
        if role == "tool":
            tid = msg.get("tool_call_id")
            if tid and str(tid) not in declared:
                if updated is None:
                    updated = [dict(m) for m in messages[:idx]]
                continue
        if updated is not None:
            updated.append(dict(msg))
    return updated if updated is not None else messages
```

### 6.2 `_backfill_missing_tool_results`

为缺结果的 assistant `tool_use` 补一条占位错误。

```python
# runner.py:937
_BACKFILL_CONTENT = "[Tool result unavailable — call was interrupted or lost]"

@staticmethod
def _backfill_missing_tool_results(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    declared: list[tuple[int, str, str]] = []  # (assistant_idx, call_id, name)
    fulfilled: set[str] = set()
    for idx, msg in enumerate(messages):
        role = msg.get("role")
        if role == "assistant":
            for tc in msg.get("tool_calls") or []:
                if isinstance(tc, dict) and tc.get("id"):
                    name = tc.get("function", {}).get("name", "")
                    declared.append((idx, str(tc["id"]), name))
        elif role == "tool":
            tid = msg.get("tool_call_id")
            if tid:
                fulfilled.add(str(tid))

    missing = [(ai, cid, name) for ai, cid, name in declared if cid not in fulfilled]
    if not missing:
        return messages

    updated = list(messages)
    offset = 0
    for assistant_idx, call_id, name in missing:
        insert_at = assistant_idx + 1 + offset
        while insert_at < len(updated) and updated[insert_at].get("role") == "tool":
            insert_at += 1
        updated.insert(insert_at, {
            "role": "tool",
            "tool_call_id": call_id,
            "name": name,
            "content": _BACKFILL_CONTENT,
        })
        offset += 1
    return updated
```

### 6.3 `_microcompact`

对可压缩工具的旧结果做摘要替换。

```python
# runner.py:978
_COMPACTABLE_TOOLS = {"read_file", "exec", "grep", "glob", "web_search", "web_fetch", "list_dir"}
_MICROCOMPACT_KEEP_RECENT = 10
_MICROCOMPACT_MIN_CHARS = 500

@staticmethod
def _microcompact(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compactable_indices: list[int] = []
    for idx, msg in enumerate(messages):
        if msg.get("role") == "tool" and msg.get("name") in _COMPACTABLE_TOOLS:
            compactable_indices.append(idx)

    if len(compactable_indices) <= _MICROCOMPACT_KEEP_RECENT:
        return messages

    stale = compactable_indices[: len(compactable_indices) - _MICROCOMPACT_KEEP_RECENT]
    updated: list[dict[str, Any]] | None = None
    for idx in stale:
        msg = messages[idx]
        content = msg.get("content")
        if not isinstance(content, str) or len(content) < _MICROCOMPACT_MIN_CHARS:
            continue
        name = msg.get("name", "tool")
        summary = f"[{name} result omitted from context]"
        if updated is None:
            updated = [dict(m) for m in messages]
        updated[idx]["content"] = summary

    return updated if updated is not None else messages
```

### 6.4 `_apply_tool_result_budget`

对每条 tool 消息：超长结果落盘替换为引用 + 截断到 `max_tool_result_chars`。

```python
# runner.py:1004
def _apply_tool_result_budget(self, spec, messages):
    updated = messages
    for idx, message in enumerate(messages):
        if message.get("role") != "tool":
            continue
        normalized = self._normalize_tool_result(
            spec,
            str(message.get("tool_call_id") or f"tool_{idx}"),
            str(message.get("name") or "tool"),
            message.get("content"),
        )
        if normalized != message.get("content"):
            if updated is messages:
                updated = [dict(m) for m in messages]
            updated[idx]["content"] = normalized
    return updated
```

### 6.5 `_snip_history`

Token 超预算时从尾部硬切。

```python
# runner.py:1025
def _snip_history(self, spec, messages):
    if not messages or not spec.context_window_tokens:
        return messages

    provider_max_tokens = getattr(getattr(self.provider, "generation", None), "max_tokens", 4096)
    max_output = spec.max_tokens if isinstance(spec.max_tokens, int) else provider_max_tokens
    budget = spec.context_block_limit or (spec.context_window_tokens - max_output - _SNIP_SAFETY_BUFFER)
    if budget <= 0:
        return messages

    estimate, _ = estimate_prompt_tokens_chain(self.provider, spec.model, messages, spec.tools.get_definitions())
    if estimate <= budget:
        return messages

    system_messages = [dict(msg) for msg in messages if msg.get("role") == "system"]
    non_system = [dict(msg) for msg in messages if msg.get("role") != "system"]

    system_tokens = sum(estimate_message_tokens(msg) for msg in system_messages)
    remaining_budget = max(128, budget - system_tokens)

    # 从尾部往前保留，直到逼近 budget
    kept: list[dict[str, Any]] = []
    kept_tokens = 0
    for message in reversed(non_system):
        msg_tokens = estimate_message_tokens(message)
        if kept and kept_tokens + msg_tokens > remaining_budget:
            break
        kept.append(message)
        kept_tokens += msg_tokens
    kept.reverse()

    # 对齐到首个 user（GLM 1214 错）
    if kept:
        for i, message in enumerate(kept):
            if message.get("role") == "user":
                kept = kept[i:]
                break
        start = find_legal_message_start(kept)
        if start:
            kept = kept[start:]

    return system_messages + kept
```

### 6.6 工具调用与 mid-turn 注入

```python
# runner.py:281
if response.should_execute_tools:
    tool_calls = list(response.tool_calls)
    # ask_user 若存在，截断只保留到 ask_user 为止
    ask_index = next((i for i, tc in enumerate(tool_calls) if tc.name == "ask_user"), None)
    if ask_index is not None:
        tool_calls = tool_calls[: ask_index + 1]

    assistant_message = build_assistant_message(
        response.content or "",
        tool_calls=[tc.to_openai_tool_call() for tc in tool_calls],
        reasoning_content=response.reasoning_content,
        thinking_blocks=response.thinking_blocks,
    )
    messages.append(assistant_message)

    # 工具执行（支持并发 batch）
    tools_used.extend(tc.name for tc in tool_calls)
    await self._emit_checkpoint(spec, ...)

    # mid-turn injection：工具执行后、最终回复前，检查 pending_queue
    injected = await self._try_drain_injections(spec, messages)
    if injected:
        had_injections = True
        continue  # 继续下一轮 LLM 调用
```

---

## 7. Provider 层最后一道转换（以 Anthropic 为例）

[`AnthropicProvider.chat()`](nanobot/providers/anthropic_provider.py#L540)

```python
# anthropic_provider.py:416
def _build_kwargs(self, messages, tools, model, max_tokens, temperature, reasoning_effort, tool_choice, supports_caching=True):
    model_name = self._strip_prefix(model or self.default_model)
    system, anthropic_msgs = self._convert_messages(self._sanitize_empty_content(messages))
    anthropic_tools = self._convert_tools(tools)

    if supports_caching:
        system, anthropic_msgs, anthropic_tools = self._apply_cache_control(
            system, anthropic_msgs, anthropic_tools,
        )

    kwargs = {
        "model": model_name,
        "messages": anthropic_msgs,
        "max_tokens": max_tokens,
    }
    if system:
        kwargs["system"] = system

    # thinking / temperature
    if reasoning_effort == "adaptive":
        kwargs["thinking"] = {"type": "adaptive"}
        kwargs["temperature"] = 1.0
    elif reasoning_effort and reasoning_effort.lower() != "none":
        budget_map = {"low": 1024, "medium": 4096, "high": max(8192, max_tokens)}
        budget = budget_map.get(reasoning_effort.lower(), 4096)
        kwargs["thinking"] = {"type": "enabled", "budget_tokens": budget}
        kwargs["max_tokens"] = max(max_tokens, budget + 4096)
        kwargs["temperature"] = 1.0
    else:
        kwargs["temperature"] = temperature

    if anthropic_tools:
        kwargs["tools"] = anthropic_tools
        tc = self._convert_tool_choice(tool_choice, bool(reasoning_effort))
        if tc:
            kwargs["tool_choice"] = tc

    return kwargs
```

转换细节：

1. **`_sanitize_empty_content`**: 修空字符串/空 block，剥 `_meta`
2. **`_convert_messages`**: OpenAI 形态 → Anthropic Messages API
   - `system` 消息抽出来（Anthropic 的 system 是顶层参数）
   - `tool` role → `{type:"tool_result", tool_use_id, content}`，合入前一条 user
   - `assistant.tool_calls` → `{type:"tool_use", id, name, input}` block
   - `thinking_blocks` → `{type:"thinking", ...}`
   - `user` 内容里 `image_url` → `{type:"image", source:{type:"base64", ...}}`
   - **`_merge_consecutive`**：合并连续同 role，strip 末尾 assistant，首条若是 assistant 则插一条 `(conversation continued)` 的 user。这是 subagent 回传产生连续 assistant 的最终兜底方案，详见 §8.2.3。
3. **`_convert_tools`**: `{name, description, input_schema=parameters}`（没有外层 `function`）
4. **`_apply_cache_control`**: Prompt caching 标记
   - 在 system 末尾、倒数第二条消息末尾、tool 列表的 builtin/MCP 边界 + 最后一条上加 `cache_control={type:"ephemeral"}`

---

## 8. 子 Agent 的上下文继承 vs 重建

文件: [`agent/subagent.py`](nanobot/agent/subagent.py)

### 8.1 子 agent 上下文是**完全重建**，不继承父 agent 历史

```python
# subagent.py:151
async def _run_subagent(self, task_id, task, label, origin, status):
    # Build subagent tools (no message tool, no spawn tool)
    tools = ToolRegistry()
    tools.register(ReadFileTool(...))
    tools.register(WriteFileTool(...))
    tools.register(EditFileTool(...))
    ...

    system_prompt = self._build_subagent_prompt()
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": task},
    ]

    result = await self.runner.run(AgentRunSpec(
        initial_messages=messages,
        tools=tools,
        ...,
    ))
```

`_build_subagent_prompt` 用 `templates/agent/subagent_system.md` 渲染：

- `time_ctx` = `ContextBuilder._build_runtime_context(None, None)`（只有 Current Time，没有 channel/chat_id）
- workspace 路径
- `skills_summary`（**不**注入 always-on skills 文本，只给 summary；子 agent 自行 `read_file`）
- **不包含** `AGENTS.md/SOUL.md/USER.md/MEMORY.md/Recent History` — 子 agent 是"焦点单任务"

工具集合也是**重新构建的精简集**（`read/write/edit/list/glob/grep` + 可选 `exec/web_*`）。**没有 `message、spawn、cron、my、ask_user`**。

### 8.2 结果如何回到父 agent（完整链路）

子 agent 完成后，结果通过消息总线异步回传给主 agent。这里涉及**两个层面的"角色"概念**，容易混淆，理解它们的关键是区分**传输层**和**持久化层**。

#### 8.2.1 传输层：`channel="system"`（消息怎么送达）

```python
# subagent.py:272-283 (_announce_result)
msg = InboundMessage(
    channel="system",           # ← 消息总线的路由标签
    sender_id="subagent",
    chat_id=f"{origin['channel']}:{origin['chat_id']}",
    content=announce_template,  # 渲染后的结果公告模板
    session_key_override=override,
    metadata={
        "injected_event": "subagent_result",
        "subagent_task_id": task_id,
    },
)
await self.bus.publish_inbound(msg)
```

`channel="system"` 是消息总线的**路由标签**，与 LLM 的 role 毫无关系。它的唯一作用是让主 loop 走进 `_process_message` 的 system 分支（[`loop.py:873`](nanobot/agent/loop.py#L873)），而不是普通用户消息分支。可以理解为"快递单号"——只描述包裹怎么运输，不描述包裹里是什么。

#### 8.2.2 持久化层：`role="assistant"`（以什么身份记入历史）

```python
# loop.py:1208-1230 (_persist_subagent_followup)
session.add_message(
    "assistant",                # ← LLM 对话中的语义角色
    msg.content,
    sender_id=msg.sender_id,
    injected_event="subagent_result",
    subagent_task_id=task_id,
)
```

这里 `role="assistant"` 是 LLM 对话历史中的**语义角色**。从模型的视角看，子 agent 的输出等价于"助手自己完成了一项后台任务"，后续模型可以直接基于这个结果继续推理或回复用户。注意 dedup 保护：同一个 `subagent_task_id` 不会重复持久化。

#### 8.2.3 连续 assistant 问题与两层合并

子 agent 结果以 `role="assistant"` 持久化后，`session.messages` 末尾变为：

```
... {role: "assistant", content: "已启动后台任务，完成后通知你"},    ← 主 agent 上一轮的最后回复
    {role: "assistant", content: "Subagent [xxx] completed. Result: ..."}  ← 新持久化的子 agent 结果
```

这产生了**两个连续的 assistant 消息**。而 Anthropic Messages API 要求 user/assistant 交替出现，直接发送会报 400 错误。nanobot 用**两层合并**解决：

**第一层：`build_messages` 边界合并**（[`context.py:157`](nanobot/agent/context.py#L157)）

```python
# 只检查最后一条历史消息与 current_role 是否冲突
if messages[-1].get("role") == current_role:
    # 冲突 → 合并内容，不新增消息
    last["content"] = self._merge_message_content(last["content"], merged)
    messages[-1] = last
```

当 subagent 回调时，`current_role="assistant"`，`current_message=""`。history 的最后一条恰好是刚持久化的子 agent 结果（也是 assistant）。两者 role 相同，于是把 `runtime_ctx` 合并到子 agent 结果消息的末尾。由于 `current_message` 是空串，不会造成内容重复。

但这**只处理了边界**（`current_message` 与最后一条 history 之间），history 内部已有的连续 assistant（上一轮的 agent 回复 + 子 agent 结果）并没有被合并。

**第二层：`_merge_consecutive` 全面合并**（[`anthropic_provider.py:267`](nanobot/providers/anthropic_provider.py#L267)）

```python
# 在发送 API 请求前的最后一刻，全面扫描 merge
@staticmethod
def _merge_consecutive(msgs):
    merged = []
    for msg in msgs:
        if merged and merged[-1]["role"] == msg["role"]:
            # 合并到上一条同 role 消息
            prev_c.extend(cur_c)
        else:
            merged.append(msg)
    # 额外规则：strip 末尾 assistant、首条若为 assistant 则插 user 占位
    ...
    return merged
```

这个方法在**发送 Anthropic API 请求前的最后一刻**执行，逐条扫描所有消息，把连续同 role 的合并成一条。此外还处理两条 Anthropic 特有约束：不能以 assistant 结尾（strip）、不能以 assistant 开头（前面插 `"(conversation continued)"` user）。详见 §7 的 `_convert_messages` 调用链。

#### 8.2.4 完整时序

```
主 Agent 回合 1:
  user: "帮我查一下 XXX"
  → assistant: "让我启动一个子任务..." + tool_use(spawn)
  → tool_result: "Subagent [xxx] started (id: abc123)"
  → assistant: "已启动后台任务，完成后通知你"

... 子 agent 在后台运行（独立 session、独立工具集、独立 LLM 调用）...

子 Agent 完成 → _announce_result:
  bus.publish_inbound(
    channel="system",          ← 传输层：路由到 system 分支
    sender_id="subagent",
    content="Subagent [xxx] completed. Result: ..."
  )

主 Agent 回合 2（由 system 消息触发）:
  _process_message(msg.channel == "system"):
    1. _persist_subagent_followup:
       session.add_message("assistant", content)  ← 持久化层：以 assistant 写入历史
       → 现在 session.messages 末尾有两个连续 assistant

    2. get_history() → 返回包含两个连续 assistant 的 history

    3. build_messages:
       current_role = "assistant"
       current_message = ""
       → 边界合并：把 runtime_ctx 合并到最后一个 assistant 消息

    4. _run_agent_loop → AgentRunner.run:
       → _request_model → AnthropicProvider.chat
         → _convert_messages → _merge_consecutive
           → 全面扫描合并连续 assistant → 合法的 Anthropic API 请求

    5. LLM 收到合并后的消息，可以基于子 agent 结果继续推理/回复用户
```

#### 8.2.5 关键设计要点

| 层级       | 组件                         | "role"                     | 含义                | 位置                                                                      |
| ---------- | ---------------------------- | -------------------------- | ------------------- | ------------------------------------------------------------------------- |
| 传输层     | `bus.publish_inbound`        | `channel="system"`         | 消息总线路由标签    | [subagent.py:272](nanobot/agent/subagent.py#L272)                         |
| 持久化层   | `_persist_subagent_followup` | `role="assistant"`         | LLM 对话语义角色    | [loop.py:1223](nanobot/agent/loop.py#L1223)                               |
| 上下文组装 | `build_messages`             | `current_role="assistant"` | 当前消息的插入角色  | [loop.py:912](nanobot/agent/loop.py#L912)                                 |
| Provider   | `_merge_consecutive`         | 合并连续 `role`            | 发 API 前的最后整形 | [anthropic_provider.py:267](nanobot/providers/anthropic_provider.py#L267) |

---

## 9. 关键裁剪/压缩触发条件汇总

| 机制                         | 文件                     | 触发条件                                                 | 行为                                                                                                                |
| ---------------------------- | ------------------------ | -------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------- |
| `get_history` 两阶段裁剪     | `session/manager.py:73`  | 每次进入 `_process_message` 都跑                         | 阶段A：排除已归档 → 取尾 max_messages → 对齐 user → legal_start；阶段B：正序构建 out → 倒序 token 预算裁尾 → 再对齐 |
| **Consolidator**（在线归档） | `memory.py:589`          | `estimated_prompt_tokens >= context - max_output - 1024` | 沿 user 边界切 chunk，LLM 摘要追加到 `history.jsonl`；最多 5 轮；目标 `budget * 0.5`                                |
| **AutoCompact**（空闲归档）  | `autocompact.py:61`      | session 空闲 ≥ `session_ttl_minutes` 且无活跃 task       | tail 切成 archive + 最近 8 条 legal suffix；摘要写到 `metadata["_last_summary"]` 与 `history.jsonl`                 |
| `enforce_file_cap`           | `session/manager.py:207` | session.messages > 2000                                  | 保留 `retain_recent_legal_suffix(2000)`，溢出 chunk 走 `on_archive=memory.raw_archive`                              |
| `_microcompact`              | `runner.py:978`          | compactable tool 结果数 > 10                             | 把超过 10 之外的、≥500 字符的 tool 结果替换为占位摘要                                                               |
| `_apply_tool_result_budget`  | `runner.py:1004`         | 每个 iteration                                           | 对每条 tool 消息走 `maybe_persist_tool_result` + 截断到 `max_tool_result_chars`                                     |
| `_snip_history`              | `runner.py:1025`         | `estimate_prompt_tokens_chain > context_block_limit`     | 从尾部硬切 non-system，保留 system + 必要尾部；对齐 user + legal_start                                              |
| **Dream**（后台整理）        | `memory.py:851`          | 由 cron 触发                                             | Phase 1 LLM 分析 + Phase 2 AgentRunner 编辑 MEMORY/SOUL/USER，推进 dream_cursor                                     |

---

## 10. 相关文件索引

| 文件                                                                         | 核心职责                                                                                                       |
| ---------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| [`agent/loop.py`](nanobot/agent/loop.py)                                     | 消息总线消费、session 路由、崩溃恢复、\_process_message 主战场                                                 |
| [`agent/context.py`](nanobot/agent/context.py)                               | ContextBuilder：system prompt 七段拼装、messages 组装、runtime_ctx 注入                                        |
| [`agent/runner.py`](nanobot/agent/runner.py)                                 | AgentRunner：6 步 context governance（orphan/backfill/microcompact/budget/snip）、工具执行、mid-turn injection |
| [`agent/memory.py`](nanobot/agent/memory.py)                                 | MemoryStore + Consolidator + Dream：长期记忆读写、在线归档、后台记忆整理                                       |
| [`agent/autocompact.py`](nanobot/agent/autocompact.py)                       | AutoCompact：空闲会话 TTL 归档                                                                                 |
| [`agent/skills.py`](nanobot/agent/skills.py)                                 | SkillsLoader：skill 发现、always-on 注入、summary 生成                                                         |
| [`session/manager.py`](nanobot/session/manager.py)                           | Session：消息存储、get_history 三层裁剪、enforce_file_cap                                                      |
| [`providers/anthropic_provider.py`](nanobot/providers/anthropic_provider.py) | Anthropic 格式转换、prompt caching、thinking 处理                                                              |
| [`providers/base.py`](nanobot/providers/base.py)                             | Provider 基类、\_sanitize_empty_content                                                                        |
| [`agent/subagent.py`](nanobot/agent/subagent.py)                             | SubagentManager：子 agent 上下文重建、结果回传                                                                 |
| [`agent/tools/ask.py`](nanobot/agent/tools/ask.py)                           | AskUserTool：pending_ask_user 分支处理                                                                         |
| [`bus/events.py`](nanobot/bus/events.py)                                     | InboundMessage / OutboundMessage 数据结构                                                                      |
| [`utils/helpers.py`](nanobot/utils/helpers.py)                               | `find_legal_message_start`、`estimate_message_tokens` 等辅助函数                                               |

---

## 11. 一句话总览

> **system** 由 identity + AGENTS/SOUL/USER/TOOLS + MEMORY + always_skills + skills_summary + recent_history 七段拼出；**messages** 由 system + 经两阶段裁剪的 history + (runtime_ctx 元数据 ⊕ 当前内容) 拼出，通过 `current_role` 控制插入位置和边界合并；**Runner** 每轮再做孤儿修补、微压缩、tool 结果截断、token 硬切；**Provider** 最后做格式翻译 + `_merge_consecutive` 全面合并连续同 role（子 agent 回传导致的连续 assistant 也在此兜底）+ cache 标记；**子 agent** 完全重建上下文，结果通过 `channel="system"` 传输 + `role="assistant"` 持久化回到父 session。
