# Token 预算估算机制

> 当 token 超过预算时，会触发 history.jsonl 的归档。本文梳理 nanobot 中 token 预算如何定义、当前 prompt 占用如何估算、以及归档的触发与目标。
>
> 对应源码：[`agent/memory.py:Consolidator`](nanobot/agent/memory.py#L443)、[`utils/helpers.py`](nanobot/utils/helpers.py#L327) 的 token 估算函数。

---

## 一、什么是「Token 预算」？

预算定义在 [agent/memory.py:535-538](nanobot/agent/memory.py#L535-L538)：

```python
@property
def _input_token_budget(self) -> int:
    return self.context_window_tokens - self.max_completion_tokens - self._SAFETY_BUFFER
```

三个量分别是：

| 量 | 来源 | 默认值 |
|---|---|---|
| `context_window_tokens` | provider 配置的模型上下文窗口 | 65536（[config/schema.py:77](nanobot/config/schema.py#L77)） |
| `max_completion_tokens` | provider 一次回答最多生成多少 token | 8192（[config/schema.py:76](nanobot/config/schema.py#L76)） |
| `_SAFETY_BUFFER` | 给 tokenizer 估算误差留的缓冲 | 1024（[agent/memory.py:448](nanobot/agent/memory.py#L448)） |

含义：**"模型总容量 − 留给回复的位置 − 误差缓冲"** = 还能塞多少输入。这样保证发请求时 input + output 不会撞上下文上限。

---

## 二、当前 prompt 实际占了多少 token？

入口在 [agent/memory.py:512-533](nanobot/agent/memory.py#L512-L533) 的 `estimate_session_prompt_tokens`：

1. 用 `session.get_history(max_messages=0, include_timestamps=True)` 拿到**未归档的全部历史**
   （注意 `max_messages=0` 会被规整成 120，跟正常请求一致；详见 [session/manager.py:86](nanobot/session/manager.py#L86)）
2. 通过 `_build_messages()` 拼出真实要发给 LLM 的 messages（system prompt + 历史 + 占位 user 消息 `"[token-probe]"`）
3. 调 `estimate_prompt_tokens_chain` 算 token

`estimate_prompt_tokens_chain` 在 [utils/helpers.py:410-429](nanobot/utils/helpers.py#L410-L429) 是个二级 fallback：

- **优先**：调 provider 自带的 `estimate_prompt_tokens` 方法（比如 Anthropic 的 `count_tokens` API，最准）
- **fallback**：走 [utils/helpers.py:327-369](nanobot/utils/helpers.py#L327-L369) 的 tiktoken 估算

tiktoken 估算的逻辑：

```python
parts = []
for msg in messages:
    parts.append(msg.content)            # 文本内容
    parts.append(json.dumps(tool_calls)) # 工具调用 JSON 化
    parts.append(reasoning_content)       # 思考链
    parts.append(name / tool_call_id)    # 角色元信息
parts.append(json.dumps(tools))          # 工具定义 schema
encoded = enc.encode("\n".join(parts))
return len(encoded) + len(messages) * 4  # 每条消息 +4 token 作为 framing overhead
```

那个 `* 4` 是 OpenAI 自己披露的"role/name 边界字符的固定开销"，业界常用的近似公式。

---

## 三、什么时候触发归档？

[agent/memory.py:589-627](nanobot/agent/memory.py#L589-L627) 的 `maybe_consolidate_by_tokens`：

```python
budget = self._input_token_budget                      # ① 算预算
target = int(budget * self.consolidation_ratio)        # ② 算目标线（默认 50%）
estimated, source = self.estimate_session_prompt_tokens(...)  # ③ 算当前占用

if estimated < budget:    # ④ 没超 → 直接 return，啥也不做
    return
```

也就是说：**只有 `estimated >= budget` 才进入归档流程**。`< budget` 就算占了 99% 也按兵不动。

---

## 四、归档归到哪里？

进入归档循环后（[agent/memory.py:630-681](nanobot/agent/memory.py#L630-L681)）：

```python
for round_num in range(5):                # 最多 5 轮
    if estimated <= target:               # 降到目标线就停
        break
    boundary = self.pick_consolidation_boundary(
        session,
        max(1, estimated - target)        # 这一轮要砍掉多少 token
    )
    chunk = session.messages[last_consolidated:end_idx]
    summary = await self.archive(chunk)   # 写到 history.jsonl
    session.last_consolidated = end_idx   # 推进游标
    estimated, source = self.estimate_session_prompt_tokens(...)  # 重测
```

关键点：

- **目标不是"压到 budget 以下"，是"压到 `budget * consolidation_ratio`"**（默认 50%）。这样下一次小幅增长不会立刻又触发归档，避免颠簸。
- `pick_consolidation_boundary` 在 [agent/memory.py:490-510](nanobot/agent/memory.py#L490-L510)，沿着消息列表往后扫，**只在 `user` 角色处停**——保证砍出来的是完整对话轮次，避免割裂 tool_call 链。它使用的单条消息估算函数是：

[utils/helpers.py:372-407](nanobot/utils/helpers.py#L372-L407) 的 `estimate_message_tokens`：

```python
parts = [content, tool_calls JSON, name, tool_call_id, reasoning_content]
return max(4, len(enc.encode("\n".join(parts))) + 4)  # 同样 +4 overhead
```

最低保底 4 token（即使是空消息也算 4，对应 framing overhead）。

---

## 五、整条链路串起来

```
context_window_tokens (65536)
   - max_completion_tokens (8192)
   - SAFETY_BUFFER (1024)
   ────────────────────────────
   = budget (≈ 56320)
        │
        ├─ target = budget * 0.5 ≈ 28160   ← 归档目标线
        │
        └─ 触发条件：estimated ≥ budget
             │
             ▼
        每轮归档 (estimated - target) tokens
             │
             ├─ 在 user 边界处切 (pick_consolidation_boundary)
             ├─ 单条 token 用 estimate_message_tokens 累计
             └─ 写入 history.jsonl，推进 last_consolidated
                                       ↑
        归档后这部分老消息不再进 get_history()
        (session/manager.py:85)
```

---

## 六、几个容易踩的细节

1. **预算和 `get_history(max_tokens=...)` 不是同一个东西**。
   - `_input_token_budget` 是**归档触发**用的全局红线。
   - `get_history()` 里的 `max_tokens` 是**单次请求**临时再砍一刀的尾部预算（[session/manager.py:126-156](nanobot/session/manager.py#L126-L156)），跟归档独立。

2. **估算优先用 provider 真实计数**。如果你的 provider 实现了 `estimate_prompt_tokens`，会比 tiktoken 准；否则统一退化到 cl100k_base 编码（GPT-4 系列编码），对 Claude 是近似值。

3. **`consolidation_ratio` 是滞后阈值**。它把"触发线"和"恢复线"分开（hysteresis），如果设成 1.0，归档会非常频繁；设成 0.5 是经验默认值。

4. **5 轮上限**（`_MAX_CONSOLIDATION_ROUNDS`）防止 LLM 摘要质量太差导致死循环。每轮失败时 `archive()` 会自动 raw-dump 防止丢消息。

---

## 七、关键源码索引

| 功能 | 位置 |
|------|------|
| 预算定义 | [`agent/memory.py:_input_token_budget`](nanobot/agent/memory.py#L535) |
| 当前 prompt 估算 | [`agent/memory.py:estimate_session_prompt_tokens`](nanobot/agent/memory.py#L512) |
| 估算二级 fallback | [`utils/helpers.py:estimate_prompt_tokens_chain`](nanobot/utils/helpers.py#L410) |
| tiktoken 估算 | [`utils/helpers.py:estimate_prompt_tokens`](nanobot/utils/helpers.py#L327) |
| 单条消息估算 | [`utils/helpers.py:estimate_message_tokens`](nanobot/utils/helpers.py#L372) |
| 归档主循环 | [`agent/memory.py:maybe_consolidate_by_tokens`](nanobot/agent/memory.py#L589) |
| 归档边界选择 | [`agent/memory.py:pick_consolidation_boundary`](nanobot/agent/memory.py#L490) |
| 状态条预算显示 | [`utils/helpers.py:build_status_content`](nanobot/utils/helpers.py#L432) |
