"""信用评分卡模型：特征加权计分，输出信用分与等级。

模型配置见 config/risk_strategy.py 的 SCORECARD_CONFIG，
生产环境可替换为阿里PAI部署的机器学习模型（见core/adapters.py的PaiPlatformAdapter），
评分卡作为离线兜底模型，保证决策不中断。
"""
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from credit_risk_system.config import risk_strategy as rs
from credit_risk_system.models.risk import CreditGrade, ScoreCardResult


@dataclass(frozen=True)
class ScoreFeature:
    name: str
    weight: float
    bins: Tuple[Tuple[float, Optional[float], float], ...] = ()   # (lo, hi, points)
    mapping: Dict[str, float] = None                              # 类别型取值映射

    def points_of(self, value: Any) -> float:
        if self.mapping:
            return float(self.mapping.get(str(value), 0))
        v = float(value or 0)
        for lo, hi, points in self.bins:
            if (lo is None or v >= lo) and (hi is None or v <= hi):
                return points
        return 0.0


class ScoreCard:
    """信用评分卡（标准评分卡结构：特征分箱 + 权重加权）。"""

    def __init__(self, config, version: str = "scorecard-v1.0"):
        self.version = version
        self.features = [
            ScoreFeature(
                name=item["name"],
                weight=item["weight"],
                bins=tuple(item.get("bins", ())),
                mapping=item.get("mapping"),
            )
            for item in config
        ]
        self.max_score = sum(f.weight * 5.0 for f in self.features)

    def evaluate(self, features: Dict[str, Any]) -> ScoreCardResult:
        """评分：各特征分箱得分按权重加权后归一化到0-100。"""
        breakdown: Dict[str, float] = {}
        total_points = 0.0
        for feat in self.features:
            points = feat.points_of(features.get(feat.name))
            weighted = points * feat.weight
            breakdown[feat.name] = weighted
            total_points += weighted
        score = int(round(total_points / self.max_score * 100)) if self.max_score else 0
        return ScoreCardResult(
            total_score=max(0, min(100, score)),
            grade=self._grade(score),
            max_score=100,
            breakdown=breakdown,
            model_version=self.version,
        )

    @staticmethod
    def _grade(score: int) -> CreditGrade:
        for threshold, grade in rs.GRADE_BANDS:
            if score >= threshold:
                return CreditGrade(grade)
        return CreditGrade.E


_score_card: Optional[ScoreCard] = None


def get_scorecard() -> ScoreCard:
    """获取评分卡单例。"""
    global _score_card
    if _score_card is None:
        _score_card = ScoreCard(rs.SCORECARD_CONFIG, rs.SCORECARD_VERSION)
    return _score_card
