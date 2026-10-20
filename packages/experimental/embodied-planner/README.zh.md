---
description: "用于 PhysicalRSI 多模态规划的无工具实验性 SDK 组合。"
kind: "package-bundle"
---

# @deepseek-ai/dsh-experimental-embodied-planner

[English](README.md) | 中文

## 概述

独立的领域规划 SDK profile，使用可持久化的多模态 Session。PhysicalRSI 提供系统提示词、有序观测、能力目录和结果校验；DSH 提供模型请求生命周期及持久化证据。本实验包尚未取得 benchmark 验收结果。

## 目录

- [使用本包](#use-this-package)
- [理解实现](#understand-the-implementation)
- [延伸阅读](#further-exploration)
- [模型体验](#model-experience)
- [已知限制与后续工作](#known-limitations-and-deferred-work)
- [开发备注](#dev-note)

<a id="use-this-package"></a>
## 使用本包

构建仓库、安装 [Python 适配器](../../../python/embodied/README.zh.md)，再用适配器的源码 profile 安装器创建隔离 profile。公共启动方式为 `dsh --profile embodied-planner`。模型、提示词和图像限制由 Python 部署配置提供；缺少必填值时初始化失败。凭证通过 `PHYSICALRSI_AGENT_API_KEY` 提供。

<a id="understand-the-implementation"></a>
## 理解实现

本 bundle 拥有完整的 Cordis 组合树，包含 SDK stdio、使用 JSON 对象模式的 pi-ai Chat Completions、附件、Agent 内核和未压缩 JSONL 持久化。请求 waterfall 在循环记录请求头前应用配置的 temperature。不注册模型工具，不启用自动压缩、图像卸载、goal 或子 Agent。工作目录校验仍然启用，但不加入提示词上下文。

<a id="further-exploration"></a>
## 延伸阅读

- [Python 适配器](../../../python/embodied/README.zh.md)：部署、回执和恢复执行端的要求。
- [pi-ai provider](../../llm/llm-pi-ai/README.zh.md)：JSON 输出和原始图像传递。

<a id="model-experience"></a>
## 模型体验

### PhysicalRSI 兼容规划

#### 模型可见内容

原生领域系统提示词及有序 `text`/`image_url` 块，不包含 Harness 人设、目录文字或工具 schema。内联图像采用归一化后存储的原始字节，不附加附件句柄；超限输入失败，不静默卸载图像。

#### Token 影响

每次规划尝试发送一次模型请求。兼容模式每次使用新 Session，不调用摘要模型。多模态输入和 JSON 输出消耗所选模型的正常 token。

#### KV Cache 影响

稳定的提示词和目录前缀可由 provider 复用；新观测改变请求后缀。运行时轮换不保证 provider 的缓存复用。

## 已知限制与后续工作

<a id="known-limitations-and-deferred-work"></a>

- JSON 输出当前要求 OpenAI Chat Completions。本 profile 声明非推理模型；启用推理后的对等性需要显式匹配原生配置，并比较实际请求。
- 超出配置限制的图像可能被存储归一化流程拒绝或改变。验收必须比较实际序列化图像和原生请求。
- 在线恢复需要执行端提供停止确认、新鲜观测、前置条件校验和绑定能力。只有 Python 协调器不能证明 RoboDojo 恢复效果或性能提升。

<a id="dev-note"></a>
### 开发备注

本 bundle 保持显式启用。真实模型冒烟测试和原生配对评测，与确定性的协议、状态机测试分别记录证据。
