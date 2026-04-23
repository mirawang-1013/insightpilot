# ============================================================================
# Makefile —— 项目统一入口
#
# 【为什么用 Makefile？】
#   面试演示时，一句 `make demo` 比让对方输一串长命令优雅得多。
#   Makefile 也起到"自文档化"作用：`make help` 就能看到所有可用操作。
#
# 【.PHONY 是干嘛的？】
#   Make 原本是给 C 项目用的，默认假设 target 对应文件。`.PHONY` 告诉 Make
#   "这些 target 不是文件，每次都要执行"，否则 Make 看到同名的目录/文件
#   会认为"目标已存在，不用跑"。
# ============================================================================

.PHONY: help install setup demo test lint format clean

# 默认 target：不带参数跑 `make` 时显示帮助
help:
	@echo "InsightPilot —— 可用命令："
	@echo "  make install    初始化环境：用 uv 创建 .venv 并装依赖"
	@echo "  make setup      下载 Olist 数据集 + 构建 DuckDB 仓库"
	@echo "  make demo       运行主演示场景"
	@echo "  make test       运行所有单元测试"
	@echo "  make lint       用 ruff 做 lint 检查"
	@echo "  make format     用 ruff 自动格式化代码"
	@echo "  make clean      清理缓存、编译产物、生成报告"

# ---- 环境初始化 ----
# uv sync：创建 .venv + 按 pyproject.toml 装依赖
#   --extra data：装 [project.optional-dependencies].data（kagglehub）
#   --group dev：装 [dependency-groups].dev（PEP 735 新标准：pytest/ruff/mypy 等）
install:
	uv sync --extra data --group dev

# ---- 数据准备 ----
# 只有第一次或换机器时需要跑。幂等：已有数据则跳过下载。
setup:
	uv run python scripts/setup_data.py

# ---- 演示 ----
# 跑主 CLI，默认执行场景 1（营收趋势）
demo:
	uv run insight-pilot demo

# ---- 测试 ----
# -m "not slow"：默认跳过慢测试；CI 里用 `make test-all`
test:
	uv run pytest -m "not slow"

test-all:
	uv run pytest

# ---- 代码风格 ----
lint:
	uv run ruff check src/ tests/ scripts/
	uv run ruff format --check src/ tests/ scripts/

format:
	uv run ruff check --fix src/ tests/ scripts/
	uv run ruff format src/ tests/ scripts/

# ---- 清理 ----
# 注意不删 .venv 和 data/warehouse.duckdb —— 那些是重建代价高的资源
clean:
	rm -rf .pytest_cache .ruff_cache .mypy_cache .coverage htmlcov
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	rm -rf outputs/*.md outputs/*.png
