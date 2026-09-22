# Auto-generated verification test
import json
from __init__ import (
    _jev_tool_available,
    _handle_jev,
    _handle_jev_guard,
    _local_guard_verdict,
    _CACHE_FILE,
)

def test_availability():
    assert _jev_tool_available() is True
    print('Availability check: PASSED')

def test_guard_offline_rules():
    """离线降级规则单元：高危 deny、破坏性词 ask、普通文本 allow"""
    r = _local_guard_verdict({"state": "执行 rm -rf / 清空所有数据"})
    assert r["verdict"] == "deny" and r["advisory"] is True, r
    r2 = _local_guard_verdict({"state": "删除旧缓存文件"})
    assert r2["verdict"] == "ask" and r2["advisory"] is True, r2
    r3 = _local_guard_verdict({"state": "读取 config.yaml 配置文件"})
    assert r3["verdict"] == "allow" and r3["advisory"] is True, r3
    print('Guard offline rules: PASSED')

def test_guard_online_path():
    """在线路径：有 key 时走 Jev 概率裁决，返回 verdict+probability+advisory=False"""
    r = json.loads(_handle_jev_guard({"state": "执行 rm -rf / 清空所有数据", "no_cache": True, "timeout_seconds": 30.0}))
    assert r.get("advisory") is False, r
    assert r.get("verdict") in ("allow", "ask", "deny"), r
    assert "probability" in r, r
    print('Guard online path: PASSED')

def test_multi_question_batching():
    """多题并发打满：3 题一次请求全部返回"""
    r = json.loads(_handle_jev({
        "state": "批量评估测试",
        "nouls": [
            {"name": "q1", "instructions": "是否安全？"},
            {"name": "q2", "instructions": "是否可逆？"},
        ],
        "noul": {"name": "q3", "instructions": "是否需要确认？"},
        "no_cache": True,
        "timeout_seconds": 30.0,
    }))
    assert "answers" in r, r
    assert len(r["answers"]) >= 3, f"expected >=3 answers, got {len(r.get('answers', {}))}"
    assert all(k in r["answers"] for k in ("q1", "q2", "q3")), r["answers"]
    print(f'Multi-question batching: PASSED ({len(r["answers"])} answers in one round-trip, {r["latency_seconds"]}s)')

def test_timeout_circuit_breaker():
    """硬超时熔断：1s 超时必然触发降级 advisory"""
    r = json.loads(_handle_jev({
        "state": "超时熔断测试",
        "noul": {"name": "t", "instructions": "是否触发？"},
        "no_cache": True,
        "timeout_seconds": 1.0,
    }))
    assert r.get("advisory") is True, r
    assert "超时" in r.get("reason", ""), r
    print('Timeout circuit breaker: PASSED')

def test_disk_persistent_cache():
    """磁盘持久化缓存：写盘 → 重新加载 → 0ms 命中（首次不跳缓存）"""
    import importlib
    args = {"state": "磁盘缓存验证", "noul": {"name": "persist", "instructions": "是否持久化？"}, "timeout_seconds": 30.0}
    # 先清掉这条 key 的旧缓存，确保首次是真实写盘
    import __init__ as cur_mod
    from __init__ import _get_cache_key
    key = _get_cache_key(args)
    cur_mod._DECISION_CACHE.pop(key, None)

    r1 = json.loads(_handle_jev(args))
    assert r1["cache_hit"] is False, r1
    assert _CACHE_FILE.exists() and _CACHE_FILE.stat().st_size > 0, "cache file not written"

    # 模拟新进程重启：reload 后内存缓存被重建，应从磁盘恢复该 key
    importlib.reload(__import__("__init__"))
    import __init__ as reloaded
    r2 = json.loads(reloaded._handle_jev(args))
    assert r2["cache_hit"] is True, r2
    assert r2["latency_seconds"] == 0.0
    print(f'Disk persistent cache: PASSED (0ms hit after reload)')

def test_journal_log():
    """审计黑匣子日志：每次判定追加 JSONL 流水"""
    import __init__ as jmod
    from __init__ import _JOURNAL_FILE
    journal = _JOURNAL_FILE
    before = 0 if not journal.exists() else len(journal.read_text(encoding="utf-8").splitlines())
    jmod._log_journal({"tool": "jev", "test_entry": True})
    after = len(journal.read_text(encoding="utf-8").splitlines())
    assert after == before + 1, (before, after)
    last = json.loads(journal.read_text(encoding="utf-8").strip().split("\n")[-1])
    assert last.get("test_entry") is True
    assert "timestamp" in last
    print(f'Journal log: PASSED ({after} total entries)')

if __name__ == '__main__':
    test_availability()
    test_guard_offline_rules()
    test_guard_online_path()
    test_multi_question_batching()
    test_timeout_circuit_breaker()
    test_disk_persistent_cache()
    test_journal_log()
    print('ALL TESTS PASSED')
