# nanobot Skills 系统笔记

Skills 是 nanobot 的扩展能力机制。本文档记录 skills 的加载、使用、生命周期等完整流程。

---

## 1. 核心架构认知

**Skills 不是 Tool，是注入到系统提示词中的 Markdown 指令。**

- 没有 `use_skill` 工具，没有 `SkillTool` 类，没有动态注册机制
- Agent "使用"一个 skill 的方式：`read_file` 读取其 `SKILL.md`，然后按指令行事
- Skill 本身不执行任何代码——它只是教会 agent 如何组合使用已有的 tool（如 `exec`、`grep`、`read_file`）

---

## 2. Skill 的目录结构

```
skill-name/
  SKILL.md              ← 必须。YAML frontmatter + Markdown 正文
  scripts/              ← 可选。可执行脚本（Python、Bash 等）
  references/           ← 可选。补充文档，agent 需要时 read_file 读取
  assets/               ← 可选。输出用的静态文件（模板、图标等）
```

### SKILL.md 的结构

```markdown
---
name: my                             # 必须，hyphen-case，须与目录名一致
description: Check and set ...       # 必须，自然语言，含触发条件
always: true                         # 可选，true 则每次对话都加载到系统提示词
metadata:
  nanobot:                           # 或 openclaw（向后兼容）
    requires:
      bins: [gh, curl]               # 依赖的 CLI 工具
      env: [GITHUB_TOKEN]            # 依赖的环境变量
    emoji: "🔧"                      # 图标
    install: "brew install gh"       # 安装说明
    os: [darwin, linux]              # 平台限制
    always: true                     # 另一种声明 always 的位置
license: MIT                         # 可选
allowed-tools: [read_file, exec]     # 可选
---

# 正文：给 agent 的行为指令、规则、示例
```

---

## 3. Skill 的来源与发现

### 两个来源（优先级从高到低）

| 来源 | 路径 | source 标记 |
|------|------|------------|
| **workspace** | `<workspace>/skills/<name>/SKILL.md` | `"workspace"` |
| **builtin** | `nanobot/skills/<name>/SKILL.md` | `"builtin"` |

workspace skill 会**覆盖（shadow）**同名 builtin skill。`disabled_skills` 配置项可以禁用任意 skill。

### 发现逻辑

[agent/skills.py:35-49](agent/skills.py#L35-L49) `_skill_entries_from_dir()`:
1. 扫描目标目录下所有子目录
2. 检查子目录中是否有 `SKILL.md`
3. 目录名即为 skill name
4. **只扫描一层，不递归**

### 启动时初始化链

```
AgentLoop.__init__()                          [agent/loop.py:247]
  └─ ContextBuilder(workspace, ...)           [agent/loop.py:247]
       └─ SkillsLoader(workspace, ...)        [agent/context.py:29]
```

`SkillsLoader` 创建时**不做扫描**，所有 `list_skills()` / `load_skill()` 调用都是懒加载——调用时才读磁盘。

---

## 4. Skills 如何进入 Agent 的系统提示词

### 整体流程

```
build_system_prompt()                          [agent/context.py:31-66]
  │
  ├─ 1. identity.md           ← 身份、workspace、channel
  ├─ 2. bootstrap files       ← SOUL.md / USER.md / TOOLS.md / AGENTS.md
  ├─ 3. MEMORY.md             ← 跨会话持久记忆
  │
  ├─ 4. always: true skills   ← 全文加载到 "Active Skills" 段
  │     get_always_skills()   ← [agent/skills.py:203-213]
  │     load_skills_for_context() ← [agent/skills.py:94-109]
  │
  ├─ 5. skills summary        ← 非 always 技能的摘要列表
  │     build_skills_summary()← [agent/skills.py:111-142]
  │     render_template("agent/skills_section.md", ...)
  │
  └─ 6. recent history        ← 最近的对话历史摘要
```

### 两类技能的呈现方式对比

| 类型 | 判定条件 | 在系统提示词中的形态 | 示例 |
|------|----------|---------------------|------|
| **always 技能** | frontmatter 中 `always: true` 且 requirements 满足 | 全文加载（去掉 YAML frontmatter），放在 `# Active Skills` 下 | `my`、`memory` |
| **普通技能** | 非 always，且 requirements 满足 | 仅摘要（名称 + 描述 + 文件路径），放在 `# Skills` 下 | `github`、`weather`、`tmux` |

### Skills 摘要模板

[templates/agent/skills_section.md](templates/agent/skills_section.md):
```markdown
# Skills

The following skills extend your capabilities. To use a skill, 
read its SKILL.md file using the read_file tool.
Unavailable skills need dependencies installed first...

- **github** — GitHub interaction via gh CLI  `path/to/github/SKILL.md`
- **tmux** — Tmux session management  `path/to/tmux/SKILL.md`
```

Agent 看到这个列表后，自行决定何时 `read_file` 哪个 skill 的 SKILL.md。

---

## 5. Skill 的需求检查

[agent/skills.py:189-196](agent/skills.py#L189-L196) `_check_requirements()`:

```python
def _check_requirements(self, skill_meta: dict) -> bool:
    requires = skill_meta.get("requires", {})
    required_bins = requires.get("bins", [])     # shutil.which() 检查
    required_env_vars = requires.get("env", [])   # os.environ.get() 检查
    return all(shutil.which(cmd) for cmd in required_bins) and \
           all(os.environ.get(var) for var in required_env_vars)
```

- 不满足依赖的 skill 仍会出现在摘要中，但标记 `(unavailable: CLI: gh)`
- `filter_unavailable=True`（默认）时，`list_skills()` 会过滤掉不可用的
- `get_always_skills()` 也默认使用 `filter_unavailable=True`，因此依赖缺失的 always skill 不会被加载

---

## 6. Skill 的 Frontmatter 解析

[agent/skills.py:215-242](agent/skills.py#L215-L242) `get_skill_metadata()`:

```
load_skill(name) → 读文件 → 正则匹配 ---/--- → yaml.safe_load() → dict
```

正则：[agent/skills.py:15-18](agent/skills.py#L15-L18)
```python
_STRIP_SKILL_FRONTMATTER = re.compile(
    r"^---\s*\r?\n(.*?)\r?\n---\s*\r?\n?", re.DOTALL
)
```

`metadata` 字段（嵌套的 `nanobot` 或 `openclaw` key）支持两种格式：
- 已解析的 YAML dict（yaml.safe_load 直接产出）
- JSON 字符串（json.loads 再解析一次）

兼容性设计：`openclaw` key 是向后兼容旧名称，代码查找的顺序是 `nanobot` → `openclaw`，见 [agent/skills.py:186](agent/skills.py#L186)。

---

## 7. 子 Agent 的 Skills

[agent/subagent.py:308-323](agent/subagent.py#L308-L323) `_build_subagent_prompt()`:

子 agent 被派发时：
- 创建**独立的** `SkillsLoader` 实例
- 只调用 `build_skills_summary()` 得到技能摘要
- **不会**注入 always skills 的全文
- 不会加载 MEMORY.md 或 bootstrap files

这意味着子 agent 看到的 skills 信息比主 agent 少，是一个"精简版"的系统提示词。

---

## 8. 当前系统没有的能力

| 缺失的能力 | 说明 |
|-----------|------|
| **缓存** | SkillsLoader 每次调用 `load_skill()` / `list_skills()` 都重新读磁盘，无内存缓存 |
| **热加载** | 没有文件监听，mid-session 修改的 skill 只在下次 session 生效（除非 agent 主动重新 read_file） |
| **工具注册** | Skill 不能注册新 tool，它只能教 agent 使用已有 tool |
| **访问控制** | 除了 `disabled_skills` 黑名单和 `requirements` 检查外，没有细粒度的 skill 权限控制 |
| **版本管理** | 无 skill 版本号或升级机制（ClawHub 可能有，但本地 loader 不关心） |

---

## 9. 完整生命周期图

```
┌─────────────────────────────────────────────────────────┐
│                    SKILL 生命周期                         │
├─────────────────────────────────────────────────────────┤
│                                                         │
│  [1. 发现]  AgentLoop 启动                               │
│     │       SkillsLoader 扫描两个目录                     │
│     │       workspace/skills/*/SKILL.md                  │
│     │       nanobot/skills/*/SKILL.md                    │
│     ▼                                                    │
│  [2. 校验]  _check_requirements()                        │
│     │       shutil.which() 检查 bins                     │
│     │       os.environ.get() 检查 env vars               │
│     ▼                                                    │
│  [3. 拼装]  build_system_prompt()                       │
│     │       always skills → 全文注入                     │
│     │       普通 skills → 摘要注入                        │
│     ▼                                                    │
│  [4. 发送]  System prompt → LLM API                     │
│     ▼                                                    │
│  [5. 激活]  Agent 自行决定 read_file(SKILL.md)           │
│     │       读取全文 → 理解指令 → 调用 tool 执行          │
│     ▼                                                    │
│  [6. 执行]  Agent 用现有 tool 完成技能任务               │
│     │       exec 运行 scripts/ 中的脚本                  │
│     │       read_file 查 references/ 中的文档            │
│     │       grep/glob 搜索代码                           │
│     ▼                                                    │
│  [7. 结束]  结果返回给用户                               │
│             下轮对话从步骤 3 重新开始                      │
│                                                         │
└─────────────────────────────────────────────────────────┘
```

---

## 10. 关键文件索引

| 文件 | 职责 | 核心类/函数 |
|------|------|-----------|
| [agent/skills.py](agent/skills.py) | Skills 加载器 | `SkillsLoader`：发现、加载、筛选、摘要 |
| [agent/context.py](agent/context.py) | 上下文拼装 | `ContextBuilder.build_system_prompt()`：把 skills 拼入系统提示词 |
| [agent/loop.py](agent/loop.py) | 会话主循环 | 创建 ContextBuilder，管理 ToolRegistry |
| [agent/subagent.py](agent/subagent.py) | 子 agent 管理 | `_build_subagent_prompt()`：子 agent 的 skills 摘要 |
| [agent/tools/self.py](agent/tools/self.py) | `my` 工具 | 运行时查询/设置 agent 状态 |
| [templates/agent/skills_section.md](templates/agent/skills_section.md) | Skills 摘要模板 | `{{ skills_summary }}` 在此展开 |
| [templates/agent/subagent_system.md](templates/agent/subagent_system.md) | 子 agent 系统提示词模板 | 含 skills_summary 占位符 |
| [config/schema.py](config/schema.py) | 配置模型 | `disabled_skills` 字段，默认空列表 |
| [skills/skill-creator/](skills/skill-creator/) | 创建/校验/打包新 skill | `init_skill.py`, `quick_validate.py`, `package_skill.py` |
| [skills/my/](skills/my/) | always skill 示例 | 教 agent 使用 `my` 工具管理运行时状态 |
| [skills/memory/](skills/memory/) | always skill 示例 | 教 agent 管理跨会话记忆 |

---

## 11. 对比：Skill vs Tool

| 维度 | Skill | Tool |
|------|-------|------|
| **本质** | Markdown 指令，指导 agent 行为 | Python 类，注册到 ToolRegistry，LLM 可直接调用 |
| **LLM 如何交互** | 作为系统提示词的一部分，影响 LLM 决策；LLM 通过 `read_file` 获取全文 | LLM 通过 function calling 直接调用 |
| **注册机制** | 无。靠文件系统发现 + 系统提示词注入 | `ToolRegistry.register()` 注册 |
| **执行机制** | Agent 自己用 `exec`、`read_file` 等 tool 执行 | `tool.execute()` 直接执行 |
| **校验机制** | Frontmatter YAML 校验 + requirements 检查 | JSON Schema 参数校验 |
| **示例** | `my`、`github`、`tmux` | `exec`、`read_file`、`grep`、`Task` |
