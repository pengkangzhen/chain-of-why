"""Smoke tests for scripts/render_tasklog.py (event append, validation, render+rotation)."""
import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "render_tasklog.py"


def _load():
    spec = importlib.util.spec_from_file_location("render_tasklog", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mod = _load()


def test_append_and_load_roundtrip(tmp_path):
    root = tmp_path / "tasklog"
    mod.append_event(root, {"ts": "2026-10-19T10:00", "by": "human", "task": "T-20261019-1",
                            "type": "task.new", "title": "t", "unit": "u"})
    mod.append_event(root, {"ts": "2026-10-19T10:05", "by": "claude-code",
                            "task": "T-20261019-1", "type": "obs", "text": "phenomenon"})
    events = mod.load_events(root)
    assert [e["type"] for e in events] == ["task.new", "obs"]
    assert events[1]["text"] == "phenomenon"
    assert mod.build_tasks(events)["T-20261019-1"]["title"] == "t"


def test_event_for_unknown_task_rejected(tmp_path):
    events = [{"ts": "2026-10-19T10:00", "by": "human", "task": "T-20261019-9",
               "type": "obs", "text": "x"}]
    with pytest.raises(SystemExit):
        mod.build_tasks(events)


def test_render_reads_handwritten_chain_and_appends(tmp_path):
    root = tmp_path / "tasklog"
    root.mkdir(parents=True)
    (root / "chain.jsonl").write_text(
        json.dumps({"ts": "2026-06-01T09:00", "by": "human", "task": "T-20260601-1",
                    "type": "task.new", "title": "旧任务", "unit": "u"}, ensure_ascii=False) + "\n",
        encoding="utf-8")
    mod.append_event(root, {"ts": "2026-10-19T10:00", "by": "human", "task": "T-20261019-1",
                            "type": "task.new", "title": "新任务", "unit": "u"})
    written = mod.render_views(root)
    assert {"index.html", "T-20260601-1.html", "T-20261019-1.html"} <= {p.name for p in written}
    index = (root / "view" / "index.html").read_text(encoding="utf-8")
    assert "旧任务" in index and "新任务" in index
    raw = (root / "chain.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(raw) == 2                                   # append lands in the same single file


def test_brief_lists_open_tasks_only(tmp_path, capsys):
    root = tmp_path / "tasklog"
    mod.append_event(root, {"ts": "2026-10-19T10:00", "by": "human", "task": "T-20261019-1",
                            "type": "task.new", "title": "未结", "unit": "u"})
    mod.append_event(root, {"ts": "2026-10-19T10:01", "by": "human", "task": "T-20261019-2",
                            "type": "task.new", "title": "已废弃", "unit": "u"})
    mod.append_event(root, {"ts": "2026-10-19T10:02", "by": "human", "task": "T-20261019-2",
                            "type": "status", "state": "dropped"})
    mod.run_brief(root)
    out = capsys.readouterr().out
    assert "T-20261019-1" in out and "未结" in out
    assert "未结（T-20261019-1）" in out                    # title first, id in parens (human-facing)
    assert "T-20261019-2" not in out
    mod.run_brief(tmp_path / "empty")  # no store at all -> silent
    assert capsys.readouterr().out == ""


def test_task_tree_attaches_options_to_causes():
    events = [
        {"ts": "2026-10-19T10:00", "by": "human", "task": "T-1", "type": "task.new",
         "title": "t", "unit": "u"},
        {"ts": "2026-10-19T10:01", "by": "human", "task": "T-1", "type": "obs", "text": "问题"},
        {"ts": "2026-10-19T10:02", "by": "human", "task": "T-1", "type": "finding", "text": "原因一"},
        {"ts": "2026-10-19T10:03", "by": "human", "task": "T-1", "type": "option",
         "option_id": "A", "text": "治因一", "ref": "2026-10-19T10:02"},
        {"ts": "2026-10-19T10:04", "by": "human", "task": "T-1", "type": "option",
         "option_id": "Z", "text": "全局方案"},
    ]
    tasks = mod.build_tasks(events)
    html = mod.task_node(tasks["T-1"], tasks)
    i_cause, i_opt = html.index("原因一"), html.index("治因一")
    assert i_cause < i_opt                                   # ref'd option nests under its cause
    assert html.index('<ul class="tree">', i_cause) < i_opt  # inside a sublist after the cause
    assert "全局方案" in html                                # unref'd option stays at task root


def test_options_grouped_under_single_problem():
    events = [
        {"ts": "2026-10-17T15:00", "by": "h", "task": "T-1", "type": "task.new",
         "title": "t", "unit": "1-risk/x"},
        {"ts": "2026-10-17T15:01", "by": "h", "task": "T-1", "type": "obs", "text": "问题"},
        {"ts": "2026-10-17T15:02", "by": "h", "task": "T-1", "type": "finding", "text": "原因"},
        {"ts": "2026-10-17T15:03", "by": "h", "task": "T-1", "type": "option",
         "option_id": "A", "text": "甲"},
        {"ts": "2026-10-17T15:04", "by": "h", "task": "T-1", "type": "option",
         "option_id": "B", "text": "乙"},
        {"ts": "2026-10-17T15:05", "by": "h", "task": "T-1", "type": "option",
         "option_id": "C", "text": "丙"},
        {"ts": "2026-10-17T15:06", "by": "h", "task": "T-1", "type": "decision",
         "chosen": "B", "rationale": "选乙"},
        {"ts": "2026-10-17T15:07", "by": "h", "task": "T-1", "type": "status", "state": "decided"},
    ]
    tasks = mod.build_tasks(events)
    html = mod.task_node(tasks["T-1"], tasks)
    seq = ['tag">问题', 'tag">原因', "3 个备选", "A ·", "B ·", "C ·", 'tag-dec">裁决', "选乙", 'tag">状态']
    pos = [html.index(x) for x in seq]
    assert pos == sorted(pos)                                # full chain nests under the problem
    assert "3 个备选" in html                                 # options collapsed into one node


def test_backfill_ts_controls_id_date(tmp_path):
    root = tmp_path / "tasklog"
    mod.append_event(root, {"ts": "2026-10-17T21:00", "by": "claude-code", "task": "T-20261017-1",
                            "type": "task.new", "title": "旧", "unit": "u"})
    assert mod.next_task_id(root, "2026-10-17T21:30") == "T-20261017-2"
    assert mod.next_task_id(root).startswith("T-2026")      # no ts -> today
    assert mod.norm_ts(None)                                # None -> now, no error
    with pytest.raises(SystemExit):
        mod.norm_ts("2026-10-17 21:00")                     # space instead of T


def test_index_groups_by_stage_and_ascending():
    events = [
        {"ts": "2026-10-01T10:00", "by": "h", "task": "T-1", "type": "task.new",
         "title": "韧性卡", "unit": "2-resilience/metrics"},
        {"ts": "2026-10-02T10:00", "by": "h", "task": "T-2", "type": "task.new",
         "title": "风险后", "unit": "1-risk/quant"},
        {"ts": "2026-09-01T10:00", "by": "h", "task": "T-3", "type": "task.new",
         "title": "风险前", "unit": "1-risk/quant"},
        {"ts": "2026-10-03T10:00", "by": "h", "task": "T-4", "type": "task.new",
         "title": "杂项", "unit": "misc"},
    ]
    stages = [("0-network", "网络层"), ("1-risk", "风险量化"), ("2-resilience", "韧性量化")]
    html = mod.index_page(mod.build_tasks(events), stages, goal="研究目标一句话")
    assert "研究目标一句话" in html                                  # charter goal on index
    assert html.index("风险量化") < html.index("韧性量化")          # stage order follows config
    assert html.index("风险前") < html.index("风险后")              # ascending within stage
    assert "其他" in html and "杂项" in html                        # unknown unit -> 其他


def test_stage_sections_autoderive_without_config():
    events = [
        {"ts": "2026-10-01T10:00", "by": "h", "task": "T-1", "type": "task.new",
         "title": "a", "unit": "2-resilience/x"},
        {"ts": "2026-10-02T10:00", "by": "h", "task": "T-2", "type": "task.new",
         "title": "b", "unit": "1-risk/y"},
        {"ts": "2026-10-03T10:00", "by": "h", "task": "T-3", "type": "task.new",
         "title": "c", "unit": "misc"},
    ]
    labels = [label for label, _ in mod.stage_sections(mod.build_tasks(events), [])]
    assert labels == ["1-risk", "2-resilience", "misc"]            # numeric prefixes first, zero config


def test_stage_sections_strips_docs_prefix():
    events = [{"ts": "2026-10-01T10:00", "by": "h", "task": "T-1", "type": "task.new",
               "title": "a", "unit": "docs/1-risk/quant/x"}]
    secs = mod.stage_sections(mod.build_tasks(events), [("1-risk", "风险量化")])
    assert secs[0][0] == "风险量化"                                # docs/ prefix tolerated


def test_load_goal_reads_agents_md_first_paragraph(tmp_path):
    (tmp_path / "AGENTS.md").write_text(
        "# t\n\n## 项目概况\n\n研究**目标**一句话：\n\n后续段落不取。\n\n## 其他\n", encoding="utf-8")
    assert mod.load_goal(tmp_path) == "研究目标一句话"
    assert mod.load_goal(tmp_path / "nope") == ""                  # missing file -> silent


def test_add_lock_blocks_second_holder(tmp_path):
    root = tmp_path / "tasklog"
    lock = mod._acquire_add_lock(root)
    assert lock.exists()
    with pytest.raises(SystemExit):
        mod._acquire_add_lock(root)                                # live holder -> refuse
    lock.unlink()
    assert mod._acquire_add_lock(root) is not None                 # released -> re-acquirable


def test_runlog_drift_reports_only_above_threshold(tmp_path):
    repo = tmp_path / "repo"
    (repo / "docs" / "u").mkdir(parents=True)
    rl = repo / "docs" / "u" / "RUNLOG.md"
    rl.write_text("x\n" * 10, encoding="utf-8")
    mod._update_runlog_marker(repo, tmp_path)                      # seed at 10 lines
    assert mod._runlog_drift(repo, tmp_path) == []
    rl.write_text("x\n" * 35, encoding="utf-8")                    # +25 lines
    msgs = mod._runlog_drift(repo, tmp_path)
    assert len(msgs) == 1 and "+25 行" in msgs[0]


def test_should_nudge_requires_keyword_and_stale_ledger():
    assert mod.should_nudge("裁决：选定方案 B", 9999) is not None
    assert mod.should_nudge("改了个 typo", 9999) is None           # no decision signal
    assert mod.should_nudge("裁决：选定方案 B", 60) is None        # ledger fresh -> silent
    assert mod.should_nudge("", None) is None                      # no signal source -> silent


def test_transcript_signals_extracts_text_messages(tmp_path):
    tp = tmp_path / "session.jsonl"
    tp.write_text(
        '{"type":"user","message":{"content":"我们敲定方案 B 吧"}}\n'
        '{"type":"assistant","message":{"content":[{"type":"text","text":"好的，已选定 B"}]}}\n'
        '{"type":"system","message":{"content":"meta 裁决"}}\n'
        'partial-garbage-line\n', encoding="utf-8")
    sig = mod.transcript_signals(str(tp))
    assert "敲定" in sig and "选定 B" in sig
    assert "meta 裁决" not in sig                    # non-conversation lines skipped
    assert mod.transcript_signals(str(tmp_path / "nope.jsonl")) == ""


def test_add_gates_trivial_reject_and_jargon_warn(capsys):
    wl = {"trivial": ["排版", "对齐"], "jargon": {"通道": "一套报道热度统计"}}
    with pytest.raises(SystemExit):
        mod.check_gates({"type": "obs", "text": "调整了卡片对齐"}, wl, raw=False)
    mod.check_gates({"type": "obs", "text": "调整对齐，但裁决：选定新布局"}, wl, raw=False)  # decision word passes
    mod.check_gates({"type": "obs", "text": "调整了卡片对齐"}, wl, raw=True)                 # raw bypass
    capsys.readouterr()
    mod.check_gates({"type": "obs", "text": "通道合并"}, wl, raw=False)                      # jargon warns only
    assert "白话提醒" in capsys.readouterr().out


def test_doctor_passes_on_healthy_store(tmp_path, capsys):
    repo = tmp_path / "repo"
    (repo / "configs" / "tasklog").mkdir(parents=True)
    (repo / "configs" / "tasklog" / "config.json").write_text(
        '{"stages": [["1-risk", "风险"]], "wordlists": {"trivial": ["排版"], "jargon": {}}}',
        encoding="utf-8")
    (repo / ".claude").mkdir()
    (repo / ".claude" / "settings.json").write_text(
        '{"hooks": {"SessionStart": [{"hooks": [{"command": "render_tasklog.py brief"}]}],'
        ' "Stop": [{"hooks": [{"command": "render_tasklog.py nudge"}]}]}}', encoding="utf-8")
    (repo / "AGENTS.md").write_text("# t\n\n## 项目概况\n\n目标。\n", encoding="utf-8")
    root = repo / ".whylog"
    mod.append_event(root, {"ts": "2026-10-20T06:00", "by": "h", "task": "T-20261020-1",
                            "type": "task.new", "title": "t", "unit": "u"})
    mod.run_doctor(root, repo)                              # no SystemExit on a healthy store
    out = capsys.readouterr().out
    assert "PASS" in out and "1 events, 1 tasks" in out and "hooks wired" in out


def test_doctor_fails_on_corrupt_store(tmp_path, capsys):
    root = tmp_path / "whylog"
    root.mkdir()
    (root / "chain.jsonl").write_text('{"broken\n', encoding="utf-8")
    with pytest.raises(SystemExit):
        mod.run_doctor(root, tmp_path)
    out = capsys.readouterr().out
    assert "[FAIL]" in out and "invalid JSON" in out


def test_transcript_signals_full_file_no_loss(tmp_path):
    tp = tmp_path / "session.jsonl"
    pad = '{"type":"assistant","message":{"content":[{"type":"text","text":"填充行"}]}}\n' * 6000
    tp.write_text('{"type":"user","message":{"content":"早期敲定方案 B"}}\n' + pad, encoding="utf-8")
    sig = mod.transcript_signals(str(tp))
    assert "敲定方案 B" in sig                    # early line still seen: no tail-window loss


def test_decision_gate_requires_user_attestation():
    wl = {}
    dec = {"type": "decision", "by": "claude-code", "chosen": "B", "rationale": "r"}
    with pytest.raises(SystemExit):
        mod.check_gates(dec, wl, raw=False)                  # agent self-ruling rejected
    mod.check_gates(dec, wl, raw=False, user_approved=True)  # attested record passes
    mod.check_gates({**dec, "by": "human"}, wl, raw=False)   # human decides, no attestation
    mod.check_gates(dec, wl, raw=True)                       # --raw explicit override
