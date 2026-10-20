# Chain of Why — 探索研究的裁决账本

> Append-only decision ledger for exploratory research. **链上每个节点是一个「为什么」**——问题记 why-there-is-a-problem，诊断记 why-it-happens，裁决记 why-this-option；任务即线程（跨天回访、追加续章），裁决长在因果树上。本地 JSONL 事件流是唯一事实源，静态 HTML 视图随写随生，纯 Python 3 标准库、零依赖。（CLI 入口 `scripts/render_tasklog.py` 沿用曾用名；数据目录 `.whylog/`）

**解决什么问题**：探索期的研究工作充斥「问题 → 诊断 → 方案空间 → 裁决」的推理链，但这些内容通常只活在 AI 会话记录或个人记忆里——会话结束即失散，写论文/答辩/交接时无法追溯「当时为什么选 B 不选 A」。Task Thread 把裁决沉淀为**可审计的事件流**。

## 架构

```
                    ┌────────────── 写入口（唯一咽喉）──────────────┐
人 / 任意 AI agent ─┤ add（schema 校验 + 平凡门 + 白话门 + 并发锁）├─▶ .whylog/chain.jsonl
                    └──────────────────────────────────────────────┘                     │
 Claude Code hooks：SessionStart → brief（注入未结任务 + RUNLOG 漂移）                     │
                    Stop        → nudge（裁决信号提醒落账，每会话至多一次）                ▼
                                                                    render ─▶ .whylog/view/*.html
                                                                   （静态因果树，可折叠，禁手编）
                                                                    内嵌 git 仓 ─▶ add 即自动 commit 备份
```

- **数据**：单文件 JSONL 事件流（`chain.jsonl`），append-only；修正 = 新事件带 `supersedes`，历史不涂改
- **触发**（hooks 层，全自动）：会话始注入状态、会话末检测「疑似有裁决未落账」并提醒；**提醒不代写**——写入永远由有上下文的 agent/人执行
- **入口双门**：平凡词（排版/对齐/字体…）命中且无裁决信号 → 拒绝（`--raw` 显式放行）；黑话词（通道/口径/锚…）命中 → 警告 + 白话建议（词表在 config，可增删）
- **多写者**：任何人、任何 agent（Claude Code / Codex / 未来工具）、甚至定时补账任务，都走同一个 CLI——规则长在咽喉上，不依赖谁读过说明书

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
| task.new | title, unit | parent, purpose, input, action, output | 建任务（ID=T-YYYYMMDD-N，契约四项） |
| obs | text | evidence | 问题入口 |
| finding | text | evidence | 诊断链一步（挂最近的问题下） |
| option | option_id, text | effect, cost, ref | 备选方案（ref=所治原因的 ts，视图挂树） |
| decision | chosen, rationale | paper_line, system_line, supersedes | 裁决（双口径强制成字段） |
| impact | text | | 改动与接口影响 |
| link | to, why | | 跨任务衔接 |
| status | state | note, method_doc | active/decided/promoted/dropped |
| note | text | | 人类批注（--by human） |

`@last` = 最新任务；回填历史用 `--ts 2026-10-17T21:00`（任务 ID 日期随之）。

## 设计原则

1. **判断不下沉**：提醒/检测/校验全自动，内容判断（方案空间、裁决依据）永远留给有上下文的写者
2. **事件流唯一事实源**：视图是纯投影，`render` 随时重建
3. **规则长在写入口**：过滤、白话、锁、校验全部在 CLI 内聚，对任何调用方呈现同一契约
4. **白话优先**：先说人话，专名放句尾括号——总览页是叙事视图不是术语表
5. **宁缺勿滥**：纯排版/微调不入账；「这行改动一年后还值得被知道吗」是入账判据

## 信号源（自含，零外部依赖）

`nudge` 直接读**会话记录**：Stop hook 的 stdin 自带 `transcript_path`（缺省时按项目 slug 回退到最新 jsonl），只取 user/assistant 的文本消息（子串预筛后仅解析候选行，全文件无尾窗丢失；账本 30 分钟内有事件时连读都跳过），对裁决信号词做匹配。不读第三方插件的内部文件、不调 LLM——grep 一手信号源即可。与会话记忆类插件（如 [remember](https://github.com/Digital-Process-Tools/claude-remember)）互不依赖、互不干扰：过程归它，裁决归 Task Thread。

## Config（configs/tasklog/config.json）

```json
{
  "stages": [["0-discovery", "发现问题"], ["1-design", "方案设计"]],
  "wordlists": {
    "jargon": {"赋能": "写明具体效果"},
    "trivial": ["排版", "对齐", "字体"]
  }
}
```

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
