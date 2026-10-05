# 医疗知识图谱对话前端

当前页面只有聊天工作区、登录和注册。Vue 3、Pinia、Vue Router、Element Plus 与 Axios 提供界面、用户状态及后端交互；接口地址由 `src/config/api.js` 统一读取。

项目整体启动方式见 `../README.md`。在本目录可运行 `npm run serve`、`npm run lint -- --no-fix`、`npm run build`。

2026-10-03 已将旧仪表盘、图谱管理页、旧 AI 页面及未引用组件移入 `../../Old/cleanup_20261003/frontend/`，移除 ECharts 与 Vuex 的声明和锁文件依赖，仅注册当前页面实际使用的图标。现有安装目录中未使用的包不做批量删除，后续重新安装按更新后的锁文件执行。
