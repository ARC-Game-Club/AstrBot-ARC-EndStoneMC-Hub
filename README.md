# 弧光 EndStone 消息中枢
[![Codacy Grade](https://app.codacy.com/project/badge/Grade/ac1ce35120504313aee1c2fd0cda7277)](https://app.codacy.com/gh/ARC-Minecraft/AstrBot-ARC-EndStoneMC-Hub/dashboard?utm_source=gh&utm_medium=referral&utm_content=&utm_campaign=Badge_grade)


AstrBot 插件（目录名 `astrbot_plugin_endstone_arc`）：QQ ↔ Minecraft 的 WebSocket 中枢。

仓库：[ARC-Minecraft/AstrBot-ARC-EndStoneMC-Hub](https://github.com/ARC-Minecraft/AstrBot-ARC-EndStoneMC-Hub)

## 配套依赖

本中枢与 MC 子服插件**成对使用**：

| 组件 | 仓库 |
|------|------|
| **本仓库（AstrBot 中枢）** | [AstrBot-ARC-EndStoneMC-Hub](https://github.com/ARC-Minecraft/AstrBot-ARC-EndStoneMC-Hub) |
| **MC 子服 QQ 互通** | [EndstoneMC-ARC-QQ-Sync-Plugin](https://github.com/ARC-Minecraft/EndstoneMC-ARC-QQ-Sync-Plugin) |
| **MC 子服 AI 助手（可选）** | [EndstoneMC-ARC-AI-Helper](https://github.com/ARC-Minecraft/EndstoneMC-ARC-AI-Helper) |

```
QQ / 其他平台
      ↕  AstrBot 平台适配器
本插件「弧光EndStone消息中枢」（WebSocket，默认 :19136）
      ↕  Hub JSON 协议
EndstoneMC-ARC-QQ-Sync-Plugin（各 MC 子服）
```

## 职责

- 监听 WebSocket（默认 `0.0.0.0:19136`），接受各 MC 子服 HubClient
- 群聊 / 指令 ↔ 游戏双向转发
- 跨服事件扇出（join / quit / chat / death / custom）固定开启
- 群指令统一要求 `/mc` 前缀（如 `/mc help`、`/mc cmd stop`），剥前缀后再下发子服
- QQ 绑定数据权威存储（`data.json` / data_rpc）
- 可选同步群名片（`sync_group_card`）
- **MC AI 对话 RPC**（`ai_chat`）：[AI Helper](https://github.com/ARC-Minecraft/EndstoneMC-ARC-AI-Helper) 将玩家消息送入 AstrBot 正式对话管线，人格 / 记忆由 AstrBot 维护。身份映射：已绑定则发送者 ID = **QQ 号**，未绑定则用 **XUID**；不传群号。弧光天星回复同样走弧光护卫关键词检测：命中则拦截原文，并对触发者施加与玩家自己说违禁词相同的处罚。服务角色 `ai_helper` 不占用子服编号、也不会播报开停服
- **MC AI 服务器工具**（`ai_tool`）：给大模型提供 `mc_list_servers` / `mc_list_players` / `mc_get_tps` / `mc_server_info` / `mc_run_command` / `mc_landmarks` / `mc_economy` / `mc_land` / `mc_arc_tp` / `mc_jail_player` / `mc_release_player` / `mc_list_prisoners` / `mc_skyeye_player` / `mc_skyeye_combat` / `mc_skyeye_location` / `mc_stock_leaderboard` / `mc_stock_quote` / `mc_player_ip`。游戏内打在消息来源服；外部对话需插件管理员先发 **`/mc activate`** 激活本会话（会话 ID 可为非数字字符串）。多开服通过 `server` 指定；**天眼查询 `server` 可留空，会搜索全部已连接服务器**（玩家不必在线）。工具会把三档 `permission_level`（助手 / 管理员 / 代理服主）回传给 AI Helper。能识别 QQ 群身份时，入狱 / 天眼 / 银行 / 领地 / 弧光传送 / 任意改世界指令仅管理员；**`mc_landmarks` / `mc_stock_leaderboard` / `mc_stock_quote` / `mc_player_ip` 只读、已激活即可**；**已绑定 QQ 用户**可在求助时对**本人绑定角色**使用 tp / effect / spawnpoint 等自救指令；**未绑定**用户无权执行改世界类工具。

## 安装

将本仓库内容放入 AstrBot：

```text
AstrBot/data/plugins/astrbot_plugin_endstone_arc/
```

然后在 AstrBot 中启用插件，并按下方配置填写群号、端口等。

## 配置要点

| 项 | 说明 |
|----|------|
| `ws_port` | 与 MC `hub_port` / FRP 一致，默认 19136 |
| `auth_token` | 与 MC `hub_token` 一致 |
| `target_groups` | **群白名单**：仅这些群可 `/mc` 互动、转发聊天、接收 MC 广播。QQ 官方填 `group_openid`；NapCat 填数字群号。留空=不限制 |
| `admins` | 管理员 ID；首次启动写入 `admins.json`，之后 `/mc addadmin` 会同步回此项 |
| `super_admins` | 超级管理员 ID；可用 `/mc addadmin` / `/mc deladmin` 任免管理员 |
| `sync_group_card` | 绑定后是否改群名片 |
| `forward_qq_chat` | 是否转发普通群聊到 MC |
| `ai_chat_timeout` | MC AI 助手走 AstrBot 对话的超时秒数，默认 180 |
| `server_aliases` | QQ 里称呼到正式 `server_name` 的映射，例如 `主服` → `弧光冒险模拟生活服务器` |

MC 子服侧（[QQ Sync](https://github.com/ARC-Minecraft/EndstoneMC-ARC-QQ-Sync-Plugin)）只需：`hub_host` / `hub_port` / `hub_token`（及可选互不相同的 `server_name`）。

AI Helper 使用独立连接：`register.role = "ai_helper"`，不会占用子服编号，也不会播报开停服。欢迎包含 `ai_chat: true` 与 `features: ["ai_chat", "ai_tools"]`。需要先升级本中枢，再启用 AI Helper 的 AstrBot 对话。

## MC AI 工具参数一览

大模型通过 AstrBot `llm_tool` 调用；`event` 由框架注入，**不是**模型参数。外部对话须先 `/mc activate`。

`server` 通则：游戏内可留空（打在消息来源服）。QQ / 其它入口多开服时，查询与改世界一般要填名称、编号或别名。**天眼三类工具例外**：`server` 可留空，会搜全部已连接服务器；指定服查空也会自动再搜其它服。

| 工具 | 权限 | 参数 | 必填 | 默认 | 含义 |
|------|------|------|------|------|------|
| `mc_list_servers` | 已激活即可 | （无） | | | 列出已连接 Helper 服 |
| `mc_list_players` | 已激活即可 | `server` | 多开服时要填 | 空 | 目标服名称 / 编号 / 别名 |
| `mc_get_tps` | 已激活即可 | `server` | 多开服时要填 | 空 | 目标服名称 / 编号 / 别名 |
| `mc_server_info` | 已激活即可 | `server` | 可留空查全部 | 空 | 目标服名称 / 编号 / 别名；含硬件且同物理机合并 |
| `mc_run_command` | 管理员；或已绑定用户仅限本人自救 | `command` | 是 | | 不含 `/` 的游戏指令。例：`effect Steve night_vision 30 0 true`；劈闪电：`execute at Steve run summon lightning_bolt ~ ~ ~`。权限受 AI Helper 三档限制；禁止 `stop` / `kill`（代理服主除外） |
| | | `server` | 多开服时要填 | 空 | 目标服名称 / 编号 / 别名 |
| `mc_landmarks` | 已激活即可（只读） | `server` | 多开服时要填 | 空 | 公开地标：出生点 / Warp / 公共领地 |
| `mc_economy` | query 查自己、transfer 发自己的红包：已绑定即可；查他人或 change：仅管理员 | `player_name` / `xuid` | query 查自己时可空 | | 弧光银行（走核心跨服经济接口） |
| | | `sub_action` | 否 | `query` | `query` / `transfer` / `change` |
| | | `delta` / `amount` | transfer/change 时要填 | | transfer 为每人金额；change 正加负减 |
| | | `targets` | transfer 时可选 | | 收款人，逗号分隔 |
| | | `to_online` | transfer 时可选 | | `true` 发给当前服在线且非自己的玩家 |
| | | `server` | 多开服时要填 | 空 | 目标服；银行数据跨服共通 |
| `mc_land` | 仅管理员 | `sub_action` | 否 | `list` | `list` / `info` / `at` |
| | | `player_name` / `xuid` | list 时其一 | | 玩家 |
| | | `land_id` | info 时要填 | | 领地 ID |
| | | `x` / `y` / `z` / `dimension` | at 时要填坐标 | | 坐标与维度 |
| | | `server` | 多开服时要填 | 空 | 目标服 |
| `mc_arc_tp` | 仅管理员 | `player_name` | 是 | | 须在线 |
| | | `sub_action` | 是 | | `home` / `warp` / `pos` |
| | | `home_name` / `warp_name` / `name` | 视 sub_action | | 家名或 Warp 名 |
| | | `x` / `y` / `z` / `dimension` | pos 时要填 | | 坐标 |
| | | `server` | 多开服时要填 | 空 | 目标服 |
| `mc_jail_player` | 仅管理员 | `player_name` | 是 | | 要关押的游戏内玩家名 |
| | | `minutes` | 否 | 空 | 刑期**分钟**数，或 `-1` / `life` / `无期`；空则用服默认一键入狱时长 |
| | | `reason` | 否 | 空 | 入狱原因，写入监狱插件 |
| | | `server` | 多开服时要填 | 空 | 目标服名称 / 编号 / 别名 |
| `mc_release_player` | 仅管理员 | `player_name` | 是 | | 要释放的游戏内玩家名 |
| | | `server` | 多开服时要填 | 空 | 目标服名称 / 编号 / 别名 |
| `mc_list_prisoners` | 已激活即可 | `server` | 多开服时要填 | 空 | 目标服名称 / 编号 / 别名 |
| `mc_skyeye_player` | 仅管理员 | `player_name` | 是 | | 游戏内玩家名；**不要求在线** |
| | | `minutes` | 否 | `30` | 由模型按用户说法换算的回溯**分钟数**。一天=`1440`，一小时=`60` |
| | | `action` | 否 | 空 | 限定行为类型，如 `BlockBreak` / `BlockPlace` / `ActorDamage` / `PlayerDeath` |
| | | `server` | 否 | 空 | **建议留空搜全服**；只查某一台时才填 |
| `mc_skyeye_combat` | 仅管理员 | `player_name` | 是 | | 游戏内玩家名；不要求在线 |
| | | `minutes` | 否 | `30` | 同上，模型换算后的分钟数 |
| | | `server` | 否 | 空 | **建议留空搜全服** |
| `mc_skyeye_location` | 仅管理员 | `x` | 是 | | X 坐标 |
| | | `y` | 是 | | Y 坐标 |
| | | `z` | 是 | | Z 坐标 |
| | | `radius` | 否 | `8` | 半径格数 |
| | | `dimension` | 否 | 空 | 维度，如 `minecraft:overworld`；空表示不限 |
| | | `minutes` | 否 | `30` | 模型换算后的回溯分钟数 |
| | | `server` | 否 | 空 | **建议留空搜全服** |
| `mc_stock_leaderboard` | 已激活即可（只读） | `mode` | 否 | `relative` | `relative` 收益率 / `absolute` 绝对盈亏 |
| | | `top` / `bottom` | 否 | `5` | 前/倒数 N 名 |
| | | `player_name` | 否 | 空 | 只查该玩家名次与盈亏 |
| | | `server` | 多开服时要填 | 空 | 通常填主服（需安装 UpsAndDowns） |
| `mc_stock_quote` | 已激活即可（只读） | `symbol` | 是 | | 股票代码，如 `AAPL` / `BTC-USD` |
| `mc_player_ip` | 已激活即可（只读） | `player_name` | | 当前对话玩家 | 在线玩家连接原始 IP（返回 `ip=`）；助手仅查自己 |
| | | `period` | 否 | `day` | `price` 仅现价；`minute` / `day` / `month` 走势 |
| | | `server` | 多开服时要填 | 空 | 通常填主服 |
| `mc_qq_binding` | query：已激活即可；bind/unbind：仅管理员 | `sub_action` | 否 | `query` | `query` / `bind` / `unbind` |
| | | `player_name` | bind 必填；其它与 qq 二选一 | | 游戏角色名（bind 经群服互通解析弧光核心） |
| | | `qq` | bind 时可选 | | 平台用户 ID：传统 QQ（5～11 位数字）或 QQ 官方 `member_openid`；已 @对方时可留空自动解析；**不要填群名片** |
| | | `force` | bind 时可选 | | `true` 强制改绑（先解旧绑） |

权限补充：QQ 群里「管理员」= 插件管理员 / 超级管理员，或能识别出的群主 / 群管（**以后端校验为准，不是 QQ 群名片**）。超级管理员映射为 AI Helper **代理服主**，普通管理员映射为 **管理员**，已绑定自救用户映射为 **助手**。外部已激活会话会在系统提示里写明当前消息发起者的 Minecraft 权限档位；模型应直接调 `mc_*` 工具，不要先查群身份。`mc_run_command` 对已绑定用户开放 tp / effect / spawnpoint 等自救，且只能打在本人绑定角色上；未绑定用户不能改世界。**已绑定用户**可调用 `mc_economy`（`sub_action=query`）查本人余额；查他人或 change 仍仅管理员。**`mc_qq_binding`** 在中枢本地处理（不经 AI Helper）；游戏内需 OP/管理员权限。游戏内对话会透传 AI Helper 发来的 `permission_level`。

## 启停与连接提示

子服可能被直接杀进程，发不出 `server_stop`。因此 **QQ 与跨服提示都以中枢 WebSocket 连上/断开为准**：

- 连上 → 扇出 `server_connected` + QQ「服务器已启动！」
- 断开 → 扇出 `server_disconnected` + QQ「服务器已停止！」
- 子服自带的 `server_start` / `server_stop` 不再向 QQ/其他服重复播报

AI Helper 的 `ai_helper` 连接**不走**上述开停服播报。

## 更新日志

- **1.7.12**：`/mc info` / `mc_server_info` 由中枢经 QQ Sync `server_info` 拉取硬件信息（CPU/内存/GPU），并按物理机指纹合并去重。需 QQ Sync ≥ 1.0.6。
- **1.7.10**：修复 QQ 侧插件管理员被大模型当成「非群管」而不敢调管理工具：系统提示改为声明权限由工具按消息发起者 ID 自动校验，并注入当前发起者的 Minecraft 权限档位；禁止先查群身份再决定是否调用。
- **1.7.9**：新增只读工具 `mc_player_ip`（在线玩家连接原始 IP），供模型传给下游地理/天气工具。需 AI Helper ≥ 2.1.9。
- **1.7.8**：新增只读工具 `mc_stock_leaderboard` / `mc_stock_quote`，对接主服 UpsAndDowns 模拟美股排行与行情。需 AI Helper ≥ 2.1.7、UpsAndDowns ≥ 0.5.2。
- **1.7.7**：MC AI 对话身份标签按请求者真实身份展示（普通玩家不再被标成「助手/管理员」误导模型）；配合 AI Helper 2.1.6 权限上限/身份分离。
- **1.7.6**：`mc_qq_binding` 支持 QQ 官方机器人 `member_openid` 等字符串平台 ID；可从消息 @ 自动解析；不再强制 5～11 位数字 QQ。
- **1.7.5**：新增 LLM 工具 `mc_qq_binding`（query/bind/unbind），管理员可对话帮人绑定/解绑；bind 仍走群服互通 `core_rpc` 解析玩家。
- **1.7.4**：`/mc 绑定` 改走群服互通 `core_rpc` → QQ Sync → 弧光核心玩家库，不再经 AI Helper。需 QQ Sync ≥ 1.0.3。
- **1.7.3**：绑定曾误走 AI Helper `player_basic_info`；已由 1.7.4 纠正为群服互通路线。
- **1.7.2**：`/mc 绑定` 用弧光核心玩家解析接口确认角色（跨服共通账号，不再只查中枢 `data.json`）；`mc_economy` 支持已绑定用户 `transfer` 从自己账户发红包。需 AI Helper ≥ 2.1.4。
- **1.7.1**：`mc_economy` 查询本人余额不再要求管理员；QQ 已绑定用户可查自己，查他人或 change 仍仅管理员。需 AI Helper ≥ 2.1.3。
- **1.7.11**：强化 `target_groups` 群白名单：仅白名单群可互动/转发；MC→QQ 广播优先发往白名单群。QQ 官方请填当前 `group_openid`（换机器人或重进群后 openid 可能变化）。
- **1.7.0**：对齐 AI Helper ≥ 2.1.1：新增 `mc_landmarks` / `mc_economy` / `mc_land` / `mc_arc_tp`；工具调用回传三档 `permission_level`；系统提示补充弧光核心能力说明。
- **1.6.14**：工具参数整理：查询类去掉无用的 `reason`；入狱时长改为 `minutes`（与天眼同一单位）；`reason` 仅保留为监狱入狱原因。
- **1.6.13**：天眼查询不再要求指定服务器或玩家在线：`server` 留空（或指定服查不到）时会搜索全部已连接服务器。回溯时长由大模型按用户说法写入 `minutes`（一天=1440）。
- **1.6.12**：Minecraft 消息中枢里的弧光天星回复也走弧光护卫：命中关键词则拦截原文，并对触发玩家施加与自己说违禁词相同的处罚（监狱 / 群禁言 / 击杀 / 警告）。身份仍是绑定 QQ，否则 XUID。
- **1.6.11**：适配 QQ 官方机器人新的 `<@member_openid>` 提及格式。此前 `/mc addadmin @群名片` 会把 openid 开头的 `824346` 误当成 QQ 号。同时把运行时管理员列表写回插件配置。
- **1.6.10**：`/mc 绑定` 改为中枢本地处理，不再广播到所有子服。修复群内绑定时子服在 WebSocket 循环上同步等 data_rpc、心跳超时、全部断连的问题。需 QQ Sync ≥ 1.0.2。
- **1.6.9**：QQ 群 AI 求助权限分层：已绑定用户可对本人角色使用 tp / effect / spawnpoint 等自救指令；未绑定用户无权调用 `mc_run_command`（需先 `/mc 绑定`）。需 AI Helper ≥ 1.2.6。
- **1.6.8**：新增持久化管理员/超级管理员权限模型。`/mc addadmin @QQ`、`/mc deladmin @QQ`、`/mc admins` 由中枢本地处理；超级管理员可任免管理员，管理员与超级管理员其它权限一致。
- **1.6.7**：MC 聊天/进服/开停服等广播改为发往所有 `/mc activate` 过的会话；`/mc help`、`/mc servers` 等中枢本地指令回复只回来源会话，不再固定往 `target_groups` 发。
- **1.6.6**：修 `/mc` 指令被 LLM 聊天抢先消费的问题：用高优先级自定义过滤器在唤醒阶段拦截，并兼容 wake_prefix 剥掉开头 `/` 后的 `mc activate` 形式。
- **1.6.5**：新增 `/mc activate`（仅 `admins` 配置的管理员可用），在本会话激活 Minecraft AI 工具并持久化会话 ID（支持非数字 ID）。未激活会话不可调用工具。
- **1.6.4**：Minecraft 工具不再限定 `target_groups` / QQ 群号；其它适配器或无私聊群号的入口也能调用。QQ 群里改世界仍仅管理员。
- **1.6.3**：新增天眼查询工具 `mc_skyeye_player` / `mc_skyeye_combat` / `mc_skyeye_location`，需弧光核心 ≥ 0.8.8 与 AI Helper ≥ 1.2.5。QQ 里与执行指令一样仅管理员。
- **1.6.2**：新增监狱一键入狱工具 `mc_jail_player` / `mc_release_player` / `mc_list_prisoners`，需游戏内监狱插件 ≥ 0.0.2 与 AI Helper ≥ 1.2.4。QQ 里入狱/释放与执行指令一样仅管理员。
- **1.6.1**：工具说明补充劈闪电正确格式 `execute at 玩家名 run summon lightning_bolt ~ ~ ~`，避免写成 `effect 玩家名 summon`。
- **1.6.0**：QQ 群聊也可使用 Minecraft 工具（查在线 / TPS / 信息 / 执行指令）。多开服用 `server` 指定目标；执行指令仅插件管理员、群主和群管。需游戏内 AI Helper 在线。
- **1.5.1**：服务连接被同名替换时写日志，便于排查多开服 AI Helper 互踢。
- **1.5.0**：MC AI 身份改为「绑定 QQ 优先、否则 XUID」，不再传群号；执行指令仍在玩家发消息的那台子服上。需搭配 AI Helper ≥ 1.2.0。
- **1.4.0**：MC AI 会话改为群聊语义：用户 ID 用玩家 XUID、群号用服务器名称，便于记忆插件按 ID 对上人（改名仍是同一人）；新增 `ai_tool` 反向 RPC 与 `mc_list_players` / `mc_get_tps` / `mc_server_info` / `mc_run_command` 工具。需搭配 AI Helper ≥ 1.2.0。
- **1.3.0**：新增 `ai_chat` RPC，可把 Minecraft AI 助手消息送进 AstrBot 对话管线（人格 / 记忆由 AstrBot 维护）；`role=ai_helper` 服务连接不占用子服编号。

## 许可证

[MIT License](LICENSE)
