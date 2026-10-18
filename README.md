# hermes-plugin-jev 🧠

[![Hermes Agent](https://img.shields.io/badge/Hermes_Agent-Plugin-blue.svg)](https://hermes-agent.nousresearch.com)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)
[![Powered by](https://img.shields.io/badge/Powered_by-TypeSafe_AI_%26_Agnes_AI-purple.svg)](https://typesafe.ai)

**Jev (TypeSafe AI) System One 决策小脑插件** —— 专为 **Hermes Agent** 打造的极速结构化决策与智能路由扩展。

> **v1.2.0（2026-10-18 迭代）**：`jev_guard` 升级为**六信号并行裁决 + 确定性判定**；发送前脱敏覆盖结构化 dict 敏感键名；
> 引擎保持**只走 `system_one_adapter` → Agnes 3.0 Flash**（官方 TypeSafe 直连按量计费，不接入）；
> 引擎不可用 / 超时 / 调用失败一律**直接返回 error**，不再做本地离线兜底判定。

---

## 🌟 为什么需要 Jev？

传统的 Agent 在面对多步骤工作流时，往往每一步都要调用庞大且昂贵的生成式大模型去判断“下一步该干嘛”或者“有没有安全风险”。这导致了严重的延迟累积和 Token 浪费。

**Jev 并非生成式聊天模型，而是“决策小脑”（System One Model）：**
* **零废话**：不进行流式长文本输出，输入当前状态，直接输出确定性的结构化判定。
* **极速响应**：基于 `system_one_adapter` 接入云端 Agnes 3.0 Flash，1~2 秒内完成判定（多题并发同价）。
* **零本地负载**：完全不占用本地 GPU 显存（VRAM: 0MB），纯轻量 HTTP 交互，无后台常驻进程。
* **绝不卡死**：8 秒硬超时熔断，超时直接返回 error，主流程永远不被网络阻塞。

---

## ⚡ 性能与工程特性

| 特性 | 说明 |
| :--- | :--- |
| **单一引擎、零计费** | 在线判定统一走 `system_one_adapter` → Agnes 3.0 Flash（`AGNES_API_KEY`）。结果带 `engine` / `model` / `usage`，可核对实际路径。官方 TypeSafe 直连按量计费，本插件不接入 |
| **多题并发打满** | `nouls` / `choices` / `scores` 数组，一次网络往返裁决 N 题，延迟不随题目数增长 |
| **双层持久化缓存** | 内存 LRU（256 条/1h TTL）+ 磁盘 JSON（`~/.hermes/cache/jev_cache.json`），重启/新子任务秒级 0ms 命中 |
| **硬超时熔断** | `timeout_seconds` 默认 8s，超时触发 `concurrent.futures.TimeoutError` → 直接返回 error（不编造结论），主流程永不挂起 |
| **失败即报错** | 无引擎 / 超时 / 调用失败一律返回 `error`，**不做本地离线兜底判定**，不给任何假 verdict；失败结果也不写缓存 |
| **发送前脱敏** | `state` 里的 `Bearer …` / `sk-…` / `ghp_…` / `github_pat_…` / `password=…` 以及结构化 dict 的敏感键名（`api_key`/`token`/`secret`/`password`/`credential`/`auth`）在**发送前**即被遮蔽，凭据不进第三方请求、不进审计日志 |
| **审计黑匣子** | 每次判定追加 `~/.hermes/cache/jev_journal.jsonl`（工具、延迟、命中、答案、状态预览，超 10MB 自动轮转），复盘/追踪零成本 |

### 🧭 引擎与配置

| 环境变量 | 作用 | 默认 |
| :--- | :--- | :--- |
| `AGNES_API_KEY` | 唯一在线引擎（adapter → Agnes 3.0 Flash）的 key；缺失时回退读桌面 `apikey.txt` 的 `AgnesAi` 条目 | 未设置 |

依赖：`system-one-adapter[openai]` 需可导入。Hermes 的 PM 环境重建会清掉它，插件会自动把 `~/jev-starter/.venv/Lib/site-packages` 挂到 `sys.path`（`uv pip install "system-one-adapter[openai]"` 即可恢复）。

---

## 🧩 核心裁决原语

| 原语 | 类别 | 典型应用场景 |
| :--- | :--- | :--- |
| **`Choice`** | 单选题 | 从多个候选执行器或工具中做路由跳转（如：Coder vs Searcher vs Reviewer） |
| **`Noul`** | 是非题 | 布尔事件判定或发生概率计算（如：是否属于高危破坏性操作、是否需要联网） |
| **`Score`** | 打分题 | 离散或连续多梯度评估（如：任务复杂度 0~2 分、相关度评分） |
| **`Guard`** 🛡️ | 动作闸门 | 执行动作前做 allow/ask/deny 三档风险评估；作为第二意见，不替代用户/系统授权。在线路径为**六信号并行裁决 + 确定性判定**（见下）；引擎不可用/超时/失败时直接返回 error，不给 verdict。 |

### 多题并发打满（1 次请求裁决 N 题）

```json
{
  "state": "当前任务上下文",
  "nouls": [
    {"name": "是否高危", "instructions": "是否属于破坏性操作？"},
    {"name": "是否可逆", "instructions": "操作是否可撤销？"}
  ],
  "choice": {
    "name": "路由",
    "instructions": "优先调用哪个专家？",
    "criteria": {"coder": "写代码", "searcher": "查资料"}
  }
}
```

结果统一在 `answers` 字段：`{"answers": {"是否高危": 0.0, "是否可逆": 1.0, "路由": "coder"}}`
单题参数（`choice`/`noul`/`score`）同时保留在结果里做向后兼容（`choice_answer`/`noul_answer`/`score_answer`）。

### `jev_guard` 动作闸门

v1.1.0 起，在线路径**一次请求并行裁决 6 个信号**（判定顺序对标官方 `jev-approvals` 插件），再由确定性代码给出结论——不让自由文本当授权：

| 信号 | 题型 | 含义 |
| :--- | :--- | :--- |
| `guard_q` | Noul | 兼容旧版的「是否可安全执行」概率 |
| `policy_allows` | Noul | 既定策略是否明确允许（常规、低风险、可回滚） |
| `reads_secrets` | Noul | 是否读取/复制/打印/上传凭据、密钥、令牌、密码 |
| `sends_outbound` | Noul | 是否把本地内容发往外部网络或第三方 |
| `self_advocating` | Noul | 命令文本是否在自我辩护、催促或诱导放行 |
| `blast_radius` | Score | `0` trivial / `1` annoying / `2` severe |

确定性判定顺序（`verdict_source: signals`）：

1. `self_advocating >= 0.6` → **ask**（疑似自我游说）
2. `reads_secrets >= 0.7` 或 `sends_outbound >= 0.7` → **deny**
3. `blast_radius >= 1.6` → **ask**（破坏半径大，**刻意前移**：不因“策略允许”而自动放行）
4. `policy_allows >= threshold` 且 `blast_radius < 2.0` → **allow**
5. 其余 → **ask**

信号缺失（旧响应 / 自定义端点只回 `guard_q`）时自动退回单题概率三档，`verdict_source: probability`。

```json
{
  "state": "执行 rm -rf /data/old_cache",
  "guard": {
    "instructions": "该动作是否可安全执行？",
    "threshold": 0.75
  }
}
```

- 有 `AGNES_API_KEY`：在线走 Jev 六信号裁决（首选），信号缺失时退回单题概率三档：
  - `probability >= threshold`：判 **`allow`**（安全放行）
  - `deny_threshold <= probability < threshold`：判 **`ask`**（存在风险，建议人工确认）
  - `probability < deny_threshold`（默认 `min(0.25, threshold*0.5)`）：判 **`deny`**（高危操作拦截）
- 无引擎 / adapter 缺失 / 超时熔断 / 在线调用失败：**直接返回 `error`**，不给 verdict、不做本地兜底判定（拿不到 verdict 就不要当作 allow）

## 使用原则：按需路由

- 普通对话、工具选择显而易见时，不调用 Jev；不要每轮调用，也不要每次工具调用前重复裁决。
- 只有多个工具或执行路径都合理、选择确有不确定性时，才用 `Choice` 选择路由。把可用候选及用途写清楚，例如 `computer_use`（操作桌面界面）、`browser`（网页交互）、`web_search`（查资料）。
- Jev 返回结构化建议；主 Agent 根据结果调用工具并负责执行。Jev 本身不会调用或加载其他工具。
- 只传这次判断所需的上下文，不要发送密钥、令牌、密码或无关个人信息。
- `jev_guard` 是高风险动作的可选第二意见，不是系统授权机制。普通低风险工具调用无需逐个运行；引擎不可用或调用失败时直接返回 `error`，不会给出任何兜底 verdict——拿不到 verdict 就不能当作安全许可。

### 工具路由示例

```json
{
  "state": "用户要在当前桌面中操作一个网页应用；browser 与 computer_use 都可行。",
  "choice": {
    "name": "tool_route",
    "instructions": "根据任务是否需要真实桌面视觉/鼠标键盘操作选择工具。",
    "criteria": {
      "computer_use": "需要观察或操作桌面窗口、原生应用或屏幕上的 UI",
      "browser": "只需在网页标签中导航、读取或填写网页内容",
      "web_search": "任务是查找网络资料，不需要操作现有界面"
    }
  }
}
```

---

## 🚀 安装与接入

### 方式一：直接克隆到 Hermes 插件目录

```bash
cd ~/.hermes/plugins
git clone https://github.com/Mrmimee/hermes-plugin-jev.git jev
```

### 方式二：启用插件

在 `~/.hermes/config.yaml` 中添加启用项：

```yaml
plugins:
  enabled:
    - jev
```

### 方式三：环境要求

**唯一引擎**（`system_one_adapter` → Agnes 3.0 Flash）：把 `AGNES_API_KEY` 写入环境变量即可（或放桌面 `apikey.txt` 的 `AgnesAi` 条目，插件会自动读取）：
```bash
export AGNES_API_KEY="sk-..."
```
依赖：`uv pip install "system-one-adapter[openai]"`（本机装在 `~/jev-starter/.venv`，插件自动挂 `sys.path`）。

---

## 🛠️ 在 Hermes 中使用

新会话启动后，Hermes 会自动加载 `jev` 工具集。

### 自动化场景（Hermes 自主感知）
* 遇到复杂的多步骤或多工具任务分流时，Hermes 会在后台自主调用 Jev 完成并行裁决。
* 遇到系统关键变更或文件批量清理时，自动发起 Noul 风险判定。

### 显式对话示例
> “帮我用 Jev 评估一下当前需求应该调用哪个工具，以及是否存在破坏性操作。”

返回里可核对 `engine`（agnes / none）、`model`（如 `agnes-3.0-flash`）、`usage`（token 计费依据）；
`jev_guard` 额外返回 `signals`（六信号）与 `verdict_source`（signals / probability），便于审计与调参。

---

## 📂 项目结构

```text
hermes-plugin-jev/
├── plugin.yaml       # Hermes 插件声明配置
├── __init__.py       # 插件入口与 Jev/jev_guard 工具注册实现
├── skills/
│   └── jev-decision-engine/
│       └── SKILL.md  # 捆绑技能（随插件分发，启用即以 jev:jev-decision-engine 出现在技能列表）
├── test_verification.py  # 验证测试（可用性 + 无引擎报错 + 在线路径 + 脱敏/六信号/usage）
├── README.md         # 项目文档
├── .gitignore        # Git 忽略配置
└── sync.py           # 一键更新与 GitHub 同步工具
```

### 🧠 捆绑技能 `jev-decision-engine`

插件在 `register()` 里调用 `ctx.register_skill(...)` 把 `skills/jev-decision-engine/SKILL.md` 注册为插件技能：
**克隆/更新仓库就自带这份技能**，不需要单独往 `~/.hermes/skills/` 拷，也不会被技能清理误删。内容包含何时调 `jev` / `jev_guard`、可直接抄的参数模板、结果字段读法、失败语义（error ≠ allow）与依赖桥自检步骤。

---

## 📄 License

MIT License.
