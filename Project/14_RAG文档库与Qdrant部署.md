# RAG文档库与Qdrant部署

更新：2026-10-22。状态：用户手动重启后，Qdrant 1.19.2已安装并运行；实际健康、合成向量读写/过滤、快照导出恢复和容器停止再启动持久化共12项检查通过。独立embedding接口后续已完成，见15；尚未导入专业文档或将embedding接入正式检索/聊天链路。

## 当前决定与范围

用户重新开启医疗项目的RAG改进，认可知识图谱与文档库配合、先做小文档库并持续增补和更新；随后明确要求安装专用向量数据库。本轮选择Qdrant，覆盖此前临时提出的“先用Neo4j向量索引”建议。2026-10-04取消本地向量路线属于历史决定，本次开启新的文档检索方案，不恢复旧BGE环境或直接为全量旧资料生成向量。

- Neo4j保留现有医疗知识图谱和关系查询；新专业文档的向量存入Qdrant。
- 本地已有medical.json派生的8,806疾病和149,975文本片段，可用于工程对照；不是已完成医学审核、上游出版来源/日期/许可完整的专业文献库。
- 新文档优先筛选中文官方标准/规范、适用指南和健康教育资料，保留发布机构、日期/版本、链接、使用条件、章节/页码。不因网页可读就认定可无限转载；目前尚未下载或入库。
- 文档管理计划支持内容哈希查重、增量索引、版本关联、启用/停用与失败状态。新版验证完成后切换；旧版保留以解释历史引用。持续更新第一版由主动导入触发，不代表自动监控官方网站。
- 文本路线计划为关键词与向量混合检索，必要时再评估重排；现有Agent的来源、上下文和预算检查继续复用。尚未实现接线。
- Embedding已采用百炼北京地域qwen3.7-text-embedding API、1024维，独立适配/配置已完成；23项离线检查及修正地址后的获批2次真实连通性检查通过。尚未导入专业文档、写入正式文档集合或接入聊天；完整过程和已结束的批次见[15_Embedding模型选型](15_Embedding模型选型.md)。下载任何本地模型仍需先说明整套资源并获批准，付费调用仍需明确新批次授权。

## 部署配置

文件：[docker-compose.qdrant.yml](../bishe/docker-compose.qdrant.yml)。已安装官方v1.19.2镜像，并固定版本及实际拉取的摘要，不使用latest自动升级。镜像摘要：`sha256:b7b0444c4c351c970b98e90a6f89c2ee4287c65b44e52b4cb503fa5b2aa927ad`；本机报告镜像大小200,184,494字节（约190.9MiB），不含Docker/WSL已有运行环境。详细记录见[temp部署报告](../temp/qdrant/deployment-20261022.json)。

| 项目 | 配置 |
| --- | --- |
| 镜像 | qdrant/qdrant:v1.19.2 |
| Compose项目/容器 | medical-rag-qdrant / medical-rag-qdrant-v1 |
| 本机REST/管理界面 | http://127.0.0.1:16333 / http://127.0.0.1:16333/dashboard |
| 本机gRPC | 127.0.0.1:16334，容器内部仍为6334 |
| 持久存储 | Docker命名卷medical-rag-qdrant-storage-v1，挂载/qdrant/storage |
| 快照目录 | 同卷的/qdrant/storage/snapshots |
| 资源上限 | 2 CPU、1GiB容器内存；这是起步配置，不是测得的需求，Docker/WSL自身另有开销 |
| 遥测 | 关闭 |
| 项目过程日志 | bishe_improve/temp/qdrant/ |

使用Docker管理的Linux命名卷，避免把Qdrant活跃数据目录直接绑定到Windows盘符。官方明确提示Windows Docker/WSL挂载可能存在文件系统问题；命名卷不是备份，Docker重置/人工删除卷仍会丢失数据。已在独立测试集合实测快照导出到项目temp及恢复到新集合；正式文档的备份管理入口和定期策略尚未实现。

启动脚本不停止其他容器、不清卷、不提供清库动作；有同名资源时检查其Compose归属。尚未连接医疗后端，不修改现有.env、账号库或Neo4j。

## 安装过程与已解决问题

- Windows x64，AMD Ryzen 5 7500F；固件虚拟化原本已开启。经用户授权启用VirtualMachinePlatform，用户手动重启后HypervisorPresent=true，WSL2可用。
- Docker客户端/引擎28.0.4、Compose v2.34.0-desktop.1，Linux x86_64引擎。初次Docker因Windows功能未启用而失败，原错误HCS_E_SERVICE_NOT_AVAILABLE已解除。
- 重启后Docker另外因旧的`C:/Users/刘文轩/AppData/Local/Docker/run/userAnalyticsOtlpHttp.sock`不可访问而退出。正常关闭超时后，仅强制关闭本次启动失败的Docker；核对run目录只包含该一个端点，再将整个run目录改名保留为`C:/Users/刘文轩/AppData/Local/Docker/run.stale-20261022-2309`，重建空run目录后Docker正常启动。单文件改名先前失败，未删除该文件或原目录。未重置Docker、未清理镜像/卷/旧容器、未停止Ubuntu。
- Docker启动后显示已有历史RAGFlow容器，未对它们执行停止/修改/清理。不要把这些历史服务与本项目Qdrant混淆。
- Windows保留端口范围6303～6402，默认6333/6334无法绑定（WinError 10013）；未修改系统保留策略，改用经绑定探测可用的16333/16334，仍只绑定127.0.0.1。
- 初次验收脚本在默认受限网络下被阻断，未写入数据库；保留该次报告`temp/qdrant/acceptance-20261022.json`，随后通过宿主机权限完成真实检查，正式成功报告见下文，不混为服务故障。
- require_escalated用于允许宿主机访问，不等于取得Windows管理员令牌。功能启用由额外的Windows RunAs管理员子进程执行；电脑始终由用户手动重启。
- 管理员执行报告：[prerequisites-20261022-225804.json](../temp/qdrant/prerequisites-20261022-225804.json)，success=true、automatic_restart=false。配置解析结果保存在temp/qdrant/compose-resolved.json。
- Compose配置解析、Windows PowerShell 5语法检查及按CRLF约定的diff检查通过；实际数据库验收见下文。

## 用户操作

当前安装完成并运行，不需要重新启用系统功能或重复安装。以后先打开Docker Desktop，待引擎就绪，再双击[启动](../bishe/scripts/qdrant-start.cmd)；需要停止时双击[停止](../bishe/scripts/qdrant-stop.cmd)，停止保留数据。服务设为unless-stopped，但不替用户更改Docker Desktop的开机启动设置。

[管理页面](http://127.0.0.1:16333/dashboard)只在本机服务运行时可用。新电脑安装用[安装入口](../bishe/scripts/qdrant-install.cmd)拉取固定摘要镜像并启动；仅在缺少系统功能时才使用[前置功能入口](../bishe/scripts/enable-qdrant-prerequisites.cmd)。这些脚本不自动启动Docker Desktop或重启电脑。

命令行等价用法：

```powershell
# 在bishe_improve目录
powershell -NoProfile -ExecutionPolicy Bypass -File .\bishe\scripts\qdrant.ps1 -Action Install
powershell -NoProfile -ExecutionPolicy Bypass -File .\bishe\scripts\qdrant.ps1 -Action Status
```

## 实际数据库验收

[成功报告](../temp/qdrant/acceptance-20261022-host.json)共12项检查通过，调用真实本机Qdrant，仅使用4维合成向量，外部模型调用0次：

- 服务身份及/readyz就绪；Qdrant版本1.19.2。
- 新建独立测试集合，写入3点；余弦检索返回预期排序，active=false旧版本被排除。
- 增量写入第4点，重复upsert同一ID后仍为4点；这是数据库ID幂等性，不代表产品的文件哈希查重功能已完成。
- 将测试点停用后查询排除，再启用恢复。
- 创建集合快照，导出129,536字节到`temp/qdrant/20261022_231024_13d13c.snapshot`，SHA256为`738c26909779217aab8161e46554e3e64ddb76f43ad5b85896557f7b9df315a9`。
- 将快照恢复到全新的测试集合，4点数据及过滤查询结果一致，没有覆盖已有集合。
- 通过正式停止/启动脚本重启本项目容器；原集合和恢复集合均保留4点，过滤结果一致。

保留测试集合`installation_check_20261022_231024_13d13c`及其`_restored`集合，不删除过程数据。一次性验收脚本在`temp/qdrant/check_installation.py`；正式运行不依赖该脚本。最后采样容器内存95.34MiB/1GiB、CPU约0.01%，仅是空库加合成验收的瞬时观察，不是正式文档规模性能或容量保证。

本轮通过实际Start/Stop/Status脚本和Compose启动验证，但没有重跑医疗后端测试或完整聊天页面验收：业务代码未变。未下载embedding模型、未付费调用、未写入Neo4j/账号库、未导入任何外部医疗文档。真实语义召回与答案忠实度仍待后续评测。

## 后续实施顺序

1. 已完成：解决Windows前置条件，安装Qdrant并做真实持久化及快照恢复验证。
2. 筛选少量可用的中文专业资料，固定资料清单与版本；建立片段与来源合同。
3. 选定embedding，说明费用/资源；建立相同资料范围的关键词与向量对照，不重复旧实验候选池不一致的问题。
4. 完成文档增补、版本切换、停用和检索过滤，再接入聊天与证据展示。
5. 用固定开发题和独立验收题分别检查证据召回、无答案行为、最终忠实度、延迟和成本。数据库安装成功不代表RAG或医学质量已验收。

## 官方依据

- [Qdrant本地部署](https://qdrant.tech/documentation/quickstart/)
- [安装与存储要求](https://qdrant.tech/documentation/installation/)
- [v1.19.2发布](https://github.com/qdrant/qdrant/releases/tag/v1.19.2)
- [MedlinePlus数据](https://medlineplus.gov/xml.html)及[使用条件](https://medlineplus.gov/about/using/usingcontent/)
- [PMC开放获取子集](https://pmc.ncbi.nlm.nih.gov/tools/openftlist/)
