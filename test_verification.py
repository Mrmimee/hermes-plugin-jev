# Auto-generated verification test
import json
from __init__ import _jev_tool_available, _handle_jev_guard, _local_guard_verdict

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
    r = json.loads(_handle_jev_guard({"state": "执行 rm -rf / 清空所有数据", "no_cache": True}))
    assert r.get("advisory") is False, r
    assert r.get("verdict") in ("allow", "ask", "deny"), r
    assert "probability" in r, r
    print('Guard online path: PASSED')

if __name__ == '__main__':
    test_availability()
    test_guard_offline_rules()
    test_guard_online_path()
