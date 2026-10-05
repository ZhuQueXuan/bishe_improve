# 多步规划挑战集 v1

冻结日期：2026-10-04。8题，开发/保留各4题。用于受约束Planning与任务工作记忆的第一轮开发，不是通用Agent benchmark。

- `tasks.json`：任务输入、范围、预算、隔离故障/恢复场景。只给控制器这一文件的相应任务。
- `oracle.json`：实际图谱对象、关系来源、原文摘录和预期缺项。只供评分器/资料可用性检查，禁止发送给Planner。
- `manifest.json`：原始JSON、正式实体/关系、题集与协议的SHA256及固定划分。已冻结内容变更时创建新版本，不静默改答案。
- `protocol.schema.json`：Action/Observation/TaskState接口设计。没有接入正式聊天，也未安装新的JSON Schema依赖；本轮仅核验JSON语法及内部引用，不宣称运行时schema校验已实现。

## 范围

从指定疾病读并发原文关系，在工具按Unicode名称升序返回的最多8个对象中取前2个，不足2个则全部，读取描述/预防摘录和出处。不要求模型推断医学异同或用户患病。范围是现有工具可见集合，不声称找齐整个图谱的全部并发对象。

P01～P04正常；P05本地屏蔽一个预防字段；P06第一次资料调用本地返回一次临时错误；P07～P08关系读取后暂停，在隔离任务状态恢复。缺失/故障都是后续测试Harness注入，正式图谱资料仍存在。本轮只验证资料可用，尚未执行这些故障或恢复测试。

保留题使用新起点实体，但共享任务模板；题集很小，不能证明泛化或统计显著。固定规则可能全部通过，必须如实记录。

## 可复查命令

在bishe目录，用后端现有虚拟环境执行：

```powershell
.\knowledge_graph_backend\.venv\Scripts\python.exe .\scripts\check_planning_challenges.py
.\knowledge_graph_backend\.venv\Scripts\python.exe .\scripts\check_planning_challenges.py --read-only --report ..\temp\planning_challenges_new_run.json
```

默认仅离线核对哈希与原始出处，不访问数据库。`--read-only`使用现有图谱适配器核对正常资料；报告必须是temp里的新文件。零模型调用、不生成最终答案、不写账号或图谱。`--freeze`只用于首次创建新题集，已有目录拒绝覆盖。

动态Planner、任务状态恢复与评分器尚未实现。具体设计、对照与验收见Project/28。
