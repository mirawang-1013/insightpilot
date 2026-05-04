"""
tools/knowledge_base.py —— ChromaDB RAG 工具

【职责】
    1. 把 data/knowledge_base/*.md 切片索引到 ChromaDB 本地数据库
    2. 提供 retrieve_business_context(query) 函数，按相似度返回 Top-K 文档片段

【为什么用 RAG，不直接塞 system prompt？】
    见 docs/design-decisions.md §9。简版理由：
    - 业务术语库可能扩到 100+ 条，全塞 prompt 会爆 token + 注意力分散
    - RAG 只检索"和当前问题相关"的 3-5 条，token 省、注意力集中
    - 知识库改动不需要改代码

【切片粒度：按 ## 标题分段】
    每个 ## 段对应一个"知识原子"（一个 KPI 定义、一种分析模式）。
    切得太细（按句）→ 上下文丢失；太粗（整文件）→ 检索精度低。

【embedding 模型选择】
    ChromaDB 默认用 sentence-transformers/all-MiniLM-L6-v2：
    - 本地跑（离线友好）
    - 多语言（中英葡都行）
    - 模型 ~100MB，第一次自动下载到 ~/.cache
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import chromadb

from insight_pilot.config import get_settings


# ============================================================================
# Collection 名称
#
# ChromaDB 的 collection 类似关系数据库的 table，存"一组相关文档 + 它们的 embedding"。
# 整个项目用一个 collection 就够了。
# ============================================================================
COLLECTION_NAME = "insight_pilot_kb"


# ============================================================================
# 工具 1：把 .md 文件切成"段"（按 ## 标题）
#
# 切片策略：
#   - 跳过 # 一级标题（那是文件标题，不是知识点）
#   - 每个 ## 二级标题及其下面的内容打包成一段
#   - 整个 .md 文件如果没有 ## 标题，整个文件作为一段
#
# 返回的每段含：
#   - text:       内容
#   - title:      二级标题（用作显示）
#   - source:     来源文件名（用于追溯）
# ============================================================================
def _split_markdown(content: str, source: str) -> list[dict[str, str]]:
    """把 markdown 文本按 ## 二级标题切成段。"""
    # 用正则按 ## 切分（lookahead 保留分隔符）
    # ^##\s+ 表示行首的 ## 后跟空白
    # re.MULTILINE 让 ^ 匹配每行开头
    sections = re.split(r"^(?=##\s+)", content, flags=re.MULTILINE)

    chunks: list[dict[str, str]] = []
    for section in sections:
        section = section.strip()
        if not section:
            continue

        # 提取 section 的标题（## 后面那行）
        title_match = re.match(r"^##\s+(.+?)$", section, re.MULTILINE)
        if title_match:
            title = title_match.group(1).strip()
        else:
            # 没有 ## 标题的部分（通常是文件开头的引言）
            # 跳过 —— 不算"知识原子"
            continue

        chunks.append({
            "text": section,
            "title": title,
            "source": source,
        })

    return chunks


# ============================================================================
# 工具 2：构建 / 重建索引
#
# 【为什么提供 force 参数？】
#   修改 .md 文件后想让索引立即反映，需要重建。
#   不加 force 默认会：collection 已存在 → 直接复用（省时间）。
# ============================================================================
def build_index(force: bool = False) -> chromadb.Collection:
    """
    构建（或加载）知识库索引。

    Args:
        force: 强制重建。默认 False（已存在则复用）。

    Returns:
        ChromaDB Collection 实例，可直接 .query() 检索。
    """
    settings = get_settings()
    kb_dir = settings.knowledge_base_dir
    chroma_dir = kb_dir / "chroma"

    # ---- 准备 ChromaDB persistent client ----
    # PersistentClient 把数据存到磁盘（vs EphemeralClient 只在内存）
    # 关闭程序后下次启动还能用同一份索引
    chroma_dir.mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(path=str(chroma_dir))

    # ---- force 模式：先删旧的 collection ----
    if force:
        try:
            client.delete_collection(COLLECTION_NAME)
        except Exception:
            pass  # 可能 collection 不存在，无所谓

    # ---- 拿 collection（不存在则创建） ----
    # get_or_create 是幂等操作：存在就返回，不存在就建
    collection = client.get_or_create_collection(
        name=COLLECTION_NAME,
        # metadata 可以存 collection 级别的元信息，这里加一个版本号方便未来 migration
        metadata={"version": "1.0"},
    )

    # ---- 检查是否已经索引过 ----
    # collection.count() 返回当前已索引的文档数
    if collection.count() > 0 and not force:
        # 已经索引过，直接返回（最常见情况）
        return collection

    # ---- 第一次索引或 force 重建 ----
    # 扫描所有 .md 文件
    all_chunks: list[dict[str, str]] = []
    for md_file in sorted(kb_dir.glob("*.md")):
        content = md_file.read_text(encoding="utf-8")
        chunks = _split_markdown(content, source=md_file.name)
        all_chunks.extend(chunks)

    if not all_chunks:
        # 没有任何 .md 文件，返回空 collection（系统能跑，只是检索为空）
        return collection

    # ---- 批量加进 collection ----
    # ChromaDB 接收 documents（文本）+ metadatas（元数据）+ ids（唯一标识）
    # 自动调用 embedding 函数（默认 all-MiniLM-L6-v2）把文本转向量
    collection.add(
        ids=[f"chunk_{i}" for i in range(len(all_chunks))],
        documents=[c["text"] for c in all_chunks],
        metadatas=[{"title": c["title"], "source": c["source"]} for c in all_chunks],
    )

    return collection


# ============================================================================
# 工具 3：检索
#
# 给定一个用户问题，返回 Top-K 最相关的知识库片段，组合成一个字符串。
# ============================================================================
def retrieve_business_context(query: str, top_k: int = 5) -> str:
    """
    根据用户问题检索相关业务知识。

    Args:
        query: 用户的自然语言问题。
        top_k: 返回前几个最相关的片段。默认 5 —— Top 5 一般够覆盖一个问题。

    Returns:
        Markdown 格式字符串，含命中的知识片段。
        无命中或异常时返回空字符串（让上层降级到无 RAG 模式）。
    """
    try:
        collection = build_index(force=False)

        if collection.count() == 0:
            return ""

        # ---- 查询 ----
        # query_texts: 待查询的问题列表（支持批量，但我们一次查一条）
        # n_results: 返回 Top-K
        # ChromaDB 自动把 query 转成向量，做余弦相似度匹配
        results = collection.query(
            query_texts=[query],
            n_results=top_k,
        )

        # results 结构：{"documents": [[doc1, doc2, ...]], "metadatas": [[m1, m2, ...]], ...}
        # 外层 list 对应 query_texts（我们只传了一个，所以取 [0]）
        documents = results.get("documents", [[]])[0]
        metadatas = results.get("metadatas", [[]])[0]

        if not documents:
            return ""

        # ---- 组合成 LLM 友好的字符串 ----
        sections: list[str] = ["# 相关业务知识（自动检索）", ""]
        for i, (doc, meta) in enumerate(zip(documents, metadatas), start=1):
            title = meta.get("title", "未命名")
            source = meta.get("source", "?")
            sections.append(f"## [{i}] {title} _(来自 {source})_")
            sections.append(doc)
            sections.append("")

        return "\n".join(sections)

    except Exception as e:
        # RAG 是辅助功能，失败不应该让整个图崩
        # 返回空字符串，上层退化到无业务上下文模式
        # 但要在 stderr 打印警告，方便调试
        import sys
        print(f"[WARN] 知识库检索失败：{e}", file=sys.stderr)
        return ""


# ============================================================================
# 开发自检
#
# 用法：
#   uv run python -m insight_pilot.tools.knowledge_base                    # 全自检
#   uv run python -m insight_pilot.tools.knowledge_base "什么是 UV"         # 跑一个查询
#   uv run python -m insight_pilot.tools.knowledge_base --rebuild           # 强制重建索引
# ============================================================================
if __name__ == "__main__":
    import sys

    args = sys.argv[1:]

    if "--rebuild" in args:
        print("强制重建索引...")
        collection = build_index(force=True)
        print(f"完成。索引了 {collection.count()} 个知识片段。\n")
        args = [a for a in args if a != "--rebuild"]

    if args:
        query = " ".join(args)
        print(f"查询：{query}\n")
        print(retrieve_business_context(query))
    else:
        # 默认跑几个测试查询
        print("构建索引...")
        collection = build_index()
        print(f"索引了 {collection.count()} 个知识片段\n")

        test_queries = [
            "什么是 UV？怎么算？",
            "Top 5 品类怎么取？",
            "投资建议要看哪些维度？",
        ]
        for q in test_queries:
            print("=" * 72)
            print(f"查询：{q}")
            print("-" * 72)
            print(retrieve_business_context(q, top_k=3))
            print()
