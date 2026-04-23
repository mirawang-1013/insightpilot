"""
config.py —— 全项目配置的单一真相源（Single Source of Truth）

【职责】
    1. 从 .env 文件和环境变量加载配置
    2. 做类型校验和转换（str → int / Path / bool）
    3. 必填项缺失时在启动阶段就报错（fail fast）
    4. 提供 get_settings() 单例访问入口

【为什么用 pydantic-settings 而不是直接 os.getenv？】
    - 类型安全：string/int/Path 自动转换
    - 必填校验：缺失必填项启动时就炸，不用等跑到第 500 行
    - 默认值：声明处即可读
    - 测试友好：可以用 fixture 替换整个 Settings

【使用方式】
    from insight_pilot.config import get_settings
    settings = get_settings()
    con = duckdb.connect(str(settings.duckdb_abs_path))
"""

from __future__ import annotations

from functools import lru_cache             # 单例缓存装饰器
from pathlib import Path                    # 路径对象
from typing import Literal                  # 限定枚举类字符串值

from pydantic import Field, computed_field  # Field 用于字段级配置，computed_field 做派生属性
from pydantic_settings import BaseSettings, SettingsConfigDict


# ============================================================================
# 常量：项目根路径的自动定位
#
# 【为什么不让用户在 .env 里配 PROJECT_ROOT？】
#   config.py 的物理位置是确定的：src/insight_pilot/config.py
#   那么项目根就是它的父父父级。自动推导比让用户配置更可靠。
#
# 路径推导：
#   __file__                                = .../src/insight_pilot/config.py
#   Path(__file__).resolve()                = 绝对路径
#   .parent                                  = .../src/insight_pilot/
#   .parent.parent                           = .../src/
#   .parent.parent.parent                    = .../  ← 项目根
# ============================================================================
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


# ============================================================================
# Settings 主类
#
# 继承 BaseSettings 自动获得：
#   - 从 .env 读取
#   - 从环境变量读取（优先级高于 .env）
#   - 字段类型校验
#   - __repr__ 隐藏密钥（防止日志打印泄露）
# ============================================================================
class Settings(BaseSettings):
    """
    全项目配置。字段定义即文档，无需额外注释（pydantic 自动生成 schema）。

    加载优先级（从高到低）：
      1. 直接传给 Settings(...) 的参数（测试用）
      2. 环境变量（export OPENAI_API_KEY=...）
      3. .env 文件
      4. 字段默认值
    """

    # -----------------------------------------------------------------------
    # model_config —— pydantic v2 的配置入口
    # pydantic v1 是 class Config: 内部类，v2 改成这个类属性
    # -----------------------------------------------------------------------
    model_config = SettingsConfigDict(
        # env_file：要读哪个 .env 文件
        # 传绝对路径，避免受"当前工作目录"影响
        env_file=_PROJECT_ROOT / ".env",
        # env_file_encoding：.env 文件编码
        env_file_encoding="utf-8",
        # case_sensitive=False：OPENAI_API_KEY / openai_api_key 都能匹配
        # 大多数项目的 .env 惯例是全大写，代码里 Python 风格是小写，这个设置让两者兼容
        case_sensitive=False,
        # extra="ignore"：.env 里有额外字段不报错（比如 KAGGLE_USERNAME 只被 setup 脚本用）
        extra="ignore",
    )

    # =======================================================================
    # OpenAI 相关
    # =======================================================================
    # Field(..., description="...")：... 是"必填哨兵"，表示无默认值 → 必填
    # 如果 .env 和环境变量都没设，Settings() 初始化时会抛 ValidationError
    openai_api_key: str = Field(
        ...,
        description="OpenAI API key。从 https://platform.openai.com/api-keys 获取。",
    )

    # Literal["..."]：限定只能是这几个值之一
    # 如果用户在 .env 里写 OPENAI_MODEL=gpt-5（还不存在），启动时会报错
    openai_model: Literal["gpt-4o", "gpt-4o-mini", "gpt-4-turbo"] = Field(
        default="gpt-4o",
        description="使用的 OpenAI 模型。预算有限可用 gpt-4o-mini。",
    )

    # =======================================================================
    # 数据层
    # =======================================================================
    # 类型是 Path：pydantic 自动把 .env 里的字符串转成 Path 对象
    # 默认值给相对路径，后面用 duckdb_abs_path 属性做绝对化
    duckdb_path: Path = Field(
        default=Path("data/warehouse.duckdb"),
        description="DuckDB 数据库文件路径（相对项目根）。",
    )

    # ge=1, le=10000：gt/ge/lt/le 是数字约束（greater-equal / less-equal）
    # 500 是合理默认，10000 是天花板（防止人为配太大爆内存）
    max_sql_rows: int = Field(
        default=500,
        ge=1,
        le=10000,
        description="SQL 查询结果行数上限。",
    )

    sql_timeout: int = Field(
        default=30,
        ge=1,
        le=300,
        description="SQL 执行超时（秒）。",
    )

    # =======================================================================
    # Python 沙盒（第三阶段用）
    # =======================================================================
    python_sandbox_timeout: int = Field(
        default=60,
        ge=1,
        le=600,
        description="Python 代码沙盒执行超时（秒）。",
    )

    # =======================================================================
    # LangGraph 循环保护
    # =======================================================================
    max_iterations: int = Field(
        default=20,
        ge=1,
        le=100,
        description=(
            "单次图执行的最大循环次数。"
            "防止 ReAct Agent 陷入死循环。"
            "20 的经验值：一个 5 步的 plan，每步平均 3-4 次 ReAct 迭代，留点余量。"
        ),
    )

    # =======================================================================
    # 日志
    # =======================================================================
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(
        default="INFO",
        description="日志级别。",
    )

    # =======================================================================
    # Computed fields —— 派生属性，不从 .env 读
    # 用 @computed_field 让它像普通字段一样出现在 .model_dump() 里
    # =======================================================================

    @computed_field  # type: ignore[prop-decorator]   mypy 对装饰属性的警告，忽略
    @property
    def project_root(self) -> Path:
        """项目根目录（config.py 向上三级）。只读。"""
        return _PROJECT_ROOT

    @computed_field  # type: ignore[prop-decorator]
    @property
    def duckdb_abs_path(self) -> Path:
        """
        DuckDB 文件的绝对路径。

        【为什么要这个属性？】
          .env 里写的是相对路径 data/warehouse.duckdb，
          但代码需要绝对路径（否则 cwd 变化时会找不到文件）。
          把"相对 → 绝对"的转换收口到这里，调用方永远用绝对路径。
        """
        if self.duckdb_path.is_absolute():
            # 用户如果配了绝对路径，尊重
            return self.duckdb_path
        return self.project_root / self.duckdb_path

    @computed_field  # type: ignore[prop-decorator]
    @property
    def data_dir(self) -> Path:
        """项目数据目录：<project_root>/data。"""
        return self.project_root / "data"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def outputs_dir(self) -> Path:
        """生成的报告和图表目录：<project_root>/outputs。"""
        return self.project_root / "outputs"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def knowledge_base_dir(self) -> Path:
        """知识库文档目录：<project_root>/data/knowledge_base。"""
        return self.data_dir / "knowledge_base"


# ============================================================================
# 单例访问器：get_settings()
#
# 【为什么用 @lru_cache 而不是全局变量？】
#   @lru_cache(maxsize=None) 等价于"永久缓存第一次调用的返回值"，
#   比手写 global _settings 更简洁、更线程安全（lru_cache 是线程安全的）。
#
#   效果：第一次调用 get_settings() 时构造 Settings 并校验 .env；
#         之后任意次数调用都返回同一个对象，零开销。
# ============================================================================
@lru_cache(maxsize=None)
def get_settings() -> Settings:
    """
    获取全局 Settings 单例。

    调用方式：
        from insight_pilot.config import get_settings
        settings = get_settings()

    测试替换方式：
        get_settings.cache_clear()              # 清缓存
        # 然后用 monkeypatch 改环境变量，下次调用会重新加载
    """
    return Settings()  # type: ignore[call-arg]
    # ^^ type: ignore 是因为 pydantic 动态解析 Field(...) 为必填，
    #    mypy 看不出来，会以为我们漏了参数。运行时完全正确。


# ============================================================================
# 便利函数：开发时快速检查配置
#
# 用法：
#   python -m insight_pilot.config
#
# 会打印当前加载到的所有配置（密钥字段会被 pydantic 自动遮蔽）。
# 部署新环境时用来确认 .env 是否正确加载。
# ============================================================================
if __name__ == "__main__":
    import json

    settings = get_settings()
    # model_dump() 导出成 dict；mode="json" 让 Path 等复杂类型变字符串
    # exclude={"openai_api_key"} 显式排除密钥，避免意外打印
    safe_dump = settings.model_dump(mode="json", exclude={"openai_api_key"})
    # 单独显示密钥是否设置（不显示内容）
    safe_dump["openai_api_key"] = "***SET***" if settings.openai_api_key else "***MISSING***"

    print("当前加载的配置：")
    print(json.dumps(safe_dump, indent=2, ensure_ascii=False))
