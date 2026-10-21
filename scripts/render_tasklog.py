#!/usr/bin/env python3
"""tasklog -- append-only exploration/decision event log with static HTML views.

Single source of truth:  .whylog/chain.jsonl (one JSON object per line, append-only).
Derived views (regenerable, never hand-edit): .whylog/view/index.html + <task-id>.html.
Local only: .whylog/ is gitignored by the main repo; an inner git repo (if present)
receives an automatic commit after every add.

Usage (repo root):
  uv run --no-sync python scripts/render_tasklog.py add task --title "..." --unit "..." [--parent ID]
      [--purpose ... --input ... --action ... --output ...]
  uv run --no-sync python scripts/render_tasklog.py add obs     --task @last --text "..."
  uv run --no-sync python scripts/render_tasklog.py add finding --task @last --text "..." [--evidence path]
  uv run --no-sync python scripts/render_tasklog.py add option  --task @last --opt-id A --text "..." [--effect ... --cost ...]
  uv run --no-sync python scripts/render_tasklog.py add decision --task @last --chosen A --rationale "..."
      [--paper-line ... --system-line ... --supersedes TS --user-approved]
  uv run --no-sync python scripts/render_tasklog.py add impact  --task @last --text "..."
  uv run --no-sync python scripts/render_tasklog.py add link    --task @last --to ID --why "..."
  uv run --no-sync python scripts/render_tasklog.py add status  --task @last --state {active,decided,promoted,dropped}
      [--note ... --method-doc doc#anchor]
  uv run --no-sync python scripts/render_tasklog.py add note    --task @last --text "..."
  uv run --no-sync python scripts/render_tasklog.py render [--open]
  uv run --no-sync python scripts/render_tasklog.py brief    # print open tasks + RUNLOG drift (SessionStart hook)
  uv run --no-sync python scripts/render_tasklog.py nudge    # Stop hook: remind once on unlogged decision signals
  uv run --no-sync python scripts/render_tasklog.py doctor  # one-shot health check: store/views/hooks/config/goal/lock
  # any add accepts --ts 2026-10-17T21:00 to backfill a historical event (task id date follows)

`@last` resolves to the most recently created task. Stdlib only.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

STATUSES = ("active", "decided", "promoted", "dropped")
STATUS_ZH = {"active": "进行中", "decided": "已裁决", "promoted": "已转正", "dropped": "已废弃"}
# chain-role labels for the timeline view (narrative register; ledger terms live on task cards)
ROLE_ZH = {"task.new": "任务", "obs": "问题", "finding": "原因", "option": "方案",
           "decision": "裁决", "impact": "影响", "link": "衔接", "status": "状态", "note": "批注"}

# event type -> (required payload keys, optional payload keys); meta keys: ts/by/task/type
SCHEMA = {
    "task.new": (["title", "unit"], ["parent", "purpose", "input", "action", "output"]),
    "obs": (["text"], ["evidence"]),
    "finding": (["text"], ["evidence"]),
    "option": (["option_id", "text"], ["effect", "cost", "ref"]),
    "decision": (["chosen", "rationale"], ["paper_line", "system_line", "supersedes"]),
    "impact": (["text"], []),
    "link": (["to", "why"], []),
    "status": (["state"], ["note", "method_doc"]),
    "note": (["text"], []),
}

# ---------------------------------------------------------------- event store

def now_ts() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M")

def chain_file(root: Path) -> Path:
    return root / "chain.jsonl"

def load_events(root: Path) -> list[dict]:
    fp = chain_file(root)
    if not fp.exists():
        return []
    events: list[dict] = []
    for ln, line in enumerate(fp.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError as e:
            raise SystemExit(f"[tasklog] {fp.name}:{ln} invalid JSON: {e}")
    events.sort(key=lambda e: e.get("ts", ""))  # stable: physical order breaks ties
    return events

def append_event(root: Path, ev: dict) -> None:
    if ev["type"] not in SCHEMA:
        raise SystemExit(f"[tasklog] unknown event type {ev['type']!r}")
    req, opt = SCHEMA[ev["type"]]
    missing = [k for k in req if not ev.get(k)]
    if missing:
        raise SystemExit(f"[tasklog] {ev['type']}: missing required {missing}")
    stray = set(ev) - {"ts", "by", "task", "type", *req, *opt}
    if stray:
        raise SystemExit(f"[tasklog] {ev['type']}: unknown fields {sorted(stray)}")
    fp = chain_file(root)
    fp.parent.mkdir(parents=True, exist_ok=True)
    with fp.open("a", encoding="utf-8") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")

def norm_ts(ts: str | None) -> str:
    ts = ts or now_ts()
    if len(ts) != 16 or ts[4] != "-" or ts[7] != "-" or ts[10] != "T" or ts[13] != ":":
        raise SystemExit(f"[tasklog] --ts must look like YYYY-MM-DDTHH:MM, got {ts!r}")
    return ts

def next_task_id(root: Path, ts: str = "") -> str:
    day = ts[:10].replace("-", "")
    if len(day) != 8 or not day.isdigit():
        day = datetime.now().strftime("%Y%m%d")
    prefix = "T-" + day + "-"
    used = {e["task"] for e in load_events(root)
            if e["type"] == "task.new" and str(e.get("task", "")).startswith(prefix)}
    n = 1
    while f"{prefix}{n}" in used:
        n += 1
    return f"{prefix}{n}"

def resolve_task_ref(root: Path, ref: str) -> str:
    if ref != "@last":
        return ref
    news = [e for e in load_events(root) if e["type"] == "task.new"]
    if not news:
        raise SystemExit("[tasklog] @last: no tasks exist yet")
    return news[-1]["task"]

def existing_options(events: list[dict], task_id: str) -> set[str]:
    return {e["option_id"] for e in events
            if e["type"] == "option" and e.get("task") == task_id}

def build_tasks(events: list[dict]) -> dict[str, dict]:
    tasks: dict[str, dict] = {}
    for ev in events:
        t = ev.get("task")
        if ev["type"] == "task.new":
            tasks[t] = {"id": t, "title": ev["title"], "unit": ev.get("unit", ""),
                        "parent": ev.get("parent"), "ts0": ev["ts"], "ts1": ev["ts"],
                        "contract": {k: ev.get(k) for k in ("purpose", "input", "action", "output")},
                        "status": "active", "events": []}
        task = tasks.get(t)
        if task is None:
            raise SystemExit(f"[tasklog] event {ev.get('ts')} {ev['type']} -> unknown task {t}")
        task["events"].append(ev)
        task["ts1"] = max(task["ts1"], ev.get("ts", task["ts1"]))
        if ev["type"] == "status":
            task["status"] = ev["state"]
    return tasks

def current_decision(task: dict) -> dict | None:
    decs = [e for e in task["events"] if e["type"] == "decision"]
    superseded = {d.get("supersedes") for d in decs if d.get("supersedes")}
    live = [d for d in decs if d["ts"] not in superseded]
    return live[-1] if live else None

def inner_git_commit(root: Path, msg: str) -> None:
    """Commit to the inner backup repo if .whylog/.git exists (best effort)."""
    if not (root / ".git").exists():
        return
    subprocess.run(["git", "-C", str(root), "add", "-A"], capture_output=True)
    for extra in ([], ["-c", "user.name=tasklog", "-c", "user.email=tasklog@local"]):
        r = subprocess.run(["git", "-C", str(root), *extra, "commit", "-m", msg],
                           capture_output=True, text=True)
        if r.returncode == 0 or "nothing to commit" in r.stdout + r.stderr:
            return

# ---------------------------------------------------------------- HTML views

CSS = """
:root{--ink:#1f2937;--muted:#6b7280;--line:#e5e7eb;--bg:#f6f7f9;--hover:#f1f5f9;
--blue:#1d4ed8;--violet:#6d28d9;--green:#15803d;--gray:#6b7280}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:14px/1.6 system-ui,-apple-system,"Hiragino Sans GB","Microsoft YaHei",sans-serif}
.wrap{max-width:1280px;margin:0 auto;padding:20px 20px 60px}
.brand{font-weight:700;letter-spacing:.08em;color:var(--muted);text-transform:uppercase;font-size:12px}
h1{font-size:21px;margin:4px 0 8px} h2{font-size:14px;margin:24px 0 10px;color:var(--muted);letter-spacing:.04em}
.goal{color:var(--muted);font-size:13.5px;margin:0 0 12px;max-width:76ch}
a{color:var(--blue);text-decoration:none} a:hover{text-decoration:underline}
.meta{color:var(--muted);font-size:12.5px}
.chip{display:inline-block;padding:1px 10px;border-radius:999px;font-size:12px;font-weight:600}
.c-active{background:#dbeafe;color:var(--blue)} .c-decided{background:#ede9fe;color:var(--violet)}
.c-promoted{background:#dcfce7;color:var(--green)} .c-dropped{background:#f3f4f6;color:var(--gray);text-decoration:line-through}
/* ---- filter bar (IDE-log style) ---- */
.bar{position:sticky;top:0;z-index:5;display:flex;gap:8px;align-items:center;flex-wrap:wrap;
background:#fff;border:1px solid var(--line);border-radius:10px;padding:8px 10px;margin:0 0 14px}
.bar input{border:1px solid var(--line);border-radius:8px;padding:4px 10px;font-size:13px;min-width:200px;outline:none}
.bar input:focus{border-color:var(--blue)}
.fch{border:1px solid var(--line);background:#fff;border-radius:999px;padding:1px 10px;
font-size:12px;cursor:pointer;color:var(--muted)}
.fch:hover{border-color:var(--muted)}
.fch.on{background:var(--ink);color:#fff;border-color:var(--ink)}
/* ---- stage bands live inside the DAG svg ---- */
/* ---- causal tree with role lanes (git-graph style) ---- */
.tree{list-style:none;margin:0;padding:0}
.tree ul{list-style:none;margin:0;padding:0}
.tree li{position:relative;padding:1px 0 1px 24px}
.tree li::before{content:"";position:absolute;left:9px;top:0;height:100%;border-left:2px solid var(--lane,var(--line))}
.tree li::after{content:"";position:absolute;left:9px;top:14px;width:13px;border-top:2px solid var(--lane,var(--line))}
.tree li:last-child::before{height:14px}
.tree>li{padding-left:4px}
.tree>li::before,.tree>li::after{display:none}
.n-obs{--lane:#93c5fd}.n-finding{--lane:#fcd34d}.n-option{--lane:#67e8f9}.n-decision{--lane:#c4b5fd}
.n-status{--lane:#cbd5e1}.n-impact{--lane:#fda4af}.n-link{--lane:#5eead4}.n-note{--lane:#d6d3d1}
/* ---- node rows (log-panel density) ---- */
.row{display:grid;grid-template-columns:50px 84px minmax(0,1fr) auto;gap:10px;align-items:baseline;
padding:2px 6px 2px 18px;border-radius:6px;position:relative}
.row:hover{background:var(--hover)}
summary.row{cursor:pointer;list-style:none}
summary.row::-webkit-details-marker{display:none}
summary.row::before{content:"▸";position:absolute;left:4px;top:50%;transform:translateY(-55%);
font-size:9px;color:var(--muted)}
details[open]>summary.row::before{content:"▾"}
.trow{grid-template-columns:50px minmax(0,1fr) auto auto}
.trow .ts-meta{max-width:34ch;overflow:hidden;text-overflow:ellipsis}
.tag{flex:none;display:inline-block;padding:0 7px;border-radius:6px;font-size:11.5px;font-weight:600;text-align:center}
.t-task{background:#1f2937;color:#fff}
.t-obs{background:#dbeafe;color:#1d4ed8}
.t-finding{background:#fef3c7;color:#b45309}
.t-option{background:#cffafe;color:#0e7490}
.t-decision{background:#6d28d9;color:#fff}
.t-status{background:#f1f5f9;color:#475569}
.t-impact{background:#ffe4e6;color:#be123c}
.t-link{background:#ccfbf1;color:#0f766e}
.t-note{background:#f5f5f4;color:#57534e}
.ts{color:var(--muted);font:11.5px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace;white-space:nowrap}
.ts-meta{color:var(--muted);font-size:12px;white-space:nowrap}
.tx{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
/* ---- task DAG graph (git-log style) ---- */
.dag{width:100%;height:auto;display:block}
.dag path{fill:none;stroke-width:2}
.e-fork{stroke:#94a3b8}
.e-merge{stroke:#0f766e;stroke-dasharray:5 4}
.dag text{font-size:12px;fill:#1f2937}
.dag a{text-decoration:none}
.dag a:hover text{fill:var(--blue);font-weight:600}
.dg-dropped{fill:#9ca3af;text-decoration:line-through}
.dg-bh{font-size:12px;font-weight:600;fill:#6b7280;letter-spacing:.05em}
.dg-bh.meta{font-weight:400;font-size:11px}
.dg-sep{stroke:#e5e7eb;stroke-width:1}
.graph-b{overflow-x:auto;background:#fff;border:1px solid var(--line);border-radius:10px;padding:10px 8px}
/* ---- task card page ---- */
.card{background:#fff;border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin-bottom:12px}
blockquote{margin:0 0 10px;padding:8px 14px;border-left:3px solid #93c5fd;background:#f8fafc;border-radius:0 8px 8px 0}
.tl{list-style:none;margin:0;padding:0}
.tl li{position:relative;padding:0 0 14px 22px;border-left:2px solid var(--line)}
.tl li::before{content:"";position:absolute;left:-5px;top:6px;width:8px;height:8px;border-radius:50%;background:var(--blue)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:12px}
.opt{border:1px solid var(--line);border-radius:10px;padding:12px 14px;background:#fff}
.opt.chosen{border-color:var(--green);background:#f0fdf4;box-shadow:0 0 0 1px var(--green)}
.opt .id{font-weight:700}
.duo{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.panel{border-radius:10px;padding:10px 14px;font-size:14px}
.p-paper{background:#eff6ff;border:1px solid #bfdbfe} .p-sys{background:#fffbeb;border:1px solid #fde68a}
.sup{text-decoration:line-through;opacity:.65}
code{background:#f3f4f6;border-radius:4px;padding:1px 6px;font-size:12.5px;
font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.note-h{border-left:4px solid #b45309} .note-a{border-left:4px solid #93c5fd}
.ev{font-size:13.5px;padding:6px 0;border-bottom:1px dashed var(--line)}
footer{margin-top:30px;color:var(--muted);font-size:12px;text-align:center}
"""

def bar_html(labels: list[str]) -> str:
    chips = ['<button class="fch on" data-f="all">全部</button>',
             '<button class="fch" data-f="open">未结</button>']
    chips += [f'<button class="fch" data-f="b:{esc(l)}">{esc(l)}</button>' for l in labels]
    return ('<div class="bar"><input id="q" type="search" placeholder="筛选：文本 / 阶段 / 状态…">'
            '<span class="fchips" id="fchips">' + "".join(chips) +
            '</span><span class="meta" id="cnt"></span></div>')


SCRIPT = """
<script>
const q=document.getElementById('q');let st='all',openOnly=false;
const rows=[...document.querySelectorAll('.dg-r')];
const edges=[...document.querySelectorAll('.dag path[data-f]')];
const bands=[...document.querySelectorAll('.dg-band')];
function apply(){const s=q.value.trim().toLowerCase();let vis=0;const V={};
 rows.forEach(a=>{
  let ok=(st==='all'||a.dataset.band===st);
  if(openOnly&&!['active','decided'].includes(a.dataset.st))ok=false;
  if(s&&!(a.dataset.txt||a.textContent).toLowerCase().includes(s))ok=false;
  V[a.getAttribute('href').replace('.html','')]=ok;
  a.setAttribute('display',ok?'':'none');
  if(ok)vis++;});
 edges.forEach(p=>p.setAttribute('display',V[p.dataset.f]&&V[p.dataset.t]?'':'none'));
 bands.forEach(b=>{
  const any=rows.some(a=>a.dataset.band===b.dataset.band
   &&V[a.getAttribute('href').replace('.html','')]);
  b.setAttribute('display',any?'':'none');});
 document.getElementById('cnt').textContent=vis+' / '+rows.length+' 任务';}
document.querySelectorAll('.fch').forEach(b=>b.addEventListener('click',()=>{
 const f=b.dataset.f;
 if(f==='all'){st='all';openOnly=false;q.value='';}
 else if(f==='open'){openOnly=!openOnly;}
 else{st=(st===f?'all':f);}
 document.querySelectorAll('.fch').forEach(x=>{
  const fx=x.dataset.f;
  x.classList.toggle('on',fx==='all'?st==='all':fx==='open'?openOnly:st===fx);});
 apply();}));
q.addEventListener('input',apply);
apply();
</script>"""

def esc(v) -> str:
    return html.escape(str(v if v is not None else ""))

def _page(title: str, header: str, body: str) -> str:
    return (
        '<!DOCTYPE html>\n<html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{esc(title)} · tasklog</title><style>{CSS}</style></head>"
        '<body><div class="wrap"><header><div class="brand">tasklog · 探索决策日志</div>'
        f"{header}</header>{body}"
        "<footer>generated by scripts/render_tasklog.py — 手编无效，事件流是唯一事实源</footer>"
        "</div></body></html>\n")

def task_page(task: dict, tasks: dict) -> str:
    tid, st = task["id"], task["status"]
    parent_html = ""
    if task.get("parent"):
        if task["parent"] in tasks:
            parent_html = (f' <span class="meta">父任务 '
                           f'<a href="{esc(task["parent"])}.html">{esc(task["parent"])}</a></span>')
        else:
            parent_html = f' <span class="meta">父任务 {esc(task["parent"])}</span>'
    header = (
        f'<h1>{esc(task["title"])}</h1>'
        f'<div><span class="chip c-{st}">{STATUS_ZH.get(st, st)}</span>'
        f'<span class="meta"> {esc(tid)} · {esc(task["unit"])} · {esc(task["ts0"])} ~ {esc(task["ts1"])}{parent_html}</span></div>')

    parts = ['<h2>契约</h2><div class="grid">' + "".join(
        f'<div class="card"><div class="meta">{zh}</div><div>{esc(task["contract"].get(k) or "—")}</div></div>'
        for zh, k in (("目的", "purpose"), ("输入", "input"), ("动作", "action"), ("输出", "output"))
    ) + "</div>"]

    kids = causal_children(task, tasks)
    if kids:
        parts.append('<h2>因果树</h2><ul class="tree">' + "".join(kids) + "</ul>")

    decs = [e for e in task["events"] if e["type"] == "decision"]
    if decs:
        superseded = {d.get("supersedes") for d in decs if d.get("supersedes")}
        blocks = []
        for e in decs:
            sup = " sup" if e["ts"] in superseded else ""
            note = (f' <span class="meta">（已被 {esc(e["supersedes"])} 修正）</span>'
                    if e.get("supersedes") else "")
            pp = f'<div class="panel p-paper"><b>论文线</b>：{esc(e.get("paper_line") or "未区分")}</div>'
            ss = f'<div class="panel p-sys"><b>系统线</b>：{esc(e.get("system_line") or "未区分")}</div>'
            blocks.append(f'<div class="card{sup}"><div class="meta">{esc(e["ts"])} · {esc(e["by"])}'
                          f' · 选 <b>{esc(e["chosen"])}</b>{note}</div>'
                          f"<p>{esc(e['rationale'])}</p><div class=\"duo\">{pp}{ss}</div></div>")
        parts.append("<h2>裁决</h2>" + "".join(blocks))

    imps = [e for e in task["events"] if e["type"] == "impact"]
    if imps:
        parts.append("<h2>影响面</h2>" + "".join(
            f'<div class="ev"><span class="meta">{esc(e["ts"])}</span> · {esc(e["text"])}</div>'
            for e in imps))

    links = [e for e in task["events"] if e["type"] == "link"]
    inb = [(x["id"], e.get("why", "")) for x in tasks.values() for e in x["events"]
           if e["type"] == "link" and e.get("to") == tid]
    if links or inb:
        rows = []
        for e in links:
            a = (f'<a href="{esc(e["to"])}.html">{esc(e["to"])}</a>'
                 if e["to"] in tasks else esc(e["to"]))
            rows.append(f'<div class="ev"><span class="meta">{esc(e["ts"])}</span> → {a} · {esc(e["why"])}</div>')
        for src, why in inb:
            rows.append(f'<div class="ev"><span class="meta">↩</span> '
                        f'<a href="{esc(src)}.html">{esc(src)}</a> · {esc(why)}</div>')
        parts.append("<h2>衔接</h2>" + "".join(rows))

    notes = [e for e in task["events"] if e["type"] == "note"]
    if notes:
        rows = []
        for e in notes:
            cls = "note-h" if e.get("by") == "human" else "note-a"
            rows.append(f'<div class="card {cls}"><span class="meta">{esc(e["ts"])} · {esc(e["by"])}</span>'
                        f"<div>{esc(e['text'])}</div></div>")
        parts.append("<h2>批注</h2>" + "".join(rows))

    return _page(f"{tid} {task['title']}", header, "".join(parts))

# Tool config (stage taxonomy) lives in configs/tasklog/config.json -- mechanical render
# parameters only. Project goal is project CONTENT, not tool config: it is referenced, never
# copied -- load_goal() extracts the first paragraph under "## 项目概况" in AGENTS.md.
def load_stages(repo_root: Path) -> list[tuple[str, str]]:
    fp = repo_root / "configs" / "tasklog" / "config.json"
    if not fp.exists():
        return []
    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
        return [(str(k), str(v)) for k, v in data.get("stages", [])]
    except (json.JSONDecodeError, OSError):
        return []

def load_goal(repo_root: Path) -> str:
    fp = repo_root / "AGENTS.md"
    try:
        lines = fp.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    try:
        i = next(k for k, ln in enumerate(lines) if ln.strip() == "## 项目概况")
    except StopIteration:
        return ""
    para: list[str] = []
    for ln in lines[i + 1:]:
        s = ln.strip()
        if s.startswith("#"):
            break
        if not s:
            if para:
                break
            continue
        para.append(s)
    return " ".join(para).replace("**", "").rstrip("：:")

def load_wordlists(repo_root: Path) -> dict:
    fp = repo_root / "configs" / "tasklog" / "config.json"
    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
        return data.get("wordlists", {})
    except (json.JSONDecodeError, OSError):
        return {}

def check_gates(ev: dict, wordlists: dict, raw: bool, user_approved: bool = False) -> None:
    """Entry gates on the single write chokepoint: reject trivial-change events
    (bypass with --raw), reject unattested agent decisions (rulings belong to
    the human -- the agent records decisions, it never makes them), warn on
    jargon (discipline: plain words first)."""
    text = " ".join(str(v) for k, v in ev.items()
                    if k in ("title", "purpose", "text", "rationale", "note") and v)
    if not raw:
        if ev["type"] == "decision" and ev.get("by") != "human" and not user_approved:
            raise SystemExit("[tasklog] 裁决权在用户：agent 记 decision 须 --user-approved"
                             "（见证用户已在对话中明确拍板）；模型自拟结论请记 finding/note，勿定案")
        trivial = wordlists.get("trivial", [])
        if trivial and not any(w in text for w in NUDGE_WORDS):
            hits = [w for w in trivial if w in text]
            if hits:
                raise SystemExit("[tasklog] 疑似无信息价值改动（命中平凡词：" + "、".join(hits) +
                                 "）——纯排版/视觉微调不入账；确有裁决请改写文案体现决策，或加 --raw 显式放行")
    jargon = wordlists.get("jargon", {})
    hits = [w for w in jargon if w in text]
    if hits:
        tips = "；".join(f"{w}→{jargon[w]}" for w in hits[:3])
        print(f"[tasklog] ⚠ 白话提醒（先说人话，专名放句尾括号）：{tips}")

def stage_sections(tasks: dict, stages: list[tuple[str, str]]) -> list[tuple[str, list]]:
    buckets: dict[str, list] = {}
    for t in tasks.values():
        unit = t["unit"]
        if unit.startswith("docs/"):  # tolerate multi-writer prefix drift, e.g. docs/1-risk/...
            unit = unit[len("docs/"):]
        buckets.setdefault(unit.split("/", 1)[0], []).append(t)
    if stages:
        sections = [(label, buckets.pop(key)) for key, label in stages if key in buckets]
        misc = [t for group in buckets.values() for t in group]
        if misc:
            sections.append(("其他", misc))
        return sections
    def sort_key(head: str):
        p = head.split("-", 1)[0]
        return (0, int(p), head) if p.isdigit() else (1, 0, head)
    return [(head, buckets[head]) for head in sorted(buckets, key=sort_key)]

def event_preview(e: dict) -> str:
    if e["type"] == "decision":
        return f'{e.get("chosen", "")} — {e.get("rationale", "")}'
    if e["type"] == "status":
        p = STATUS_ZH.get(e.get("state", ""), e.get("state", ""))
        return f'{p}（{e["note"]}）' if e.get("note") else p
    if e["type"] == "option":
        return f'{e.get("option_id", "")} · {e.get("text", "")}'
    if e["type"] == "link":
        return f'→ {e.get("to", "")} · {e.get("why", "")}'
    return str(e.get("text") or e.get("title") or e.get("why") or "")

def _node(e: dict, inner: str = "", tasks: dict | None = None) -> str:
    p = event_preview(e)
    tag = ROLE_ZH.get(e["type"], e["type"])
    role = e["type"].split(".")[0]
    short_ts = e["ts"][5:].replace("T", " ")      # 10-19 09:34 (full ts in title attr)
    if e["type"] == "link" and tasks and e.get("to") in tasks:
        text = f'<a href="{esc(e["to"])}.html">→ {esc(e["to"])}</a> · {esc(e.get("why", ""))}'
    else:
        text = esc(p[:100] + ("…" if len(p) > 100 else ""))
    row = (f'<span class="tag t-{role}">{tag}</span>'
           f'<span class="ts" title="{esc(e["ts"])}">{esc(short_ts)}</span>'
           f'<span class="tx" title="{esc(p)}">{text}</span>')
    if inner:  # nodes with children are collapsible
        return f'<li class="n-{role}"><details open><summary class="row">{row}</summary>{inner}</details></li>'
    return f'<li class="n-{role}"><div class="row">{row}</div></li>'

def _options_node(opts: list[dict]) -> list[str]:
    """Render options: collapse >=2 siblings into one folded 方案（N 个备选） node."""
    if len(opts) >= 2:
        inner = f'<ul class="tree">{"".join(_node(o) for o in opts)}</ul>'
        return [f'<li class="n-option"><details><summary class="row">'
                f'<span class="tag t-option">方案</span>'
                f'<span class="tx ts-meta">{len(opts)} 个备选</span></summary>{inner}</details></li>']
    return [_node(o) for o in opts]

def causal_children(t: dict, tasks: dict) -> list[str]:
    """Causal chain. Single-problem task: everything hangs under the problem node
    (问题 -> 原因s -> 方案s -> 裁决 -> 状态 -> 衔接/批注). Multi-problem: options/
    decision/status stay at task level (task-wide convergence)."""
    problems: list[list] = []   # [obs, [findings...]] -- findings hang under the latest obs
    orphans: list[dict] = []    # findings that arrived before any obs
    opts_by_ref: dict[str, list] = {}  # option.ref (finding ts) -> options addressing that cause
    root_opts: list[dict] = []         # options without ref (address the task as a whole)
    for e in t["events"]:
        if e["type"] == "obs":
            problems.append([e, []])
        elif e["type"] == "finding":
            (problems[-1][1] if problems else orphans).append(e)
        elif e["type"] == "option":
            (opts_by_ref.setdefault(e["ref"], []) if e.get("ref") else root_opts).append(e)
    tail = _options_node(root_opts)
    cur = current_decision(t)
    if cur:
        tail.append(_node(cur))
    last_status = next((e for e in reversed(t["events"]) if e["type"] == "status"), None)
    if last_status:
        tail.append(_node(last_status))
    tail += [_node(e, tasks=tasks) for e in t["events"] if e["type"] == "link"]
    tail += [_node(e) for e in t["events"] if e["type"] == "note"]
    tail += [task_node(sub, tasks) for sub in tasks.values() if sub.get("parent") == t["id"]]
    single = len(problems) == 1
    children = [_node(e) for e in orphans]
    for obs, causes in problems:
        cause_lis = []
        for c in causes:
            subs = _options_node(opts_by_ref.get(c["ts"], []))
            sub = f'<ul class="tree">{"".join(subs)}</ul>' if subs else ""
            cause_lis.append(_node(c, sub))
        inner_lis = cause_lis + (tail if single else [])
        inner = f'<ul class="tree">{"".join(inner_lis)}</ul>' if inner_lis else ""
        children.append(_node(obs, inner))
    if not single:
        children += tail
    if not children:  # young task, no obs/option yet: fall back to contract purpose
        pur = t["contract"].get("purpose")
        if pur:
            children.append(f'<li class="n-task"><div class="row"><span class="tag t-task">目的</span>'
                            f'<span class="tx">{esc(pur[:100] + ("…" if len(pur) > 100 else ""))}</span></div></li>')
    return children

def task_node(t: dict, tasks: dict) -> str:
    inb = sum(1 for x in tasks.values() for e in x["events"]
              if e["type"] == "link" and e.get("to") == t["id"])
    inb_html = f'<span class="ts-meta" title="被其他任务衔接">↩{inb}</span>' if inb else ""
    summary = (f'<summary class="row trow"><span class="tag t-task">任务</span>'
               f'<a class="tx" href="{esc(t["id"])}.html">{esc(t["title"])}</a>'
               f'<span class="ts-meta">{esc(t["id"])} · {esc(t["unit"])}</span>{inb_html}'
               f'<span class="chip c-{t["status"]}">{STATUS_ZH.get(t["status"], t["status"])}</span></summary>')
    kids = causal_children(t, tasks)
    body = f'<ul class="tree">{"".join(kids)}</ul>' if kids else ""
    return f"<li><details>{summary}{body}</details></li>"

GRAPH_DOT = {"active": "#1d4ed8", "decided": "#6d28d9", "promoted": "#15803d", "dropped": "#9ca3af"}

def _dag_edge(parts: list[str], x1: float, y1: float, x2: float, y2: float,
              cls: str, extra: str, tip: str, src: str, dst: str) -> None:
    d = (f"M{x1} {y1}L{x2} {y2}" if x1 == x2 else
         f"M{x1} {y1}C{x1} {(y1 + y2) / 2},{x2} {(y1 + y2) / 2},{x2} {y2}")
    parts.append(f'<path class="{cls}" data-f="{src}" data-t="{dst}" d="{d}"{extra}>'
                 f"<title>{esc(tip)}</title></path>")


def task_graph(tasks: dict, stages: list[tuple[str, str]]) -> str:
    """The single task surface, git-log style: stage bands group the rows, time flows
    down inside a band, lanes carry edges. parent = solid fork, link = dashed
    merge-back (hover = why). Rows carry data-* attrs for the filter bar."""
    secs = stage_sections(tasks, stages)
    if not secs:
        return ""
    PAD, LANE_W, ROW, HDR = 16, 22, 28, 34
    order: list[dict] = []
    bands: list[tuple[str, int, int]] = []          # (label, first row index, count)
    for label, group in secs:
        g = sorted(group, key=lambda x: x["ts1"])
        bands.append((label, len(order), len(g)))
        order.extend(g)
    rows = {t["id"]: i for i, t in enumerate(order)}
    lane_of: dict[str, int] = {}
    last_row: list[int] = []
    for t in order:
        p = t.get("parent")
        lane = None
        if p in lane_of and last_row[lane_of[p]] < rows[t["id"]]:
            lane = lane_of[p]                       # child continues on the parent's lane
        else:
            lane = next((i for i, r in enumerate(last_row) if r < rows[t["id"]]), None)
        if lane is None:
            last_row.append(-1)
            lane = len(last_row) - 1
        lane_of[t["id"]] = lane
        last_row[lane] = rows[t["id"]]
    lx = PAD + len(last_row) * LANE_W + 6           # label column starts right of all lanes

    ys: dict[str, int] = {}
    tband: dict[str, str] = {}
    head: list[str] = []
    y = PAD
    W = lx + 620
    for label, start, n in bands:
        if y > PAD:
            head.append(f'<line class="dg-sep" x1="0" x2="{W}" y1="{y - 12}" y2="{y - 12}"/>')
        head.append(f'<g class="dg-band" data-band="{esc(label)}">'
                    f'<text class="dg-bh" x="2" y="{y + 14}">{esc(label)}</text>'
                    f'<text class="dg-bh meta" x="{lx + 6}" y="{y + 14}">{n} 任务</text></g>')
        y += HDR
        for t in order[start:start + n]:
            ys[t["id"]] = y
            tband[t["id"]] = label
            y += ROW
    H = y + PAD

    inb: dict[str, int] = {}
    for t in order:
        for e in t["events"]:
            if e["type"] == "link" and e.get("to") in tasks:
                inb[e["to"]] = inb.get(e["to"], 0) + 1

    parts = [f'<svg class="dag" viewBox="0 0 {W} {H}" role="img">',
             '<defs><marker id="arr" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" '
             'markerHeight="7" orient="auto-start-reverse">'
             '<path d="M0 0L8 4L0 8z" fill="#0f766e"/></marker></defs>', *head]
    for t in order:
        p = t.get("parent")
        if p in rows and p != t["id"]:
            _dag_edge(parts, PAD + lane_of[p] * LANE_W, ys[p], PAD + lane_of[t["id"]] * LANE_W,
                      ys[t["id"]], "e-fork", "", f"{p} 分叉出 {t['id']}", p, t["id"])
    for t in order:
        for e in t["events"]:
            if e["type"] == "link" and e.get("to") in rows and e["to"] != t["id"]:
                _dag_edge(parts, PAD + lane_of[t["id"]] * LANE_W, ys[t["id"]],
                          PAD + lane_of[e["to"]] * LANE_W, ys[e["to"]], "e-merge",
                          ' marker-end="url(#arr)"',
                          f"{t['id']} 衔接 {e['to']}：{e.get('why', '')}", t["id"], e["to"])
    for t in order:
        cx, cy = PAD + lane_of[t["id"]] * LANE_W, ys[t["id"]]
        n = inb.get(t["id"], 0)
        lab = esc(t["title"][:44] + ("…" if len(t["title"]) > 44 else "")) + (f" ↩{n}" if n else "")
        tip = esc(f"{t['id']} · {t['title']} · {STATUS_ZH.get(t['status'], t['status'])}")
        txt = esc(f"{t['title']} {t['id']} {t['unit']} " + " ".join(
            str(v) for e in t["events"] for v in e.values() if isinstance(v, str)))
        parts.append(f'<a class="dg-r" data-st="{t["status"]}" data-band="{esc(tband[t["id"]])}" '
                     f'data-txt="{txt}" href="{esc(t["id"])}.html">'
                     f'<circle cx="{cx}" cy="{cy}" r="4.5" fill="{GRAPH_DOT.get(t["status"], "#6b7280")}">'
                     f"<title>{tip}</title></circle>"
                     f'<text x="{lx}" y="{cy + 4}" class="dg-{t["status"]}">{lab}</text></a>')
    parts.append("</svg>")
    return f'<div class="graph-b">{"".join(parts)}</div>'


def index_page(tasks: dict, stages: list[tuple[str, str]], goal: str = "") -> str:
    counts = {s: sum(1 for t in tasks.values() if t["status"] == s) for s in STATUSES}
    chips = " ".join(f'<span class="chip c-{s}">{STATUS_ZH[s]} {counts[s]}</span>' for s in STATUSES)
    header = "<h1>任务总览</h1>"
    if goal:
        header += f'<div class="goal">{esc(goal)}</div>'
    header += f"<div>{chips}</div>"

    body = [bar_html([label for label, _ in stage_sections(tasks, stages)]), task_graph(tasks, stages)]
    return _page("index", header, "".join(body) + SCRIPT)

def render_views(root: Path) -> list[Path]:
    view = root / "view"
    view.mkdir(parents=True, exist_ok=True)
    events = load_events(root)
    tasks = build_tasks(events)
    written = []
    for tid, task in tasks.items():
        fp = view / f"{tid}.html"
        fp.write_text(task_page(task, tasks), encoding="utf-8")
        written.append(fp)
    fp = view / "index.html"
    fp.write_text(index_page(tasks, load_stages(REPO_ROOT), load_goal(REPO_ROOT)), encoding="utf-8")
    written.append(fp)
    return written

# ---------------------------------------------------------------- CLI

def build_add_parser(add: argparse.ArgumentParser) -> None:
    at = add.add_subparsers(dest="etype", required=True, metavar="etype")

    def with_by(s):
        s.add_argument("--by", default="claude-code",
                       help="who wrote this event: human | claude-code | codex ...")
        s.add_argument("--ts", help="override timestamp for backfill, e.g. 2026-10-17T21:00")
        s.add_argument("--raw", action="store_true", help="bypass the trivial-change gate (explicit override)")
        return s

    def with_task(s):
        s.add_argument("--task", required=True, help="task id, or @last for the newest task")
        return s

    s = at.add_parser("task")
    s.add_argument("--title", required=True)
    s.add_argument("--unit", required=True)
    for k in ("parent", "purpose", "input", "action", "output"):
        s.add_argument(f"--{k.replace('_', '-')}")
    with_by(s)

    for name in ("obs", "finding"):
        s = at.add_parser(name)
        with_task(s)
        s.add_argument("--text", required=True)
        s.add_argument("--evidence")
        with_by(s)

    s = at.add_parser("option")
    with_task(s)
    s.add_argument("--opt-id", required=True, dest="option_id", help="short label like A/B/C")
    s.add_argument("--text", required=True)
    s.add_argument("--effect")
    s.add_argument("--cost")
    s.add_argument("--ref", help="ts of the finding this option addresses (tree attachment)")
    with_by(s)

    s = at.add_parser("decision")
    with_task(s)
    s.add_argument("--chosen", required=True)
    s.add_argument("--rationale", required=True)
    s.add_argument("--paper-line")
    s.add_argument("--system-line")
    s.add_argument("--supersedes", help="ts of the decision event this one replaces")
    s.add_argument("--user-approved", action="store_true", dest="user_approved",
                   help="attest the user explicitly approved this choice in conversation "
                        "(agents record decisions; they never make them)")
    with_by(s)

    s = at.add_parser("impact")
    with_task(s)
    s.add_argument("--text", required=True)
    with_by(s)

    s = at.add_parser("link")
    with_task(s)
    s.add_argument("--to", required=True)
    s.add_argument("--why", required=True)
    with_by(s)

    s = at.add_parser("status")
    with_task(s)
    s.add_argument("--state", required=True, choices=STATUSES)
    s.add_argument("--note")
    s.add_argument("--method-doc", help="doc anchor, e.g. docs/2-resilience/x.md#sec")
    with_by(s)

    s = at.add_parser("note")
    with_task(s)
    s.add_argument("--text", required=True)
    with_by(s)

PAYLOAD_KEYS = {
    "obs": ("text", "evidence"),
    "finding": ("text", "evidence"),
    "option": ("option_id", "text", "effect", "cost", "ref"),
    "decision": ("chosen", "rationale", "paper_line", "system_line", "supersedes"),
    "impact": ("text",),
    "link": ("to", "why"),
    "status": ("state", "note", "method_doc"),
    "note": ("text",),
}

def run_add(root: Path, args: argparse.Namespace) -> None:
    lock = _acquire_add_lock(root)
    try:
        _run_add_locked(root, args)
    finally:
        lock.unlink(missing_ok=True)

def _run_add_locked(root: Path, args: argparse.Namespace) -> None:
    events = load_events(root)
    known = {e["task"] for e in events if e["type"] == "task.new"}
    ts = norm_ts(getattr(args, "ts", None))
    if args.etype == "task":
        task_id = next_task_id(root, ts)
        ev = {"ts": ts, "by": args.by, "task": task_id, "type": "task.new",
              "title": args.title, "unit": args.unit}
        for k in ("parent", "purpose", "input", "action", "output"):
            v = getattr(args, k, None)
            if v:
                ev[k] = v
    else:
        task_id = resolve_task_ref(root, args.task)
        if task_id not in known:
            raise SystemExit(f"[tasklog] unknown task {task_id}")
        ev = {"ts": ts, "by": args.by, "task": task_id, "type": args.etype}
        for k in PAYLOAD_KEYS[args.etype]:
            v = getattr(args, k, None)
            if v:
                ev[k] = v
        if args.etype == "link" and ev["to"] not in known:
            raise SystemExit(f"[tasklog] link target {ev['to']} does not exist")
        if args.etype == "decision":
            allowed = existing_options(events, task_id) | {"none"}
            if ev["chosen"] not in allowed:
                raise SystemExit(f"[tasklog] --chosen must be an existing option id "
                                 f"{sorted(allowed)} (or 'none')")
    check_gates(ev, load_wordlists(REPO_ROOT), getattr(args, "raw", False),
                getattr(args, "user_approved", False))
    append_event(root, ev)
    render_views(root)
    inner_git_commit(root, f"{ev['type']} {task_id}")
    _update_runlog_marker(REPO_ROOT, root)
    title = ev.get("title") or next((e.get("title", "") for e in events
                                     if e["type"] == "task.new" and e["task"] == task_id), "")
    print(f"[tasklog] + {ev['ts']} {ev['type']} -> {task_id} · {title} (views regenerated)")

def run_render(root: Path, args: argparse.Namespace) -> None:
    written = render_views(root)
    print(f"[tasklog] rendered {len(written)} views under {root / 'view'}")
    if args.open:
        idx = root / "view" / "index.html"
        if sys.platform == "darwin":
            subprocess.run(["open", str(idx)])
        else:
            print(f"open manually: {idx}")

# ------------- borrowed from .remember: locks / position pointer / nudge (mechanical only)

RUNLOG_DRIFT_LINES = 20
NUDGE_WORDS = ("裁决", "敲定", "选定", "定稿", "否决", "判死", "采纳", "最终选择")
NUDGE_FRESH_S = 30 * 60

def _acquire_add_lock(root: Path) -> Path:
    """Atomic noclobber lock guarding append+render+commit; a dead holder is taken over."""
    lock = root / "tmp" / "add.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        holder = lock.read_text().strip()
        try:
            pid = int(holder)
        except ValueError:
            pid = 0
        if pid:
            try:
                os.kill(pid, 0)
                raise SystemExit(f"[tasklog] another add is running (pid {pid}); retry in a moment")
            except ProcessLookupError:
                pass  # holder died: take over
        lock.unlink(missing_ok=True)
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)
    return lock

def runlog_lines(repo_root: Path) -> dict[str, int]:
    out: dict[str, int] = {}
    docs = repo_root / "docs"
    if not docs.is_dir():
        return out
    for fp in docs.rglob("RUNLOG.md"):
        try:
            out[str(fp.relative_to(repo_root))] = len(fp.read_text(encoding="utf-8").splitlines())
        except OSError:
            continue
    return out

def _update_runlog_marker(repo_root: Path, root: Path) -> None:
    """Position pointer (last-save.json pattern): RUNLOG line counts as of the last add."""
    mp = root / "tmp" / "runlog-marker.json"
    mp.parent.mkdir(parents=True, exist_ok=True)
    mp.write_text(json.dumps({"lines": runlog_lines(repo_root)}, ensure_ascii=False), encoding="utf-8")

def _runlog_drift(repo_root: Path, root: Path) -> list[str]:
    mp = root / "tmp" / "runlog-marker.json"
    if not mp.exists():
        _update_runlog_marker(repo_root, root)  # seed silently on first run
        return []
    try:
        marker = json.loads(mp.read_text(encoding="utf-8"))["lines"]
    except (json.JSONDecodeError, OSError, KeyError):
        return []
    msgs = []
    for path, now_lines in runlog_lines(repo_root).items():
        drift = now_lines - int(marker.get(path, now_lines))
        if drift >= RUNLOG_DRIFT_LINES:
            msgs.append(f"- RUNLOG 漂移：{path} +{drift} 行未见落账——检查是否有裁决待补")
    return msgs

def should_nudge(entries_text: str, last_event_age_s: float | None) -> str | None:
    """Reminder reason when decision signals meet a stale ledger; None to stay quiet.
    A wrong nudge is harmless -- it never writes, and fires at most once per session."""
    if not entries_text:
        return None
    if last_event_age_s is not None and last_event_age_s < NUDGE_FRESH_S:
        return None
    hits = [w for w in NUDGE_WORDS if w in entries_text]
    if not hits:
        return None
    words = "、".join(hits[:4])
    return (f"检测到疑似未落账的裁决信号（{words}）。用户已拍板 → add decision 记录"
            f"（须 --user-approved 见证拍板）；仅模型自拟的结论不得定案——记 finding/note "
            f"并保持 active；确无裁决则直接结束（每会话至多提醒一次）。")

def transcript_signals(transcript_path: str) -> str:
    """Human/assistant text from a Claude Code session transcript -- the
    self-contained signal source for nudge (grep-only: no distillation).
    Full-file scan, no tail-window loss: candidate lines are substring-
    prefilters before JSON parsing, so tool-result bulk never pays parse cost."""
    try:
        chunk = Path(transcript_path).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    texts: list[str] = []
    for line in chunk.splitlines():
        line = line.strip()
        if not ('"type":"user"' in line or '"type": "user"' in line
                or '"type":"assistant"' in line or '"type": "assistant"' in line):
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("type") not in ("user", "assistant"):
            continue
        content = (entry.get("message") or {}).get("content")
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            texts.append(" ".join(p.get("text", "") for p in content
                                  if isinstance(p, dict) and p.get("type") == "text"))
    return "\n".join(texts)

def newest_transcript(repo_root: Path) -> str:
    """Newest session jsonl under ~/.claude/projects/<slug>/ (fallback when the hook
    payload carries no transcript_path)."""
    slug = re.sub(r"[^a-zA-Z0-9]", "-", str(repo_root))
    d = Path.home() / ".claude" / "projects" / slug
    try:
        return str(max(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime))
    except ValueError:
        return ""

def run_nudge(root: Path) -> None:
    """Stop hook: read hook JSON on stdin, block-once with the nudge reminder."""
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    try:
        hook = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        hook = {}
    sid = "".join(c for c in str(hook.get("session_id", "")) if c.isalnum() or c == "-")[:64] or "nosession"
    marker = root / "tmp" / f"nudged-{sid}"
    if marker.exists():
        return
    cf = chain_file(root)
    age = time.time() - cf.stat().st_mtime if cf.exists() else None
    if age is not None and age < NUDGE_FRESH_S:
        return  # fresh ledger means the signals are already logged -- skip the transcript read
    tp = str(hook.get("transcript_path") or "") or newest_transcript(REPO_ROOT)
    reason = should_nudge(transcript_signals(tp), age)
    if reason:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("1", encoding="utf-8")
        print(json.dumps({"decision": "block", "reason": f"[tasklog] {reason}"}, ensure_ascii=False))

def run_brief(root: Path) -> None:
    """Open tasks (active/decided) + RUNLOG drift, for session-start injection; silent when none."""
    lines: list[str] = []
    if chain_file(root).exists():
        tasks = build_tasks(load_events(root))
        open_tasks = [t for t in tasks.values() if t["status"] in ("active", "decided")]
        if open_tasks:
            lines.append("[tasklog] 未结任务（决策发生即记账 add … --task @last；视图 .whylog/view/index.html）")
            for t in sorted(open_tasks, key=lambda x: x["ts1"], reverse=True)[:5]:
                lines.append(f"- {t['title']}（{t['id']}）[{STATUS_ZH[t['status']]}] "
                             f"单位 {t['unit']} 最后活动 {t['ts1']}")
    lines += _runlog_drift(REPO_ROOT, root)
    if lines:
        print("\n".join(lines))


# ---------------------------------------------------------------- doctor

def run_doctor(root: Path, repo_root: Path) -> None:
    """One-shot health check (borrowed from remember's doctor): every hook runs
    under `2>/dev/null || true`, so breakage is silent by design -- this makes
    it visible in one command. Exit 1 on FAILs; warns do not fail."""
    fails = 0

    def ok(msg: str) -> None:
        print(f"[ok]   {msg}")

    def warn(msg: str) -> None:
        print(f"[warn] {msg}")

    def fail(msg: str) -> None:
        nonlocal fails
        fails += 1
        print(f"[FAIL] {msg}")

    print(f"[tasklog] doctor -- store {root}")

    cf = chain_file(root)
    events_n = 0
    if not cf.exists():
        fail(f"{cf.name} missing -- no ledger (first `add task` creates it)")
    else:
        bad = 0
        last_ts = ""
        for line in cf.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            events_n += 1
            try:
                last_ts = max(last_ts, json.loads(line).get("ts", ""))
            except json.JSONDecodeError:
                bad += 1
        if bad:
            fail(f"{cf.name}: {bad} invalid JSON line(s) -- add/brief/nudge will refuse to load")
        else:
            tasks_n = sum(1 for e in load_events(root) if e["type"] == "task.new")
            ok(f"{cf.name}: {events_n} events, {tasks_n} tasks, last ts {last_ts or '?'}")

    view = root / "view"
    pages = sorted(view.glob("*.html")) if view.is_dir() else []
    if not pages:
        warn("view/ empty or missing -- run `render` to (re)build")
    elif cf.exists() and max(p.stat().st_mtime for p in pages) < cf.stat().st_mtime:
        warn("views older than the ledger -- run `render`")
    else:
        ok(f"view/: {len(pages)} pages, up to date")

    cfg = repo_root / "configs" / "tasklog" / "config.json"
    if cfg.exists():
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
            wl = data.get("wordlists", {})
            ok(f"config: {len(data.get('stages', []))} stages, "
               f"{len(wl.get('trivial', []))} trivial / {len(wl.get('jargon', {}))} jargon words")
        except json.JSONDecodeError as e:
            fail(f"config invalid JSON ({e}) -- gates silently disabled (load_* swallows errors)")
    else:
        warn("configs/tasklog/config.json absent -- zero-config mode (autoderived stages, no gates)")

    wired = {"SessionStart": "brief", "Stop": "nudge"}
    sp = repo_root / ".claude" / "settings.json"
    missing = list(wired)
    if sp.exists():
        try:
            hooks = json.loads(sp.read_text(encoding="utf-8")).get("hooks", {})
            missing = [ev for ev, needle in wired.items()
                       if not any(needle in h.get("command", "")
                                  for grp in hooks.get(ev, []) for h in grp.get("hooks", []))]
        except json.JSONDecodeError:
            warn(".claude/settings.json invalid JSON -- hooks state unknown")
    if missing:
        warn(f"hooks not wired: {', '.join(missing)} (brief/nudge automation off -- see README)")
    else:
        ok("hooks wired: SessionStart brief + Stop nudge")

    if load_goal(repo_root):
        ok("goal: AGENTS.md 项目概况 first paragraph found")
    else:
        warn("goal: AGENTS.md 项目概况 missing -- index page loses the goal line")

    lock = root / "tmp" / "add.lock"
    if lock.exists():
        try:
            pid = int(lock.read_text().strip())
            os.kill(pid, 0)
            warn(f"add.lock held by live pid {pid} -- an add is running (or crashed mid-run)")
        except ValueError:
            ok("add.lock unreadable -- next add takes it over automatically")
        except ProcessLookupError:
            ok("add.lock stale (dead holder) -- next add takes it over automatically")
        except PermissionError:
            warn(f"add.lock held by pid {pid} (no permission to probe) -- check for a stuck add")
    else:
        ok("no add.lock residue")

    if (root / ".git").exists():
        r = subprocess.run(["git", "-C", str(root), "log", "-1", "--format=%h %s"],
                           capture_output=True, text=True)
        ok(f"inner backup git: {r.stdout.strip() or 'no commits yet'}")
    else:
        warn("no inner git repo in store -- optional local backup off")

    print(f"[tasklog] doctor: {'FAIL' if fails else 'PASS'}" + (f" ({fails} failing)" if fails else ""))
    if fails:
        raise SystemExit(1)


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(
        prog="render_tasklog.py",
        description="Append-only exploration/decision event log with HTML views (see module docstring).")
    parser.add_argument("--root", type=Path, default=REPO_ROOT / ".whylog",
                        help="tasklog root dir (default: <repo>/.whylog)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    build_add_parser(sub.add_parser("add", help="append an event (validates, re-renders, inner-git commits)"))
    rp = sub.add_parser("render", help="regenerate all HTML views")
    rp.add_argument("--open", action="store_true", help="open index.html in the browser (macOS)")
    sub.add_parser("brief", help="print open tasks + RUNLOG drift (for SessionStart hook injection)")
    sub.add_parser("nudge", help="Stop hook: remind once on unlogged decision signals (reads hook JSON on stdin)")
    sub.add_parser("doctor", help="one-shot health check: store/views/hooks/config/goal/lock")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    if args.cmd == "add":
        run_add(root, args)
    elif args.cmd == "brief":
        run_brief(root)
    elif args.cmd == "nudge":
        run_nudge(root)
    elif args.cmd == "doctor":
        run_doctor(root, REPO_ROOT)
    else:
        run_render(root, args)

if __name__ == "__main__":
    main()
