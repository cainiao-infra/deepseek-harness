# 实验性具身 Harness 适配器

[English](README.md) | 中文

本适配器将 PhysicalRSI 的共享领域规划器连接到 DSH 公共 Python SDK。兼容规划保留原生消息和校验，执行仍由 PhysicalRSI 负责。恢复协调器提供显式执行端接口，但原生 RoboDojo runner 尚未提供物理停止确认。目前尚未完成任务成功率或延迟对等性验收。

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

复制[部署配置示例](configs/deepseek-flash.example.json)，设置绝对路径；进行对照时，将原生规划器的 `max_tokens` 配置为相同上限。通过凭证环境导出 `PHYSICALRSI_PLANNER_BACKEND=dsh`、`PHYSICALRSI_DSH_CONFIG=/absolute/path/to/config.json` 和 `PHYSICALRSI_AGENT_API_KEY`。SDK 进程继承凭证，配置和回执不包含密钥。原生传输仍使用 `api` 后端。

## 规划与证据

`PlannerBridge` 同时只允许一次推理，兼容调用使用新 Session。达到配置的调用次数或领域提示词变化时，在空闲状态轮换运行时。`begin_episode` 返回 reset generation；`reset` 使迟到结果失效。过期、截断、无效、超时或重复请求均不会返回可执行计划。超时表示推理结果未知，并关闭所拥有的运行时；它不停止机器人，也不保证停止 provider 计费。

每次请求在 run/episode/request 标识下写入不可覆盖的 `request.json`、`events.json` 和终态 `receipt.json`。回执将输入和模型配置摘要关联到 SDK Session 及校验后的计划。成功事件在返回计划前刷盘；关闭 bridge 后，运行时的完整 JSONL Session 日志完成刷新。runner 仍须关联物理观测、动作、绑定版本及原生结果，才能构成完整 episode 证据。

## 在线恢复执行端

`recovery.py` 定义 `PhysicalRuntime`、`RuntimeState`、`StopReceipt`、`PlanRevision` 和 `RecoveryCoordinator`。使用方必须将每个动作块经过协调器。观测触发异常后先禁止派发，在时限内等待队列和在途动作为零的确认，再获取更新的观测，并根据固定标识和当前前置条件校验替换计划。绑定前记录提交，替换计划必须改变由能力及可执行参数生成、经执行端校验的 `execution_key`，说明文字不参与该摘要。异常后保持禁止派发，只有原生结果才能标记成功。

不能把 `reset()` 或 `close()` 包装成停止回执来启用恢复。仿真器或机器人 runner 必须统一管理停止、观测序列和动作派发。多轮增强规划、观测事件检测、runner 集成、关联任务结果的评测，以及离线历史到 skill 的准入流程，仍按设计文档单独推进。

## 验证

单元测试覆盖请求生命周期及停止、观测、提交失败。构建后 profile 测试使用记录的本地模型响应，验证实际序列化的领域文本和 PNG 字节，并检查持久化 Session 证据。该测试要求构建完成，并允许绑定本地端口。

```sh
PYTHONPATH=python/embodied/src DSH_EMBODIED_INTEGRATION=1 python -m pytest python/embodied/tests
```

`scripts/smoke.py` 对同一份生成输入执行一次原生和一次 DSH 真实推理，不执行机器人动作，要求显式部署配置和凭证。这仅是协议证据；G1–G4 必须通过预注册的 RoboDojo 配对实验，不能通过此冒烟测试判定达标。
