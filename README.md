# 栗子提醒 · AstrBot

把栗子 QQ 机器人项目 `liziqqbotpy` 的提醒功能迁移为 AstrBot 插件，适配 QQ 官方 WebSocket、QQ 官方 Webhook 和 OneBot v11。插件要求 AstrBot `>=4.16,<5`，Python 3.10 及以上。

[开源仓库](https://github.com/itxiaohui66/astrbot_plugin_tixing) · [下载安装包](https://github.com/itxiaohui66/astrbot_plugin_tixing/releases/latest) · [报告问题](https://github.com/itxiaohui66/astrbot_plugin_tixing/issues)

## 安装

推荐在 AstrBot WebUI 的插件页选择通过仓库链接安装，填入：

```text
https://github.com/itxiaohui66/astrbot_plugin_tixing
```

也可以从 [GitHub Releases](https://github.com/itxiaohui66/astrbot_plugin_tixing/releases/latest) 下载 `astrbot_plugin_tixing-v1.2.2.zip`，在插件页上传安装。手动安装时，把 ZIP 中的 `astrbot_plugin_tixing` 文件夹解压到 AstrBot 的 `data/plugins/` 下，然后重启或重载插件。运行文件 `main.py`、`metadata.yaml` 和 `_conf_schema.json` 必须直接位于该插件文件夹中。

安装时 AstrBot 会读取 `requirements.txt` 安装 `tzdata`。配置默认采用北京时间 `Asia/Shanghai`；每人在每个群或私聊最多 10 个活跃提醒，每 30 秒扫描一次。确认机器人已经接入 QQ 官方适配器，群内使用 `@机器人 /tx help` 查看帮助。

## 完整功能对照

| 旧项目功能 | 本插件行为 |
| --- | --- |
| `/tx X分钟/X小时/X天 内容` | 全部保留，支持原来的 `minute/hour/day/min/h/d`，另支持复数和“后” |
| 每天提醒 | `每天9:00`、`每天 9:00` 均支持 |
| 每周提醒 | 支持一到六、日、天、1–7；修正同一天未来时刻错误跳到下周的问题 |
| 提醒自己 / @ 他人 | 到期真正 @ 对象；官方 QQ 保存成员 OpenID，OneBot 保存 QQ 号 |
| 每人最多 10 条 | 按平台 + 群/私聊 + 创建者计算，仅统计活跃任务；支持配置 |
| 30 秒轮询 | 可配置；通常在到期后一个扫描周期内发送，拥塞时可能更晚 |
| 数据库存储、重启恢复 | SQLite 位于 AstrBot `data/plugin_data/astrbot_plugin_tixing/reminders.db` |
| 投递失败重试、达到阈值取消 | 普通投递错误默认重试间隔 60 秒，连续 3 次失败自动取消；主动消息无权限时保留任务，不增加失败次数 |
| `/cxzd` 重启提醒调度器 | 保留；使用 AstrBot 管理员或插件授权管理员，不丢任务 |
| 旧帮助中的 `/提醒`、`/remind` | 实现为 `/tx` 别名 |
| 旧帮助中的今天、明天、指定日期 | 原代码未实现的格式已补齐，并支持后天 |
| 旧代码提示“先取消一些”但没有取消入口 | 新增列表、详情、历史、按编号取消和取消自己的全部任务 |

## 创建提醒

```text
/tx 30分钟 开会
/tx 1小时后 开会
/tx 2天 检查服务器
/tx 30min 吃饭
/tx 10:00 今天的会议
/tx 今天 10:00 今天的会议
/tx 明天10:00 明天的会议
/tx 后天 10:00 后天的会议
/tx 2026-12-31 20:00 跨年
/tx 每天9:00 早起打卡
/tx 每周一 9:00 周报
/tx 每周7 20:00 总结
/tx 1小时 开会 @某人
```

时间和内容之间要有空格。内容可以包含空格、换行，默认最多 1000 个字符。单次 `HH:MM` 表示今天该时刻，已经过去则提示改用未来时间；每天和每周自动计算下一次。提醒间隔必须大于 0，小时范围 0–23，分钟范围 0–59。

到期后在创建时的群或私聊发送提醒。QQ 官方默认使用**主动发送**，无需等待群成员再次 @机器人。群主或管理员需在本群的机器人权限设置中开启“允许机器人主动发送消息”；入群或首次使用提醒命令时会提示检查该开关。

循环任务按创建时的时区续期，修改配置时区只影响新任务。离线或等待权限期间错过多次循环，恢复投递时补发一次并续到下一次未来时刻，避免刷屏。一次性逾期提醒也会保留并补发；如果适配器还未加载或运行，不增加失败次数。OneBot 的发送方式保持原样。

## 管理命令

| 命令 | 行为 |
| --- | --- |
| `/tx help` | 全部帮助 |
| `/tx list [页码]` / `/cktx` / `/提醒列表` | 当前会话中自己创建的活跃提醒 |
| `/tx history [页码]` | 自己的全部记录，包括完成、取消、失败 |
| `/tx detail 编号` | 查看全文、时间、状态、失败次数和最近错误 |
| `/tx cancel 编号` / `/qxtx 编号` / `/取消提醒 编号` | 取消自己创建的任务 |
| `/tx cancel all` | 仅取消自己在当前会话创建的所有活跃任务 |
| `/tx retry 编号` | 恢复投递失败后自动取消的任务，或重试待投递任务；仍检查当前会话、创建者和数量限制 |
| `/tx identity` | 当前平台、群、用户标识和本群授权管理员标识 |
| `/tx status` | 调度器状态、当前会话活跃数量、时区、扫描间隔和最近错误 |
| `/tx permission` / `/tx 权限` | 当前官方群的权限记录和开启说明 |
| `/tx permission check` | 实际发送一条主动测试消息；成功后立即唤醒等待权限的到期提醒 |
| `/cxzd` / `/重启提醒` | 管理员重启提醒调度器 |
| `/tx list all [页码]` | 管理员查看当前会话所有人的活跃任务 |
| `/tx history all [页码]` | 管理员查看当前会话所有人的历史 |

管理员可以取消当前会话其他人的单条任务、查看详情。按编号操作也受平台和群隔离限制。群管理员并不会自动获得机器人管理权限；使用 AstrBot 管理员配置，或在插件配置中加入 `admin_ids`、`group_admin_ids`。`group_admin_ids` 的格式为 `平台ID|群OpenID|用户OpenID`，可从 `/tx identity` 复制。

列表每页 2 条，长内容显示预览，可用 `detail` 查看全文。主动发送无权限时，状态显示“等待主动消息权限”，不会因此自动取消。普通投递错误超过失败阈值后仍会自动取消，可以在 `history` 中查到原因。

从 v1.0.0 / v1.1.0 更新后，数据库自动升级并保留原提醒。v1.2.0 默认恢复主动发送，旧 `official_delivery_mode` 配置由 `proactive_reminders` 替代。已经因“主动消息失败，无权限”自动取消的任务不会被擅自恢复；先开启权限并验证，再在原群中发送 `/tx retry 2`、`/tx retry 3` 等对应编号恢复。等待中的提醒、未到期提醒仍占用活跃任务额度。

## 官方 QQ 的对象和发送权限

官方 QQ 下发 `group_openid`、`member_openid`、`user_openid`；普通 QQ 号、群号不能直接代替它们。提醒他人优先解析真实 @ 消息段或平台提供的 mentions。若官方事件没有提供被 @ 成员的标识，让对方先在本群 `@机器人 /tx identity`，再使用：

```text
/tx 30分钟 开会 @对方的用户标识
```

v1.2.2 修复 SDK 的非空 `mentions` 列表丢失成员字段时覆盖完整原始数据，导致“提醒别人”退回提醒发起人的情况。解析会合并原始 QQ payload 和 SDK 信息，并从当前消息的原始正文恢复被适配器省略的真实 @，包括命令前的 @；不从引用消息中选取接收者。已知机器人及其别名始终排除在提醒对象之外。收到无法解析的额外 @ 元数据时会提示“未创建提醒”，不静默改成提醒自己。

创建回复始终显示“提醒对象”：自己显示“你自己”，他人显示昵称或成员标识。发起人仍管理该任务，到期只 @ 保存的接收者。旧版已错误保存为提醒自己的任务无法从数据库还原原定接收者，请 `/tx cancel 编号` 后重新设置，并检查创建回复中的提醒对象。

插件会记住本群里机器人实际收到的成员消息，名字唯一且官方提供昵称时也支持 `@昵称`。不同群里的成员记录不会混用，重名时要求明确标识。OneBot 模式会查询目标是否是本群成员。私聊只能提醒自己。

v1.2.0 默认采用 **主动发送**。新群的 `GROUP_ADD_ROBOT` 事件到来时，插件使用该真实事件的 `event_id` 回复管理员开启说明；若事件缺失、发送失败或插件是后来才安装的，则在首次提醒命令回复中附加说明。提示按平台和群持久化，成功提示后不会在每次命令中重复。

插件接收官方 SDK 的 `GROUP_MSG_RECEIVE` / `GROUP_MSG_REJECT` 事件，分别记录允许和关闭状态。已收到允许事件或实际发送成功的群不会被误报为未开启；没有证据时仅提示“确认已开启”。退群及重新入群事件也会更新记录。生命周期监听支持 WebSocket 和 Webhook，保留其他组件已有的回调，重载时移除本插件监听。

管理员开启后，在原群发送 `@机器人 /tx permission check`。验证消息不带 `msg_id` 或 `event_id`，接口返回消息 ID 后才确认主动投递成功，并唤醒等待权限的到期任务。群开关允许后仍以 QQ 接口实际返回为准；应用本身的权限、发送额度等限制会显示在验证结果或提醒记录中。官方事件依据 [腾讯 botpy 群管理事件](https://github.com/tencent-connect/botpy/blob/master/botpy/manage.py) 和 [事件解析器](https://github.com/tencent-connect/botpy/blob/master/botpy/connection.py)，消息接口依据 [腾讯官方消息收发文档](https://github.com/tencent-connect/bot-docs/blob/main/docs/develop/api-v2/server-inter/message/send-receive/send.md)。

主动消息被 QQ 拒绝且错误指向权限时，插件保留任务、不增加失败次数，并每 5 分钟重试。若明确收到管理员拒绝消息或退群事件，则暂停主动投递，等待允许事件或主动验证成功。允许事件和验证成功会使等待权限的到期任务立即进入扫描；接口仍失败时继续保留。命令回复使用当前消息进行被动回复，因此也能在权限未开时显示设置说明。

如需使用被动回复，可关闭 `proactive_reminders`。这时只有存在有效的近期用户消息和回复额度才投递，超出窗口则等待下一条同会话消息。群、频道、频道私信保守采用 4 分钟窗口，C2C 59 分钟；每条消息最多投递 3 条提醒，给命令回复和其他插件预留额度。回复上下文按平台、场景和目标隔离；重复事件不会刷新有效期或额度，受限时不会切换为主动发送。被动模式无法保证长时间后准点提醒。

v1.2.1 修复官方群到期提醒把 `<qqbot-at-user id="OpenID" />` 原样显示的问题：群提醒改用 `msg_type=2`，将原生 @ 标签放入 `markdown.content`，由 QQ 渲染为可点击的 @ 用户。主动和被动投递均使用该格式，仍保留原本的发送权限和回复上下文。提醒内容中的 Markdown 标点作为普通文字显示，@ 标签单独放在正文开头。

默认使用原生 Markdown。如果 QQ 返回“不允许发送原生 markdown”等错误，任务显示“等待 Markdown 配置”，保留并每 5 分钟重试，不会改回显示长串标签的普通文本消息。原生 Markdown 权限与群管理员的主动消息开关是两项不同设置；需在机器人应用中开通原生 Markdown，或配置已有的获批 Markdown 模板。

使用模板时，在插件配置填写 `official_markdown_template_id`；模板正文应包含 `{{.content}}`，并将 `official_markdown_parameter` 设置为 `content`。如果模板的变量名是 `body`，则正文使用 `{{.body}}`，参数设置为 `body`。插件向该变量填入完整的 @ 标签、提醒内容及编号。开通能力或配置模板后，可 `/tx retry 编号` 立即重试。

解析被 @ 成员时，官方群优先使用该成员的本群 `member_openid`，并统一消息段和原始标签里的别名；频道仍使用频道成员 ID。未提供昵称时，提醒正文不会把创建者 OpenID 当作昵称。群、C2C、频道、频道私信分别走对应 SDK 接口；私聊不发送 @ 标签，OneBot 继续发送原生 `at` 消息段。命令回复仍由 AstrBot 当前事件的配置发送。

格式依据 [腾讯官方文本交互与 @ 协议](https://github.com/tencent-connect/bot-docs/blob/main/docs/develop/api-v2/server-inter/message/trans/text-chain.md) 和 [Markdown 消息协议](https://github.com/tencent-connect/bot-docs/blob/main/docs/develop/api-v2/server-inter/message/type/markdown.md)。客户端最终显示需在实际 QQ 环境确认。

旧数据库里的数字 QQ 号/群号无法自动换算为官方 OpenID，所以本次迁移的是功能，没有修改或直接导入旧数据库。更换 QQ 应用或 AstrBot 平台 ID 时，原 OpenID/路由可能失效，需要重建对应提醒。备份时停止 AstrBot 后复制整个 `data/plugin_data/astrbot_plugin_tixing/`。

正常重载和并发扫描由数据库租约避免重复发送，卸载时会等待正在发送的任务完成并保存结果。数据库和 QQ 网络接口无法组成同一事务；如果进程在“QQ 已收消息但本地尚未保存”时被强制结束，或服务器收消息后响应超时，恢复重试仍可能重复。建议只在一个 AstrBot 实例里运行该插件。

## 配置

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `proactive_reminders` | `true` | 默认到点主动投递并提示群权限；关闭后使用被动回复 |
| `official_markdown_template_id` | 空 | 官方群提醒默认原生 Markdown；应用不支持时填写已获批的模板 ID |
| `official_markdown_parameter` | `content` | 模板中接收完整提醒正文的变量名，未填模板时忽略 |
| `timezone` | `Asia/Shanghai` | IANA 时区 |
| `max_per_user` | `10` | 当前会话每人最多活跃任务 |
| `check_interval_seconds` | `30` | 扫描周期，最小 1 秒 |
| `max_delivery_failures` | `3` | 失败取消阈值，最小 1 |
| `retry_interval_seconds` | `60` | 失败重试间隔，最小 1 秒 |
| `max_content_length` | `1000` | 内容长度，最大配置值 1500 |
| `admin_ids` | `[]` | 额外机器人管理员用户标识 |
| `group_admin_ids` | `[]` | 当前群的授权管理员标识 |

配置保存后重载插件生效，任务保留。

## 验证和打包

```text
python -m pip install -r requirements.txt -r requirements-dev.txt
python -m pytest -q
ruff check .
ruff format --check .
python tools/package.py
```

自动化测试使用真实 SQLite 和时间解析；AstrBot 主机边界和 QQ 发送接口使用测试替身，覆盖时间格式、上限、群隔离、成员识别、失败重试、循环补发、权限、入群提示、首次使用去重、允许/拒绝事件、回调恢复、重载，以及回复时效、额度、序号、重复事件、即时唤醒和数据库升级。GitHub Actions 会在 Linux（Python 3.10、3.12）和 Windows（Python 3.12）上运行测试、代码检查和打包。真实 QQ 投递仍需在实际部署的 AstrBot 和账号中验证。

安装后可用以下流程确认部署环境：

1. 首次命令或入群时确认收到权限开启提示；管理员开启“允许机器人主动发送消息”后，`@机器人 /tx permission check` 确认主动验证成功。
2. `@机器人 /tx 1分钟 测试提醒`，确认返回任务编号，到期收到真正 @ 的通知；`/tx history` 检查状态，失败时查看原因和日志。
3. `/tx 5分钟 重启测试`，重载插件后用 `/tx list` 确认仍在，再 `/tx cancel 编号` 确认取消。
4. `@机器人 /tx 1分钟 他人提醒测试 @另一成员`，确认创建回复中的提醒对象是对方；到期只 @ 对方，不 @ 发起人。若平台缺少标识，让对方先 `/tx identity` 再使用本群用户标识。
5. `@机器人 /tx 6分钟 主动提醒测试`，期间不再 @机器人，确认到期仍收到提醒。若因权限不足显示等待，开好权限并再次验证；旧版已自动取消的任务需先 `/tx retry 编号` 恢复。

## 来源

功能参考作者的原项目 `liziqqbotpy`，主要为 `plugins/reminder.py`、`core/event.py`、`core/scheduler.py`、`database/models.py`、`database/sqlite.py` 和 `plugins/help.py`。原项目未提供公开仓库链接。本插件保留提醒行为，重写 AstrBot 入口、存储和平台适配，不依赖旧项目的 NoneBot、NapCat 或全局调度器。

接口依据 [AstrBot 插件开发文档](https://docs.astrbot.app/dev/star/plugin-new.html)、[QQ 官方 WebSocket 适配器源码](https://github.com/AstrBotDevs/AstrBot/blob/master/astrbot/core/platform/sources/qqofficial/qqofficial_platform_adapter.py)、[Webhook 适配器源码](https://github.com/AstrBotDevs/AstrBot/blob/master/astrbot/core/platform/sources/qqofficial_webhook/qo_webhook_adapter.py) 和 [腾讯 botpy SDK](https://github.com/tencent-connect/botpy/blob/master/botpy/api.py)。

## 开源与贡献

本项目采用 [MIT License](LICENSE)。欢迎提交 Issue 和 Pull Request；修改代码后请运行上面的测试、格式检查和打包命令。请在报告问题时说明 AstrBot 版本、QQ 适配器类型、触发命令和经过脱敏的错误日志。
