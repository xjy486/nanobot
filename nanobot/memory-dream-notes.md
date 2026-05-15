# Dream 记忆整理机制笔记

> 基于 nanobot 源码分析，涵盖 Dream 的两阶段 LLM 调用、游标推进、git 版本控制等完整链路。

---

## 一、定位

Dream 是**后台周期性**的记忆整理子系统。它消费 `history.jsonl` 中的对话历史条目，经过两阶段 LLM 处理后，对 `MEMORY.md`、`SOUL.md`、`USER.md` 做精确局部编辑，并可在 `skills/` 下创建新技能。

与 Consolidator 的关系：Consolidator 是**在线生产者**，把对话压缩摘要写入 `history.jsonl`；Dream 是**离线消费者**，读取 `history.jsonl` 整理长期记忆。

源码：[agent/memory.py:694-1003](agent/memory.py#L694)

---

## 二、关键常量

| 常量                               | 值     | 说明                                                   |
| ---------------------------------- | ------ | ------------------------------------------------------ |
| `_STALE_THRESHOLD_DAYS`            | 14     | 行龄标注阈值，超过此天数的 MEMORY.md 行会被标注 `← Nd` |
| `_MEMORY_FILE_MAX_CHARS`           | 32,000 | Phase 1/2 prompt 中 MEMORY.md 预览的字符上限           |
| `_SOUL_FILE_MAX_CHARS`             | 16,000 | Phase 1/2 prompt 中 SOUL.md 预览的字符上限             |
| `_USER_FILE_MAX_CHARS`             | 16,000 | Phase 1/2 prompt 中 USER.md 预览的字符上限             |
| `_HISTORY_ENTRY_PREVIEW_MAX_CHARS` | 4,000  | 每条 history 条目在 prompt 中的预览字符上限            |

### 配置常量（DreamConfig，[config/schema.py:35](config/schema.py#L35)）

| 字段                 | 默认值 | 说明                                     |
| -------------------- | ------ | ---------------------------------------- |
| `interval_h`         | 2      | 调度间隔（小时）                         |
| `max_batch_size`     | 20     | 每次 run() 最多处理的 history 条目数     |
| `max_iterations`     | 15     | Phase 2 AgentRunner 最多 tool 调用次数   |
| `annotate_line_ages` | `True` | 是否在 Phase 1 中给 MEMORY.md 加行龄标注 |
| `model_override`     | `None` | 可选，Dream 专用模型（不填则用主模型）   |

---

## 三、触发方式

### 3.1 定时调度（主路径）

在 [cli/commands.py:919-924](cli/commands.py#L919-L924) 注册为受保护的系统 cron job：

```python
cron.register_system_job(CronJob(
    id="dream",
    name="dream",
    schedule=dream_cfg.build_schedule(config.agents.defaults.timezone),
    payload=CronPayload(kind="system_event"),
))
```

调度表达式由 `DreamConfig.build_schedule()` 生成——默认 `every 2h`，也支持 legacy cron 表达式覆盖。

在 [cli/commands.py:708-710](cli/commands.py#L708-L710)，cron 回调直接调 `agent.dream.run()`，不经过 agent loop。

### 3.2 手动触发

`/dream` 命令（[command/builtin.py:116](command/builtin.py#L116)）：异步执行 `dream.run()`，完成后返回耗时或"nothing to process"。

### 3.3 保护机制

Dream job 是**受保护的系统 job**，/cron remove 会拒绝删除（[agent/tools/cron.py:277-282](agent/tools/cron.py#L277-L282)）。

---

## 四、完整流程

```
cron 触发 / /dream 命令
    │
    ▼
dream.run()
    │
    ├── get_last_dream_cursor()          → 上次处理到的 history cursor
    ├── read_unprocessed_history(since)  → 取未处理的条目
    │       └── 空 → return False
    │
    ├── 截断到 max_batch_size 条
    │
    ├── 构建 prompt 上下文:
    │       ├── history_text           (每条预览截断到 4k 字符)
    │       ├── current_memory          (_annotate_with_ages() 加龄标注)
    │       ├── current_soul            (截断到 16k)
    │       └── current_user            (截断到 16k)
    │
    ▼
┌─────────────────────────────────────────────┐
│ Phase 1: 分析 (plain LLM, 无 tools)          │
│   system prompt: dream_phase1.md             │
│   输入: history_text + file_context           │
│   输出: analysis 文本 ( [FILE] / [FILE-REMOVE] / [SKILL] )  │
└─────────────────────────────────────────────┘
    │
    │ 失败 → return False
    │
    ▼
┌─────────────────────────────────────────────┐
│ Phase 2: 执行 (AgentRunner + 文件工具)        │
│   system prompt: dream_phase2.md             │
│   输入: analysis + file_context + skills 列表│
│   工具: read_file, edit_file, write_file      │
│                                              │
│   edit_file  → 局部编辑 MEMORY/SOUL/USER     │
│   write_file → 创建 skills/<name>/SKILL.md   │
│   read_file  → 参考 skill-creator 模板       │
└─────────────────────────────────────────────┘
    │
    ▼
后处理:
    ├── set_last_dream_cursor(batch[-1].cursor)  ← 无论如何都推进
    ├── compact_history()                         ← 清理超量条目
    ├── 统计 tool_events → changelog
    │
    └── 有变更 → git.auto_commit()
            ├── author: nanobot <nanobot@dream>
            ├── tracked: SOUL.md, USER.md, memory/MEMORY.md
            └── commit message: "dream: <ts>, N change(s)" + analysis
```

### 4.1 `file_context` 是什么

在 Phase 1 和 Phase 2 中传入的 `file_context` 是**当前 memory 文件快照**，源码在 [agent/memory.py:892-897](agent/memory.py#L892-L897)：

```python
file_context = (
    f"## Current Date\n{current_date}\n\n"
    f"## Current MEMORY.md ({len(current_memory)} chars)\n{current_memory}\n\n"
    f"## Current SOUL.md ({len(current_soul)} chars)\n{current_soul}\n\n"
    f"## Current USER.md ({len(current_user)} chars)\n{current_user}"
)
```

| 组成部分       | 来源                  | 预处理                                                |
| -------------- | --------------------- | ----------------------------------------------------- |
| `Current Date` | `datetime.now()`      | 无，供 LLM 判断内容时效                               |
| `MEMORY.md`    | `store.read_memory()` | `_annotate_with_ages()` 加行龄标注 → 截断到 32k chars |
| `SOUL.md`      | `store.read_soul()`   | 截断到 16k chars                                      |
| `USER.md`      | `store.read_user()`   | 截断到 16k chars                                      |

然后拼入 Phase 1 的 user message：

```python
phase1_prompt = (
    f"## Conversation History\n{history_text}\n\n{file_context}"
)
```

这样 LLM 一次性看到**近期对话历史 + 当前记忆文件全貌**，才能对比判断哪些是新事实、哪些已重复、哪些已过时。Phase 2 同样接收这份 `file_context`（配合 analysis 结果）来执行编辑。

> 注意：Phase 1/2 prompt 中的文件内容是**截断后的预览**，主要供 LLM 分析。Phase 2 实际编辑时，`edit_file` 工具操作的是完整文件。

---

## 五、Phase 1 分析提示词详解

模板：[templates/agent/dream_phase1.md](templates/agent/dream_phase1.md)

简体中文翻译

```

你有**两项同等重要**的任务：
1. 从对话历史中提取全新客观事实
2. 对现有记忆文件进行去重处理——找出并标记冗余、重叠或过时的内容，**即便该内容未在本次对话历史中被提及**

每条发现结果单独占一行输出：
- `[FILE]` 原子化事实（此前未存入记忆）
- `[FILE-REMOVE]` 删除原因说明
- `[SKILL]` 短横线命名法名称：可复用模式的单行描述

## 文件分类
- USER（用户身份、个人偏好）
- SOUL（机器人行为模式、语气风格）
- MEMORY（知识信息、项目背景）

## 规则说明
- 原子化事实示例：写**“养了一只名叫露娜的猫”**，而非笼统表述**“聊过宠物养护相关话题”**
- 信息修正格式：`[USER] 常住地为东京，非大阪`
- 需收录**经用户确认认可**的处理方式

## 去重规则——扫描全部记忆文件，排查以下冗余类型
1. 同一事实在多处重复表述（例如“使用中文沟通”同时出现在 USER.md 和多条 MEMORY.md 记录中）
2. 不同板块内容重叠、嵌套，讲述同一主题
3. MEMORY.md 中已存在的内容，在 USER.md、SOUL.md 中已有收录（MEMORY.md 不得重复存放固定档案内容）
4. 表述冗长的条目，可精简凝练且不丢失有效信息

每发现一处重复内容，对**权威性较低**的副本标注 `[FILE-REMOVE]`（优先保留事实在其标准归属文件中）

## 内容过时处理规则
MEMORY.md 条目末尾可能带有 `← N天` 后缀，表示距上次修改已过去 N 天：
1. SOUL.md、USER.md 无时间标注，属于**永久档案**，仅在信息有误时修正更新
2. 时间仅代表最后编辑时间，不直接作为删除依据
3. 内容判定原则：用户习惯、偏好、性格特质永久有效，不受时间影响
4. 仅清理**客观上已失效**的内容：已结束的事件、已解决的待办、被新方案替代的旧方法
5. 标注 `← N天` 且天数超过设定过时阈值的条目，需重点复核，但**不自动删除**
6. 删除原则：优先删除单条条目，而非整块章节

## 可复用技能识别
同时满足以下所有条件时，标记为 `[SKILL]`：
1. 某一固定可复用工作流程在对话历史中**出现2次及以上**
2. 包含清晰执行步骤（非模糊偏好，如“喜欢简洁回复”这类不算）
3. 具备独立成一套操作指引的价值（非读取文件这类琐碎小事）
4. 无需担心技能重复，后续阶段会与已有技能做比对校验

## 禁止收录内容
请勿录入：实时天气、临时状态、暂时性报错、对话无意义寒暄话术。

若无任何内容需要更新，直接标注 `[SKIP]` 即可。
```

### 5.1 输出格式

每行一条指令，三种类型：

| 指令            | 含义                        | 示例                                                        |
| --------------- | --------------------------- | ----------------------------------------------------------- |
| `[FILE]`        | 需添加到某文件的原子事实    | `[USER] has a cat named Luna`                               |
| `[FILE-REMOVE]` | 需从某文件删除的内容 + 原因 | `[MEMORY-REMOVE] duplicate of USER.md entry`                |
| `[SKILL]`       | 可复用的工作流模式          | `[SKILL] deploy-check: verify deployment health after push` |

目标文件：`USER`（身份/偏好）、`SOUL`（bot 行为/语调）、`MEMORY`（知识/项目上下文）。

### 5.2 去重规则

扫描所有 memory 文件的冗余模式：

- 同一事实在多处出现
- 重叠或嵌套的同类主题段落
- MEMORY.md 中已有 USER.md/SOUL.md 涵盖的信息
- 可压缩的冗长条目

对每个冗余项输出 `[FILE-REMOVE]`，优先保留在**规范位置**的副本。

### 5.3 老化判断

- MEMORY.md 行可能有 `← Nd` 后缀（git blame 自动标注，N > 14 天才标注）
- SOUL.md 和 USER.md **无年龄标注**——它们是永久文件，只在有修正时更新
- 年龄只表示"上次被触碰的时间"，**不是**自动删除的依据
- 用户习惯/偏好/性格特征是永久的，不论年龄
- 仅删除**客观上过时**的内容：已过去的事件、已解决的追踪、已替代的方案
- 删除时优先删单条 item 而非整节

### 5.4 技能发现

同时满足以下条件才标记 `[SKILL]`：

1. 特定的、可重复的工作流在对话历史中出现 ≥2 次
2. 包含明确的步骤（不是"喜欢简洁回答"这种模糊偏好）
3. 足够复杂，值得独立成指令集
4. 不用关心重复——Phase 2 会检查已有技能

`[SKIP]`：无事可做时输出。

---

## 六、Phase 2 执行提示词详解

模板：[templates/agent/dream_phase2.md](templates/agent/dream_phase2.md)
简体中文翻译

```
根据以下分析更新记忆文件。
- 【文件条目】：将描述内容添加至对应合适文件中
- 【文件删除条目】：从记忆文件中删除对应内容
- 【技能条目】：通过`write_file`在 `skills/<name>/SKILL.md` 路径下新建技能文件

## 文件路径（相对于工作区根目录）
- SOUL.md
- USER.md
- memory/MEMORY.md
- skills/<name>/SKILL.md（仅适用于【技能条目】）

**严禁自行猜测文件路径。**

## 编辑规则
- 直接编辑：下文已提供文件内容，无需调用`read_file`
- 原文匹配需完全一致：原样引用旧文本，包含周边空行以确保精准唯一匹配
- 同一文件的多处修改合并为一次`edit_file`调用
- 删除操作规范：将板块标题及下方所有项目符号内容作为旧文本，新文本置空
- 仅做精准局部修改，**禁止重写整个文件**
- 若无任何需要更新的内容，直接终止流程，不调用任何工具

## 技能创建规则（适用于【技能条目】）
- 通过`write_file` 创建 `skills/<name>/SKILL.md` 文件
- 写入前，读取 `{{ skill_creator_path }}` 文件作为格式参考（包含前置元数据结构、命名规范、质量标准）
- **去重校验**：读取下方已列出的现有技能，确认新建技能无功能冗余；若已有技能可覆盖相同工作流程，则跳过本次创建
- 文件需包含 YAML 前置元数据，必填字段：名称、描述
- SKILL.md 篇幅控制在2000字以内，内容简洁、可直接执行
- 内容必须包含：适用场景、操作步骤、输出格式、至少一个示例
- **禁止覆盖已有技能**：若对应技能目录已存在，则跳过创建
- 需参考智能体可调用的指定工具（read_file、write_file、exec、web_search 等）
- 技能为指令说明集合，**不得包含实现代码**

## 内容质量要求
- 每一行内容都需具备独立有效信息
- 层级标题清晰，下属条目简洁凝练
- 精简内容（非删除）时：保留核心信息，删减冗余赘述
- 无法确定是否需要删除的内容，予以保留并标注「(需核验时效性)」
```

### 6.1 核心指令

根据 Phase 1 的 analysis 结果执行文件变更：

- `[FILE]` → 将内容添加到对应文件
- `[FILE-REMOVE]` → 从对应文件删除内容
- `[SKILL]` → 用 `write_file` 创建 `skills/<name>/SKILL.md`

### 6.2 编辑规则

- 直接编辑文件内容（已在 prompt 中提供，无需 `read_file`）
- 使用精确文本匹配作为 `old_text`，包含周围空行确保唯一
- **同一文件的多次修改合并为一次 `edit_file` 调用**
- 删除时：section header + 所有 bullets 作为 old_text，new_text 为空
- 仅做外科手术式编辑——绝不重写整个文件
- 无事可做则直接停止，不调用工具

### 6.3 技能创建规则

- 用 `write_file` 创建 `skills/<name>/SKILL.md`
- 创建前先 `read_file` 读取 skill-creator 模板作为格式参考
- **去重检查**：对比已有技能列表，功能重复则跳过
- 包含 YAML frontmatter（name + description）
- 控制在 2000 词内
- 包含：何时使用、步骤、输出格式、至少一个示例
- 不覆盖已有技能目录
- 技能是**指令集**，不是代码

---

## 七、行龄标注机制 —— `_annotate_with_ages()`

源码：[agent/memory.py:805-849](agent/memory.py#L805-L849)

### 7.1 数据流

```
MEMORY.md (working tree)
    │
    ▼
git.line_ages("memory/MEMORY.md")
    │  └── dulwich.porcelain.annotate()  ← git blame
    │  └── _compute_line_ages()           ← 时间戳 → age_days
    │
    ▼
逐行比对:
  - 空行 → 原样保留
  - age_days > 14 → 追加 "  ← Nd"
  - age_days ≤ 14 → 原样保留
```

### 7.2 安全保护

**行数不匹配时跳过**：如果 HEAD-blob 的行数与 working-tree 内容的行数不一致（可能因为未提交的编辑），整个标注过程跳过，返回未标注的原始内容。这避免了把第 3 行的年龄标到第 5 行这种错位标注。

`LineAge` 数据结构（[utils/gitstore.py:28-32](gitstore.py#L28-L32)）：

```python
@dataclass
class LineAge:
    age_days: int  # days since last modification
```

---

## 八、游标与上下文注入

### 8.1 dream_cursor

- 存储文件：`memory/.dream_cursor`（[memory.py:53](agent/memory.py#L53)）
- `get_last_dream_cursor()`：读取上次处理到的 history cursor
- `set_last_dream_cursor(cursor)`：Phase 2 结束后推进到 `batch[-1].cursor`
- **无论 Phase 2 成功与否，游标都会推进**——避免 LLM 降级时重复处理同一批条目

### 8.2 上下文注入链路

Dream cursor 直接影响 system prompt 中的 "Recent History" 段（[context-assembly-notes.md §3](context-assembly-notes.md)）：

```
ContextBuilder.build_system_prompt()
    │
    ├── read_unprocessed_history(since_cursor=get_last_dream_cursor())
    │       └── 只取 cursor > dream_cursor 的条目
    │
    └── 截断到最近 50 条 / 32k 字符 → "# Recent History" 段
```

**含义**：已被 Dream 消化过的条目不再出现在 Recent History 中，避免 LLM 看到"已整理过的旧闻"。

---

## 九、Git 版本控制

### 9.1 自动提交

Phase 2 完成后，若有实际文件变更（tool_events 中有 status=="ok" 的条目），自动执行 git commit：

- 作者/提交者：`nanobot <nanobot@dream>`
- 跟踪文件：`SOUL.md`、`USER.md`、`memory/MEMORY.md`
- 提交信息格式：`dream: <batch最后条目时间戳>, <N> change(s)` + 空行 + Phase 1 analysis 全文

源码：[utils/gitstore.py:121-153](gitstore.py#L121-L153)

### 9.2 版本浏览与回滚

| 命令             | 功能                                     | 源码                                              |
| ---------------- | ---------------------------------------- | ------------------------------------------------- |
| `/dream-log`     | 查看最近一次 Dream 的 diff（或指定 sha） | [command/builtin.py:217](command/builtin.py#L217) |
| `/dream-restore` | 列出最近 10 次提交，或恢复到指定版本     | [command/builtin.py:267](command/builtin.py#L267) |

`/dream-restore <sha>` 内部调用 `git.revert(sha)`，创建一个新的"安全提交"来逆转变更，而非破坏性 reset。

---

## 十、工具集

Phase 2 的 AgentRunner 装备了最小工具集（[memory.py:753-773](agent/memory.py#L753-L773)）：

| 工具         | 允许范围                        | 用途                                     |
| ------------ | ------------------------------- | ---------------------------------------- |
| `read_file`  | workspace + builtin skills 目录 | 读取 skill-creator 模板、已有 skill 文件 |
| `edit_file`  | workspace                       | 局部编辑 MEMORY.md / SOUL.md / USER.md   |
| `write_file` | `skills/` 目录                  | 创建新技能 `skills/<name>/SKILL.md`      |

注意：`write_file` 的 `allowed_dir` 限定在 `skills/` 下，防止 Phase 2 LLM 意外覆盖其他文件。

---

## 十一、容错设计

| 场景                      | 处理方式                                                           |
| ------------------------- | ------------------------------------------------------------------ |
| Phase 1 LLM 调用失败      | `return False`，不推进游标，不执行 Phase 2                         |
| Phase 2 AgentRunner 异常  | 仍然推进游标（避免重复处理），记录 warning 日志                    |
| Phase 2 返回非 completed  | 推进游标，记录 warning（`stop_reason` 可能是 `max_iterations` 等） |
| git blame 失败/行数不匹配 | `_annotate_with_ages()` 返回原始内容，不阻塞流程                   |
| line_ages 返回空          | 跳过标注，feed 原始 MEMORY.md                                      |
| git 未初始化              | `auto_commit()` 返回 None，不报错                                  |
| 无 new history entries    | `run()` 返回 `False`，不做任何事                                   |

---

## 十二、与 Consolidator / AutoCompact 的关系

```
在线（每次请求前）:
  Consolidator.maybe_consolidate_by_tokens()
      │
      └── archive() → append_history(summary) → history.jsonl

空闲（session TTL 过期）:
  AutoCompact._archive()
      │
      └── consolidator.archive() → append_history(summary) → history.jsonl

离线（cron 定时）:
  Dream.run()
      │
      ├── read_unprocessed_history() ← 读 history.jsonl
      ├── Phase 1 + Phase 2 → 编辑 MEMORY/SOUL/USER
      └── git auto_commit
```

Consolidator 和 AutoCompact 是**生产者**，往 `history.jsonl` 追加摘要；Dream 是**消费者**，消化这些摘要整理长期记忆。三者的分工：

| 维度                  | Consolidator         | AutoCompact        | Dream                         |
| --------------------- | -------------------- | ------------------ | ----------------------------- |
| 频率                  | 高频（token 超限时） | 低频（分钟级空闲） | 定时（2h）                    |
| 写入目标              | history.jsonl        | history.jsonl      | MEMORY.md / SOUL.md / USER.md |
| LLM 调用              | 单次（无 tools）     | 单次（无 tools）   | 两阶段（Phase 2 有 tools）    |
| 是否推进 dream_cursor | 否                   | 否                 | 是                            |
| Git 提交              | 否                   | 否                 | 是                            |

---

## 十三、相关文件索引

| 文件                                                               | 核心职责                                         |
| ------------------------------------------------------------------ | ------------------------------------------------ |
| [agent/memory.py](agent/memory.py)                                 | `Dream` 类主实现、`_annotate_with_ages`、`run()` |
| [templates/agent/dream_phase1.md](templates/agent/dream_phase1.md) | Phase 1 系统提示词模板                           |
| [templates/agent/dream_phase2.md](templates/agent/dream_phase2.md) | Phase 2 系统提示词模板                           |
| [config/schema.py](config/schema.py)                               | `DreamConfig` Pydantic 模型                      |
| [utils/gitstore.py](utils/gitstore.py)                             | `line_ages()`、`LineAge`、`auto_commit()`        |
| [cli/commands.py](cli/commands.py)                                 | Dream cron job 注册、调度回调                    |
| [command/builtin.py](command/builtin.py)                           | `/dream`、`/dream-log`、`/dream-restore` 命令    |
| [agent/loop.py](agent/loop.py)                                     | `Dream` 实例化、provider 热切换                  |
| [agent/tools/cron.py](agent/tools/cron.py)                         | Dream 系统 job 保护逻辑                          |
