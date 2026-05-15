# 记忆整合机制笔记

> 基于 nanobot 源码分析，涵盖 Consolidator（在线压缩）和 AutoCompact（空闲压缩）两个子系统。

---

## 一、Consolidator — 在线 token 预算驱动的压缩

### 1.1 定位

在每次 LLM 调用前检查 prompt token 是否超出预算，超出则循环归档直到安全。

源码：[agent/memory.py:443-669](agent/memory.py#L443)

### 1.2 关键常量

| 常量 | 值 | 说明 |
|------|-----|------|
| `_MAX_CONSOLIDATION_ROUNDS` | 5 | 单次最多压缩轮数 |
| `_SAFETY_BUFFER` | 1024 | token 估算漂移的额外安全空间 |
| `_RAW_ARCHIVE_MAX_CHARS` | 16,000 | 降级 raw dump 的字符上限 |
| `_ARCHIVE_SUMMARY_MAX_CHARS` | 8,000 | LLM 摘要的字符上限 |
| `_HISTORY_ENTRY_HARD_CAP` | 64,000 | append_history 的紧急硬上限 |

### 1.3 压缩流程

```
maybe_consolidate_by_tokens()
    │
    ├── 估算当前 prompt token 数 → 未超预算则跳过
    │
    ▼ 超预算
    for round in range(5):          ← 最多 5 轮
        │
        ├── pick_consolidation_boundary()  → 在 user 消息边界处切断
        │
        ├── archive(chunk)
        │       ├── _format_messages() 格式化消息
        │       ├── _truncate_to_token_budget() 截断到输入预算
        │       ├── LLM 调用 (tools=None, tool_choice=None)
        │       │       └── system prompt: consolidator_archive.md
        │       ├── 成功 → append_history(summary)
        │       └── 失败 → raw_archive(messages) 降级
        │
        ├── last_consolidated = end_idx  (无论成败都推进游标)
        │
        └── 重新估算 token → 低于 target 则退出
```

### 1.4 边界选择算法 —— `pick_consolidation_boundary()`

源码：[agent/memory.py:490-510](agent/memory.py#L490-L510)

#### 函数签名

```python
def pick_consolidation_boundary(
    self, session: Session, tokens_to_remove: int
) -> tuple[int, int] | None:
```

- **输入**：`session`（当前会话）、`tokens_to_remove`（需要移除的目标 token 数）
- **输出**：`(end_idx, removed_tokens)` 元组，或 `None`
- `end_idx` 是归档消息的**结束索引（不含）**，即 `messages[start:end_idx]` 为待归档的 chunk
- `removed_tokens` 是该 chunk 的 token 估算值（近似值，非精确计数）

#### 逐行走读

假设消息序列如下：

```
消息:  [user_1, assistant_1, user_2, assistant_2, user_3, ...]
索引:    0       1            2         3           4
         ↑ start (last_consolidated)
```

扫描循环（第 502-510 行）有两个关键操作，**先检查再累加**：

| 步骤 | 操作 | 说明 |
|------|------|------|
| 1 | 检查是否为边界 | 当前消息 role == `"user"` 且 `idx > start` → 记录 `(idx, removed_tokens)` 为候选边界 |
| 2 | 累加 token | `removed_tokens += estimate_message_tokens(message)` |

**关键：** 步骤 1 中的 `removed_tokens` 是当前 user 消息**之前**所有消息的 token 累计值。这意味着边界索引 `idx` 指向的 user 消息**不会被归档**，它将是保留会话的第一条消息。

**具体追踪示例**（假设 `start = 0`）：

```
idx=0, message=user_1:
  → idx > start? 0>0 = false，不记录边界
  → removed_tokens += 100  (= 100)

idx=1, message=assistant_1:
  → role != "user"，不记录边界
  → removed_tokens += 100  (= 200)

idx=2, message=user_2:
  → idx > start? 2>0 = true, role == "user" → 边界!
  → last_boundary = (2, 200)    ← 归档 messages[0:2]，移除 200 token
  → 若 200 ≥ tokens_to_remove → 立即返回 (2, 200)
  → removed_tokens += 100  (= 300)
```

**结果**：归档 `[user_1, assistant_1]`，保留 `[user_2, assistant_2, user_3, ...]`。保留的会话从一个完整的 user 轮次开始。

#### 三种退出路径

| 路径 | 条件 | 返回值 |
|------|------|--------|
| 精确命中 | 某 user 边界处 `removed_tokens ≥ tokens_to_remove` | 立即返回该边界 |
| 尽力而为 | 扫描到底仍未达标，但至少找到了一个 user 边界 | 返回最后一个边界 |
| 无法归档 | `start ≥ len(messages)`、`tokens_to_remove ≤ 0`、或全程无 user 消息 | `None` |

#### 调用方

在 [maybe_consolidate_by_tokens()](agent/memory.py#L589-L691) 第 634 行调用：

```python
boundary = self.pick_consolidation_boundary(session, max(1, estimated - target))
```

其中：
- `budget = context_window_tokens - max_completion_tokens - 1024`（安全空间）
- `target = int(budget * consolidation_ratio)`（默认 ratio=0.5，即压到窗口的 50%）
- `tokens_to_remove = estimated - target`（需要砍掉的 token 数）
- 多轮循环，每轮归档一个 chunk 后重新估算，直到低于 target 或找不到边界

#### 设计要点

| 要点 | 说明 |
|------|------|
| **用户轮次对齐** | 永远在 user 消息处切割，保证保留的会话从一个完整的 user→assistant 轮次开始，而非从半截的 assistant 回复开始 |
| **`idx > start` 守卫** | 防止把 `start` 处的第一条消息当作边界——如果第一条就是 user 消息，归档 0 条消息没有意义 |
| **先检查再累加** | 边界处的 `removed_tokens` 不含当前 user 消息本身，确保归档的消息集语义完整 |
| **token 数是估算值** | 使用 `estimate_message_tokens()` 而非精确 tokenizer 计数，所以 `removed_tokens` 只是近似值，实际由 `_SAFETY_BUFFER` 兜底 |

### 1.5 容错：两级回退

```
LLM 摘要归档 (tools=None)
    │
    ▼ 失败
Raw Archive ([RAW] 标记 + 原文写入 history.jsonl)
```

### 1.6 与旧版 MemoryConsolidator 的区别

| 维度 | 旧版 | 新版 |
|------|------|------|
| 工具 | `save_memory` 虚拟 tool（含 `history_entry` + `memory_update` 字段） | 无 tool |
| tool_choice | `"required"` 强制调用 | `None` |
| 输出 | 结构化 JSON | 自由文本摘要 |
| 写入目标 | MEMORY.md + HISTORY.md | 仅 history.jsonl |
| MEMORY.md 编辑 | Consolidator 负责 | 交由 Dream（cron 离线）处理 |
| 容错 | 3 级（tool_choice → auto → raw） | 2 级（LLM 摘要 → raw） |

---

## 二、AutoCompact — 空闲会话的主动压缩

### 2.1 定位

当会话超过 TTL 没有活动时，后台异步归档旧消息，保留最近 8 条热上下文。

源码：[agent/autocompact.py](agent/autocompact.py)

### 2.2 关键常量

| 常量 | 值 | 说明 |
|------|-----|------|
| `_RECENT_SUFFIX_MESSAGES` | 8 | 归档后保留的最近消息数 |

### 2.3 触发时机

在 [agent/loop.py:658-665](agent/loop.py#L658) 主循环中：

```python
while self._running:
    msg = await asyncio.wait_for(self.bus.consume_inbound(), timeout=1.0)
    # 超时 → 消息总线空闲
    self.auto_compact.check_expired(
        self._schedule_background,
        active_session_keys=self._pending_queues.keys(),  # 排除活跃 session
    )
```

### 2.4 完整流程

```
主循环空闲（1s 超时）
    │
    ▼
check_expired() 扫描所有 session
    │
    ├── 跳过：活跃 session / 已在归档中 / 未过期
    │
    ▼ 过期
schedule_background(_archive(key))
    │
    ▼
_split_unconsolidated()
    ├── 取 session.messages[last_consolidated:]
    ├── 用 probe Session 模拟 retain_recent_legal_suffix(8)
    ├── tail[:cut]  → archive_msgs（可归档前缀）
    └── tail[cut:]  → kept_msgs（保留 8 条，从 user 对齐）
    │
    ▼
consolidator.archive(archive_msgs)  → LLM 摘要 → history.jsonl
    │
    ▼
session.messages = kept_msgs        → 替换为保留后缀
session.last_consolidated = 0       → 重置游标
_summaries[key] = (summary, ...)    → 热路径（内存 dict）
metadata["_last_summary"] = ...     → 冷启动备份（持久化）
```

### 2.5 拆分逻辑详解

`retain_recent_legal_suffix(8)` 在 [session/manager.py:165-193](session/manager.py#L165)：

1. 取尾部 8 条 → 对齐到第一个 `user` 消息
2. 尾部没有 `user` → 回溯整个 session 中最后一个 `user`，向前取 8 条
3. `find_legal_message_start()` 删除前导孤儿 tool result

### 2.6 摘要注入

下次用户发消息时，`prepare_session()` 消费摘要：

```
热路径：_summaries.pop(key)        ← 进程未重启
冷路径：session.metadata["_last_summary"]  ← 进程重启后恢复
```

格式化为：

> Inactive for 35 minutes.
> Previous conversation summary: User discussed deployment pipeline...

### 2.7 三条保护规则

- `self._archiving` set — 防止同一 session 被重复提交
- `active_session_keys` — 正在处理消息的不压缩
- `_is_expired()` — `updated_at` 超过 `session_ttl_minutes` 才算过期

---

## 三、Consolidator vs AutoCompact 对比

| | Consolidator | AutoCompact |
|---|---|---|
| **触发时机** | LLM 调用前（token 超预算） | 消息总线空闲 + session TTL 过期 |
| **频率** | 高频（每次请求都可能） | 低频（分钟级） |
| **压缩策略** | 渐进式，按 token 预算逐轮推进 | 一次性：前缀归档 + 保留 8 条后缀 |
| **游标** | 推进 `last_consolidated` | 重置为 0（消息整体替换） |
| **职责** | 防止上下文溢出，保证 LLM 调用成功 | 控制内存中 session 的存储开销 |

---

## 四、历史归档模板

[consolidator_archive.md](templates/agent/consolidator_archive.md) 的核心指令：

> Extract key facts from this conversation. Only output items matching these categories:
> - User facts, Decisions, Solutions, Events, Preferences
> - Priority: user corrections > solutions > decisions > events > environment facts
> - Skip: code patterns derivable from source, git history, or already captured in existing memory
> - If nothing noteworthy happened, output: (nothing)

---

## 五、关键设计原则

1. **在线/离线分离**：Consolidator 在线写 history.jsonl（日志），Dream 离线消费日志编辑 MEMORY.md
2. **游标推进无论如何都发生**：避免 LLM 降级时重复归档同一段消息
3. **无 tool 设计**：新版 Consolidator 用纯文本 system prompt 替代 `tool_choice` 约束，降低复杂度
4. **双重存储**：AutoCompact 摘要同时存内存 dict（热路径）和 session metadata（冷启动恢复）
