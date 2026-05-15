# 定时任务机制

nanobot 有两种定时任务类型：**Heartbeat（心跳任务）** 和 **Cron（定时任务）**。两者在触发方式、任务来源、调度精度上有本质区别。

---

## 一、Heartbeat（心跳任务）

### 1.1 概述

基于 `HEARTBEAT.md` 文件的、由 LLM 决策的周期性检查机制。默认每 30 分钟触发一次，由 `HeartbeatService` 驱动。

核心文件：
- [heartbeat/service.py](heartbeat/service.py) — HeartbeatService 主逻辑
- [templates/HEARTBEAT.md](templates/HEARTBEAT.md) — 任务文件模板
- [cli/commands.py](cli/commands.py) — 服务初始化和回调接线（第 796-868 行）

### 1.2 工作流程

Heartbeat 分两个阶段执行：

```
每隔 interval_s (默认 30 分钟)
  │
  ▼
读取 HEARTBEAT.md 文件内容
  │
  ├─ 文件不存在/为空 → 跳过本轮
  │
  ▼
Phase 1 — 决策阶段（_decide）
  ├─ 将文件内容 + 当前时间发送给 LLM
  ├─ LLM 调用虚拟 tool（heartbeat），返回 action：
  │   ├─ "skip" → 无任务，本轮结束
  │   └─ "run"  + tasks 描述 → 进入 Phase 2
  │
  ▼
Phase 2 — 执行阶段（on_heartbeat_execute）
  ├─ heartbeat_preamble + tasks → 拼接为一条"用户消息"
  ├─ 通过 agent.process_direct() → _process_message() 走完整 agent loop
  ├─ agent 在 loop 中可调用所有工具（send_message、edit_file、exec 等）
  ├─ 过滤非可交付响应（_is_deliverable，过滤空响应和推理泄露）
  ├─ evaluate_response() → 再次用 LLM 判断是否值得通知用户
  │   ├─ 值得通知 → on_heartbeat_notify() 投递到用户 channel
  │   └─ 不值得   → 静默丢弃
```

使用 tool-call（而非自由文本解析）来做 skip/run 决策，避免不可靠的字符串匹配。

### 1.3 HEARTBEAT.md 任务格式

任务用**纯自然语言**描述，跟和 agent 聊天一样。默认模板：

```markdown
# Heartbeat Tasks

This file is checked every 30 minutes by your nanobot agent.
Add tasks below that you want the agent to work on periodically.

If this file has no tasks (only headers and comments), the agent will skip the heartbeat.

## Active Tasks

<!-- Add your periodic tasks below this line -->


## Completed

<!-- Move completed tasks here or delete them -->
```

**任务描述示例：**

```markdown
## Active Tasks

- 每天早上检查邮箱，有紧急邮件立即通知我
- 每小时检查一次服务器 CPU 使用率，超过 80% 就报警
- 帮我发一条消息给用户 channel，消息内容为：快来和我聊天吧。

## Completed

- ~~整理上周的会议纪要~~
```

### 1.4 从自然语言到执行的数据流

`on_heartbeat_execute`（[cli/commands.py:805-826](cli/commands.py#L805-L826)）将任务包装为入站消息，走标准 agent loop：

```
heartbeat_preamble + tasks (自然语言)
  │
  ▼
InboundMessage → agent.process_direct() → _process_message()
  │  （session_key="heartbeat"，独立会话）
  │
  ▼
agent 在 loop 中看到这条"消息"，用工具执行任务
  │
  ▼
产出响应 → on_heartbeat_notify() 投递到用户 channel
```

**heartbeat_preamble** 在执行前拼接在任务前面：

```
[Your response will be delivered directly to the user's messaging app.
 Output ONLY the final user-facing message. Never reference internal
 files (HEARTBEAT.md, AWARENESS.md, etc.), your instructions, or your
 decision process. If nothing needs reporting, respond with just
 'All clear.' and nothing else.]
```

这个 preamble 告诉 LLM：
- 回复会直接投递到用户消息应用
- 只输出最终用户消息，不要暴露内部推理
- 没什么事就说 "All clear."

### 1.5 Session 隔离

Heartbeat 使用**独立的 session**，不复用任何用户 channel session。

关键代码（[agent/loop.py:967](agent/loop.py#L967)）：

```python
key = session_key or msg.session_key
```

`process_direct` 显式传入 `session_key="heartbeat"`，因此 `key` 始终为 `"heartbeat"`，不会 fallback 到 `msg.session_key`（即 `f"{channel}:{chat_id}"`）。

```
Session "heartbeat" (独立)              Session "telegram:8281248569" (用户)
├─ Round 1: 心跳执行历史                ├─ 用户: "你好"
├─ Round 2: 心跳执行历史                ├─ agent: "你好！有什么可以帮你的？"
├─ ... (retain_recent_legal_suffix)     └─ ...
└─ 用途: agent 看到过去心跳执行上下文

投递方向: heartbeat 最终回复 → 通过 record=True 注入到用户 session
```

隔离的好处：
- 心跳内部推理过程不污染用户对话历史
- 心跳 session 独立累积历史，agent 能看到过去几轮做了什么，避免重复执行
- 只有最终用户可见消息才写入目标 channel session

### 1.6 投递目标选择

心跳消息投递到哪个用户 channel，由 `_pick_heartbeat_target()`（[cli/commands.py:780-794](cli/commands.py#L780-L794)）决定：

```python
def _pick_heartbeat_target() -> tuple[str, str]:
    enabled = set(channels.enabled_channels)
    for item in session_manager.list_sessions():  # 按 updated_at 倒序
        key = item.get("key") or ""
        if ":" not in key:
            continue
        channel, chat_id = key.split(":", 1)
        if channel in {"cli", "system"}:
            continue
        if channel in enabled and chat_id:
            return channel, chat_id  # 第一个匹配的就拿走
    return "cli", "direct"  # 找不到则降级，不投递
```

逻辑：遍历所有 session（按 `updated_at` 倒序）→ 跳过 cli/system → 找到第一个启用了 channel 的外部会话。

**不基于任务创建者**——HEARTBEAT.md 是文件，不记录任务归属。策略是启发式的：最近跟 bot 说过话的那个活跃用户就是接收目标。如果找不到任何外部 channel，返回 `("cli", "direct")`，`on_heartbeat_notify` 直接 return，不投递。

### 1.7 上下文构建

Heartbeat 走完整 agent loop，**所有标准上下文文件都会读取**（[agent/context.py](agent/context.py)）：

| 上下文来源 | 是否读取 | 说明 |
|-----------|---------|------|
| Identity (agent/identity.md) | ✅ | 同正常消息 |
| AGENTS.md | ✅ | Bootstrap 文件 |
| SOUL.md | ✅ | Bootstrap 文件 |
| USER.md | ✅ | Bootstrap 文件 |
| TOOLS.md | ✅ | Bootstrap 文件 |
| MEMORY.md | ✅ | 非模板内容时加载 |
| Skills (always + summary) | ✅ | 同正常消息 |
| Recent History (history.jsonl) | ✅ | 最近 50 条 |
| Session 历史 | ✅ (heartbeat 独立 session) | 之前心跳执行记录 |

与普通用户消息的唯一区别：session 历史来自 `"heartbeat"` 独立 session，而非用户 channel session。

### 1.8 任务创建与管理

任务的**唯一入口是用户对话**。默认 HEARTBEAT.md 是空的（只有标题骨架）。

**创建流程：**

```
用户对 agent 说: "帮我每天早上检查邮箱"
  │
  ▼
agent 根据 AGENTS.md 指引:
  "When the user asks for a recurring/periodic task,
   update HEARTBEAT.md instead of creating a one-time cron reminder."
  │
  ▼
agent 调用 edit_file → 在 HEARTBEAT.md 的 Active Tasks 下追加任务
```

AGENTS.md 给 agent 提供了三种管理 HEARTBEAT.md 的方式：

| 操作 | 工具 | 用途 |
|------|------|------|
| Add | `edit_file` | 追加新任务到 Active Tasks |
| Remove | `edit_file` | 删除已完成任务 |
| Rewrite | `write_file` | 整体替换任务列表 |

### 1.9 Active ↔ Completed 状态转换

**没有自动化机制，完全由 LLM 在执行过程中自行决定。**

心跳执行时，agent 在 agent loop 中有文件工具权限，可以调用 `edit_file` 更新 HEARTBEAT.md：

- **一次性任务** → agent 执行完后，应该从 Active 删除，写入 Completed（或直接删除）
- **重复性任务** → agent 执行完后，留在 Active 不动，等待下次心跳

`heartbeat_preamble` 虽要求 "Output ONLY the final user-facing message"，但只约束最终回复文本，不影响 agent 在中间过程使用工具。

### 1.10 与 Cron 的决策区分

LLM 根据任务的**时间精度要求**决定用 heartbeat 还是 cron：

| 用户说的是 | 选择 | 原因 |
|-----------|------|------|
| "每天早上 9 点给我发日报" | cron | 精确时间 |
| "5 分钟后提醒我开会" | cron | 精确延迟 |
| "每隔 2 小时检查服务器" | cron | 精确间隔 |
| "有空的时候帮我整理笔记" | heartbeat | 模糊，无时间要求 |
| "定期检查有没有新邮件" | heartbeat | 周期性但不需要精确时刻 |

核心区分标准：**需不需要精确的时间控制？**
- 需要 → cron（支持 `every_seconds`、`cron_expr`、`at` 三种精确调度）
- 不需要 → heartbeat（每 30 分钟自然触发，LLM 自己判断做不做）

AGENTS.md 指引原文：

> When the user asks for a recurring/periodic task, update HEARTBEAT.md instead of creating a one-time cron reminder.

---

## 二、Cron（定时任务）

### 2.1 概述

结构化定时任务调度引擎，类似传统 crontab，支持精确的时间调度。由 `CronService` 驱动，任务持久化到 JSON 文件。

核心文件：
- [cron/service.py](cron/service.py) — CronService 调度主逻辑
- [cron/types.py](cron/types.py) — CronJob、CronSchedule、CronPayload 等数据结构
- [agent/tools/cron.py](agent/tools/cron.py) — LLM 可调用的 cron 工具
- [cli/commands.py](cli/commands.py) — `on_cron_job` 回调接线（第 704-772 行）

三种调度类型（`CronSchedule.kind`）：

| kind | 说明 | 示例 |
|------|------|------|
| `at` | 一次性定时 | 指定毫秒时间戳，到点触发一次 |
| `every` | 固定间隔 | `every_seconds: 7200`（每 2 小时） |
| `cron` | cron 表达式 | `cron_expr: "0 9 * * *"`（每天 9 点） |

### 2.2 调度机制：自调度 asyncio 任务链

CronService **不是后台线程**，而是一条自我续接的 asyncio 任务链。核心是 `_arm_timer()`（[cron/service.py:280-300](cron/service.py#L280-L300)）：

```python
def _arm_timer(self) -> None:
    if self._timer_task:
        self._timer_task.cancel()          # 取消旧定时器

    next_wake = self._get_next_wake_ms()   # 找所有 job 中最早的 next_run_at_ms
    if next_wake is None:
        delay_ms = self.max_sleep_ms       # 没有 job，睡 5 分钟兜底
    else:
        delay_ms = min(self.max_sleep_ms, max(0, next_wake - now))

    async def tick():
        await asyncio.sleep(delay_s)       # 精准睡眠到最近 job 的触发时间
        if self._running:
            await self._on_timer()         # 醒来干活

    self._timer_task = asyncio.create_task(tick())
```

**不是固定间隔轮询，而是精准睡眠**——直接睡到最近一个 job 的 `next_run_at_ms`，不到点不醒。`max_sleep_ms=5分钟` 只是无任务时的兜底上限。

整体是一条自我续接的链：

```
start()
  │
  ├─ _load_store()          加载所有 job 和 action.jsonl 中的待处理操作
  ├─ _recompute_next_runs() 为每个 enabled job 计算 next_run_at_ms
  ├─ _save_store()          保存到 jobs.json
  └─ _arm_timer()           asyncio.sleep(到最近 job 的时间)
       │
       ▼
   _on_timer()
       ├─ _load_store()          重新加载（支持外部修改）
       ├─ 找出到期 job（now >= next_run_at_ms）
       ├─ 逐个 _execute_job(job)
       │    ├─ 调用 on_job(job) 回调
       │    ├─ 记录状态: ok / error
       │    ├─ 记录耗时、错误信息
       │    ├─ 写入 run_history（保留最近 20 条）
       │    └─ 调度后续:
       │         ├─ at 类型 → delete_after_run ? 删除 : 禁用
       │         └─ every/cron 类型 → 重新计算 next_run_at_ms
       ├─ _save_store()          持久化状态
       └─ _arm_timer()           重新计算，睡到下一次
            │
            ▼
        _on_timer() → ...
```

三层并发安全：

| 机制 | 说明 |
|------|------|
| `FileLock` | 文件锁保护 `action.jsonl` 的读写 |
| `action.jsonl` 命令队列 | 多实例的增删改操作通过追加 JSON 行来同步，`_load_store()` 时合并 |
| `_timer_active` 标志 | 防止 `_on_timer` 执行期间 `_load_store`（如来自 `list_jobs` 轮询）替换正在操作的 store |

### 2.3 执行方式

跟 heartbeat 一样，cron 也通过 `agent.process_direct()` 走完整 agent loop。

`on_cron_job` 回调（[cli/commands.py:705-772](cli/commands.py#L705-L772)）：

```python
async def on_cron_job(job: CronJob) -> str | None:
    # dream 是系统内部 job，不走 agent loop
    if job.name == "dream":
        await agent.dream.run()
        return None

    # 拼接提醒前缀
    reminder_note = (
        "The scheduled time has arrived. Deliver this reminder to the user now, "
        "as a brief and natural message in their language. Speak directly to them — "
        "do not narrate progress, summarize, include user IDs, or add status reports "
        "like 'Done' or 'Reminded'.\n\n"
        f"Reminder: {job.payload.message}"
    )

    # 设置保护机制
    cron_tool.set_cron_context(True)
    message_tool.set_record_channel_delivery(True)

    # 跟 heartbeat 一模一样的入口
    resp = await agent.process_direct(
        reminder_note,
        session_key=f"cron:{job.id}",        # 每个 job 独立 session
        channel=job.payload.channel or "cli",
        chat_id=job.payload.to or "direct",
        on_progress=_silent,
    )
```

与 heartbeat 的对比：

```
Heartbeat:  heartbeat_preamble + tasks → process_direct(session_key="heartbeat")
Cron:       reminder_note + job.message → process_direct(session_key="cron:{job.id}")
```

每个 cron job 有自己独立的 session（`cron:{job.id}`），互不干扰。

### 2.4 防护机制一：防递归 —— `cron_context`

**问题**：cron 任务执行时，agent 在 loop 里可以调用 `cron` 工具。如果 agent 又创建了一个 cron 任务，新任务触发时又创建，就会递归爆炸。

**解决**：用 `ContextVar` 作为"正在 cron 执行中"的标记位。

设置端（[cli/commands.py:726-729](cli/commands.py#L726-L729)）：

```python
cron_token = cron_tool.set_cron_context(True)   # 进入 cron 上下文

try:
    resp = await agent.process_direct(...)       # 执行 agent turn
finally:
    cron_tool.reset_cron_context(cron_token)     # 无论如何都要恢复
```

检查端（[agent/tools/cron.py:140](agent/tools/cron.py#L140)）：

```python
if self._in_cron_context.get():
    return "Error: cannot schedule new jobs from within a cron job execution"
```

使用 `ContextVar` 而非普通布尔值的原因：
- asyncio 安全：每个协程有独立的上下文，不会互相干扰
- `set()` 返回 token，`reset(token)` 恢复到设置前的值，天然支持嵌套

### 2.5 防护机制二：投递追踪 —— `_record_channel_delivery` + `_sent_in_turn`

**问题**：cron 在独立 session（`cron:{job.id}`）中执行，但消息要发到用户 channel（`telegram:8281248569`）。两个问题需要解决：

| 问题 | 说明 |
|------|------|
| 记录问题 | agent 调 `send_message` 发出的消息，怎么写入**用户 channel 的 session**？ |
| 重复问题 | agent 最终文本响应跟 `send_message` 已经发出的内容重复怎么办？ |

#### _record_channel_delivery：解决记录问题

它的作用就是给 `send_message` 发出的消息贴一个标签，告诉 `_deliver_to_channel`："这条消息也要写入目标 channel 的 session"。

**第一步**：cron 执行前开启标记（[cli/commands.py:735-736](cli/commands.py#L735-L736)）：

```python
message_tool.set_record_channel_delivery(True)
```

**第二步**：MessageTool 检测到标记，把 `_record_channel_delivery: True` 写入消息 metadata（[agent/tools/message.py:161-162](agent/tools/message.py#L161-L162)）：

```python
if self._record_channel_delivery_var.get():
    metadata["_record_channel_delivery"] = True
```

**第三步**：`_deliver_to_channel` 看到标签，写入用户 channel session（[cli/commands.py:676-697](cli/commands.py#L676-L697)）：

```python
record = record or bool(metadata.pop("_record_channel_delivery", False))
if record and msg.channel != "cli":
    key = session_key or f"{msg.channel}:{msg.chat_id}"
    session = session_manager.get_or_create(key)
    session.add_message("assistant", msg.content, _channel_delivery=True)
    session_manager.save(session)
```

**效果对比**：

```
不开 _record_channel_delivery:
  send_message("早上好！") → Telegram 推送 ✅
                            → telegram:8281248569 session 没有记录 ❌

开启 _record_channel_delivery:
  send_message("早上好！") → Telegram 推送 ✅
                            → telegram:8281248569 session 写入 ✅
```

#### _sent_in_turn：解决重复问题

标记本轮 agent 是否已经通过 `send_message` 给用户发过消息了。

MessageTool 中（[agent/tools/message.py:76-78](agent/tools/message.py#L76-L78)、[agent/tools/message.py:175-176](agent/tools/message.py#L175-L176)）：

```python
def start_turn(self):
    self._sent_in_turn = False       # 每轮开始时重置

# send_message 执行时（仅匹配默认 channel/chat_id）：
if channel == default_channel and chat_id == default_chat_id:
    self._sent_in_turn = True
```

`on_cron_job` 中的判断（[cli/commands.py:754](cli/commands.py#L754)）：

```python
if job.payload.deliver and message_tool._sent_in_turn:
    return response   # agent 自己发过了，跳过外部投递
```

#### 两机制配合的完整数据流

```
cron 执行前:
  cron_tool.set_cron_context(True)          ← 机制①：锁住 cron 工具
  message_tool.set_record_channel_delivery(True) ← 机制②：开启记录模式
  message_tool.start_turn()                 ← _sent_in_turn = False

agent loop 执行中:
  ┌───────────────────────────────────────────────┐
  │ agent 调 send_message("早上好！今天没有紧急邮件") │
  │   ├─ _sent_in_turn = True                      │
  │   ├─ metadata["_record_channel_delivery"]=True │
  │   └─ _deliver_to_channel(msg)                  │
  │       ├─ push 到 Telegram ✅                    │
  │       └─ record=True → 写入用户 session ✅       │
  │                                                │
  │ agent 可能调 cron.add → 被机制①拒绝              │
  │                                                │
  │ agent 最终文本: "已完成检查" ← loop 的返回值       │
  └───────────────────────────────────────────────┘

cron 执行后:
  ┌────────────────────────────────────────────┐
  │ 情况 A: _sent_in_turn = True                │
  │   → agent 自己发过了，跳过外部投递            │
  │                                            │
  │ 情况 B: _sent_in_turn = False               │
  │   → 走 evaluate_response() 判断是否值得通知   │
  │   → 值得 → _deliver_to_channel() 外部投递    │
  │   → 不值得 → 静默丢弃                        │
  └────────────────────────────────────────────┘
```

### 2.6 防护机制三：evaluate 过滤

**问题**：agent 没主动 `send_message`，但最终文本响应可能不值得推送给用户。比如 `"All clear."`、`"已完成检查，没有发现问题。"`、空字符串等。如果每轮 cron 都推送，用户会被无意义通知淹没。

**解决**：用一次轻量级 LLM 调用判断是否值得通知。

[evaluator.py](utils/evaluator.py) 核心逻辑：

```python
async def evaluate_response(response, task_context, provider, model) -> bool:
    llm_response = await provider.chat_with_retry(
        messages=[
            {"role": "system", "content": "你是后台 agent 的通知看门人..."},
            {"role": "user", "content": f"## 原始任务\n{task_context}\n\n## Agent 响应\n{response}"},
        ],
        tools=[{
            "function": {
                "name": "evaluate_notification",
                "parameters": {
                    "should_notify": "boolean",  # 是否值得通知
                    "reason": "string"            # 一句理由
                }
            }
        }],
        model=model,
        max_tokens=256,
        temperature=0.0,     # 确定性判断，不随机
    )
    args = llm_response.tool_calls[0].arguments
    return bool(args.get("should_notify", True))
```

评判标准（[templates/agent/evaluator.md](templates/agent/evaluator.md)）：

| 应该通知 | 应该抑制 |
|---------|---------|
| 有可操作信息 | 例行状态检查，无新内容 |
| 有错误/异常 | "一切正常" 类确认 |
| 已完成的任务交付物 | 空响应 |
| 用户明确要求提醒的内容 | agent 的元推理（"我应该通知用户吗？"） |
| 提醒/定时器触发 | 暴露内部文件名、决策逻辑的内容 |

容错设计——出错时默认通知，宁可多发也不丢重要消息：

```python
except Exception:
    logger.exception("evaluate_response failed, defaulting to notify")
    return True
```

heartbeat 和 cron 共用同一个 `evaluate_response`，但 heartbeat 多一层前置过滤 `_is_deliverable()`，用于滤掉心跳特有的空响应回退和推理泄露模式。

### 2.7 调度与执行的职责分离

```
CronService（调度层）                       AgentLoop（执行层）
─────────────────────                     ──────────────────
_arm_timer()  计算下次唤醒                 process_direct()
_on_timer()   找到期 job                  _process_message()
_execute_job() 记录状态 + 调用回调 ───→    完整 agent turn
  记录 run_history                            ├─ 调工具
  计算 next_run_at_ms                         ├─ 发消息
  保存到 JSON                                 └─ 产出响应
                                        ←─── 返回响应给 on_cron_job
                                              on_cron_job 决定是否投递
```

`CronService` 只管"什么时候该触发"，不管"触发后怎么做"——后者完全委托给 agent loop。

### 2.8 与 Heartbeat 的对比

| 维度 | Heartbeat | Cron |
|------|-----------|------|
| 任务来源 | `HEARTBEAT.md` 自然语言 | 结构化 CronJob（JSON 持久化） |
| 触发方式 | 固定间隔轮询（默认 30 分钟） | 按任务时间点精确调度 |
| 调度类型 | 无（仅固定间隔） | `at` / `every` / `cron` 三种 |
| 决策者 | LLM 读取文件后判断 skip/run | 系统根据时间直接触发 |
| Session | 固定 `"heartbeat"` | 每个 job 独立 `cron:{job.id}` |
| 持久化 | 无（文件即状态） | JSON 文件 + 20 条运行历史 |
| 防递归 | 不需要（没有 cron 工具调用风险） | cron_context 标记 |
| 投递追踪 | 不需要（直接外部投递） | record_channel_delivery + _sent_in_turn |
| 执行入口 | `process_direct(preamble + tasks)` | `process_direct(reminder_note + message)` |
| 适用场景 | "有空时帮我看下这个" | "每天 9 点给我发日报" |
