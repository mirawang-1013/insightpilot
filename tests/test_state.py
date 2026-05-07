"""
tests/test_state.py —— state.py 的单元测试

测什么：
  1. ExecutionStep 的 Pydantic 运行时校验
  2. QueryResult / AnalysisResult 的 to_llm_string 格式化
  3. create_initial_state 字段齐全（防呆设计的自动化验证）
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from insight_pilot.state import (
    AgentState,
    AnalysisResult,
    ExecutionStep,
    QueryResult,
    create_initial_state,
)


# ============================================================================
# ExecutionStep 的 Pydantic 校验
#
# 【为什么要测这些？】
#   ExecutionStep 是 LLM 通过 structured output 产出的，
#   LLM 可能产出烂数据（少字段、类型错、拼错 Literal）。
#   Pydantic 的作用是"坏数据在入口就炸"，这些测试验证这个机制工作。
# ============================================================================
class TestExecutionStep:
    """ExecutionStep 的运行时校验测试。"""

    def test_valid_step_constructs_successfully(self):
        """最基本的 happy path：合法字段能构造。"""
        step = ExecutionStep(
            step_id=1,
            step_type="query",
            description="取 2017 年每月的订单数和总营收",
        )
        assert step.step_id == 1
        assert step.step_type == "query"
        assert step.depends_on == []  # 默认值

    def test_rejects_step_id_zero(self):
        """step_id=0 应被拒（ge=1 约束）。"""
        # pytest.raises 的用法：被包起来的代码必须抛指定异常，否则测试失败
        with pytest.raises(ValidationError) as exc_info:
            ExecutionStep(
                step_id=0,
                step_type="query",
                description="这是一段合法长度的描述文字",
            )
        # exc_info.value 是抛出的异常对象；Pydantic 的错误消息里会包含约束名
        assert "greater than or equal to 1" in str(exc_info.value)

    def test_rejects_invalid_step_type(self):
        """step_type 不在 Literal 列表里应被拒。"""
        with pytest.raises(ValidationError):
            ExecutionStep(
                step_id=1,
                step_type="visualization",  # 不是 "query" 或 "analysis"
                description="这是一段合法长度的描述文字",
            )

    def test_rejects_short_description(self):
        """description 太短应被拒（min_length=10）。"""
        with pytest.raises(ValidationError):
            ExecutionStep(
                step_id=1,
                step_type="query",
                description="取数",  # 只有 2 字符
            )

    def test_accepts_depends_on(self):
        """depends_on 字段可选，能传。"""
        step = ExecutionStep(
            step_id=3,
            step_type="analysis",
            description="基于步骤 1 和 2 的结果画对比图",
            depends_on=[1, 2],
        )
        assert step.depends_on == [1, 2]


# ============================================================================
# QueryResult 的格式化
# ============================================================================
class TestQueryResult:
    """QueryResult 的 to_llm_string 测试。"""

    def test_success_format_shows_row_count_and_columns(self):
        """成功结果应显示行数、耗时、列名、样例。"""
        result = QueryResult(
            sql="SELECT * FROM orders",
            success=True,
            columns=["order_id", "total"],
            rows=[{"order_id": "o1", "total": 99.5}],
            row_count=1,
            execution_ms=15,
        )
        output = result.to_llm_string()
        assert "[SQL 执行成功]" in output
        assert "1 行" in output
        assert "15ms" in output
        assert "order_id" in output
        assert "total" in output

    def test_failure_format_shows_error(self):
        """失败结果只显示错误，不打印空的 columns/rows。"""
        result = QueryResult(
            sql="SELECT bad_column FROM orders",
            success=False,
            error="字段不存在：bad_column",
        )
        output = result.to_llm_string()
        assert "[SQL 执行失败]" in output
        assert "字段不存在" in output

    def test_truncated_marker_shown(self):
        """被截断时应显示 (已截断) 标记。"""
        result = QueryResult(
            sql="SELECT * FROM big",
            success=True,
            columns=["x"],
            rows=[{"x": i} for i in range(500)],
            row_count=500,
            truncated=True,
        )
        output = result.to_llm_string()
        assert "已截断" in output

    def test_preview_rows_limits_output(self):
        """to_llm_string(preview_rows=3) 只应显示前 3 行。"""
        result = QueryResult(
            sql="SELECT * FROM t",
            success=True,
            columns=["n"],
            rows=[{"n": i} for i in range(10)],
            row_count=10,
        )
        output = result.to_llm_string(preview_rows=3)
        # 应该提到"还有 7 行未显示"
        assert "还有 7 行" in output


# ============================================================================
# AnalysisResult 的格式化
# ============================================================================
class TestAnalysisResult:
    """AnalysisResult 的 to_llm_string 测试。"""

    def test_success_format_shows_stdout_and_charts(self):
        """成功结果应包含 stdout 和图表路径。"""
        result = AnalysisResult(
            step_id=2,
            success=True,
            code="print('hi')",
            stdout="hi\n",
            chart_paths=["outputs/chart_1.png"],
            execution_ms=120,
        )
        output = result.to_llm_string()
        assert "step_2" in output
        assert "hi" in output
        assert "chart_1.png" in output
        assert "120ms" in output

    def test_failure_format_shows_error(self):
        """失败时应显示错误和 step_id。"""
        result = AnalysisResult(
            step_id=3,
            success=False,
            error="NameError: 'df' is not defined",
        )
        output = result.to_llm_string()
        assert "执行失败" in output
        assert "step_3" in output
        assert "NameError" in output

    def test_long_stdout_truncated(self):
        """超长 stdout 应被截断，不让它把 LLM context 撑爆。"""
        long_output = "x" * 1000
        result = AnalysisResult(
            step_id=1,
            success=True,
            stdout=long_output,
        )
        output = result.to_llm_string()
        # 应出现"还有 N 字符"的提示
        assert "还有" in output
        assert "500" in output  # 500 是 to_llm_string 里的阈值


# ============================================================================
# create_initial_state —— 工厂函数的防呆验证
#
# 【这部分测试的核心价值】
#   验证"工厂函数填齐了所有 AgentState 字段"。
#   如果未来有人加了一个字段到 AgentState 但忘了在工厂里初始化，
#   这些测试会立刻发现。
# ============================================================================
class TestCreateInitialState:
    """create_initial_state 的合约测试。"""

    def test_fills_user_query(self, test_settings):
        """user_query 字段应正确传入。"""
        state = create_initial_state("测试问题")
        assert state["user_query"] == "测试问题"

    def test_fills_all_required_fields(self, test_settings):
        """AgentState 定义的所有字段都应该在返回的 state 里。"""
        state = create_initial_state("测试")

        # 预期字段清单 —— 这个清单必须和 AgentState 的字段保持一致
        # 如果未来加字段到 AgentState，这个列表也要更新，否则测试失败提醒
        expected_keys = {
            "user_query",
            "execution_plan",
            "current_step_index",
            "business_context",
            "retrieved_exemplars",
            "explored_schemas",
            "query_results",
            "analysis_results",
            "chart_paths",
            "report_markdown",
            "messages",
            "iteration_count",
            "max_iterations",
            "needs_human_review",
            "human_feedback",
            "status",
            "error",
        }
        # set 做对称差：找出"该有但没有"+"有但不该有"的字段
        missing = expected_keys - set(state.keys())
        extra = set(state.keys()) - expected_keys
        assert not missing, f"工厂函数漏了字段：{missing}"
        assert not extra, f"工厂函数多了不该有的字段：{extra}"

    def test_list_fields_are_empty_list_not_none(self, test_settings):
        """所有 list 类型字段必须是 []，不能是 None（否则 .append 会崩）。"""
        state = create_initial_state("测试")
        assert state["execution_plan"] == []
        assert state["explored_schemas"] == []
        assert state["query_results"] == []
        assert state["analysis_results"] == []
        assert state["chart_paths"] == []
        assert state["messages"] == []

    def test_initial_status_is_planning(self, test_settings):
        """初始 status 必须是 'planning'（Literal 合法值之一）。"""
        state = create_initial_state("测试")
        assert state["status"] == "planning"

    def test_respects_max_iterations_override(self, test_settings):
        """显式传 max_iterations 应被尊重。"""
        state = create_initial_state("测试", max_iterations=5)
        assert state["max_iterations"] == 5

    def test_max_iterations_defaults_from_settings(self, test_settings):
        """不传 max_iterations 时应从 settings 读。"""
        state = create_initial_state("测试")
        assert state["max_iterations"] == test_settings.max_iterations

    def test_human_feedback_starts_none(self, test_settings):
        """人机协同相关字段的初始值。"""
        state = create_initial_state("测试")
        assert state["needs_human_review"] is False
        assert state["human_feedback"] is None

    def test_error_starts_none(self, test_settings):
        """error 字段初始应是 None。"""
        state = create_initial_state("测试")
        assert state["error"] is None
