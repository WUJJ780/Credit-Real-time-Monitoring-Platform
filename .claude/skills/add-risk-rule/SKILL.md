---
name: add-risk-rule
description: 在规则引擎中新增一条声明式风控规则（准入/反欺诈/信用/审批/合规/贷后）。触发条件：用户要求"加一条规则/新增风控规则/某情况也要拦截/加上XX校验"等。按声明式规则约定实现并单测验证。
---

# 新增风控规则

规则引擎（`credit_risk_system/core/rule_engine.py`）是**银行级可解释决策的核心**：
所有规则声明式定义，命中即带 score/severity，决策留痕可追溯。

## 规则结构

```python
Rule(
    name="多头借贷",                # 规则名（出现在理由链/审计/前端标签）
    category="multiloan",          # 模块分类（用于评分封顶聚合）
    description="借贷平台数超阈值",  # 说明（合规提示词会展示）
    condition=lambda f: _num("multi_platforms")(f) >= 5,  # 条件：facts -> bool
    score=8,                       # 命中加分（静态值或 callable(facts) 动态分）
    message="多头借贷平台数异常",    # 命中提示
    severity="WARNING",            # VIOLATION=硬性 / WARNING=提示
)
```

## 步骤

### 1. 确定 facts 键（标准化事实字典）

规则条件读取的键来自 `ctx.application.facts()` + 各 Agent 的 evidence。现有键约定
（见 rule_engine.py 模块 docstring）：基础 `age income amount term purpose memo
phone_months education marital housing work_years income_evidence consent authorization channel`；
进件 `missing_docs ocr_confidence identity_verified`；反欺诈 `blacklist_max multi_platforms
query_7d rooted emulator proxy first_seen_days account_count ip_mismatch gang_score
similar_fraud`；信用 `has_report history_years overdue_m1..m3 overdue_total utilization
query_3m credit_cards dti free_cash_flow credit_score grade`；审批 `fraud_score fraud_level
composite_risk suggested_max`；合规 `annual_rate decision audit_count`。

**新规则需要的键不存在时**：先在对应 Agent 的 `collect_evidence` 或 `_facts(extra=...)` 中补齐，
再写规则。

### 2. 条件构造器（必须用，不要裸写 lambda 读 dict）

- `ge(key, v)` / `lt(key, v)` / `between(key, lo, hi)` / `eq(key, v)`
- `is_false(key)`（授权/开关类布尔）
- `contains_keyword(key, keywords)`（用途/备注关键词，如合规禁止用途）
- 多条件组合：lambda 里 `and/or`（参考「影像质量提示」规则）

### 3. 阈值进配置中心

规则涉及的数字阈值**先加 `config/risk_strategy.py`**（如 `rs.MY_NEW_THRESHOLD`），
规则、提示词、策略文档统一引用——不要硬编码。

### 4. severity 语义（风控安全底线）

- `VIOLATION`：硬性规则，命中即拒绝/否决（准入拒绝、黑名单、合规一票否决）
- `WARNING`：提示规则，命中转人工复核或整改提示
- 拿不准选 `WARNING` 转人工——**宁可转人工，不可误放行**

### 5. 单测验证（不需要起服务）

```python
from credit_risk_system.core.rule_engine import get_rule_engine
engine = get_rule_engine("antifraud")   # 对应规则集
facts = {"multi_platforms": 9, "query_7d": 3, ...}
triggered = engine.triggered(facts)
assert any(r.rule.name == "多头借贷" for r in triggered)
```

### 6. 全量回归

```bash
.venv/Scripts/python.exe main.py          # 5 案例决策对照 README 预期表
.venv/Scripts/python.exe main.py --concurrent
```

## 常见坑

- 条件读不存在的键：`_num(key)` 对缺失键返回 0、`eq` 返回 False——静默不命中，
  务必先确认 facts 键存在（第 1 步）
- 动态分用 `lambda f: float(f.get("xx") or 0)`（参考「多头借贷」规则），
  不要直接引用可变全局变量
- 规则名与提示词/审计展示直接关联，起名用业务语言（如「设备ROOT/越狱」而非「rule_01」）
- 合规类规则（利率/用途/授权）新增后，同步检查 `resume/prompts.py` 合规提示词里的规则清单
  （`_compliance_rule_list()` 是动态注入的，规则名会自动出现）
