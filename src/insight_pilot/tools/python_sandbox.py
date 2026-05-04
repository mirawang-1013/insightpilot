"""
tools/python_sandbox.py —— Python 代码 subprocess 沙盒（Analysis Agent 的执行后端）

【职责】
    接受 LLM 生成的 Python 代码字符串，在隔离的子进程里执行，
    返回 AnalysisResult（stdout / chart_paths / dataframe_summary）。

【安全模型】
    Phase 3 用 subprocess + timeout：进程级隔离，足以防止：
      - 代码崩溃影响主进程
      - 无限循环卡死 Agent
      - 简单的命名空间污染

    生产级隔离需要 Docker / E2B（见 docs/design-decisions.md §5）。
    本文件抽象出 CodeExecutor Protocol，未来切换实现只需新加一个类。

【数据通道】
    上一步 SQL 的结果通过 JSON 文件传递给沙盒：
      主进程：把 query_results 序列化到 outputs/_step_<id>_input.json
      沙盒：通过 INPUT_DATA_PATH 环境变量找到文件并加载

【输出捕获】
    - stdout 通过 subprocess.PIPE 捕获
    - 图表通过"扫描 outputs/ 目录新增文件"获取
    - DataFrame 摘要：让 LLM 主动 print(df.describe()) 自然就到 stdout
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from insight_pilot.config import get_settings
from insight_pilot.state import AnalysisResult


# ============================================================================
# Sandbox 输入：把 SQL 结果序列化给沙盒
#
# 【为什么要这个 dataclass？】
#   把"塞进沙盒的数据"和"沙盒输出的结果"分开类型，更清晰。
#   未来扩展（比如再注入历史分析结果）只改这一个类。
# ============================================================================
@dataclass
class SandboxInput:
    """传递给沙盒的输入数据。"""

    code: str                       # LLM 写的 Python 代码
    step_id: int                    # 对应 ExecutionStep.step_id（用于命名图表/输入文件）
    query_results: list[dict]       # 上一步 SQL 结果（QueryResult.to_dict() 列表）


# ============================================================================
# 沙盒前导脚本（Boilerplate）
#
# 【这段代码的作用】
#   每次跑沙盒前，把这段"前导"和 LLM 代码拼到一起。
#   前导负责：
#     - 通用 import（pandas / matplotlib）
#     - matplotlib 设成无 GUI 模式（Agg backend）
#     - 加载 SQL 结果作为 query_results 变量
#     - 准备 outputs 目录
#
#   LLM 看到的提示是"你可以直接用 query_results 这个变量"。
#   LLM 不用每次写 import，降低出错率。
#
# 【为什么用 textwrap.dedent？】
#   字符串里的代码有缩进，dedent 把首层缩进去掉，避免 Python 语法错。
# ============================================================================
SANDBOX_PRELUDE = textwrap.dedent("""
    import json
    import os
    import sys
    import warnings

    # ---- 静音常见无害警告，避免污染 stdout ----
    warnings.filterwarnings("ignore", category=DeprecationWarning)
    warnings.filterwarnings("ignore", category=FutureWarning)

    # ---- pandas / matplotlib 默认设置 ----
    import pandas as pd
    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 200)

    import matplotlib
    matplotlib.use("Agg")  # 无 GUI（subprocess 里没 X server / Quartz）
    import matplotlib.pyplot as plt
    plt.rcParams["figure.figsize"] = (10, 6)
    plt.rcParams["figure.dpi"] = 100

    # ---- 加载上一步 SQL 结果 ----
    # query_results 形如 [
    #     {"sql": "...", "columns": [...], "rows": [{"col": val, ...}], ...},
    #     ...
    # ]
    # LLM 通常会用 pd.DataFrame(query_results[0]["rows"]) 拿到 DataFrame
    _input_path = os.environ.get("INPUT_DATA_PATH")
    if _input_path and os.path.exists(_input_path):
        with open(_input_path, "r", encoding="utf-8") as _f:
            query_results = json.load(_f)
    else:
        query_results = []

    # ---- 提供便捷函数：把第 i 个 query 结果转成 DataFrame ----
    def get_df(index: int = 0):
        \"\"\"把 query_results[index] 转成 DataFrame。\"\"\"
        if index >= len(query_results):
            raise IndexError(f"query_results 只有 {len(query_results)} 条，访问 [{index}] 越界")
        return pd.DataFrame(query_results[index]["rows"])

    # ---- step_id 让 LLM 知道当前步骤号（命名图表用）----
    step_id = int(os.environ.get("STEP_ID", "0"))

    # =========== LLM 代码开始 ===========
""").lstrip()


# ============================================================================
# 主函数：execute_python
# ============================================================================
def execute_python(sandbox_input: SandboxInput) -> AnalysisResult:
    """
    在 subprocess 沙盒里执行 LLM 生成的 Python 代码。

    Args:
        sandbox_input: 含 code + step_id + query_results 的输入。

    Returns:
        AnalysisResult，含 stdout / chart_paths / 错误信息。
    """
    settings = get_settings()
    timeout = settings.python_sandbox_timeout
    outputs_dir = settings.outputs_dir
    outputs_dir.mkdir(exist_ok=True)

    # ---- 1. 把 SQL 结果写到 JSON 文件 ----
    # 用 uuid 防止并发时文件名碰撞（LangGraph 可能并发跑节点）
    run_id = uuid.uuid4().hex[:8]
    input_path = outputs_dir / f"_step_{sandbox_input.step_id}_input_{run_id}.json"
    with input_path.open("w", encoding="utf-8") as f:
        json.dump(sandbox_input.query_results, f, ensure_ascii=False, default=str)
        # default=str：处理 datetime 等非默认 JSON 类型，转成字符串

    # ---- 2. 拼接完整脚本 ----
    full_script = SANDBOX_PRELUDE + sandbox_input.code

    # ---- 3. 记录沙盒运行前的 outputs/ 文件清单（用来识别"新生成的图表"）----
    # 用 set 做差集，只保留新增的 PNG / JPG
    files_before = _list_image_files(outputs_dir)

    # ---- 4. 启动 subprocess ----
    t0 = time.perf_counter()
    try:
        result = subprocess.run(
            # sys.executable 是当前 Python 解释器（保证版本一致）
            [sys.executable, "-c", full_script],
            # 工作目录：outputs/，让 LLM 写图直接到这里
            cwd=str(outputs_dir),
            # 环境变量：传入 INPUT_DATA_PATH 和 STEP_ID
            env={
                **os.environ,                              # 继承父进程环境（PATH 等）
                "INPUT_DATA_PATH": str(input_path),
                "STEP_ID": str(sandbox_input.step_id),
                # PYTHONPATH 让子进程能 import 项目内的包（如果代码需要）
                "PYTHONPATH": str(settings.project_root / "src"),
                # MPLBACKEND 双保险：环境变量级别也设定 Agg
                "MPLBACKEND": "Agg",
            },
            # 捕获 stdout / stderr
            capture_output=True,
            text=True,                # 用文本模式（自动 decode utf-8）
            timeout=timeout,
            # check=False：不抛异常，我们自己处理错误
            check=False,
        )
        execution_ms = int((time.perf_counter() - t0) * 1000)

    except subprocess.TimeoutExpired as e:
        # 超时：进程已经被 SIGKILL，stdout/stderr 部分可能可用
        execution_ms = int((time.perf_counter() - t0) * 1000)
        return AnalysisResult(
            step_id=sandbox_input.step_id,
            success=False,
            code=sandbox_input.code,
            stdout=(e.stdout or "") if isinstance(e.stdout, str) else "",
            error=(
                f"代码执行超时（{timeout} 秒）。"
                f"修复建议：检查是否有死循环；如果数据量大，考虑用 sample 或聚合后再处理。"
            ),
            execution_ms=execution_ms,
        )

    finally:
        # 不管成败都清理 input 文件（保留太多碎片会让 outputs/ 难看）
        # 注意：图表文件保留，那是真正的产出
        try:
            input_path.unlink()
        except FileNotFoundError:
            pass

    # ---- 5. 处理执行结果 ----
    # subprocess 没抛异常，但 returncode 可能非 0（代码内 raise 了）
    if result.returncode != 0:
        return AnalysisResult(
            step_id=sandbox_input.step_id,
            success=False,
            code=sandbox_input.code,
            stdout=result.stdout,
            error=_format_python_error(result.stderr),
            execution_ms=execution_ms,
        )

    # ---- 6. 找出新生成的图表文件 ----
    files_after = _list_image_files(outputs_dir)
    new_charts = sorted(files_after - files_before)

    return AnalysisResult(
        step_id=sandbox_input.step_id,
        success=True,
        code=sandbox_input.code,
        stdout=result.stdout,
        # 转相对路径（相对项目根），方便 README / 报告引用
        chart_paths=[str(Path(c).relative_to(settings.project_root)) for c in new_charts],
        execution_ms=execution_ms,
    )


# ============================================================================
# 辅助：扫描 outputs/ 找图表文件
# ============================================================================
def _list_image_files(directory: Path) -> set[str]:
    """返回 directory 里所有图片文件的绝对路径集合。"""
    extensions = {".png", ".jpg", ".jpeg", ".svg", ".pdf"}
    return {
        str(p.absolute())
        for p in directory.iterdir()
        if p.is_file() and p.suffix.lower() in extensions
    }


# ============================================================================
# 辅助：把 Python traceback 整理成 LLM 友好的错误
#
# 【设计理念跟 duckdb_executor._format_error 一致】
#   不让 LLM 看完整 traceback（信息密度低），
#   提取最后一行的异常类型 + 消息，加上修复建议。
# ============================================================================
def _format_python_error(stderr: str) -> str:
    """从 stderr 里提取关键错误信息，附修复建议。"""
    if not stderr:
        return "Python 代码执行失败（无 stderr 输出）。"

    # traceback 最后一行通常是 'ExceptionType: message'
    last_line = stderr.strip().split("\n")[-1]

    # 常见错误的修复建议
    if "NameError" in last_line:
        return (
            f"{last_line}\n"
            f"修复建议：变量未定义。检查变量名拼写；"
            f"注意 query_results、get_df()、step_id 是已注入的可用变量。"
        )

    if "KeyError" in last_line:
        return (
            f"{last_line}\n"
            f"修复建议：字典键不存在。如果是访问 query_results[i]['rows'] 里的字段，"
            f"先 print(query_results[i]['columns']) 看实际字段名。"
        )

    if "IndexError" in last_line and "query_results" in stderr:
        return (
            f"{last_line}\n"
            f"修复建议：query_results 索引越界。"
            f"先 print(len(query_results)) 看有几条数据。"
        )

    if "AttributeError" in last_line:
        return (
            f"{last_line}\n"
            f"修复建议：检查对象是否是预期类型。常见情况：把 list 当 DataFrame 用，"
            f"应该先 pd.DataFrame(query_results[0]['rows']) 转换。"
        )

    if "ValueError" in last_line and "could not convert" in stderr:
        return (
            f"{last_line}\n"
            f"修复建议：类型转换失败。检查字段是否含 None / 空字符串；"
            f"必要时用 pd.to_numeric(x, errors='coerce')。"
        )

    # 默认情况：返回最后一行 + 完整 stderr 后 5 行（traceback 上下文）
    tail = "\n".join(stderr.strip().split("\n")[-5:])
    return f"{last_line}\n详细：\n{tail}"


# ============================================================================
# 开发自检
#
# 用法：
#   uv run python -m insight_pilot.tools.python_sandbox
# ============================================================================
if __name__ == "__main__":
    # 模拟一个上一步 SQL 结果
    fake_query_result = {
        "sql": "SELECT month, revenue FROM ...",
        "columns": ["month", "revenue"],
        "rows": [
            {"month": "2017-01", "revenue": 130510},
            {"month": "2017-02", "revenue": 275562},
            {"month": "2017-03", "revenue": 418978},
            {"month": "2017-04", "revenue": 397419},
            {"month": "2017-05", "revenue": 576106},
        ],
    }

    test_code = textwrap.dedent("""
        # LLM 会写这种风格的代码
        df = get_df(0)
        print("形状:", df.shape)
        print(df.head())
        print()
        print("总营收:", df["revenue"].sum())

        # 画图
        plt.figure()
        plt.plot(df["month"], df["revenue"], marker="o")
        plt.title("Monthly Revenue")
        plt.xticks(rotation=45)
        plt.tight_layout()
        plt.savefig(f"chart_{step_id}_revenue.png")
        plt.close()
        print(f"chart saved")
    """)

    sandbox_input = SandboxInput(
        code=test_code,
        step_id=99,
        query_results=[fake_query_result],
    )

    print("执行沙盒测试...\n")
    result = execute_python(sandbox_input)

    print(f"成功: {result.success}")
    print(f"耗时: {result.execution_ms}ms")
    print(f"\nstdout:\n{result.stdout}")
    if result.chart_paths:
        print(f"\n图表: {result.chart_paths}")
    if result.error:
        print(f"\n错误: {result.error}")
