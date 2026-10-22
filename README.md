# Chain of Why — 探索研究的决策日志

[中文](#chain-of-why--探索研究的决策日志) · [English](#english)

> Append-only decision log for exploratory research. **链上每个节点是一个「为什么」**——问题记 why-there-is-a-problem，诊断记 why-it-happens，裁决记 why-this-option；任务即线程（跨天回访、追加续章），裁决长在因果树上。本地 JSONL 事件流是唯一事实源，静态 HTML 视图随写随生，纯 Python 3 标准库、零依赖。（CLI 入口 `scripts/render_tasklog.py` 沿用曾用名；数据目录 `.whylog/`）

**解决什么问题**：探索期的研究工作充斥「问题 → 诊断 → 方案空间 → 裁决」的推理链，但这些内容通常只活在 AI 会话记录或个人记忆里——会话结束即失散，写论文/答辩/交接时无法追溯「当时为什么选 B 不选 A」。Chain of Why 把裁决沉淀为**可审计的事件流**：git 记录代码的变更，Chain of Why 记录项目的演进。

## 架构

```
                    ┌────────────── 写入口（唯一咽喉）──────────────┐
人 / 任意 AI agent ─┤ add（schema 校验 + 平凡门 + 白话门 + 并发锁）├─▶ .whylog/chain.jsonl
                    └──────────────────────────────────────────────┘                     │
 Claude Code hooks：SessionStart → brief（注入未结任务 + RUNLOG 漂移）                     │
                    Stop        → nudge（裁决信号提醒落账，每会话至多一次）                ▼
                                                                    render ─▶ .whylog/view/*.html
                                                                   （index 主线图 + 任务卡，禁手编）
                                                                    内嵌 git 仓 ─▶ add 即自动 commit 备份
```

- **数据**：单文件 JSONL 事件流（`chain.jsonl`），append-only；修正 = 新事件带 `supersedes`，或 status 重申，历史不涂改
- **触发**（hooks 层，全自动）：会话始注入状态、会话末检测「疑似有裁决未落账」并提醒；**提醒不代写**——写入永远由有上下文的 agent/人执行
- **入口三门**：平凡词（排版/对齐/字体…）命中且无裁决信号 → 拒绝；黑话词（通道/口径/锚…）命中 → 警告 + 白话建议；**裁决见证**——agent 写 decision 须 `--user-approved`，裁决权在用户，agent 只记录不定案（均可 `--raw` 显式放行；词表在 config，可增删）
- **多写者**：任何人、任何 agent（Claude Code / Codex / 未来工具）、甚至定时补账任务，都走同一个 CLI——规则长在咽喉上，不依赖谁读过说明书

## 主线图（index.html）

git-log 风格的一页式界面：一行一个任务，按出生顺序排布；**每行可直接展开该任务的推理链**（问题→发现→方案→裁决→衔接），点标题进入单任务卡页。

| 轨道 | 进入条件 | 语义 |
|---|---|---|
| **主轨**（深色粗线） | 裁决带 `paper_line`/`system_line`，或 status `promoted` | 服务交付物的任务——项目主线 |
| **项目外轨**（琥珀色，主轨左侧） | `scope=external` | 另一项目自己的主线，与本项目平行推进 |
| **支线**（浅灰细线） | 其余（进行中/已废弃） | 项目内未决线程；成果经 `link` 以虚线汇入弧回到主线 |

- 主线是**梳理出来的，不是事先定的**：启发式给初稿，任务随裁决升降轨；
- 圆点颜色 = 任务状态（进行中/已裁决/已转正/已废弃），与页首统计同源；
- 图上接不上的地方就是缺账的地方：支线没有汇入弧 = 缺 `link`。

## Quick start

```bash
# 1. 拷入你的仓库（或任意目录）
mkdir -p scripts tests configs/tasklog
cp scripts/render_tasklog.py scripts/
cp tests/test_tasklog.py tests/

# 2. （可选）写项目章程：阶段分类 + 词表；不写则零配置自动按 unit 前缀分节
cp config.example.json configs/tasklog/config.json

# 3. 验证
uv run --no-sync pytest tests/test_tasklog.py -q    # 或 python -m pytest

# 4. 开账
uv run --no-sync python scripts/render_tasklog.py add task \
    --title "加载失败：为何 X 总在 Y 之后" --unit "1-invest/module-x" \
    --purpose "定位根因" --input "日志" --action "复现+二分" --output "诊断报告"
uv run --no-sync python scripts/render_tasklog.py add obs --task @last --text "现象……"
uv run --no-sync python scripts/render_tasklog.py render --open
```

```bash
# 5. 体检（钩子层静默失败的可视化）
python3 scripts/render_tasklog.py doctor   # store/views/hooks/config/lock 健康检查
```

项目目标行：自动提取 `AGENTS.md`（或 `CLAUDE.md`）「## 项目概况」首段显示在总览页——引用而非复制，不制造第二份事实源。

## 事件模型（9 型）

| type | 必填 | 可选 | 含义 |
|---|---|---|---|
| task.new | title, unit | parent, purpose, input, action, output, scope | 建任务（ID=T-YYYYMMDD-N，契约四项；scope=external 落项目外轨） |
| obs | text | evidence | 问题入口 |
| finding | text, kind | evidence | 发现（封闭三源 kind：survey 调研=外部考察 / cause 原因=内部诊断 / trial 实证=实验测量） |
| option | option_id, text | effect, cost, ref | 备选方案（ref=所治原因的 ts，视图挂树） |
| decision | chosen, rationale | paper_line, system_line, supersedes | 裁决（双口径强制成字段；agent 记录须 `--user-approved` 见证用户拍板） |
| impact | text | | 改动与接口影响 |
| link | to, why | | 跨任务衔接 |
| status | state | note, method_doc, scope | active/decided/promoted/dropped；`--scope external` 复核项目归属 |
| note | text | | 人类批注（--by human） |

`@last` = 最新任务；回填历史用 `--ts 2026-10-17T21:00`（任务 ID 日期随之）。

## 设计原则

1. **判断不下沉**：提醒/检测/校验全自动，内容判断（方案空间、裁决依据）永远留给有上下文的写者
2. **事件流唯一事实源**：视图是纯投影，`render` 随时重建
3. **规则长在写入口**：过滤、白话、锁、校验全部在 CLI 内聚，对任何调用方呈现同一契约
4. **白话优先**：先说人话，专名放句尾括号——总览页是叙事视图不是术语表
5. **宁缺勿滥**：纯排版/微调不入账；「这行改动一年后还值得被知道吗」是入账判据
6. **裁决权在用户**：agent 调研、建议、记账，但 decision 定案须 `--user-approved` 见证用户在对话中的明确拍板——账本记录裁决，不产生裁决
7. **人读标题，机读 ID**：任务 ID（T-YYYYMMDD-N）是 agent/CLI 的引用锚；一切用户可见输出（简报注入、add 确认、视图标题）标题在前、ID 入括号/小字
8. **落账即完成**：工具的 `[why]` 回执就是确认，不在对话里另写落账总结——账本、回执、总结三重记录是浪费

## 信号源（自含，零外部依赖）

`nudge` 直接读**会话记录**：Stop hook 的 stdin 自带 `transcript_path`（缺省时按项目 slug 回退到最新 jsonl），只取 user/assistant 的文本消息（子串预筛后仅解析候选行，全文件无尾窗丢失；账本 30 分钟内有事件时连读都跳过），对裁决信号词做匹配。不读第三方插件的内部文件、不调 LLM——grep 一手信号源即可。与会话记忆类插件（如 [remember](https://github.com/Digital-Process-Tools/claude-remember)）互不依赖、互不干扰：过程归它，裁决归 Chain of Why。

## Config（configs/tasklog/config.json）

```json
{
  "stages": [["0-discovery", "发现问题", "这个阶段要回答什么问题？"], ["1-design", "方案设计", "…"]],
  "wordlists": {
    "jargon": {"赋能": "写明具体效果"},
    "trivial": ["排版", "对齐", "字体"]
  }
}
```

阶段三元组：`[key, label, charter_question]`——章程问题挂在主线图阶段标签的悬停提示里。

## Claude Code hooks 接入（可选）

```json
{
  "hooks": {
    "SessionStart": [{"hooks": [{"type": "command",
      "command": "python3 \"$CLAUDE_PROJECT_DIR/scripts/render_tasklog.py\" brief 2>/dev/null || true"}]}],
    "Stop": [{"hooks": [{"type": "command",
      "command": "python3 \"$CLAUDE_PROJECT_DIR/scripts/render_tasklog.py\" nudge || true"}]}]
  }
}
```

## License

MIT

---

## English

# Chain of Why — decision log for exploratory research

> Append-only decision log for exploratory research. **Every node on the chain is a "why"** — problems record why-there-is-a-problem, diagnoses why-it-happened, decisions record why-this-option. Tasks are threads (revisited across days, appended in chapters); decisions grow on causal trees. A local JSONL event stream is the single source of truth; static HTML views regenerate on every write; pure Python 3 stdlib, zero dependencies. (CLI entry `scripts/render_tasklog.py` keeps the legacy name; data dir `.whylog/`.)

**The problem it solves**: exploratory research is full of "problem → diagnosis → option space → decision" chains, but these usually live only in AI session transcripts or personal memory — scattered once the session ends, unrecoverable when you write the paper, defend the thesis, or hand the project over. Chain of Why settles decisions into an **auditable event stream**: git records changes to code, Chain of Why records the evolution of the project.

## Architecture

```
              ┌────────── write chokepoint (the only entrance) ──────────┐
human / any  ─┤ add (schema check + trivial gate + jargon gate + lock)  ├─▶ .whylog/chain.jsonl
AI agent      └───────────────────────────────────────────────────────────┘        │
 Claude Code hooks: SessionStart → brief (inject open tasks + RUNLOG drift)             │
                    Stop        → nudge (remind once per session on unlogged decisions) ▼
                                                                     render ─▶ .whylog/view/*.html
                                                                    (index mainline map + task cards)
                                                     inner git repo ─▶ auto-commit on every add
```

- **Data**: single-file JSONL event stream (`chain.jsonl`), append-only; corrections = new events with `supersedes`, or a status re-statement; history is never rewritten
- **Triggers** (hooks layer, fully automatic): session start injects state, session end nudges on "decisions possibly unlogged"; **nudges never write** — the write is always performed by the agent/human with context
- **Three entry gates**: trivial words (layout/alignment/font…) with no decision signal → reject; jargon → warn with a plain-word suggestion; **decision witness** — an agent writing a decision must pass `--user-approved`: the right to decide belongs to the user, agents record but never rule (all bypassable with `--raw`; word lists live in config)
- **Many writers**: any person, any agent (Claude Code / Codex / future tools), even scheduled backfill jobs — all through the same CLI; the rules live at the chokepoint, nobody needs to have read a manual

## The mainline map (index.html)

A one-page git-log-style view: one row per task in birth order; **each row unfolds that task's reasoning chain in place** (problem → findings → options → decision → links); the title opens the per-task card page.

| Rail | Entry condition | Meaning |
|---|---|---|
| **Trunk** (dark, thick) | decision carries `paper_line`/`system_line`, or status `promoted` | tasks serving the deliverables — the project mainline |
| **External rail** (amber, left of the trunk) | `scope=external` | another project's own mainline, running in parallel |
| **Side lanes** (light grey) | everything else (active/dropped) | open threads inside the project; outcomes merge back via dashed `link` arcs |

- The mainline is **combed out, not declared**: a heuristic drafts it, and tasks rise or fall as decisions land;
- dot color = task status (active/decided/promoted/dropped), same mapping as the header legend;
- gaps on the map are gaps in the ledger: a side lane without a merge arc = a missing `link`.

## Quick start

```bash
# 1. copy into your repo (or any directory)
mkdir -p scripts tests configs/tasklog
cp scripts/render_tasklog.py scripts/
cp tests/test_tasklog.py tests/

# 2. (optional) project charter: stages + wordlists; without it, zero-config mode derives sections from unit prefixes
cp config.example.json configs/tasklog/config.json

# 3. verify
python -m pytest tests/test_tasklog.py -q

# 4. start logging
python3 scripts/render_tasklog.py add task \
    --title "Load failure: why does X always follow Y" --unit "1-invest/module-x" \
    --purpose "Locate root cause" --input "logs" --action "reproduce + bisect" --output "diagnosis"
python3 scripts/render_tasklog.py add obs --task @last --text "symptom……"
python3 scripts/render_tasklog.py render --open
```

```bash
# 5. health check (makes silent hook failures visible)
python3 scripts/render_tasklog.py doctor   # store/views/hooks/config/lock
```

The project goal line is extracted automatically from the first paragraph under "## 项目概况" in `AGENTS.md` (or `CLAUDE.md`) — referenced, never copied, so there is no second source of truth.

## Event model (9 types)

| type | required | optional | meaning |
|---|---|---|---|
| task.new | title, unit | parent, purpose, input, action, output, scope | create a task (id auto T-YYYYMMDD-N; contract quadruple; scope=external puts it on the external rail) |
| obs | text | evidence | problem entry |
| finding | text, kind | evidence | finding (closed triad: survey = external scan / cause = internal diagnosis / trial = empirical measurement) |
| option | option_id, text | effect, cost, ref | an option (ref = ts of the cause it addresses; hangs under it in the tree) |
| decision | chosen, rationale | paper_line, system_line, supersedes | the ruling (dual-track fields; agents need `--user-approved` to witness the user's call) |
| impact | text | | impact and interface notes |
| link | to, why | | cross-task link |
| status | state | note, method_doc, scope | active/decided/promoted/dropped; `--scope external` re-affirms project scope |
| note | text | | human annotation (--by human) |

`@last` = the newest task; backfill history with `--ts 2026-10-17T21:00` (the task id date follows).

## Design principles

1. **Judgment never sinks**: reminders/detection/validation are automatic; content judgment (option spaces, decision grounds) always stays with the writer who has context
2. **The event stream is the single source of truth**: views are pure projections; `render` rebuilds them at any time
3. **Rules live at the write chokepoint**: filtering, plain-word gates, locks and validation are inside the CLI — one contract for every caller
4. **Plain words first**: say it in human language, jargon at the end in parentheses — the index is a narrative view, not a glossary
5. **Better absent than trivial**: pure typography/micro-tweaks don't enter the log; "will this change still matter in a year?" is the bar
6. **The right to decide belongs to the user**: agents research, suggest, and record, but a decision requires `--user-approved` witnessing the user's explicit call — the log records decisions, it never makes them
7. **Titles for humans, IDs for machines**: the task id (T-YYYYMMDD-N) is the reference anchor for agents/CLIs; every user-visible surface puts the title first, the id in small print
8. **Logged means done**: the tool's `[why]` receipt is the confirmation — never follow up with a prose summary of what was logged; ledger + receipt + summary is triple bookkeeping

## Signal source (self-contained, zero external dependencies)

`nudge` reads the **session transcript** directly: the Stop hook's stdin carries `transcript_path` (falling back to the newest jsonl by project slug), takes only user/assistant text messages (substring prefilter, then parse only candidate lines — full-file scan with no tail-window loss; skipped entirely when the log has events younger than 30 minutes) and matches decision-signal words. No third-party plugin internals, no LLM calls — grep is the signal source. It stays independent of session-memory plugins (e.g. [remember](https://github.com/Digital-Process-Tools/claude-remember)): process goes to them, decisions go to Chain of Why.

## Config (configs/tasklog/config.json)

```json
{
  "stages": [["0-discovery", "Discovery", "What question does this stage answer?"], ["1-design", "Design", "…"]],
  "wordlists": {
    "jargon": {"synergy": "state the concrete effect"},
    "trivial": ["layout", "alignment", "font"]
  }
}
```

Stage triples: `[key, label, charter_question]` — the charter question rides the stage label's tooltip on the mainline map.

## Claude Code hooks (optional)

```json
{
  "hooks": {
    "SessionStart": [{"hooks": [{"type": "command",
      "command": "python3 \"$CLAUDE_PROJECT_DIR/scripts/render_tasklog.py\" brief 2>/dev/null || true"}]}],
    "Stop": [{"hooks": [{"type": "command",
      "command": "python3 \"$CLAUDE_PROJECT_DIR/scripts/render_tasklog.py\" nudge || true"}]}]
  }
}
```

## License

MIT
