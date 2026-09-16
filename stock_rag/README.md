# Stock RAG

这是一个本地轻量级 RAG 示例，用于对 stock_rule.txt 等业务文档做知识检索。

## 目录结构
- rag_docs/: 文档目录
- vector_db/: 向量检索数据库
- build_rag.py: 构建索引
- query_rag.py: 查询入口
- vector_store.py: 向量库实现
- rag_service.py: 检索与问答封装

## 使用方式
1. 先构建索引：
   python build_rag.py

2. 查询：
   python query_rag.py "常规安全库存阈值是什么"

## 说明
- 这是一个 local RAG，基于 TF-IDF 向量检索，适用于小规模本地知识库。
- 适合在不依赖大模型服务的情况下，对私有规则文档进行快速检索。
