---
name: add-risk-agent
description: 在信贷风控系统中新增一个风控岗位Agent（如催收岗/额度管理岗）。触发条件：用户要求"新增Agent/新增风控岗位/加一个环节/扩展流程"等。按项目约定完成输出模型登记、提示词登记、规则集、双图注册与全量验证。
---

# 新增风控岗位 Agent

本项目是多 Agent 信贷风控流水线，新增 Agent 有**固定的登记约定**（单一事实源原则），
必须按顺序完成以下 6 步，缺一步都会导致运行时报错或审计缺失。

## 背景约定（先确认）

- 三层兜底：每个 Agent 的决策按「LLM推理 → 规则引擎 → 默认兜底」执行，任一成功即返回
- 安全原则：**宁可转人工，不可误放行**——默认兜底必须保守（转人工/拒绝）
- 双图并存：同步图（CLI/测试）+ 异步图（高并发流水线）必须**同时注册**
- 单一事实源：结构化输出在 `agents/resume/state.py`，提示词在 `agents/resume/prompts.py`

## 步骤

### 1. 登记结构化输出模型 —— `credit_risk_system/agents/resume/state.py`

- 继承 `AgentResult` 定义新输出模型（如 `CollectionResult`）
- docstring 必须写明：**作用 / 生产者 / 消费者**（谁产生、谁消费、在流程中的角色）
- 枚举值若需新增（如催收策略枚举），加在该文件「通用枚举」区
- 兼容层 `models/risk.py` 无需改动（惰性 re-export 自动可见）

### 2. 实现 Agent 类 —— `credit_risk_system/agents/<name>_agent.py`

继承 `BaseAgent`，**只实现 3 个方法**（`build_prompt` 有默认实现，从提示词库按 name 自动取）：

```python
class CollectionAgent(BaseAgent):
    name = "collection_agent"          # 必须唯一，作为提示词库/审计/消息总线键
    role = "催收管理Agent（催收岗）"
    result_model = CollectionResult
    tool_names = []                     # LLM Function Calling 工具（可选）

    def collect_evidence(self, ctx): ...   # 证据采集（阻塞SDK走 run_blocking）
    def rule_decide(self, ctx, evidence): ...   # 规则引擎决策（确定性主路径）
    def default_decide(self, ctx, evidence): ... # 保守兜底（转人工）
```

参考 `agents/intake_agent.py`（最简范式）。注意 `rule_decide` 用 `self._facts(ctx, evidence, **extra)`
构造标准化事实字典。

### 3. 登记提示词 —— `credit_risk_system/agents/resume/prompts.py`

在 `AGENT_PROMPTS` 注册 `PromptSpec`：

- `system`：角色定位 → 职责范围 → 决策口径 → 可调用工具 → 安全原则
- **决策口径中的阈值/权重必须用 `risk_strategy` 常量 f-string 插值**（保证与规则引擎同步，如 `rs.DTI_CAP`）
- `instruction`：输出要求（列出要产出的字段名）

### 4. 添加规则集 —— `credit_risk_system/core/rule_engine.py`

- 用声明式 `Rule(name, category, description, condition, score, message, severity)` 定义规则
- 条件用构造器：`between/ge/lt/eq/is_false/contains_keyword`（参考现有规则集）
- `severity` 语义：`VIOLATION`=硬性（拒绝/否决）、`WARNING`=提示（转人工/整改）
- 新建 `build_<stage>_rules()` 并在 `_BUILDERS` 注册
- 如涉及新阈值，**先加到 `config/risk_strategy.py`**，规则与提示词都引用它

### 5. 双图注册节点与路由 —— `credit_risk_system/core/orchestrator.py`

**两张图都要注册**（`_build_graph` 同步 + `_build_agraph` 异步），节点执行器模板：

```python
# 同步图
g.add_node("collection", self._node("collection", self._run_collection))
# 异步图
g.add_node("collection", self._anode("collection", self._arun_collection))

async def _arun_collection(self, ctx):        # 异步节点调 arun()
    ctx.collection_result = await self.collection_agent.arun(ctx)
```

- 边与条件路由加在共享方法 `_add_edges`（两张图共用）
- 条件路由函数加 `@staticmethod _route_after_xxx(state)`
- 若新环节有输出，在 `PipelineContext` 增加对应字段（`state.py`）

### 6. 验证（全部通过才算完成）

```bash
.venv/Scripts/python.exe main.py                 # 5 案例决策与 README 预期一致
.venv/Scripts/python.exe main.py --concurrent    # 异步路径同样通过
.venv/Scripts/python.exe server.py               # 服务启动无报错
# 可选：压测
.venv/Scripts/python.exe scripts/load_test.py --concurrency 50 --requests 100
```

## 常见坑

- 只注册同步图 → 异步流水线/API 服务里新环节**静默缺失**（双图必须同步注册）
- 提示词漏登记 → `build_prompt` 抛 `KeyError: 提示词库中未登记Agent`
- 默认兜底写成 PASS → 违反安全原则（必须保守：转人工）
- 规则阈值写死在提示词/规则里 → 违反策略配置中心约定（必须引用 `rs` 常量）
- 输出模型 docstring 只写字段不写作用 → 破坏 state.py 的文档约定
