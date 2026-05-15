# Session 历史检索与截断机制

> 对应源码：[`nanobot/session/manager.py:get_history()`](nanobot/session/manager.py#L73)、[`nanobot/utils/helpers.py:find_legal_message_start()`](nanobot/utils/helpers.py#L142)

## 一、为什么要截断？

会话消息会随着对话无限增长。假设用户聊了 1000 轮：

- **全部塞进 prompt** → 超出模型上下文上限 → API 报错或性能暴跌
- **直接取最近 N 条** → 可能从中间切断一次工具调用 → **非法序列**

因此 `get_history()` 在把历史喂给 LLM 之前，会做**三层裁剪** + **边界合法性检查**。

---

## 二、三层裁剪流程

### 第 1 层：排除已归档消息

```python
unconsolidated = self.messages[self.last_consolidated:]
```

`last_consolidated` 表示"已经归档到文件的消息数量"。这些老消息不再直接参与 LLM prompt，只在需要深度回忆时通过其他机制检索。

### 第 2 层：消息数硬上限

```python
sliced = unconsolidated[-max_messages:]  # 默认取最近 120 条
```

只保留最近的 `max_messages` 条（默认 120）。

### 第 3 层：Token 预算（可选）

```python
if max_tokens > 0:
    for message in reversed(out):
        tokens = estimate_message_tokens(message)
        if kept and used + tokens > max_tokens:
            break
        kept.append(message)
        used += tokens
```

如果配置了 `max_tokens`，在消息数上限的基础上进一步按 token 数裁剪，从尾部向前累加，超出预算的丢弃。

---

## 三、什么是 "Legal Boundary"？

LLM 的工具调用遵循严格的**请求-响应模式**：

```
assistant: "我来查一下天气"  [tool_calls: [{id: "call_1", ...}]]
tool:      "北京晴，25°C"    [tool_call_id: "call_1"]
```

如果截断后变成下面这样，就是一个**孤儿 tool result**（orphaned tool result）—— 前面没有对应的 `assistant` 发起调用：

```
tool: "北京晴，25°C"  [tool_call_id: "call_1"]  ← 非法！没有对应的 assistant tool_calls
```

大多数 LLM API 会拒绝这种输入，或者模型会困惑。

### find_legal_message_start 的作用

```python
def find_legal_message_start(messages) -> int:
    declared: set[str] = set()
    start = 0
    for i, msg in enumerate(messages):
        role = msg.get("role")
        if role == "assistant":
            for tc in msg.get("tool_calls") or []:
                if isinstance(tc, dict) and tc.get("id"):
                    declared.add(str(tc["id"]))
        elif role == "tool":
            tid = msg.get("tool_call_id")
            if tid and str(tid) not in declared:
                start = i + 1
                declared.clear()
                # 重新统计 start 之后所有 assistant 声明的 tool_call ID
                for prev in messages[start : i + 1]:
                    if prev.get("role") == "assistant":
                        for tc in prev.get("tool_calls") or []:
                            if isinstance(tc, dict) and tc.get("id"):
                                declared.add(str(tc["id"]))
    return start
```

**扫描逻辑**：从左到右遍历消息列表
- 遇到 `assistant` 消息 → 记录它声明的 `tool_calls` ID
- 遇到 `tool` 消息 → 检查其 `tool_call_id` 是否已被声明
  - **如果没有** → 说明这是孤儿 tool result，前面的消息必须全部跳过
  - 返回一个安全的起始索引

**示例**：

| 索引 | 角色 | 内容 | 扫描结果 |
|-----|------|------|---------|
| 0 | user | "查天气" | — |
| 1 | assistant | "好的" | 声明 `call_1` |
| 2 | tool | "北京晴" | `call_1` 已声明 ✓ |
| 3 | assistant | "再查一个" | 声明 `call_2` |
| 4 | tool | "上海雨" | `call_2` 已声明 ✓ |

如果 `max_messages=3` 只取后 3 条（索引 2, 3, 4）：
- 索引 2 是 tool，`call_1` 未在前面声明 → **非法**
- `find_legal_message_start` 返回 `3`
- 最终保留索引 3, 4（`call_2` 完整）

---

## 四、额外的对齐策略

### 1. 从 user 开始

```python
for i, message in enumerate(sliced):
    if message.get("role") == "user":
        start = i
        sliced = sliced[start:]
        break
```

对话序列应该以 user 的问题开始，而不是以 assistant 的回答或 tool result 开始。这样 LLM 看到的上下文更自然、语义更连贯。

### 2. 保留主动推送（`_channel_delivery`）

```python
if i > 0 and sliced[i - 1].get("_channel_delivery"):
    start = i - 1
```

cron 或 heartbeat 的主动推送（assistant 先说，用户后回复）需要保留那条推送消息。如果不保留，用户的回复看起来就像在自言自语，LLM 无法理解上下文。

---

## 五、完整数据流

```
session.messages (全部历史)
    |
    v
排除已归档 (last_consolidated)
    |
    v
取最近 N 条 (-max_messages)
    |
    v
对齐到第一个 user（或保留前一个 _channel_delivery）
    |
    v
find_legal_message_start: 删除开头的孤儿 tool result
    |
    v
[可选] 按 max_tokens 进一步裁切（从尾部向前）
    |
    v
再次对齐到 user + 再次检查 tool 边界
    |
    v
返回给 LLM
```

---

## 六、总结

| 问题 | 解决方案 |
|------|---------|
| 历史太长，塞不下 | 消息数截断（默认 120 条）+ token 预算截断 |
| 截断破坏了 tool 调用链 | `find_legal_message_start()` 删除孤儿 tool result |
| 截断后从 assistant 开始，语义奇怪 | 对齐到第一个 `user` 消息 |
| cron/heartbeat 推送后，用户回复上下文丢失 | 保留 `_channel_delivery` 消息 |

这套机制保证了：**无论怎么裁剪，喂给 LLM 的历史始终是一个完整、合法、语义连贯的对话片段**。
