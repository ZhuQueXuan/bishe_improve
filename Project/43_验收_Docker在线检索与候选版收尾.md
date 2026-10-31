# Docker、真实文档检索与候选版收尾

2026-10-27，承接[42](42_验收_真实回答小批与运行交接收尾.md)。用户授权完成剩余验收，并在Docker引擎不可用时启动；随后两次确认已开启浏览器控制并重新打开。

当前结果：Docker和原Qdrant恢复，专业文档真实检索、依赖新环境安装、账号备份/恢复副本、实际完整启停通过；修复两处前端显示与运行脚本兼容问题。**保留为本机候选版，尚未宣布正式版**：浏览器工具仍返回禁用，最后的续做/遗忘/重置点击及窄屏视觉未验。个人记忆向量化继续正式版之后讨论，Mini SWE Agent项目未动。

## 1. Docker恢复与原库核验

初次启动Docker Desktop后，引擎没有就绪。原生日志及错误窗口均指向`userAnalyticsOtlpHttp.sock`的陈旧遥测socket，Windows返回“系统无法访问此文件”。正常`docker desktop stop --timeout 15`未能完成，官方`docker desktop stop --force --timeout 15`关闭当时没有运行容器的Desktop进程。

Windows单文件重命名也失败，因此使用本机已有Ubuntu的Python，仅将下面一个文件重命名并保留：

`C:\Users\刘文轩\AppData\Local\Docker\run\userAnalyticsOtlpHttp.sock` → 同目录`userAnalyticsOtlpHttp.sock.stale-20261027`。

没有删除文件、恢复出厂设置、重装、拉镜像或重建卷。旧`stale-20261023`也保留。重新打开原Desktop后，`docker info`返回引擎28.0.4，原`medical-rag-qdrant-v1`按既有重启策略上线，使用原`medical-rag-qdrant-storage-v1`。其他容器可能随Docker自身策略启动，本轮没有操作其服务。Docker正常启动会更新日志和运行元数据，不声称整个Docker目录字节不变。

## 2. 两条真实专业文档检索

冻结计划：`bishe/data/memory_system_v1/final_online_20261027_plan.json`。仅询问公开问题“如何预防老年人跌倒？”、“高血压患者如何减少食盐摄入？”，最多2次query embedding、0次回答模型、无自动重试，计划估算上限0.05元。不是个人记忆向量接入。

通过生产`DocumentSearch`和原Qdrant执行：6个当前有效文档版本的134点/向量/来源回读通过；每问恰好1次embedding。55+57=112 tokens，按项目既有保守系数估算0.0000560元，不是服务商账单或现价证明。14项工程检查通过，查询前后134点的payload和向量整体哈希一致，账号/原文/目录/旧成果保护核对一致。

首位分别返回健康素养2024的PDF18页、成人高血压食养指南2023的PDF6页，并与本地原文匹配。检索结果不等于全部相关：跌倒查询第3条来自相邻PDF20页，包含其他主题，说明仍有召回噪声。未给出盲测、医学或总体准确率，也未将两条查询当成实时模型+图谱+文档全链路复验。

实际请求及通过报告已经保存后，原脚本关闭账本时使用Windows默认编码读取中文JSON，发生解码错误。保留原账本副本，另用UTF-8仅做本地结账，2条已结算请求保持，状态completed；没有重跑两次请求或增加费用。计划为执行前冻结材料，实际执行状态以账本及本次索引为准。

证据：`temp/final_online_20261027_ec3faa9e60/{report,ledger,ledger.before-local-finalization}.json`。

## 3. 代码修复与离线回归

- `ChatWorkspace.vue`为分步复选项增加独立窄屏样式，避免通用提示的隐藏规则同时隐藏它。编译与模板检查通过，实际窄屏视觉仍待。
- `messageDisplay.js`只在答案附带的来源中核对行号/证据ID后，去掉正文重复的`medical.json 第…行，字段 symptom`等精确位置括号；保留E引用、医学文字、原始回答与来源面板。未知来源、错误证据ID及用户消息不静默改写。
- `process-records.ps1/start.ps1/stop.ps1`补齐真实持久化和Windows PowerShell兼容，详见下一节。

4条已有真实模型回答在当前显示函数回放，新增20项通过，未追加模型请求。原前端58项、lint和候选构建通过；构建仍有既有的依赖包大小和浏览器兼容数据库陈旧提示。最终后端590项全部通过，使用内存账号库并阻断网络，后端业务代码保持G版本。没有因显示修改而改写旧模型回答或旧成绩。

证据：`temp/final_display_20261027_report.json`、`temp/system_round1_replay_frontend_c0940252b4/report.json`、`temp/memory_check_final-20261027_72f3acfe09/verification.json`；候选构建在`temp/final_candidate_build_20261027/`。

## 4. 实际启停发现的问题与最终结果

42的模拟检查没有经过真实JSON持久化和跨PowerShell读取，本轮实际停止暴露：新版PowerShell把创建时间解析成DateTime，旧版则可能保留字符串；JSON还会省略末尾零。原直接字符串比较不兼容。现统一严格ISO/有时区的时间为相同精度UTC后比较，仍拒绝缺失时区、无效时间和一tick变化，命令哈希/项目路径校验不变。[PowerShell官方开源说明](https://github.com/MicrosoftDocs/PowerShell-Docs/blob/main/reference/7.6/Microsoft.PowerShell.Utility/ConvertFrom-Json.md)也明确了日期解析选项；没有引入第三方进程管理框架，未使用仅新版支持的参数要求用户升级。

Windows自带PowerShell5.1还将JSON数组作为一个管道对象输出；旧start中的外层`@()`产生嵌套数组，重复启动丢失归属。改为直接接收解码结果。真实停止中父进程可能随最后一个子进程退出，原脚本把已完成退出当失败；现仅在重新核实目标确已不存在时接受该结果，仍存在的进程或其他失败继续报错，不强杀图谱。

原有遗留服务只在本次明确维护中逐一核对监听祖先、项目路径、创建时间和命令哈希后，写入临时维护身份；这不是产品自动接管任意已存在服务。早期失败均保留，没有回填为通过。

最终使用与双击入口相同的Windows PowerShell5.1执行26项真实验收：原服务正常关闭、全新启动身份有效、重复启动身份完全一致、新服务再次停止、最终重新启动、正式账号库字节相同、图谱统计相同、5个服务健康。图谱始终正常退出，未重导入；最终8,806疾病/27,098节点/301,314关系保持。另有27项替身/日期/语法检查在5.1通过，以及4项5.1真实重复启动/身份检查。

最终证据：`temp/final_lifecycle_20261027_3552571b26/`、`temp/final_process_records_win51_20261027_v3_report.json`、`temp/final_windows51_20261027_report.json`。

中间证据：`final_lifecycle_20261027_39c610d537`首次拒绝停止；`a403bae352`为第一版日期修复后PowerShell7的26项通过；`7b9e6599d8`为5.1停止中父进程已退出的失败。它们不是最终版本成绩，均保留。日期修复第一版及Windows5.1丢失记录也留在temp，不隐藏迭代失败。

## 5. 新环境依赖、配置与备份

从已安装环境的7个直接依赖递归提取25个固定版本，保存`knowledge_graph_backend/requirements-tested-20261027.txt`。随后从官方PyPI下载wheel，建立全新临时venv、离线安装、`pip check`、7模块导入均通过；另在新环境执行Django配置检查通过。原运行venv未被重装。范围是本机Windows/CPython3.10.6，不宣称异机或跨平台复现通过；前端沿用已有package-lock，本轮未重装node_modules。

正式账号库以只读方式打开，经SQLite一致性备份接口生成独立备份再恢复到新副本，完整性/外键/所有原表结构与行内容指纹匹配；仅在恢复副本测试写入，正式库哈希保持。该演练连同依赖现状检查13项通过。

补上可双击的`scripts/backup-account.cmd`及Python入口，默认把新快照放入temp/account_backups。它只生成新文件，拒绝覆盖已有文件、来源自身或正式账号库；完整性和外键通过后生成本地报告。实际入口备份→恢复到新副本→逐表内容相同→拒绝覆盖→副本写入隔离，共7项通过。没有执行正式数据库替换或新增迁移。备份包含账号/聊天等私人内容，不纳入候选发布材料。

两份README补充固定依赖、配置模板、17687/16333、本地入口与副本恢复方法；专业资料仍按既有metadata下载核验，图谱/资料库/Qdrant需分别备份，不把账号备份当成完整项目恢复。

证据：`temp/final_clean_dependencies_20261027_8b9cd4a4d0/report.json`、`temp/final_recovery_20261027_2cd7330f45/report.json`、`temp/final_backup_entry_20261027_466d850a7e/report.json`。

## 6. 页面阻塞与正式版门槛

用户已两次确认开启并重开。重新初始化CUA运行时后，浏览器清单仍返回`Local browser use is disabled in Work settings`。本轮没有新增页面动作、没有用API/其他浏览器绕过、没有复用旧90秒暂停令牌。新隔离Vue/WSGI服务仅准备后即通过专用关闭入口收尾：模型输入0、工具读取0、外部调用0、正式保护文件一致。

旧42实际10项页面观察仍有效于当时版本，不能代替本轮点击。尚缺：新任务90秒内点击“继续整理”直到最终答案与累计依据；忘记/重置后的页面行为；窄屏入口视觉。旧临时视口因工具不可用未复位，最后截图也未导出，不声称已完成。

独立fixture证据：`temp/final_browser_20261027_fb15b9e1f7/{browser_obstacle,server_report}.json`。候选索引：`bishe/data/memory_system_v1/final_acceptance_20261027_manifest.json`，明确`formal_release=false`。页面验收完成后再定正式版与最终发布范围，本轮不自动上传、发版或接个人向量。

当前用户可打开本机8080页面，或用`scripts/start.cmd/health-check.cmd/stop.cmd/backup-account.cmd`管理。已有真实模型小批仍只有42的4次；本轮新增仅2次公开问题embedding，0次模型回答。医学审核、任意自然语言理解、通用长期任务和全历史语义遗忘仍不在已验证能力中。
