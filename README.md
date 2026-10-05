# 栗子提醒 · AstrBot

把栗子 QQ 机器人项目 `liziqqbotpy` 的提醒功能迁移为 AstrBot 插件，适配 QQ 官方 WebSocket、QQ 官方 Webhook 和 OneBot v11。插件要求 AstrBot `>=4.16,<5`，Python 3.10 及以上。

[开源仓库](https://github.com/itxiaohui66/astrbot_plugin_tixing) · [下载安装包](https://github.com/itxiaohui66/astrbot_plugin_tixing/releases/latest) · [报告问题](https://github.com/itxiaohui66/astrbot_plugin_tixing/issues)

## 安装

推荐在 AstrBot WebUI 的插件页选择通过仓库链接安装，填入：

```text
https://github.com/itxiaohui66/astrbot_plugin_tixing
```

也可以从 [GitHub Releases](https://github.com/itxiaohui66/astrbot_plugin_tixing/releases/latest) 下载 `astrbot_plugin_tixing-v1.0.0.zip`，在插件页上传安装。手动安装时，把 ZIP 中的 `astrbot_plugin_tixing` 文件夹解压到 AstrBot 的 `data/plugins/` 下，然后重启或重载插件。运行文件 `main.py`、`metadata.yaml` 和 `_conf_schema.json` 必须直接位于该插件文件夹中。

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
| 投递失败重试、达到阈值取消 | 默认重试间隔 60 秒，连续 3 次失败自动取消；保留错误原因 |
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

到期后在创建时的群或私聊发送提醒。循环任务按创建时的时区续期，修改配置时区只影响新任务。离线时错过多次循环，会在恢复后补发一次并续到下一次未来时刻，避免刷屏。一次性逾期提醒在恢复后补发；如果适配器还未加载或运行，不增加失败次数。

## 管理命令

| 命令 | 行为 |
| --- | --- |
| `/tx help` | 全部帮助 |
| `/tx list [页码]` / `/cktx` / `/提醒列表` | 当前会话中自己创建的活跃提醒 |
| `/tx history [页码]` | 自己的全部记录，包括完成、取消、失败 |
| `/tx detail 编号` | 查看全文、时间、状态、失败次数和最近错误 |
| `/tx cancel 编号` / `/qxtx 编号` / `/取消提醒 编号` | 取消自己创建的任务 |
| `/tx cancel all` | 仅取消自己在当前会话创建的所有活跃任务 |
| `/tx identity` | 当前平台、群、用户标识和本群授权管理员标识 |
| `/tx status` | 调度器状态、当前会话活跃数量、时区、扫描间隔和最近错误 |
| `/cxzd` / `/重启提醒` | 管理员重启提醒调度器 |
| `/tx list all [页码]` | 管理员查看当前会话所有人的活跃任务 |
| `/tx history all [页码]` | 管理员查看当前会话所有人的历史 |

管理员可以取消当前会话其他人的单条任务、查看详情。按编号操作也受平台和群隔离限制。群管理员并不会自动获得机器人管理权限；使用 AstrBot 管理员配置，或在插件配置中加入 `admin_ids`、`group_admin_ids`。`group_admin_ids` 的格式为 `平台ID|群OpenID|用户OpenID`，可从 `/tx identity` 复制。

列表每页 2 条，长内容显示预览，可用 `detail` 查看全文。投递超过失败阈值后会自动取消，可以在 `history` 中查到原因；修复权限或网络后重新创建即可。

## 官方 QQ 的对象和发送权限

官方 QQ 下发 `group_openid`、`member_openid`、`user_openid`；普通 QQ 号、群号不能直接代替它们。提醒他人优先解析真实 @ 消息段或平台提供的 mentions。若官方事件没有提供被 @ 成员的标识，让对方先在本群 `@机器人 /tx identity`，再使用：

```text
/tx 30分钟 开会 @对方的用户标识
```

插件会记住本群里机器人实际收到的成员消息，名字唯一且官方提供昵称时也支持 `@昵称`。不同群里的成员记录不会混用，重名时要求明确标识。OneBot 模式会查询目标是否是本群成员。私聊只能提醒自己。

定时提醒属于主动消息，插件直接使用当前官方适配器的客户端发送，不保存旧 `msg_id`、不复用过期回复窗口。QQ 官方的主动消息权限、额度、风控和目标可达性仍适用。平台拒绝或未返回消息 ID 时记录为失败，只有获得消息 ID 后才标记成功或计算下一次时间。相关依据见 [腾讯官方 SDK 使用说明](https://github.com/tencent-connect/qqbot-nodejs/blob/main/USAGE.md)。

QQ 普通群使用 `<qqbot-at-user id="OpenID" />` 实现真正 @，频道使用频道成员 ID。群、C2C、频道、频道私信分别走对应 SDK 接口。插件自己的到期提醒使用普通文本，不要求 Markdown 模板；命令回复仍由 AstrBot 当前事件的配置发送。

旧数据库里的数字 QQ 号/群号无法自动换算为官方 OpenID，所以本次迁移的是功能，没有修改或直接导入旧数据库。更换 QQ 应用或 AstrBot 平台 ID 时，原 OpenID/路由可能失效，需要重建对应提醒。备份时停止 AstrBot 后复制整个 `data/plugin_data/astrbot_plugin_tixing/`。

正常重载和并发扫描由数据库租约避免重复发送，卸载时会等待正在发送的任务完成并保存结果。数据库和 QQ 网络接口无法组成同一事务；如果进程在“QQ 已收消息但本地尚未保存”时被强制结束，或服务器收消息后响应超时，恢复重试仍可能重复。建议只在一个 AstrBot 实例里运行该插件。

## 配置

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
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

自动化测试使用真实 SQLite 和时间解析；AstrBot 主机边界和 QQ 发送接口使用测试替身，覆盖时间格式、上限、群隔离、成员识别、失败重试、循环补发、权限和热重载。GitHub Actions 会在 Linux（Python 3.10、3.12）和 Windows（Python 3.12）上运行测试、代码检查和打包。真实 QQ 投递仍需在实际部署的 AstrBot 和账号中验证。

安装后可用以下流程确认部署环境：

1. `@机器人 /tx 1分钟 测试提醒`，确认返回任务编号，并在一分钟后加一个扫描周期内收到真正 @ 的通知。
2. `/tx history` 检查该任务状态；失败时检查记录里的原因和 AstrBot 日志。
3. `/tx 5分钟 重启测试`，重载插件后用 `/tx list` 确认仍在，再 `/tx cancel 编号` 确认取消。
4. 让另一成员先发送 `/tx identity`，测试本群的他人提醒。

## 来源

功能参考作者的原项目 `liziqqbotpy`，主要为 `plugins/reminder.py`、`core/event.py`、`core/scheduler.py`、`database/models.py`、`database/sqlite.py` 和 `plugins/help.py`。原项目未提供公开仓库链接。本插件保留提醒行为，重写 AstrBot 入口、存储和平台适配，不依赖旧项目的 NoneBot、NapCat 或全局调度器。

接口依据 [AstrBot 插件开发文档](https://docs.astrbot.app/dev/star/plugin-new.html)、[QQ 官方 WebSocket 适配器源码](https://github.com/AstrBotDevs/AstrBot/blob/master/astrbot/core/platform/sources/qqofficial/qqofficial_platform_adapter.py)、[Webhook 适配器源码](https://github.com/AstrBotDevs/AstrBot/blob/master/astrbot/core/platform/sources/qqofficial_webhook/qo_webhook_adapter.py) 和 [腾讯 botpy SDK](https://github.com/tencent-connect/botpy/blob/master/botpy/api.py)。

## 开源与贡献

本项目采用 [MIT License](LICENSE)。欢迎提交 Issue 和 Pull Request；修改代码后请运行上面的测试、格式检查和打包命令。请在报告问题时说明 AstrBot 版本、QQ 适配器类型、触发命令和经过脱敏的错误日志。
