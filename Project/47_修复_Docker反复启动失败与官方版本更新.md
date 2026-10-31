# Docker反复启动失败与官方版本更新

后续清理：用户确认运行正常并明确要求删除13.64 GB备份后，已逐项删除本次的`docker_data.vhdx`、`docker_main_ext4.vhdx`和`settings-store.before.json`三个副本。正在使用的原数据盘与原配置保留；以下备份描述是修复时的历史过程，备份现已不可用于恢复。清理回执见`temp/docker_stability_repair_20261027/backup-removal-report.json`，其余修复报告和安装包保留。

2026年10月27日，针对用户反馈的Docker反复报错，完成本机Docker Desktop原位更新和两轮真实退出重启复验。当前Desktop为4.97.0、Engine为29.9.2，医疗项目五项服务均返回200，原Qdrant容器、卷和134条向量记录保留。医疗应用版本仍为1.0.2。

## 原因与修复选择

本次启动日志明确报错：OTel manager无法清理`userAnalyticsOtlpHttp.sock`，Windows返回“The file cannot be accessed by the system”，随后Docker后端关闭。这与43记录的两次临时恢复属于同一故障；不是从Docker API管道缺失直接推断原因。20:43至20:44有正常Windows关机事件，但不据此断言是谁或哪一步留下了socket。

已核对Docker官方GitHub问题[460](https://github.com/docker/desktop-feedback/issues/460)：4.89的初步修复仍有重现，维护者在10月7日说明修复应包含于4.92及以后版本。原安装4.40.0早于修复。优先复用官方修复，未引入第三方清理工具、常驻修复程序或重复删除socket的脚本。

官方更新检查提供4.97.0，按[发布说明](https://docs.docker.com/desktop/release-notes/#4970)获取Windows x64安装包，620496816字节，SHA256为`2668273f82b342a058a28f9ab53ea7bddde10bb6d51307ce2ec54c2d36459744`，与[官方校验文件](https://desktop.docker.com/win/main/amd64/244584/checksums.txt)一致；Authenticode有效，发布者为Docker Inc。下载中断保留原片段，通过同一官方文件的Range续传完成，没有换用第三方镜像。

## 数据备份与更新

当前引擎未运行、全部WSL发行版停止后，先用官方`docker desktop stop --force --timeout 15`退出崩溃残留进程。按[官方备份方法](https://docs.docker.com/desktop/settings-and-maintenance/backup-and-restore/)复制停止状态的数据盘：`docker_data.vhdx`为13520338944字节，Docker主发行版`ext4.vhdx`为118489088字节，并保存原settings-store.json；逐文件来源与副本SHA256一致。副本约13.64 GB，保留于`temp/docker_stability_repair_20261027/`。没有把备份恢复到当前磁盘，也没有执行恢复出厂设置、WSL注销或手动文件删除。

通过Windows管理员确认执行官方安装器`install --quiet --backend=wsl-2`，安装器检测到原全用户安装，沿用`C:\Program Files\Docker\Docker`与WSL2，成功退出码0。现有classic存储选择`UseContainerdSnapshotter=false`和`AutoStart=false`保留；未切换存储引擎、迁移数据目录或要求Windows重启。本轮没有手工重命名任何socket，更新后的程序直接完成启动。

## 实际复验

更新后首次启动及其后两轮正常退出、重新启动均完成。两轮都使用不带force的`docker desktop stop --timeout 45`，实际确认Qdrant随引擎停止，再启动原Desktop；分别约12.08秒与12.05秒恢复Qdrant就绪。

三次读取均核对原容器`medical-rag-qdrant-v1`，创建时间仍为10月22日，容器ID保持；原命名卷`medical-rag-qdrant-storage-v1`仍挂载在`/qdrant/storage`。全部134个点ID和来源payload与本地目录一致，每个向量1024维且数值有效；三次完整payload和向量统一哈希均为`33c21b87ec94fc1ff7cf5cac0facd07954c7faa014f302f3432c3f191895523f`。这些是本地读请求，没有新embedding或回答模型调用。

最终8080前端、8000后端、图谱API、7474 Neo4j与16333 Qdrant全部200。原1.0.2的223个发布源文件在本轮文档收尾前相同，37个保护文件和原源码ZIP相同，正式账号库未改；本轮只更新Docker环境及本次记录，未改医疗业务功能或Mini主项目。没有重复执行原608项后端回归，也不把它称为本轮新测试。

## 使用与剩余边界

当前直接打开[医疗项目](http://127.0.0.1:8080/)即可试用。Windows重新登录后，Docker仍沿用原来的不自启偏好；需要资料向量检索时，打开桌面的Docker Desktop，等待引擎就绪。医疗项目原双击入口`bishe/scripts/start.cmd`、`health-check.cmd`继续使用。

本次已经替换包含该故障修复的官方程序，并通过两轮正常重启；尚未进行整机重启复验，也不承诺所有未来Windows/WSL故障均不会发生。停机磁盘副本已校验，未做覆盖当前数据的恢复演练。46的邮箱重登页面复验仍待浏览器可用，不由本次环境修复回填为通过。个人记忆向量接入仍为后续独立讨论项。

证据统一保存于`temp/docker_stability_repair_20261027/`：backup-report、download-report、install-request/result、official-install-log、upgrade-first/restart-1/restart-2-verification、restart-cycles和closeout-report。阶段索引为`bishe/data/memory_system_v1/docker_stability_20261027_manifest.json`。备份、安装包和原记录均保留，不上传。
