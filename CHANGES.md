# CHANGES

## 2.0.0（开发中）

整体重写，与 1.x 不兼容。1.x 历史见 git 记录。

### 2.0.0.dev1（当前）—— 结构解耦

- **插件 API 独立成 `gensokyoai.apis`**：`@scene` / `@tool` / `SceneContext` 从 `api.py` 搬过去；
  旧路径 `gensokyoai.api` 改 re-export，外部插件不需要改代码
- **删除内置 `chat` 场景**：`--scene` 不再有默认值，不给就是裸会话（默认连发 3 泡、
  默认唤醒策略、直接经 mouth 出口）；真实场景走插件
- **`Loop` 拆成三个协作者**（都在 `gensokyoai/engine/`）：
  - `Loop`：调度壳——唤醒策略、退避重试、失败批次锁定、放弃判断
  - `Turn`：一次回合的状态机（取未读 → 问模型 → 执行动作）
  - `Refresher`：前缀缓存同步与压缩，跟"回不说话"无耦合
- **公开访问器补齐**：`loop.turn` / `loop.refresher` / `loop.is_awake` / `loop.scene` /
  `loop.is_repeat` / `loop.should_give_up`；测试不再依赖任何私有成员

### 2.0.0.dev0

### 架构

- 房间（Room）为唯一事实源：append-only 事件流，器官只依赖房间、互相不见面
- 旧的大脑层 / 决策管线 / 喊话式工具提取 / 注意力档位全部移除
- Mind 改为原生 tool_call：模型用 `send_message / wait / read_events` 动作说话，正文直接丢弃
- 场景插件系统：`@scene` / `@tool` 声明，entry points 或目录扫描挂载，内置 chat 场景
- 角色卡简化为三字段（version / name / detail），设定全在 detail；开场白功能移除
- 模型适配层：厂商无关的消息（文字/图片块 + 工具调用），openai / anthropic 两套适配器，
  按 usage 计费（价格表前缀匹配，`/doctor` 看得到花费）

### 持久化

- 房间事件落盘（ayafileio，append-only JSONL），重启回放，单文件超限轮转
- 记忆 JSONL：bigram 召回，条数过压力线时调模型压缩成概要

### 运行时

- BgTasks：后台任务登记处（一次性 / 周期），统一监督、退出前 drain
- DMN 静息整理：房间静默超阈值才跑整理
- ws 网关：token 校验、房间数上限、ReplyStyle 按连接声明
- 斜杠命令：/doctor、/help，权限分级
- Loop 单轮失败退避重试，模型连接全进程复用
