# 实验性具身 Harness 适配器

[English](README.md) | 中文

本适配器将 PhysicalRSI 的共享领域规划器连接到 DSH 公共 Python SDK。兼容规划保留原生消息和校验，执行仍由 PhysicalRSI 负责。RoboDojo 在线执行端在同步动作检查点丢弃缓存尾部，并通过原生 PhysicalRSI 技能绑定模型选择的恢复方案。目前尚未完成任务成功率或延迟对等性验收。

## 安装与启动

PhysicalRSI 需要 Python 3.11 或更高版本，Node 版本须符合仓库要求。使用构建后的 CLI 和 bundle 前先构建 DSH。以下命令从 DSH 仓库根目录执行；绝对路径需替换为自己的隔离检出目录和 Harness home。

```sh
pnpm install --frozen-lockfile
pnpm run build:artifacts
python -m pip install -e python/sdk-runtime -e python/sdk -e python/embodied
python -m deepseek_harness_embodied.profile --dsh-home /absolute/path/to/home --bundle /absolute/path/to/deepseek-harness/packages/experimental/embodied-planner
```

安装器创建新 profile，拒绝覆盖已有 profile。它链接构建后的工作区 bundle，适用于源码检出目录，不适用于独立发布的 wheel。Node 应用仅通过 `dsh --profile embodied-planner` 启动，适配器不增加其他 Node 入口。

## PhysicalRSI 集成

[集成补丁](integrations/physicalrsi.patch) 提取 `build_planner_messages` 和 `validate_planner_result`，增加显式 `dsh` 后端，并在规划前分配 episode。仅在已审查的本地 PhysicalRSI 源码副本上执行 `git apply --check` 成功后应用。已发布副本及远端修改过的规划器是不同输入；检查失败时需要审查后移植，不能强行覆盖。

[远端源码变体补丁](integrations/physicalrsi-remote.patch) 保留已审查远端规划器的 `api`、`codex` 和 `remote-codex` 后端，通过同一领域构造器和校验器接入 DSH。先核对[源码清单](integrations/physicalrsi-remote.sources.json)中的两个 `before_sha256`，仅在隔离的 PhysicalRSI 副本中应用，再核对两个 `after_sha256`。源码不匹配时需要重新审查移植。

可选的[工作进程关闭补丁](integrations/physicalrsi-worker-close.patch)向健康且空闲的原生技能发送已有的关闭消息，并等待进程退出。正在执行或已失败的工作进程收到终止信号；超时后强制结束，并在进程退出、活动管道调用结束后关闭句柄。原生代码策略设置 `shutdown_timeout_s` 和 `kill_timeout_s` 必须为正有限数，默认分别为 15 和 10 秒。仅对[清单](integrations/physicalrsi-worker-close.sources.json)中哈希匹配的隔离副本应用补丁；所有对照组必须使用相同工作进程实现。

复制[部署配置示例](configs/deepseek-flash.example.json)，设置绝对路径；进行对照时，将原生规划器的 `max_tokens` 配置为相同上限。通过凭证环境导出 `PHYSICALRSI_PLANNER_BACKEND=dsh`、`PHYSICALRSI_DSH_CONFIG=/absolute/path/to/config.json` 和 `PHYSICALRSI_AGENT_API_KEY`。SDK 进程继承凭证，配置和回执不包含密钥。原生传输仍使用 `api` 后端。

## 规划与证据

`PlannerBridge` 同时只允许一次推理，兼容调用使用新 Session。达到配置的调用次数或领域提示词变化时，在空闲状态轮换运行时。`begin_episode` 返回 reset generation；`reset` 使迟到结果失效。过期、截断、无效、超时或重复请求均不会返回可执行计划。超时表示推理结果未知，并关闭所拥有的运行时；它不停止机器人，也不保证停止 provider 计费。

每次请求在 run/episode/request 标识下写入不可覆盖的 `request.json`、`events.json` 和终态 `receipt.json`。回执将输入和模型配置摘要关联到 SDK Session 及校验后的计划。成功事件在返回计划前刷盘；关闭 bridge 后，运行时的完整 JSONL Session 日志完成刷新。runner 仍须关联物理观测、动作、绑定版本及原生结果，才能构成完整 episode 证据。

`ModelSpec.deepseek_reasoning` 通过 DSH 现有 DeepSeek 兼容配置显式选择 `off`、`low`、`high` 或 `max`。不提供该值时保留 provider 默认行为，不代表关闭思考。`off` 发送 `thinking.type=disabled`，且不发送 `reasoning_effort`。对照的每个实验组必须固定相同设置；[官方思考模式指南](https://api-docs.deepseek.com/guides/thinking_mode/)说明了对应 wire 语义。

## 在线恢复执行端

`recovery.py` 定义 `PhysicalRuntime`、`RuntimeState`、`StopReceipt`、`PlanRevision` 和 `RecoveryCoordinator`。使用方必须将每个动作块经过协调器。观测触发异常后先禁止派发，在时限内等待队列和在途动作为零的确认，再获取更新的观测，并根据固定标识和当前前置条件校验替换计划。绑定前记录提交，替换计划必须改变由能力及可执行参数生成、经执行端校验的 `execution_key`，说明文字不参与该摘要。异常后保持禁止派发，只有原生结果才能标记成功。

单环境 `robodojo.py` 执行端管理动作派发及缓存尾部。恢复检查点先禁止派发、丢弃尾部、确认仿真控制队列为空，再请求 `OnlineModel` 回收同步技能工作进程。规划期间不推进仿真。这确认的是动作间隔处的仿真控制静默，不代表真机运动停止。原生控制队列未排空或 RPC 结果未知时，不能替换计划。

动作证据在派发前将数值数组和标量转换为 JSON 值，原始命令仍直接传给原生执行器。不支持的值或非有限数值会在派发记录和物理调用前失败。证据序列化不改变 RoboDojo 消费的动作单位、坐标或数组类型。

`native_binding.py` 定义执行服务及其同步代码策略提供方，负责原生模型创建、已准入计划传递、观测、动作分片、缓存尾部和工作进程清理。重新创建时必须复现已捕获的配置、目录、operation 注册表和记忆；不一致时在动作执行前关闭替代模型。在线规划器消费该服务，并将提供方身份纳入执行绑定版本。 清理异常后永久禁止复用及再次确认停止，即使原生模型已设置关闭标记也不例外；确认工作进程状态后必须替换所属服务。

首次绑定只传递一次初始观测，下一次原生观测更新发生在首个动作完成后，保留原生观测/动作调用节奏。恢复仍必须在停止确认后采集更新观测，再绑定替换计划。

`online_model.py` 保留 PhysicalRSI 的原生目录、记忆、领域提示词构造、校验和绑定实现。请求包含按阶段定义的 JSON Schema；operation 必须是已注册的 ID 字符串，不能附带自行构造的运行时参数。增强决策增加 `execution`：首次 `bind`、同能力 `restart`、不同能力 `switch`，或 `abort`。重启创建新的技能工作进程，依据新 RGB 和本体感知重新计算动作，不重置场景。首次绑定沿用原生校验；缺少重启条件不应将能力排除在初始选择之外。替换计划要求配置的可观测重启前置条件成立；状态未知或夹爪闭合时可以拒绝恢复。执行摘要不含说明文字和历史。本版只接纳同步代码策略工作进程。

在线请求区分历史触发事件和 `current_observation_status`，后者描述停止并重新采集后提供的相机状态。此前丢失 RGB 不代表当前仍无 RGB。恢复观测序列必须新于触发事件，过期数据包在推理前失败。状态只来自所提供的观测，不包含扰动标签或 benchmark 真值。

`recovery_enabled=false` 选择 H1-R 恢复消融。初始规划、观测事件记录、当前原生绑定和原生结果检查仍然启用，但事件不能请求停止或替换计划。原生技能错误仍使 episode 失败。省略设置时解析为 `true`；其他类型在加载配置时失败。仅在关闭恢复时允许空的重启条件映射。关闭恢复不能证明某项能力支持安全重启。

两个进程均通过 `DSH_ROBODOJO_ONLINE_CONFIG` 使用[在线配置](configs/robodojo-online.example.json)。`scripts/robodojo_policy.py` 根据显式包含 `port`、`ready_file` 和 `model` 的 JSON 部署配置启动 RoboDojo 本地 WebSocket 服务；`scripts/robodojo_client.py` 从隔离输出目录向原生 RoboDojo main 提供在线评测执行端。两者均要求可导入已审查的 PhysicalRSI 和 RoboDojo Python 模块。观测事件检测器检查 RGB 缺失、指令变化，以及配置窗口内关节收到运动命令却没有移动；运动规则要求配置的观测和命令前置条件成立；pickup 示例要求两侧夹爪均保持张开，因此正常闭合接触不会触发。事件保留实测关节运动量和命令误差。不从 benchmark 状态推断抓取失败或目标位姿。

初始规划和恢复请求共用一个持久化 DSH Session。episode 历史受修订次数、事件次数、provider 上下文限制和共同截止时间约束；活动链路中需要轮换运行时时直接失败，不丢弃历史。自动上下文摘要和离线历史到 skill 准入仍单独推进。不可覆盖的 runner 记录关联观测事件、停止确认、新观测序列、计划提交、原生绑定、已派发动作及原生终态；请求回执关联实际传感器输入和 Session。

`CallBudget` 在每次模型轮次前预留不可覆盖的槽位，跨进程和重启共享。失败或结果未知的尝试也占用槽位。共用预算的运行必须使用相同的不可变额度；工具和 provider 重试保持禁用。记录的本地模型测试使用独立账本。额度耗尽后禁止继续规划和派发动作。

## 配对开发预实验

复制[预实验规格](configs/paired-pilot.example.json)，设置绝对部署路径，并导出 `PHYSICALRSI_AGENT_API_KEY`。每个指定 case 按登记顺序分别运行原生 API 规划（N）、DSH 兼容规划（H0）及在线恢复（H1），各自独立重置仿真。N/H0 保留原生 planner 及 skill 执行；两个 RoboDojo 脚本通过 `--mode native` 选择原生路径。示例使用此前已探索的 pickup case，属于开发数据。

```sh
python python/embodied/scripts/paired_pilot.py --spec /absolute/path/to/pilot.json
```

回环转发器使用系统分配端口及私有 token，转发至 DeepSeek 官方端点，并保留收到及发出的请求、响应字节、全部提供方尝试、耗时和用量。规格中的 `deepseek_reasoning` 对各组统一选择 `off`、`low`、`high` 或 `max`；缺省时保留原预实验的 `off`。其他消息及图像保持原样。冲突的模型或输出配置在推理前拒绝。原生传输重试分别消耗提供方名额。缺失用量保留为未知。子进程只收到转发 token；真实 API key 不写入证据。

预实验保留原生结果文件、异常、进程期限、动作数及初始可观测状态/RGB 摘要。记录的评测函数耗时包含 skill 冷启动及各路径自身的收尾，不能用于正式原生终止时延的验收。失败 episode 保留且不自动重跑。运行顺序属于开发配置，不是统计随机化协议。执行器拒绝非 development 分区，且不宣布 G1–G4 通过；正式验收还要求经确认的最强原生配置、完整 S/L/U/X 覆盖、独立保留场景、功效计算、预登记配对区间及共同费用计量。

`native-terminal.json` 单独记录首次既有原生终止检查及首次动作派发。它不增加环境查询，忽略终止前的 success 标志，并将评测收尾排除在完成时间外。冷启动仍计入；仅此记录不能完成要求中的预热或资源控制验收。

可选的绝对路径 `online_config` 和 `policy_python` 为所有组选择经审查的恢复模板和策略解释器。省略时保留仓库示例和原有策略环境。启动器在创建分配或预留模型调用前验证两者，将模板一次性保存在 `resolved-inputs.json`，并记录模板及解释器文件的 SHA-256。每个 episode 的证据路径和统一期限替换模板中的占位值。检测阈值与重启条件保持为已捕获配置。可执行文件哈希不代表已安装依赖，也不能证明学习策略可用。

case 可同时固定 `layout_id` 和 `layout_sha256`。该索引选择 CLI seed 目录中按数字排序的原生布局；仅更改 `--seed` 不会选择目录内其他文件。客户端验证原文件，保留逐字节相同的不可覆盖快照，并让原生加载器读取快照；保留原生恢复排除规则。报告按 case 检查所选索引和快照摘要；缺失选择证据不能支持固定布局 case 成功。布局文件和标识不进入模型输入。

`scripts/generate_layout_cohort.py` 在隔离工作进程中复用已安装的 PhysicalRSI 配置目录生成器和输入限制，不调用模型、不读取 benchmark 布局。生成前冻结分配、采样器源码及按分区隔离的种子流。几何生成不代表物理或 benchmark 分布验证。validation 场景集要求已冻结的对照 SHA-256。开发 case 的 `fresh_layout` 包含绝对路径 `manifest`、其 `manifest_sha256`、原场景集 `index` 及 `split=development`，同时设置 `layout_id=0` 和场景文件 SHA-256。原生新布局执行端将该已验证条目绑定到索引零，保留的选择记录仍包含原场景集标识。开发预实验拒绝 validation 场景集。文件被改动、场景重复、场景集不完整或缺失回执时不能进入绑定。

`scripts/paired_pilot_report.py /absolute/path/to/pilot-output` 在全部指定组结束后输出描述性报告。缺失或重复分配、源码版本混用会拒绝生成报告。原生结果、异常、共同期限及提供方预留名额分别检查；退出码为零的失败仍计入分母，缺失提供方回执使其用量保持未知。结果文件摘要支持与保留证据核验。报告始终将统计验收标记为证据不足。

[验收记录](evaluations/2026-10-21-acceptance/report.zh.md)包含固定 case 的开发结果、证据摘要及尚未完成的正式验收条件。

## 固定输入 Thinking 对照

`scripts/thinking_comparison.py` 校验证据归档的 SHA-256，在推理前冻结全部已存提供方请求及随机配对分配，再向 DeepSeek 官方端点逐一发送一次。两组保留相同消息文本、图像和历史，统一使用非流式传输。token 上限、重复次数、并发数、超时和种子均由 CLI 显式配置。开启 Thinking 使用 `high` 强度；两组请求保留 temperature，但提供方在开启 Thinking 时忽略该参数。可通过 `--key-file` 提供原始密钥文件，凭证不进入保存的请求或协议。

判定器仅接纳已检查的单能力 pickup 目录，检查原生字段类型、已注册 ID 及初始/恢复执行选择，不评价物理任务成功或理由质量。缺失、失败、截断及无效请求均保留在分母。`--online-variants` 增加独立冻结的原始/澄清在线提示词对照，不改变历史轮次或图像。`scripts/thinking_comparison_report.py` 从原始响应重新计算决策，并拒绝与冻结分配不符的请求、回执或响应。在这些开发快照上的重复调用不能完成 G1–G4 验收。

[Thinking 诊断](evaluations/2026-10-21-thinking/report.zh.md)记录模型配置及提示词对照、失败机制和证据摘要。

## 验证

单元测试覆盖请求生命周期及停止、观测、提交失败。构建后 profile 测试使用记录的本地模型响应，验证实际序列化的领域文本和 PNG 字节，并检查持久化 Session 证据。该测试要求构建完成，并允许绑定本地端口。

```sh
PYTHONPATH=python/embodied/src DSH_EMBODIED_INTEGRATION=1 python -m pytest python/embodied/tests
```

`scripts/smoke.py` 对同一份生成输入执行一次原生和一次 DSH 真实推理，不执行机器人动作，要求显式部署配置和凭证。这仅是协议证据；G1–G4 必须通过预注册的 RoboDojo 配对实验，不能通过此冒烟测试判定达标。
