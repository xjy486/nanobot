# Session.messages 变更机制

> 对应源码：[`nanobot/session/manager.py`](nanobot/session/manager.py)、[`nanobot/agent/loop.py`](nanobot/agent/loop.py)、[`nanobot/agent/autocompact.py`](nanobot/agent/autocompact.py)、[`nanobot/agent/memory.py`](nanobot/agent/memory.py)

## 一、JSONL 写入方式

`SessionManager.save()` 是**全量重写**，不是追加写。

```python
# manager.py:420
with open(tmp_path, "w", encoding="utf-8") as f:  # "w" 模式
    f.write(json.dumps(metadata_line) + "\n")
    for msg in session.messages:                   # 遍历全部消息
        f.write(json.dumps(msg) + "\n")
os.replace(tmp_path, path)                         # 原子替换
```

每次保存都是将内存中 `session.messages` 完整快照序列化写入临时文件，再原子替换原文件。

---

## 二、会物理修改 `session.messages` 的操作

只有以下四种：

### 1. 尾部追加

| 场景 | 方法 | 触发条件 | 位置 |
|---|---|---|---|
| 正常用户/助手消息 | `add_message()` | 每条消息处理时 | [loop.py:1062](nanobot/agent/loop.py#L1062) |
| 子代理结果注入 | `messages.append()` | 子代理完成任务后 | [loop.py:1205](nanobot/agent/loop.py#L1205) |
| 崩溃恢复：补齐未完成的回话 | `messages.append()` | 上次运行在 user 发送后崩溃，注入错误占位 | [loop.py:1321](nanobot/agent/loop.py#L1321) |
| 检查点恢复：去重后批量恢复 | `messages.extend()` | 恢复中断的工具调用链 | [loop.py:1307](nanobot/agent/loop.py#L1307) |
| 心跳消息注入 channel session | `add_message(assistant, _channel_delivery=True)` | heartbeat 推送到目标 channel | [commands.py:696](nanobot/cli/commands.py#L696) |

### 2. 前缀截断

**方法**：`retain_recent_legal_suffix(max_messages)`

保留最近 N 条合法后缀，从头部丢弃旧消息。内部通过 `self.messages = retained` 赋值。

| 触发条件 | 位置 |
|---|---|
| 每条消息处理后，消息数超过 `FILE_MAX_MESSAGES`（2000） | [loop.py:932](nanobot/agent/loop.py#L932) → `enforce_file_cap()` |
| 心跳 session 裁剪历史（由配置 `keep_recent_messages` 控制） | [commands.py:823](nanobot/cli/commands.py#L823) |

### 3. 全部替换（自动压缩）

**触发条件**：`AutoCompact` 定时检查闲置 session，当 `updated_at` 超过 TTL 时触发。

```python
# autocompact.py:92-93
session.messages = kept_msgs       # 替换为最近 8 条
session.last_consolidated = 0      # 重置游标
```

流程：将未整合的消息拆分为"待归档前缀"和"保留后缀"两部分，前缀交给 LLM 生成摘要存入 metadata，只保留最近 8 条在 messages 中。

### 4. 全部清空

**触发条件**：用户执行 `/new` 命令。

```python
# builtin.py:103-104
snapshot = session.messages[session.last_consolidated:]
session.clear()                    # → self.messages = []
```

清空前会将未整合的消息交给 `archive()` 做最后的持久化归档。

---

## 三、不会修改 `session.messages` 的操作

### Token 过量整合（`maybe_consolidate_by_tokens`）

```python
# memory.py:645-665
chunk = session.messages[session.last_consolidated:end_idx]  # 读取
# ... LLM 摘要 ...
session.last_consolidated = end_idx  # 只推进游标
```

**只推进 `last_consolidated` 游标，不删除 messages 列表中的任何条目。** 旧消息仍在列表中，只是被游标"逻辑跳过"，不再进入 prompt。只有 `save()` 才会将游标的值持久化到 JSONL 文件的 metadata 行中。

---

## 四、变更汇总

```
session.messages 变更路径
├── 尾部追加（高频）
│   ├── add_message()          —— 正常对话
│   ├── messages.append()      —— 子代理结果 / 崩溃恢复
│   └── messages.extend()      —— 检查点恢复
├── 前缀截断（容量控制）
│   └── retain_recent_legal_suffix() → messages = 保留后缀
├── 全部替换（闲置压缩）
│   └── messages = kept_msgs   —— 旧消息归档为摘要
├── 全部清空（用户主动）
│   └── clear() → messages = []
│
└── 不修改 messages，只推进游标
    └── last_consolidated = N  —— Token 过量整合
```
