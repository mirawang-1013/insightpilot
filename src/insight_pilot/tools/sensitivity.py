"""
tools/sensitivity.py —— 报告敏感性分类器

【职责】
    判断 Reporter 产出的报告是否含"敏感结论"，
    决定 Reviewer 节点要不要触发 interrupt() 让人审批。

【两层判定策略】
    Layer 1: 关键词正则（快、零成本，抓 80% 显式信号）
    Layer 2: LLM 兜底（gpt-4o-mini，抓"绕开关键词的隐式建议"）

【保守原则】
    分类器异常或不确定 → 默认判定为 sensitive。
    漏报（False Negative）= 错误结论流到用户 = 真实损失
    误报（False Positive）= 用户多按一次"通过" = 轻微烦躁
    代价不对称，决定了应该偏向触发。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from insight_pilot.config import get_settings


# ============================================================================
# 返回结构
# ============================================================================
@dataclass
class SensitivityResult:
    """敏感性分类的结构化结果。"""

    is_sensitive: bool                  # True = 需要人工审批
    reason: str                         # 给用户/日志看的说明
    matched_layer: str                  # "keyword" / "llm" / "default" / "none"


# ============================================================================
# Layer 1: 关键词规则
#
# 设计原则：
#   1. 中英都覆盖（LLM 输出可能中英混杂）
#   2. 用 \b 词边界避免误伤（"投资建议" 命中，"投资业的状况"不命中）
#   3. 模式分组按"语义类别"，方便 reason 给具体原因
# ============================================================================

# ---- 模式 1：明确的投资建议 ----
# 例如：建议投资、强烈推荐投资、建议加大投入
_INVESTMENT_PATTERNS = [
    r"建议投资",
    r"建议(?:加大|增加|加倍|提升)(?:投入|投资|资源|预算)",
    r"应当(?:投资|投入)",
    r"\binvest in\b",
    r"\brecommend(ed)?\s+investment\b",
]

# ---- 模式 2：裁撤 / 否定决策 ----
# 例如：应该裁撤、停止合作、不再投放、放弃 X 品类
_NEGATIVE_DECISION_PATTERNS = [
    r"(?:应当|应该|建议)(?:裁撤|裁减|停止|放弃|退出|关闭)",
    r"不再(?:投放|合作|经营)",
    r"\b(?:cease|terminate|stop|abandon)\b",
    r"\bcut(?:ting)?\s+(?:off|loose|ties)\b",
]

# ---- 模式 3：声誉 / 用户体验负面结论 ----
# 例如：用户体验差、质量问题、配送拉跨
_REPUTATION_PATTERNS = [
    r"用户体验(?:差|很差|不佳|糟糕|拉跨)",
    r"(?:服务|质量|体验)(?:堪忧|令人失望|存在问题)",
    r"\bpoor\s+(?:user\s+experience|customer\s+experience|UX)\b",
    r"\bquality\s+(?:issues|problems)\b",
]

# ---- 模式 4：具体金额建议 ----
# 例如："投入 50 万雷亚尔"、"预算 100 万"、"X million BRL"
# 这类含数字的建议风险特别高 —— LLM 容易瞎编数字
_AMOUNT_PATTERNS = [
    r"(?:投入|投资|预算|资源)\s*[¥$]?\s*\d+\s*(?:万|亿|百万|千万|million|billion|BRL|R\$)",
    r"\b\d+\s*(?:million|billion)\s+(?:BRL|R\$|reais)\b",
]

# ---- 模式 5：绝对化措辞 ----
# 例如：必须、强烈建议、绝对、毫无疑问
# 这些是"AI 自信过头"的常见信号
_ABSOLUTE_PATTERNS = [
    r"必须(?:立即|马上|尽快)",
    r"强烈建议",
    r"毫无疑问",
    r"绝对(?:不应|应当)",
    r"\bmust\s+(?:immediately|absolutely)\b",
    r"\bstrongly\s+recommend\b",
]


# 把所有模式统一编译，加 IGNORECASE
_COMPILED_PATTERNS: dict[str, list[re.Pattern]] = {
    "投资建议": [re.compile(p, re.IGNORECASE) for p in _INVESTMENT_PATTERNS],
    "裁撤决策": [re.compile(p, re.IGNORECASE) for p in _NEGATIVE_DECISION_PATTERNS],
    "声誉负面": [re.compile(p, re.IGNORECASE) for p in _REPUTATION_PATTERNS],
    "具体金额": [re.compile(p, re.IGNORECASE) for p in _AMOUNT_PATTERNS],
    "绝对措辞": [re.compile(p, re.IGNORECASE) for p in _ABSOLUTE_PATTERNS],
}


def _classify_by_keywords(report: str) -> SensitivityResult | None:
    """
    Layer 1：关键词分类。

    Returns:
        命中 → SensitivityResult(is_sensitive=True, ...)
        不命中 → None（让上层进 Layer 2）
    """
    matched_categories: list[str] = []

    for category, patterns in _COMPILED_PATTERNS.items():
        for pattern in patterns:
            if pattern.search(report):
                matched_categories.append(category)
                break  # 一个类别命中一次就够，不重复加

    if matched_categories:
        return SensitivityResult(
            is_sensitive=True,
            reason=f"关键词匹配：{', '.join(matched_categories)}",
            matched_layer="keyword",
        )

    return None


# ============================================================================
# Layer 2: LLM 兜底判断
#
# 用 gpt-4o-mini（便宜约 20 倍）做一次 yes/no 判断。
# Prompt 教它：什么算敏感、什么不算。
# ============================================================================

_LLM_CLASSIFIER_PROMPT = """\
你是数据分析报告的安全审查员。判断给你的报告是否含"敏感结论"。

【敏感（需人工审批）】
- 投资建议（"应该把资源投入到 X"、"建议优先 Y"）
- 裁撤 / 否定决策（"应该停止做 X"、"不再投放 Y"）
- 声誉负面结论（"X 的用户体验差"、"Y 的质量有问题"）
- 具体金额建议（"投入 N 万到 X"）
- 绝对化措辞（"必须立即 X"、"强烈建议 Y"）

【非敏感（直接通过）】
- 趋势描述（"营收 YoY +20%"）
- 数据汇总（"Top 5 品类是 ..."）
- 中性洞察（"X 和 Y 负相关"）
- 模式识别（"11 月有黑五效应"）
- 探索性发现（"SP 州贡献了 35% 订单"）

【规则】
1. 看到任何"行动建议"（应该/建议/推荐 + 动作）就判敏感
2. 看到"对某主体的负面评价"就判敏感
3. 单纯描述事实和数据 → 不敏感
4. 不确定时偏向判敏感

只回答一行 JSON：{"is_sensitive": true/false, "reason": "<10字内>"}
"""


def _classify_by_llm(report: str) -> SensitivityResult:
    """
    Layer 2：LLM 兜底。

    用 gpt-4o-mini（成本低）做二次判断。失败时默认 sensitive（保守）。
    """
    try:
        settings = get_settings()
        # 用便宜模型省成本，反正只是分类
        llm = ChatOpenAI(
            model="gpt-4o-mini",
            temperature=0,
            api_key=settings.openai_api_key,
        )
        # 截断报告防超 token（取前 3000 字符已经够看出敏感性）
        report_preview = report[:3000]
        messages = [
            SystemMessage(content=_LLM_CLASSIFIER_PROMPT),
            HumanMessage(content=f"报告：\n{report_preview}"),
        ]
        response = llm.invoke(messages)
        content = str(response.content).strip()

        # 解析 JSON 输出
        # LLM 可能包 ```json ... ``` 代码块，剥掉
        if content.startswith("```"):
            content = re.sub(r"^```(?:json)?\s*\n", "", content)
            content = re.sub(r"\n```\s*$", "", content)

        import json
        parsed = json.loads(content)
        return SensitivityResult(
            is_sensitive=bool(parsed.get("is_sensitive", True)),  # 解析不出默认 sensitive
            reason=f"LLM 判断：{parsed.get('reason', '未指明')}",
            matched_layer="llm",
        )

    except Exception as e:
        # 异常 → 保守判定 sensitive
        # 这里既不打日志（避免污染 stdout），也不抛 —— 因为分类器失败不应该让整图崩
        return SensitivityResult(
            is_sensitive=True,
            reason=f"LLM 分类器异常，保守触发审批（{type(e).__name__}）",
            matched_layer="default",
        )


# ============================================================================
# 主入口：classify_sensitivity
# ============================================================================
def classify_sensitivity(report: str) -> SensitivityResult:
    """
    判断报告是否敏感，决定要不要触发 interrupt。

    分两层：
      1. 关键词规则（命中即返回）
      2. LLM 兜底

    Returns:
        SensitivityResult（不会返回 None，永远有结论）
    """
    if not report or not report.strip():
        return SensitivityResult(
            is_sensitive=False,
            reason="报告为空",
            matched_layer="none",
        )

    # Layer 1
    keyword_result = _classify_by_keywords(report)
    if keyword_result is not None:
        return keyword_result

    # Layer 2
    return _classify_by_llm(report)


# ============================================================================
# 开发自检
# ============================================================================
if __name__ == "__main__":
    test_reports = [
        # ---- 应该判定敏感 ----
        ("含投资建议（关键词）", "基于分析，建议投资 health_beauty 品类，预计 ROI 高于 15%。"),
        ("含具体金额（关键词）", "建议投入 500 万 BRL 到 SP 州的物流升级。"),
        ("含绝对化措辞（关键词）", "强烈建议立即裁撤排名最末的 5 个品类。"),
        # ---- 应该判定非敏感 ----
        ("纯趋势描述", "2017 年营收 YoY 增长 23%，11 月达到峰值 116 万 BRL。"),
        ("中性洞察", "配送延迟和评分负相关，相关系数 -0.41。"),
        ("数据汇总", "Top 5 品类是 health_beauty / sports_leisure / bed_bath / ..."),
        # ---- 边界情况（看 LLM 怎么判）----
        ("隐式建议", "health_beauty 表现优于其他品类，投资回报最高。"),
    ]

    for name, report in test_reports:
        result = classify_sensitivity(report)
        marker = "🚨 SENSITIVE" if result.is_sensitive else "✓ safe"
        print(f"[{result.matched_layer:>8}] {marker:>14}  {name}")
        print(f"           reason: {result.reason}")
        print()
