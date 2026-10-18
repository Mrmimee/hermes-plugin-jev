---
name: jev-decision-engine
description: "Use when a choice has real uncertainty (which tool or path to take) or a risky action needs a structured second opinion before executing."
version: 1.0.0
metadata:
  author: Hermes Agent
  tags: [jev, decision, routing, guard, approval, agnes]
  domain: decision-support
  hermes_adaptation: 与 jev 插件同仓库分发；插件启用即以 jev:jev-decision-engine 出现在技能列表
---

# Jev 决策引擎（system_one_adapter → Agnes 3.0 Flash）

结构化决策小脑：一次网络往返返回 Choice / Noul / Score 判定。**不是聊天模型**，只输出结构化答案。

## 什么时候用 / 不用

| 场景 | 工具 | 例子 |
| :-- | :-- | :-- |
| 多个工具/路径都合理，需取舍 | `jev` + `choice` / `choices` | computer_use vs browser vs web_search |
| 是非 / 风险 / 工作量判断 | `jev` + `noul` / `score` | 这个动作可逆吗？任务复杂度几档？ |
| 有实质风险的动作要第二意见 | `jev_guard` | 删文件、改配置、外发内容、读取凭据 |

**不要**用于：普通对话、显而易见的工具选择、每次工具调用前都裁决。不每轮调、不重复裁。

## 参数写法（可直接抄）

`jev`（单题用 `choice`/`noul`/`score`，多题并发用 `choices`/`nouls`/`scores`——一次往返全返回，别为每题单独调）：

```json
{
  "state": "只写与本次判断相关的上下文",
  "choice": {"name": "route", "instructions": "下一步走哪条路径？",
             "criteria": {"browser": "网页标签里导航", "computer_use": "操作桌面窗口"}},
  "nouls": [{"name": "risky", "instructions": "是否可能造成不可逆损失？"}],
  "score": {"name": "effort", "instructions": "工作量？", "criteria": ["低", "中", "高"]},
  "timeout_seconds": 8
}
```

`jev_guard`：

```json
{"state": "删除 ~/.hermes/cache 下 7 天前的临时文件", "guard": {"threshold": 0.75}}
```

## 结果怎么读

- `jev`：`answers{题名: 答案}`，外加单题别名 `choice_answer` / `noul_answer` / `score_answer`；`engine` / `model` / `usage` 标明实际路径与 token 消耗。
- `jev_guard`：`verdict`（allow / ask / deny）+ `verdict_source`（signals / probability）+ `signals`（policy_allows、reads_secrets、sends_outbound、self_advocating、blast_radius）+ `probability`。
  `ask` 就停下来问人；`deny` 不要执行；`allow` 也只是第二意见，不是授权。

## 每次调用都要留一行可见摘要

`jev` / `jev_guard` 返回后，**在同一轮回复里写一行**，让用户看得见这次裁决，不要把 JSON 直接甩出来：

```
🧠 Jev · <题名>=<答案> · <latency_seconds>s[ · 缓存]
🛡️ Jev Guard · <verdict> (<probability>) · <latency_seconds>s
```

- 一轮里调了多次就写多行，按调用顺序，别合并成一句笼统的"做了几次判断"。
- `answers` 里的值原样给出（0/1/2 或概率），不要改写成形容词，除非用户问的是结论本身。
- 返回 `error` 时写 `🧠 Jev · 失败：<error 摘要>`，并说明这次没有裁决结果。
- 这一行是摘要，不是解释：不要附带推理过程，也不要复述 `state`。

## 失败语义（最重要）

- **没有本地兜底**：引擎不可用 / 超时 / 调用失败一律返回 `error`，**不给 verdict**。看到 error 就等于「没裁决过」——按自己的判断继续，并在回复里说明 guard 没跑成；**绝不允许把 error 当成 allow**。
- `state` 在发送前自动脱敏（`Bearer …`/`sk-…`/`ghp_…`/`github_pat_…`/`password=`，以及 dict 里的 `api_key`/`token`/`secret`/`password` 等键值）。别指望把密钥传进去。
- 默认 8s 硬超时，超时即 error，不会拖死主流程；可用 `timeout_seconds` 调整。
- 结果按 `state + 题目` 缓存：`cache_hit=true` 说明复用了旧结论；状态变了就传 `no_cache: true`。

## 环境自检（不可用时按序查）

1. `AGNES_API_KEY`：`.env` 里没有时，插件会读桌面 `apikey.txt` 的 `AgnesAi` 行。
2. 适配器依赖：`~/jev-starter/.venv/Lib/site-packages/system_one_adapter` 必须存在。Hermes PM 重建环境会清掉它，重装：
   ```bash
   cd ~/jev-starter && uv pip install --python .venv/Scripts/python.exe "system-one-adapter[openai]"
   ```
3. 审计：每次调用追加 `%LOCALAPPDATA%\hermes\cache\jev_journal.jsonl`（工具/延迟/命中/答案/状态预览），判过什么看这里。
