# 本次索引变更的恢复说明

本次只增加 medical_fulltext_v1 全文索引，未修改节点、关系、原文和聊天记录。
恢复此前状态：在明确连接本次 plan.json 的目标数据库后，仅执行 `DROP INDEX medical_fulltext_v1 IF EXISTS`。重新建立使用本目录计划和正式管理程序。
schema_before.json 和 graph_before.json 记录变更前状态；此目录不是数据库全量备份。
