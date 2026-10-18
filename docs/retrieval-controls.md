# 显式检索控制与 ATI 复用

## 范围与接入状态

这是 PR #1 路由改动之上的可选控制层，不是正式部署。模块只有标准库依赖，
不读取密钥、不导入 HTTP 客户端、不启用监控或检索。现有行情、模型路由、
主 Agent 和 OpenClaw 的 web_search/web_fetch 均不被自动改写。
安装模块本身不会降低或封顶这些未接入路径的费用。

`RetrievalGate.execute()` 是未来显式 Search/Fetch 适配器的唯一入口；没有显式
传入 transport 会拒绝执行。此次不提供真实 DeepSeek HTTP transport，
也不新增可由 Agent 直接执行的付费 CLI 命令。
DeepSeek 在服务端内部发起的工具调用不经过本地控制层，无法由它计量或限额。
严格预算路径必须另行确认禁用服务端自动搜索的方法，不能只依赖提示词。

## 离线用法

从 `lobster_quant_agent.retrieval_control` 导入 `Policy`、`RetrievalGate`；
无需导入 CLI，也无需 OpenClaw。以下仅返回虚构数据：

```python
from tempfile import TemporaryDirectory
from pathlib import Path
from lobster_quant_agent.retrieval_control import RetrievalGate

with TemporaryDirectory() as temporary:
    gate = RetrievalGate(Path(temporary).resolve())
    result = gate.execute(
        'search', {'query': 'synthetic fixture', 'need_content': True},
        task_id='research-001', provider='synthetic.v1', reason='citation',
        transport=lambda kind, args: (200, {
            'results': [{'url': 'https://example.com/fixture',
                         'content': 'Invented offline evidence.'}]
        }),
    )
    assert not result['cache_hit']
    assert gate.status(task_id='research-001')['task']['successful'] == 1
```

本地 CLI `retrieval status TASK_ID` 只查看计量，不调用模型或检索适配器。
该 CLI 没有开放进插件工具枚举，避免误解为现成联网工具。

## 配额与统计口径

默认每任务允许8个成功或状态未知的调用、最多12次尝试；北京时间每自然月
允许20,000个成功或未知调用、最多24,000次尝试。Search 与 Fetch 合并计数。
默认费率估算为每成功调用5,000微元（0.005元），20,000次对应100元。
这是 DeepSeek 价格口径，不是其他供应商账单或模型 token 费用。
不同价格的供应商应使用独立账本；本版本不支持跨币种成本汇总。

Policy 可显式配置以上限额及缓存TTL，包括设为0关闭新增调用。
必须由应用固定同一账本的统一策略；不要让模型或子任务传入更宽松策略。
任务ID由顶层调度器生成，并在子 Agent、重试、续作、进程重启中保持不变。
月配额由共享账本跨任务执行；新建任务ID不能绕过月限额。

- 在 transport 执行前提交 pending 预留，事务序列化并发预算检查。
- HTTP 200且Search结果非空或Fetch正文非空，记为 success。
- 非200、空搜索或空正文记为 failed，释放成功额度，但仍占尝试数。
- 异常、超时、格式不明的响应保留 pending，显示为 reserved_unknown；不重试。
- 缓存命中不占新调用或尝试额度，单独记录任务到原始证据的引用。
- 跨月完成仍归属发出请求时的北京时间月份。

统计是本地估算，必须与供应商账单核对。客户端超时不证明服务端免费失败。
缓存正文未经核验不能自动当作研究结论。此次没有添加自动退款、清零或
未知结果解决命令；遇到未知调用应停下核对供应商记录，不能删除账本重试。
保留预留可能使可用额度低于真实剩余额度，这是有意的保守策略。

## 缓存与可追溯性

默认Search缓存15分钟，Fetch缓存1小时。键包含版本化provider标识、接口和
全部已支持请求参数；搜索count、need_content、日期过滤（query内）均影响键。
URL只规范化主机大小写、空路径和片段，不删除查询参数或混淆不同网页版本。
缓存过期保留历史正文并创建新调用记录，不覆盖旧证据。

返回call_id、provider、请求和获取时间、正文payload和SHA-256。
`read_evidence(call_id)` 在缓存过期后仍可离线读回原始成功证据并核对哈希。
命中缓存保留原始获取时间，不伪装为实时获取。任务应在自己的研究证据清单中
保存查询条件、call_id、哈希、原始URL、发布时间、引用段落与截止时间。
账本不保存原始查询文本，只保存规范化请求哈希；需要回放时由应用的私有证据
清单补齐原请求。Search适配器不能剥离源URL、日期或正文；来源发布时间未知
应保持未知。不得把缓存过期当作删除审计证据的依据。

同一未完成请求会拒绝第二次传输，返回pending_or_unknown；调用方稍后查状态，
不可遇到该错误便换provider名称或改写query重试。不同任务可复用成功证据。
所有失败尝试也有上限，防止无结果循环。复杂的语义相似查询去重不在本版本内，
应由规划器对“没有新增证据”设置停止条件。

## 私有状态与故障边界

使用专用0700目录、0600数据库。拒绝路径链接、宽权限、缺失初始化标记、
损坏数据库和不支持的schema。状态不进入Git/npm包。不要手动删除或回滚账本，
它和本地权限不是防恶意所有者篡改的安全边界；备份恢复必须包含整个目录，
并核对恢复点之后的供应商记录。进程中途退出的预留会保留。

执行器应显式设置网络超时、最大响应体、SSRF/目标域名策略、凭据注入和供应商
账单归属，并禁止自动重试；当前模块的transport协议由调用方保证这些条件。
正文落盘前模块限制序列化结果为2MB，但真实transport还必须限制下载过程。
当前同步execute不会强制中断一个卡住的自定义transport。

## ATI 接入建议

ATI直接导入控制层，不要调用Lobster CLI或复用OpenClaw用户凭据。
先以ATI测试用fake port接入，保持candidate/research-only权限；检索成功不授予
验证、策略晋级、数据真值或交易权限。EVA与ATI如需共用月额度，应指向同一
专用账本、统一Policy、固定provider版本标识，并将各自任务ID加产品前缀。
不同进程的SQLite预留与配额事务共用；默认不会后台清理或联网。

ATI把返回的call_id、哈希、来源URL、抓取时间接入自身证据清单和replay/readback，
引用绑定不可变版本。行情继续走已有结构化数据源；仅对明确的时效、引用、
冲突或用户要求使用reason=freshness/citation/conflict/user_requested。
真实transport、服务端自动工具禁用与用量核对属于后续单独接入和验收范围。
