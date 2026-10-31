# 第一版专业文档库

**聊天接线已完成（2026-10-23）：** 已审核的6份/134片段用于正式聊天，支持文档混合检索、图谱联合回答、文档标题/版本/PDF页码与原文查看，见[21接线记录](../../../Project/21_过程_图谱与文档联合问答接线.md)。`retrieval_scopes.json`固定聊天允许的确切版本与主题范围；新增版本须完成审核/索引/激活并登记，更新时保留审核过的旧版本条目供历史出处查看。没有自动扩充或定时外部调用，管理上传页面仍未实现。下面“未接线”保留阶段历史，不代表当前状态。

**当前状态（2026-10-23增量后）：** 共6份资料的选定范围、134有效片段/真实向量；新增健康素养2024的1—50页（第50页59条起排除）、慢阻肺服务规范2024的1—3页（第3页流程标题起排除）、肥胖指南2024的1—3页，共99片段。2017基本公卫规范暂缓。医学状态保持not_verified，正式聊天尚未接入。准入与范围记录为`admission_review_20261023.json`、各`metadata/`和[20过程记录](../../../Project/20_过程_中文候选逐份准入与增量入库.md)。后续导入预览入口`scripts/ingest_reviewed_documents.py`，默认零调用/零正式写入；旧`index_documents.py`只用于首次固定seed的历史验收。下面3份/35片段及固定费用描述保留首次批次范围。

2026-10-23：本机已有3份官方公开PDF，原始共128页/4,346,261字节，首版选择23页正文，生成35个片段。保留原文、文件SHA256、来源链接、版次、PDF实际页码、印刷页码和提取文本字符区间。全部为`not_verified`；这里只做工程检查，不表示医学专业审核。

**真实向量更新：** 获批首批已用5次embedding请求完成35片段向量入库及12开发查询，11,253tokens/估算0.0056265元。3个版本已向量就绪，开发题dense首位11/12、混合首位12/12，均前5命中12/12。报告见根`temp/documents/live-seed-20261023-approved.json`。正式聊天尚未接线；下文“首次真实建索引尚未执行”为准备阶段说明，不可重用旧批授权自动重跑。

| 文档 | 首版检索范围 | 片段 |
| --- | --- | --- |
| 基层医疗卫生机构高血压防治管理标准 WS/T 872—2025 | PDF第3—6页；第4、6页表题及后续表格不索引 | 7 |
| 成人高血压食养指南（2023年版） | PDF第3—10页，前言/疾病特点/食养原则 | 11 |
| 成人糖尿病食养指南（2023年版） | PDF第3—13页，前言/疾病特点/食养原则 | 17 |

表格、流程图及附录食谱未做可靠结构化处理，首版不检索；原PDF完整保留。食养指南不能单独支持个体诊断、用药或处方。2023年食养文献不是当前所有诊疗问题的最新标准。

`metadata/`是正式的逐文件来源与范围记录，`seed_manifest.json`固定本批文件/版本/片段统计，`retrieval_dev_cases.json`是按资料编写的12道开发题，不能作为盲测或医学正确率。公开可访问不等于已评估再分发许可，PDF与本地派生库不提交Git。

## 本地使用

在`bishe`目录运行，或让助手处理。双击`scripts/documents.cmd`可查看现有版本，关闭窗口不影响数据。

```bat
scripts\documents.cmd list
scripts\documents.cmd check
scripts\documents.cmd search "家庭血压应该在什么时间测量？"
```

上述搜索仅使用本地中文双字词BM25，不调用embedding或回答模型。输出是证据片段，尚未接入正式聊天。

导入先预览，不写库；确认文件/来源/页码后显式导入，新版本默认暂存，不替换当前版本：

```bat
scripts\documents.cmd preview --pdf data\document_library_v1\originals\nhc-hypertension-diet.pdf --metadata data\document_library_v1\metadata\nhc-hypertension-diet.json
scripts\documents.cmd import --pdf data\document_library_v1\originals\nhc-hypertension-diet.pdf --metadata data\document_library_v1\metadata\nhc-hypertension-diet.json
scripts\documents.cmd activate VERSION_ID
scripts\documents.cmd disable nhc-hypertension-diet
```

同一文档更新沿用`doc_id`，更换文件SHA256/版次/来源/选页记录后导入；相同文件与相同元数据重复导入不产生新版本，获取时间变化不产生新版本。改变文件、版次、来源或索引范围会形成新的不可变版本。显式切换后旧版仍保留；回退就是重新激活旧`VERSION_ID`，没有删除操作。目前是命令入口，上传/审核管理页面尚未实现。

## 存储与复现

- `originals/`：下载的原文件（忽略Git）。
- `library/objects/<sha256>.pdf`：导入时按内容寻址保存的原件，不覆盖（忽略Git）。
- `library/catalog.sqlite3`：独立来源、页面、片段、版本、激活指针、向量就绪状态及事件记录，不是账号/聊天数据库。
- Qdrant：集合名带文档库ID和embedding指纹；先写入并逐点回读，全部通过才记录向量就绪。检索使用同一份本地有效版本快照过滤旧版，不靠逐个修改远端`active`标记。

新机器先安装后端`requirements.txt`，按`metadata/`的`download_url`下载各PDF并保存为清单中的`local_original`，核对SHA256，再逐个预览/导入/激活。该目录不包含原PDF的公开再分发副本。备份时一并保存完整`library/`、原文件/元数据及Qdrant快照；不能仅备份原账号数据库。

`scripts/index_documents.py`默认只输出范围/费用计划，零模型请求。当前35片段与12个开发查询计划最多5次embedding请求，固定预算0.05元，保守估算0.0244665元；首次真实建索引尚未执行。执行开关要求新批次授权标签、所审阅范围的SHA256和新的temp报告，失败即停、usage未知即停，不自动重试。该估算不是服务商账单硬限额；实际是否扣免费额度须在百炼查看。

已完成34项文档生命周期/混合检索离线检查、6项调用预算检查、23项embedding回归，以及本机Qdrant新适配器12项真实合成向量检查。关键词开发题12/12首位命中预期文档页；尚无真实语义检索、生成答案或医学质量结果。
