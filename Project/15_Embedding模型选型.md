# Embedding模型选型

日期：2026-10-22。状态：用户已同意选型并自行完成账号/实名和密钥创建；独立embedding接口及本地配置已完成，23项阻断网络的离线检查通过。首批1次请求因抄录API Host漏字母返回403并停止；按控制台原文修正后，用户另行批准的新批2次真实调用通过，合计111tokens、公开价格估算0.0000555元。两批均已结束，无自动追加额度。没有下载模型，文档库/正式聊天尚未接线。

## 首选方案

第一版已采用阿里云百炼北京地域的`qwen3.7-text-embedding` API，输出固定1024维、L2归一化，后续文档集合使用Cosine相似度。DeepSeek继续承担查询理解/规划和最终回答；embedding只生成检索向量，不承担医学审核或回答。当前真实连通性验证未向Qdrant写入embedding向量，不能写成文档库已接线。

这是基于中文文档需求、云API部署工作量和公开费用的工程选择；没有进行本项目的模型效果排名，不声称此模型在医疗检索上已优于其他方案。模型能力与医疗语料质量分别验证。

## 核对的候选

| 候选 | 官方资料支持的能力 | 本项目取舍 |
| --- | --- | --- |
| qwen3.7-text-embedding / 百炼API | 支持中文及多语言、可选输出维度，提供query/document区分及查询任务指令 | 首选1024维，避免先维护本地推理环境；需账号、对应地域/业务空间访问地址和密钥，文本片段将发送给该服务 |
| text-embedding-v4 / 百炼API | Qwen3-Embedding系列，支持中文、多维度输出 | 同服务的备选，不与qwen3.7混用到同一向量空间；若首选账号不可用再评估，不自动切换 |
| Qwen/Qwen3-Embedding-0.6B / 开源模型 | 官方模型卡列出100+语言、32K上下文、最高1024维 | 后续需要离线部署时的候选；未核对本机实际推理速度和整套安装磁盘/内存需求，不在本轮下载 |
| BAAI/bge-m3 / 开源模型 | 官方模型卡列出1024维、8192序列长度及多语言/多种检索表示 | 可作为后续独立对照候选，不恢复旧bge-small-zh-v1.5环境，也不因模型卡多种表示就声称服务已实现混合召回 |

不按公开综合榜单直接判断中文医疗语料的好坏，也不以更大的输出维度代替实际证据召回评测。初期只启用稠密向量；关键词召回与重排各自评估，避免一次叠加多项改动无法解释收益。

## 调用费用及范围

2026-10-22核对的百炼北京地域同步调用公开单价：qwen3.7-text-embedding为每1000输入token 0.0005元，即每100万token约0.5元。按该价格计算，10万token约0.05元。仅是embedding费用，不含DeepSeek回答、重排或其他服务。实际费用随地域、服务规则和真实计量变化，执行前再次核对；不假定用户账户有免费额度。

计费输入包括文档正文、加入的标题/章节/检索指令及查询文本，重复切片或重新编码也会增加输入。只对新增/改变的检索文本生成新向量，已处理内容使用带配置版本的缓存。更换模型或维度通常需要重新编码；同一维度不代表不同模型可以混用。

尚未统计正式文档token，不能承诺整库费用。两次连通性批次各自取得用户明确的最多2次/0.01元授权；不使用历史DeepSeek额度或本轮剩余次数，不以“价格低”推断追加付费授权。

## 已实现的接口合同与后续要求

- [api/embedding.py](../bishe/knowledge_graph_backend/api/embedding.py)采用DashScope原生HTTP接口以支持`text_type=query/document`及查询`instruct`；不新增SDK依赖。endpoint由本地北京业务空间API Host构建，校验域名并拒绝HTTP重定向，避免将凭据转发到意外地址。没有复用DeepSeek密钥。
- 文档按document编码，问题按query编码；采用固定、版本化的英文检索指令，如“Given a Chinese medical knowledge question, retrieve relevant passages from medical guidelines and health education documents.”。指令效果属于待测假设，不保证改善。
- 已实现维度1024、稠密向量、L2归一化；校验响应条数/唯一输入索引、向量维度、有限数值与非零范数，按text_index恢复顺序。不接受静默截断、补零、重复索引和缺条。单文本最多8192 UTF-8字节/每批最多20条与65536字节，属于本项目的保守上限，不冒称供应商token上限。
- 文档和查询记录同一embedding配置指纹：服务商、地域、模型ID、维度、输入模板/指令、归一化约定、数据与切片版本。API别名不能假装成固定权重revision；若服务无法锁定权重，记录这一可复现性限制，使用缓存及固定探针识别变化，不把新旧结果无检查混写。
- 原始片段与出处独立保存，不把embedding输出当作事实。新集合与现有4维安装验收集合隔离，不覆盖后者。
- 使用独立且被Git忽略的`.env.embedding`，不修改原聊天`.env`。用户已将密钥发入聊天；不在文档/日志/回复中再次回显，建议用户后续轮换，提供[本地隐藏输入入口](../bishe/scripts/configure-embedding.cmd)。替换前备份受影响当前配置到temp/embedding，备份同样不进入Git。
- 从文件读取时关闭dotenv插值；环境变量可覆盖文件，配置检查只报告是否可用、固定模型/维度及指纹。错误日志仅保存静态错误码和少量白名单供应商分类，不记录原HTTP正文、请求文本或密钥。
- 单请求超时默认20秒、上限30秒，无自动重试；构造真实客户端需要显式opt-in。usage缺失记为unknown，响应校验失败仍保留已知usage；不会按0元结算。真实smoke脚本固定最多2次，unknown或失败即停，报告在请求前以独占新文件创建。没有自动续跑或使用余额。

## 实现入口与验证记录

- 模型适配：[embedding.py](../bishe/knowledge_graph_backend/api/embedding.py)；离线边界测试：[test_embedding.py](../bishe/knowledge_graph_backend/api/test_embedding.py)。
- 本地配置/替换：[configure-embedding.cmd](../bishe/scripts/configure-embedding.cmd)，输入密钥时不回显；模板[.env.embedding.example](../bishe/knowledge_graph_backend/.env.embedding.example)。
- [check_embedding.py](../bishe/scripts/check_embedding.py)默认只校验配置，0外部调用；真实开关仅用于当次明确获批批次。
- [离线报告](../temp/embedding/offline-20261022.json)：23项通过，socket连接被阻断，无数据库访问；检查请求协议、响应顺序、维度/NaN/布尔/零向量、未知用量、域名/重定向、密钥保护、超时/无重试等。
- [首批报告](../temp/embedding/live-20261022-first.json)：1次文档请求，错误API Host返回403，usage未知、费用未确认；立即停止，没有第二次请求。错误为助手从截图抄录Host漏了一个字母；随后对照浏览器控制台原文修正，旧配置保存在temp/embedding/config-before-host-fix-*。保留失败记录，不改成成功、不推断账号无权限。
- [修正后新批](../temp/embedding/live-20261022-host-corrected.json)：用户另行批准最多2次/0.01元，实际2次，文档2条/53tokens、查询1条/58tokens，合计111tokens。按0.5元/百万token估算0.0000555元，非账户扣款；失败首批费用仍未知，不能把此数称为两批总扣款。
- 新批仅发送两段英文合成fixture和一个查询，未发送聊天历史或真实医疗文档。两条资料分别为血压监测主题与数据库管理主题，查询向量与对应资料相似度约0.7145，与无关资料约0.3289。只证明该fixture接线、维度和排序成立，不是中文医疗检索准确率或泛化评测。
- 原业务代码、Neo4j和账号库未接线/写入；未运行全量医疗后端回归，新增适配的23项检查不与历史173项混称为正式聊天累计验收。

在bishe_improve目录执行以下命令仅检查本地配置，不收费：

```powershell
& .\bishe\knowledge_graph_backend\.venv\Scripts\python.exe -B .\bishe\scripts\check_embedding.py
```

## 第一次验证怎样做

1. 已完成账号、北京地域配置及独立真实连通性检查；下一步按来源清单选择文档，不重复已结束批次。
2. 固定少量来源明确的中文文档，核对解析质量与切片，再统计实际输入规模并提交首批调用范围/预算。
3. 冻结约30～50个开发问题及对应原文证据，包含病名/术语、日常改写、多段证据、无答案和易混对象；最终验收另留新题。
4. 相同资料范围分别比较关键词检索、向量检索和二者融合，统计前5/前10是否召回正确证据、无关证据比例、延迟和费用。不比较候选池不同的旧40疾病实验分数。
5. 检索可用后再接回答，分别核对引用合法、断言受资料支持和版本有效；API已接通或embedding返回1024维不等于RAG效果通过。

## 官方依据

- [百炼向量化模型、北京地域价格及query/document/instruct参数](https://help.aliyun.com/zh/model-studio/embedding)
- [Qwen3-Embedding-0.6B官方模型卡](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B)
- [BGE-M3官方模型卡](https://huggingface.co/BAAI/bge-m3)

数据库部署与上一步验收见[14](14_RAG文档库与Qdrant部署.md)。
