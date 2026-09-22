#!/usr/bin/env python3
"""Jev 插件完整验证套件（mock 化，可离线重复跑，不烧 token）。

运行：python test_verification.py
所有在线路径通过 monkeypatch `_execute_system_one_with_timeout` 注入假响应，
保证 CI / 离线环境也能完整覆盖逻辑分支。
"""
import json
import sys
import types
from pathlib import Path

# 导入插件模块
sys.path.insert(0, str(Path(__file__).resolve().parent))
import __init__ as jmod


# ---------- Mock 基础设施 ----------
def make_fake_response(nouls=None, choices=None, scores=None):
    """构造一个假的 system_one 返回值。"""
    ns = types.SimpleNamespace()
    ns.nouls = {k: types.SimpleNamespace(noul=v) for k, v in (nouls or {}).items()}
    ns.choices = {k: types.SimpleNamespace(choice=v) for k, v in (choices or {}).items()}
    ns.scores = {k: types.SimpleNamespace(score=v) for k, v in (scores or {}).items()}
    return ns


def patch_online(resp, side_effect=None):
    """临时替换在线执行函数；side_effect 可模拟超时/异常。"""
    def fake(client, state, questions, provider, timeout):
        if side_effect:
            side_effect()
        return resp
    orig = jmod._execute_system_one_with_timeout
    jmod._execute_system_one_with_timeout = fake
    return orig


def restore_online(orig):
    jmod._execute_system_one_with_timeout = orig


# ---------- 测试用例 ----------
def test_availability():
    """有 key + adapter 时装载可用；无 key 时离线降级但不崩。"""
    jmod._adapter_available = True
    jmod._get_agnes_key = lambda: "fake-key-for-test"
    assert jmod._jev_tool_available() is True
    jmod._get_agnes_key = lambda: ""
    assert jmod._jev_tool_available() is False
    print("Availability check: PASSED")


def test_guard_offline_rules():
    """离线降级规则单元：高危 deny、破坏性词 ask、普通文本 allow。"""
    r = jmod._local_guard_verdict({"state": "执行 rm -rf / 清空所有数据"})
    assert r["verdict"] == "deny" and r["advisory"] is True, r
    r2 = jmod._local_guard_verdict({"state": "删除旧缓存文件"})
    assert r2["verdict"] == "ask" and r2["advisory"] is True, r2
    r3 = jmod._local_guard_verdict({"state": "读取 config.yaml 配置文件"})
    assert r3["verdict"] == "allow" and r3["advisory"] is True, r3
    print("Guard offline rules: PASSED")


def test_guard_online_three_tiers():
    """在线 guard 三档：prob>=threshold→allow，中间→ask，<deny_threshold→deny。"""
    jmod._adapter_available = True
    jmod._get_agnes_key = lambda: "fake"

    cases = [
        (0.90, "allow"),
        (0.50, "ask"),
        (0.10, "deny"),
    ]
    for prob, expect in cases:
        orig = patch_online(make_fake_response(nouls={"guard_q": prob}))
        r = json.loads(jmod._handle_jev_guard({
            "state": f"在线三档测试 prob={prob}",
            "guard": {"threshold": 0.75},
            "no_cache": True,
        }))
        restore_online(orig)
        assert r["verdict"] == expect, (prob, r)
        assert r["advisory"] is False, r
        assert "deny_threshold" in r, r
    print("Guard online three-tier (allow/ask/deny): PASSED")


def test_guard_online_timeout_fallback():
    """在线 guard 超时 → 熔断降级本地 fail-closed 规则。"""
    jmod._adapter_available = True
    jmod._get_agnes_key = lambda: "fake"
    import concurrent.futures
    def raise_timeout():
        raise concurrent.futures.TimeoutError()
    orig = patch_online(None, side_effect=raise_timeout)
    r = json.loads(jmod._handle_jev_guard({
        "state": "执行 rm -rf / 清空所有数据",
        "no_cache": True,
        "timeout_seconds": 0.5,
    }))
    restore_online(orig)
    assert r["advisory"] is True, r
    assert r["verdict"] == "deny", r  # 离线 fail-closed
    assert "超时" in r.get("reason", ""), r
    print("Guard online timeout fallback (fail-closed): PASSED")


def test_guard_threshold_clamp():
    """threshold 越界 clamp：5.0→1.0，-1→0.0，"abc"→默认 0.75。"""
    jmod._adapter_available = True
    jmod._get_agnes_key = lambda: "fake"

    orig = patch_online(make_fake_response(nouls={"guard_q": 0.9}))
    r = json.loads(jmod._handle_jev_guard({
        "state": "clamp-test", "guard": {"threshold": 5.0}, "no_cache": True,
    }))
    restore_online(orig)
    assert r["threshold"] == 1.0, r

    orig = patch_online(make_fake_response(nouls={"guard_q": 0.5}))
    r = json.loads(jmod._handle_jev_guard({
        "state": "clamp-test", "guard": {"threshold": -1}, "no_cache": True,
    }))
    restore_online(orig)
    assert r["threshold"] == 0.0, r

    orig = patch_online(make_fake_response(nouls={"guard_q": 0.5}))
    r = json.loads(jmod._handle_jev_guard({
        "state": "clamp-test", "guard": {"threshold": "abc"}, "no_cache": True,
    }))
    restore_online(orig)
    assert r["threshold"] == 0.75, r
    print("Guard threshold clamp: PASSED")


def test_jev_multi_question_mock():
    """多题并发打满（mock）：3 题一次往返全返回。"""
    jmod._adapter_available = True
    jmod._get_agnes_key = lambda: "fake"
    orig = patch_online(make_fake_response(
        nouls={"q1": 1.0, "q2": 0.0, "q3": 0.5},
        choices={"route": "coder"},
        scores={"complexity": "高"},
    ))
    r = json.loads(jmod._handle_jev({
        "state": "批量评估测试",
        "nouls": [
            {"name": "q1", "instructions": "是否安全？"},
            {"name": "q2", "instructions": "是否可逆？"},
        ],
        "noul": {"name": "q3", "instructions": "是否需要确认？"},
        "choice": {"name": "route", "instructions": "优先调用哪个专家？", "criteria": {"coder": "写代码", "searcher": "查资料"}},
        "score": {"name": "complexity", "instructions": "复杂度？", "criteria": ["低", "中", "高"]},
        "no_cache": True,
    }))
    restore_online(orig)
    assert "answers" in r, r
    assert r["answers"]["q1"] == 1.0 and r["answers"]["q2"] == 0.0 and r["answers"]["q3"] == 0.5, r["answers"]
    assert r["answers"]["route"] == "coder", r["answers"]
    assert r["answers"]["complexity"] == "高", r["answers"]
    assert r["choice_answer"] == "coder", r
    assert r["noul_answer"] == 0.5, r
    assert r["score_answer"] == "高", r
    assert r["advisory"] is False, r
    print(f"Multi-question batching (mock): PASSED ({len(r['answers'])} answers)")


def test_jev_timeout_circuit_breaker():
    """jev 主工具超时熔断（mock）：超时必然触发 advisory 降级。"""
    jmod._adapter_available = True
    jmod._get_agnes_key = lambda: "fake"
    import concurrent.futures
    def raise_timeout():
        raise concurrent.futures.TimeoutError()
    orig = patch_online(None, side_effect=raise_timeout)
    r = json.loads(jmod._handle_jev({
        "state": "超时熔断测试",
        "noul": {"name": "t", "instructions": "是否触发？"},
        "no_cache": True,
        "timeout_seconds": 0.5,
    }))
    restore_online(orig)
    assert r.get("advisory") is True, r
    assert "超时" in r.get("reason", ""), r
    print("Timeout circuit breaker: PASSED")


def test_jev_error_fallback():
    """jev 在线异常（mock）：非超时异常也降级 advisory。"""
    jmod._adapter_available = True
    jmod._get_agnes_key = lambda: "fake"
    def raise_err():
        raise RuntimeError("boom")
    orig = patch_online(None, side_effect=raise_err)
    r = json.loads(jmod._handle_jev({
        "state": "异常降级测试",
        "noul": {"name": "e", "instructions": "是否异常？"},
        "no_cache": True,
    }))
    restore_online(orig)
    assert r.get("advisory") is True, r
    assert "boom" in r.get("reason", ""), r
    print("Online error fallback: PASSED")


def test_disk_persistent_cache():
    """磁盘持久化缓存（纯本地，不触网）：写盘 → 重新加载 → 0ms 命中。"""
    args = {"state": "磁盘缓存验证-mock", "noul": {"name": "persist", "instructions": "是否持久化？"}, "timeout_seconds": 30.0}
    key = jmod._get_cache_key(args)
    jmod._DECISION_CACHE.pop(key, None)

    jmod._adapter_available = True
    jmod._get_agnes_key = lambda: "fake"
    orig = patch_online(make_fake_response(nouls={"persist": 1.0}))
    r1 = json.loads(jmod._handle_jev(args))
    restore_online(orig)
    assert r1["cache_hit"] is False, r1
    assert jmod._CACHE_FILE.exists() and jmod._CACHE_FILE.stat().st_size > 0

    import importlib
    importlib.reload(jmod)
    import __init__ as reloaded
    r2 = json.loads(reloaded._handle_jev(args))
    assert r2["cache_hit"] is True, r2
    assert r2["latency_seconds"] == 0.0
    print("Disk persistent cache: PASSED (0ms hit after reload)")


def test_journal_log():
    """审计黑匣子日志：每次判定追加 JSONL 流水。"""
    from __init__ import _JOURNAL_FILE
    journal = _JOURNAL_FILE
    before = 0 if not journal.exists() else len(journal.read_text(encoding="utf-8").splitlines())
    jmod._log_journal({"tool": "jev", "test_entry": True})
    after = len(journal.read_text(encoding="utf-8").splitlines())
    assert after == before + 1, (before, after)
    last = json.loads(journal.read_text(encoding="utf-8").strip().split("\n")[-1])
    assert last.get("test_entry") is True
    assert "timestamp" in last
    print(f"Journal log: PASSED ({after} total entries)")


def test_offline_result_cached():
    """验证：offline advisory 结果会被缓存（设计如此，1h 内不反复降级）。"""
    jmod._adapter_available = False
    jmod._get_agnes_key = lambda: ""
    args = {"state": "offline-cache-test", "noul": {"name": "oc", "instructions": "测试？"}, "timeout_seconds": 1.0}
    key = jmod._get_cache_key(args)
    jmod._DECISION_CACHE.pop(key, None)
    r = json.loads(jmod._handle_jev(args))
    assert r["advisory"] is True, r
    assert key in jmod._DECISION_CACHE, "offline advisory should be cached"
    jmod._DECISION_CACHE.pop(key, None)
    print("Offline advisory caching (by design): PASSED")


if __name__ == "__main__":
    test_availability()
    test_guard_offline_rules()
    test_guard_online_three_tiers()
    test_guard_online_timeout_fallback()
    test_guard_threshold_clamp()
    test_jev_multi_question_mock()
    test_jev_timeout_circuit_breaker()
    test_jev_error_fallback()
    test_disk_persistent_cache()
    test_journal_log()
    test_offline_result_cached()
    print("ALL TESTS PASSED")
