"""
tools/exemplar_store.py —— 历史成功查询存储与检索（Few-shot Exemplar Retrieval）

【职责】
    1. 把成功执行（且经审批）的查询沉淀成 exemplar
    2. 新查询进来时，按语义相似度检索 Top-K 历史 exemplar
    3. 把这些 exemplar 作为 few-shot 注入 Planner prompt，提升下次准确率

【设计原则】
    - 只存高质量样本（Reviewer approve 才存，驳回的不存）
    - 用独立 ChromaDB collection（不污染业务知识库）
    - 失败 silent fallback（不让存储/检索异常拖垮主流程）

【数据结构】
    每条 exemplar 含：
      - user_question     原始 NL 问题（embed 用）
      - execution_plan    Planner 输出的步骤（拆解参考）
      - sqls              所有成功 SQL 列表（SQL pattern 参考）
      - timestamp         创建时间（便于"最近优先"过滤）
      - approved_by_reviewer  是否经审批

【自我改善飞轮】
    用户问 → 跑成功 → Reviewer 通过 → 存 exemplar
       ↑                                      │
       └────── 下次类似问题，检索 + 复用 ←────┘
"""

from __future__ import annotations

import json
import sys
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any

import chromadb

from insight_pilot.config import get_settings


# ============================================================================
# Collection 名称（独立于知识库）
# ============================================================================
EXEMPLAR_COLLECTION_NAME = "insight_pilot_exemplars"


# ============================================================================
# 数据类：Exemplar
# ============================================================================
@dataclass
class Exemplar:
    """一条历史成功查询的完整记录。"""

    user_question: str                          # 用户原问题
    execution_plan: list[dict[str, Any]]        # ExecutionStep.model_dump() 列表
    sqls: list[str]                             # 所有成功的 SQL
    timestamp: str                              # ISO 格式时间戳（创建时间）
    approved_by_reviewer: bool = False          # 是否经审批
    exemplar_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    # ─── 优化字段 1：过期机制 ───
    last_validated_at: str = ""                 # 上次重跑 SQL 验证还能跑通的时间。空 = 从未验证
    is_stale: bool = False                      # 标记为过期（如 schema 变更后 SQL 跑不通）

    # ─── 优化字段 2：质量评分 ───
    # 0-100 范围。检索时按"相似度 × quality_score"排序
    # 默认 50（中性）；approved 路径默认 75；upvote +5；downvote -10
    quality_score: int = 50

    # ─── 优化字段 3：多租户隔离 ───
    team_id: str | None = None                  # None = 单租户兼容；多租户场景下按 team 过滤

    def to_metadata(self) -> dict[str, str]:
        """
        ChromaDB metadata 字段必须是基础类型（str / int / float / bool）。
        把 list / dict 字段 JSON 序列化成字符串。
        """
        return {
            "exemplar_id": self.exemplar_id,
            "execution_plan_json": json.dumps(self.execution_plan, ensure_ascii=False),
            "sqls_json": json.dumps(self.sqls, ensure_ascii=False),
            "timestamp": self.timestamp,
            "approved_by_reviewer": str(self.approved_by_reviewer),
            "last_validated_at": self.last_validated_at,
            "is_stale": str(self.is_stale),
            "quality_score": str(self.quality_score),
            "team_id": self.team_id or "",   # ChromaDB metadata 不接受 None
        }

    @classmethod
    def from_metadata(cls, metadata: dict[str, Any], document: str) -> "Exemplar":
        """从 ChromaDB 查询结果反序列化回 Exemplar 对象。"""
        # team_id 反序列化：空字符串 → None
        team_id_raw = metadata.get("team_id", "")
        team_id = team_id_raw if team_id_raw else None

        return cls(
            user_question=document,
            execution_plan=json.loads(metadata.get("execution_plan_json", "[]")),
            sqls=json.loads(metadata.get("sqls_json", "[]")),
            timestamp=metadata.get("timestamp", ""),
            approved_by_reviewer=metadata.get("approved_by_reviewer", "False") == "True",
            exemplar_id=metadata.get("exemplar_id", ""),
            last_validated_at=metadata.get("last_validated_at", ""),
            is_stale=metadata.get("is_stale", "False") == "True",
            # int 字段在 metadata 里以 str 存，反序列化时转回 int；防御性默认 50
            quality_score=int(metadata.get("quality_score", "50") or "50"),
            team_id=team_id,
        )

    def to_prompt_block(self) -> str:
        """渲染成 LLM 友好的字符串（注入 prompt 用）。"""
        lines = [f"问题: {self.user_question}"]

        if self.execution_plan:
            lines.append("拆解:")
            for step in self.execution_plan:
                step_id = step.get("step_id", "?")
                step_type = step.get("step_type", "?")
                description = step.get("description", "")[:100]
                lines.append(f"  {step_id}. [{step_type}] {description}")

        if self.sqls:
            lines.append("SQL:")
            for sql in self.sqls[:2]:  # 最多展示 2 条 SQL，避免 prompt 爆
                # 单行化压缩 SQL，节省 token
                compact = " ".join(sql.split())[:300]
                lines.append(f"  {compact}")

        return "\n".join(lines)


# ============================================================================
# 工具：获取 ChromaDB collection（独立于知识库）
# ============================================================================
def _get_collection() -> chromadb.Collection:
    """打开（或创建）exemplar collection。"""
    settings = get_settings()
    chroma_dir = settings.knowledge_base_dir / "chroma"
    chroma_dir.mkdir(parents=True, exist_ok=True)

    client = chromadb.PersistentClient(path=str(chroma_dir))
    return client.get_or_create_collection(
        name=EXEMPLAR_COLLECTION_NAME,
        metadata={"version": "1.0", "purpose": "execution memory for InsightPilot"},
    )


# ============================================================================
# 核心 1：保存
# ============================================================================
def save_exemplar(
    user_question: str,
    execution_plan: list[dict[str, Any]] | list[Any],
    sqls: list[str],
    approved_by_reviewer: bool = False,
    team_id: str | None = None,
) -> str | None:
    """
    保存一条 exemplar 到向量库。

    Args:
        user_question: 用户原问题。
        execution_plan: ExecutionStep 列表（Pydantic 对象）或已经 dump 过的 dict 列表。
        sqls: 成功执行的 SQL 列表。
        approved_by_reviewer: 是否经人工审批（建议只存 True 的）。
        team_id: 可选，多租户隔离用。None = 单租户。

    Returns:
        exemplar_id（成功时）或 None（失败时静默吞掉）。

    【质量评分初始化逻辑】
      - 经审批通过 → 75 分（中等偏上）
      - 自动通过 → 50 分（中性）
      未来通过 upvote/downvote/validate 调整。
    """
    if not user_question.strip():
        return None
    if not sqls:
        return None

    try:
        plan_dicts = []
        for step in execution_plan:
            if hasattr(step, "model_dump"):
                plan_dicts.append(step.model_dump())
            elif isinstance(step, dict):
                plan_dicts.append(step)
            else:
                continue

        # 初始 quality_score：经审批的默认 75，未审批默认 50
        initial_score = 75 if approved_by_reviewer else 50

        exemplar = Exemplar(
            user_question=user_question.strip(),
            execution_plan=plan_dicts,
            sqls=sqls,
            timestamp=datetime.now().isoformat(),
            approved_by_reviewer=approved_by_reviewer,
            quality_score=initial_score,
            team_id=team_id,
            # last_validated_at 初始为空 —— 从未验证过
            # is_stale 初始为 False —— 新存的不可能 stale
        )

        collection = _get_collection()
        collection.add(
            ids=[exemplar.exemplar_id],
            documents=[exemplar.user_question],   # ChromaDB 用这个 embed
            metadatas=[exemplar.to_metadata()],
        )

        return exemplar.exemplar_id

    except Exception as e:
        # 不让 exemplar 保存失败拖垮主流程
        print(f"[WARN] save_exemplar failed: {e}", file=sys.stderr)
        return None


# ============================================================================
# 核心 2：检索
# ============================================================================
def retrieve_exemplars(
    query: str,
    top_k: int = 3,
    only_approved: bool = True,
    exclude_stale: bool = True,
    team_id: str | None = None,
    min_quality_score: int = 0,
) -> list[Exemplar]:
    """
    按相似度检索 Top-K 个历史 exemplar，含多种过滤选项。

    Args:
        query: 当前用户问题（用作语义查询）。
        top_k: 返回数量上限。
        only_approved: 只返回经审批的（推荐 True，保证质量）。
        exclude_stale: 排除标记为 stale 的（schema 变更后失效的）。
        team_id: 可选，多租户场景下只返回本 team 的 exemplar。None = 不过滤。
        min_quality_score: 最低质量分阈值（0-100）。默认 0（不过滤）。

    Returns:
        Exemplar 列表，按 "ChromaDB 相似度 × quality_score" 加权排序。
        失败 / 无结果时返回空 list。

    【排序逻辑】
      ChromaDB 的相似度本身已经是排序基础。
      在 only_approved + 过滤后的候选里，再按 quality_score 加权：
        final_rank = chroma_rank_position × (100 / quality_score)
      score 越高，加权越小（rank 数字越小代表越靠前）。
    """
    if not query.strip():
        return []

    try:
        collection = _get_collection()
        if collection.count() == 0:
            return []

        # 多取一些应对各种过滤后剩余不够
        n_search = top_k * 5
        results = collection.query(
            query_texts=[query],
            n_results=min(n_search, collection.count()),
        )

        documents = results.get("documents", [[]])[0]
        metadatas = results.get("metadatas", [[]])[0]

        # ---- 第一遍：过滤 ----
        candidates: list[tuple[int, Exemplar]] = []   # (chroma_rank, exemplar)
        for chroma_rank, (doc, meta) in enumerate(zip(documents, metadatas)):
            ex = Exemplar.from_metadata(meta, doc)

            # 过滤 1：是否需经审批
            if only_approved and not ex.approved_by_reviewer:
                continue
            # 过滤 2：是否排除 stale
            if exclude_stale and ex.is_stale:
                continue
            # 过滤 3：team 隔离
            if team_id is not None and ex.team_id != team_id:
                continue
            # 过滤 4：最低质量分
            if ex.quality_score < min_quality_score:
                continue

            candidates.append((chroma_rank, ex))

        if not candidates:
            return []

        # ---- 第二遍：按 quality_score 加权重排 ----
        # 公式：final_score = (chroma_rank + 1) × (100 / quality_score)
        # 排序 ascending（值小靠前）。
        #
        # 【为什么是 chroma_rank + 1，不是 chroma_rank？】
        #   如果用裸 chroma_rank，rank=0（最相似）会让 final_score=0，
        #   不管 quality 多差都成绝对赢家 —— 质量分被完全忽略。
        #   加 +1 偏置：rank=0 也会被 quality 影响，让两个维度真正参与排序。
        #
        # 【举例 quality 怎么影响排序】
        #   chroma_rank=0, q=100 → (0+1) × 1 = 1     ← 最高质量的语义最近样本
        #   chroma_rank=0, q=30  → (0+1) × 3.33 = 3.33 ← 同样最相似但质量差
        #   chroma_rank=2, q=100 → (2+1) × 1 = 3     ← 不那么相似但质量满分
        #   排序：q=100/rank=0 第一，q=100/rank=2 第二，q=30/rank=0 第三 ✓
        #
        # 【防御 max(quality_score, 1)】
        #   万一 quality 为 0（理论可能：极端 downvote 后），避免除零。
        def weighted_rank(item: tuple[int, Exemplar]) -> float:
            chroma_rank, ex = item
            quality_factor = 100 / max(ex.quality_score, 1)
            return (chroma_rank + 1) * quality_factor

        candidates.sort(key=weighted_rank)

        # 取 Top-K
        return [ex for _, ex in candidates[:top_k]]

    except Exception as e:
        print(f"[WARN] retrieve_exemplars failed: {e}", file=sys.stderr)
        return []


# ============================================================================
# 优化 API 1：更新单条 exemplar 的字段
#
# ChromaDB 没有"原地更新单条字段"的 API，需要 .update() 整体替换 metadata。
# 这个内部辅助函数封装了"加载 → 修改 → 写回"的流程。
# ============================================================================
def _update_exemplar_metadata(exemplar_id: str, **field_updates: Any) -> bool:
    """
    更新 exemplar 的部分字段。

    Args:
        exemplar_id: 要更新的 exemplar ID。
        **field_updates: 要更新的字段 → 新值。
            支持：last_validated_at, is_stale, quality_score, team_id

    Returns:
        True 成功；False 找不到或失败。
    """
    try:
        collection = _get_collection()
        result = collection.get(ids=[exemplar_id])

        documents = result.get("documents") or []
        metadatas = result.get("metadatas") or []

        if not documents:
            return False

        # 加载现有 exemplar
        ex = Exemplar.from_metadata(metadatas[0], documents[0])

        # 应用更新
        for field_name, new_value in field_updates.items():
            if hasattr(ex, field_name):
                setattr(ex, field_name, new_value)

        # 写回
        collection.update(
            ids=[exemplar_id],
            metadatas=[ex.to_metadata()],
        )
        return True

    except Exception as e:
        print(f"[WARN] _update_exemplar_metadata failed: {e}", file=sys.stderr)
        return False


# ============================================================================
# 优化 API 2：验证 + 标记过期
# ============================================================================
def validate_exemplar(exemplar_id: str) -> bool:
    """
    重跑 exemplar 里的 SQL，验证是否还能跑通。
    跑不通则标 is_stale=True；跑通则更新 last_validated_at。

    Args:
        exemplar_id: 要验证的 exemplar ID。

    Returns:
        True = SQL 仍能跑通；False = 跑不通（已标 stale）或找不到。

    【使用场景】
      - 定时任务：每天验证 N 条最久未验证的 exemplar
      - schema 变更后：批量验证，标记失效的
      - retrieval 时按需触发（如 last_validated_at 超过 30 天）
    """
    # 懒导入 execute_sql 避免循环依赖
    from insight_pilot.tools.duckdb_executor import execute_sql

    try:
        collection = _get_collection()
        result = collection.get(ids=[exemplar_id])
        documents = result.get("documents") or []
        metadatas = result.get("metadatas") or []
        if not documents:
            return False

        ex = Exemplar.from_metadata(metadatas[0], documents[0])

        # 跑所有 SQL，全部成功才算 valid
        all_ok = True
        for sql in ex.sqls:
            qr = execute_sql(sql)
            if not qr.success:
                all_ok = False
                break

        if all_ok:
            # 更新验证时间，清除 stale 标记
            _update_exemplar_metadata(
                exemplar_id,
                last_validated_at=datetime.now().isoformat(),
                is_stale=False,
            )
            return True
        else:
            # 标记为 stale
            _update_exemplar_metadata(
                exemplar_id,
                is_stale=True,
                last_validated_at=datetime.now().isoformat(),
            )
            return False

    except Exception as e:
        print(f"[WARN] validate_exemplar failed: {e}", file=sys.stderr)
        return False


def mark_stale(exemplar_id: str) -> bool:
    """手动标记一条 exemplar 为 stale（业务变化触发等场景）。"""
    return _update_exemplar_metadata(exemplar_id, is_stale=True)


# ============================================================================
# 优化 API 3：用户反馈（upvote / downvote）
# ============================================================================
def upvote_exemplar(exemplar_id: str, increment: int = 5) -> bool:
    """
    用户点赞：quality_score +5（默认）。

    Args:
        exemplar_id: 要点赞的 exemplar ID。
        increment: 加分幅度，默认 5。

    Returns:
        True 成功；False 找不到或失败。
    """
    try:
        collection = _get_collection()
        result = collection.get(ids=[exemplar_id])
        metadatas = result.get("metadatas") or []
        if not metadatas:
            return False
        old_score = int(metadatas[0].get("quality_score", "50") or "50")
        new_score = min(old_score + increment, 100)   # 上限 100
        return _update_exemplar_metadata(exemplar_id, quality_score=new_score)
    except Exception as e:
        print(f"[WARN] upvote_exemplar failed: {e}", file=sys.stderr)
        return False


def downvote_exemplar(exemplar_id: str, decrement: int = 10) -> bool:
    """
    用户踩：quality_score -10（默认，比 upvote 力度大 —— 负反馈惩罚更重）。

    Args:
        exemplar_id: 要踩的 exemplar ID。
        decrement: 减分幅度，默认 10。

    Returns:
        True 成功；False 找不到或失败。
    """
    try:
        collection = _get_collection()
        result = collection.get(ids=[exemplar_id])
        metadatas = result.get("metadatas") or []
        if not metadatas:
            return False
        old_score = int(metadatas[0].get("quality_score", "50") or "50")
        new_score = max(old_score - decrement, 0)   # 下限 0
        return _update_exemplar_metadata(exemplar_id, quality_score=new_score)
    except Exception as e:
        print(f"[WARN] downvote_exemplar failed: {e}", file=sys.stderr)
        return False


# ============================================================================
# 优化 API 4：批量验证（管理工具）
# ============================================================================
def validate_all_stale_candidates(
    older_than_days: int = 30,
    limit: int = 50,
) -> dict[str, int]:
    """
    批量验证：找出"超过 N 天没验证"的 exemplar，逐个跑 SQL。

    适合作为定时任务（cron / GitHub Actions）每周跑一次。

    Args:
        older_than_days: 只验证超过这个天数的（默认 30 天）。
        limit: 一次最多处理多少条（默认 50，防止过载）。

    Returns:
        统计字典：{"checked": N, "valid": N, "stale": N}
    """
    try:
        from datetime import timedelta

        collection = _get_collection()
        if collection.count() == 0:
            return {"checked": 0, "valid": 0, "stale": 0}

        cutoff = (datetime.now() - timedelta(days=older_than_days)).isoformat()

        result = collection.get(limit=collection.count())
        documents = result.get("documents") or []
        metadatas = result.get("metadatas") or []
        ids = result.get("ids") or []

        stats = {"checked": 0, "valid": 0, "stale": 0}
        for ex_id, doc, meta in zip(ids, documents, metadatas):
            ex = Exemplar.from_metadata(meta, doc)
            # 跳过：① 已经 stale 的 ② 最近验证过的
            if ex.is_stale:
                continue
            if ex.last_validated_at and ex.last_validated_at >= cutoff:
                continue
            if stats["checked"] >= limit:
                break

            stats["checked"] += 1
            if validate_exemplar(ex_id):
                stats["valid"] += 1
            else:
                stats["stale"] += 1

        return stats

    except Exception as e:
        print(f"[WARN] validate_all_stale_candidates failed: {e}", file=sys.stderr)
        return {"checked": 0, "valid": 0, "stale": 0}


# ============================================================================
# 工具：把 exemplars 渲染成 prompt 友好的文本块
#
# 这一段会被注入到 Planner 的 SystemMessage 里，告诉 LLM
# "下面是你历史上做过的类似问题"。
# ============================================================================
def format_exemplars_for_prompt(exemplars: list[Exemplar]) -> str:
    """把 exemplar 列表渲染成 markdown 文本，准备塞 prompt。"""
    if not exemplars:
        return ""

    parts = ["# 历史相似查询参考（可作为拆解灵感，但要根据当前问题调整）"]
    for i, ex in enumerate(exemplars, start=1):
        parts.append(f"\n## 参考 [{i}]")
        parts.append(ex.to_prompt_block())

    return "\n".join(parts)


# ============================================================================
# 管理工具：清空 / 列表
# ============================================================================
def clear_all_exemplars() -> int:
    """清空所有 exemplar（开发 / 测试用）。返回清空的数量。"""
    try:
        settings = get_settings()
        chroma_dir = settings.knowledge_base_dir / "chroma"
        client = chromadb.PersistentClient(path=str(chroma_dir))
        # 拿到当前数量再删
        try:
            col = client.get_collection(EXEMPLAR_COLLECTION_NAME)
            count = col.count()
            client.delete_collection(EXEMPLAR_COLLECTION_NAME)
            return count
        except Exception:
            return 0
    except Exception as e:
        print(f"[WARN] clear_all_exemplars failed: {e}", file=sys.stderr)
        return 0


def list_all_exemplars(limit: int = 20) -> list[Exemplar]:
    """列出当前所有 exemplar（按时间倒序，最新在前）。开发用。"""
    try:
        collection = _get_collection()
        if collection.count() == 0:
            return []

        # ChromaDB 的 get() 返回所有数据
        result = collection.get(limit=limit)
        documents = result.get("documents") or []
        metadatas = result.get("metadatas") or []

        exemplars = [
            Exemplar.from_metadata(meta, doc)
            for doc, meta in zip(documents, metadatas)
        ]
        # 按时间倒序
        exemplars.sort(key=lambda e: e.timestamp, reverse=True)
        return exemplars

    except Exception as e:
        print(f"[WARN] list_all_exemplars failed: {e}", file=sys.stderr)
        return []


# ============================================================================
# 开发自检
# ============================================================================
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Exemplar store CLI")
    parser.add_argument(
        "command",
        choices=["save", "list", "search", "clear",
                 "validate", "validate-all", "upvote", "downvote"],
        help="操作类型",
    )
    parser.add_argument("--query", help="搜索 query（仅 search 用）")
    parser.add_argument("--id", help="exemplar id（validate / upvote / downvote 用）")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--days", type=int, default=30,
                        help="validate-all：只验证超过 N 天没验证的")
    args = parser.parse_args()

    if args.command == "save":
        # 存 3 条假的高质量 exemplar 测试用
        seeds = [
            (
                "2017 年月度营收趋势",
                [
                    {"step_id": 1, "step_type": "query",
                     "description": "取 2017 年每月总营收，按月升序"},
                    {"step_id": 2, "step_type": "analysis",
                     "description": "画折线图"},
                ],
                ["SELECT DATE_TRUNC('month', order_purchase_timestamp) AS month, "
                 "SUM(payment_total) AS revenue FROM orders_full "
                 "WHERE EXTRACT(year FROM order_purchase_timestamp) = 2017 "
                 "GROUP BY month ORDER BY month"],
            ),
            (
                "Top 5 营收品类",
                [{"step_id": 1, "step_type": "query",
                  "description": "用 order_items_enriched 视图按 category_en 聚合"}],
                ["SELECT category_en, SUM(price) AS revenue "
                 "FROM order_items_enriched WHERE category_en IS NOT NULL "
                 "GROUP BY category_en ORDER BY revenue DESC LIMIT 5"],
            ),
            (
                "SP 州的总订单数",
                [{"step_id": 1, "step_type": "query",
                  "description": "过滤 customer_state = 'SP' 计数"}],
                ["SELECT COUNT(*) FROM orders_full WHERE customer_state = 'SP' "
                 "AND order_status IN ('delivered', 'shipped')"],
            ),
        ]
        for q, plan, sqls in seeds:
            eid = save_exemplar(q, plan, sqls, approved_by_reviewer=True)
            print(f"已存 [{eid}]: {q}")

    elif args.command == "list":
        exemplars = list_all_exemplars(limit=args.limit)
        print(f"共 {len(exemplars)} 条 exemplar:")
        for e in exemplars:
            approved = "✓" if e.approved_by_reviewer else "✗"
            stale = "🪨STALE" if e.is_stale else ""
            print(
                f"  [{approved}] q={e.quality_score:>3} {stale:<8} "
                f"{e.timestamp[:19]} [{e.exemplar_id}] {e.user_question}"
            )

    elif args.command == "search":
        if not args.query:
            print("需要 --query 参数")
            sys.exit(1)
        results = retrieve_exemplars(args.query, top_k=3, only_approved=True)
        print(f"查询 '{args.query}'，找到 {len(results)} 个相似 exemplar:")
        print()
        print(format_exemplars_for_prompt(results))

    elif args.command == "clear":
        count = clear_all_exemplars()
        print(f"已清空 {count} 条 exemplar")

    elif args.command == "validate":
        if not args.id:
            print("需要 --id 参数")
            sys.exit(1)
        ok = validate_exemplar(args.id)
        print(f"{'✓ 仍可跑通' if ok else '✗ 已标 stale'}: {args.id}")

    elif args.command == "validate-all":
        stats = validate_all_stale_candidates(older_than_days=args.days, limit=args.limit)
        print(f"批量验证完成（>{args.days} 天未验证）：")
        print(f"  检查 {stats['checked']} 条")
        print(f"  仍有效 {stats['valid']} 条")
        print(f"  已标 stale {stats['stale']} 条")

    elif args.command == "upvote":
        if not args.id:
            print("需要 --id 参数")
            sys.exit(1)
        ok = upvote_exemplar(args.id)
        print(f"{'✓ 已点赞 +5' if ok else '✗ 失败'}: {args.id}")

    elif args.command == "downvote":
        if not args.id:
            print("需要 --id 参数")
            sys.exit(1)
        ok = downvote_exemplar(args.id)
        print(f"{'✓ 已踩 -10' if ok else '✗ 失败'}: {args.id}")
