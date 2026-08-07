<p align="center">
  <img src="logo.jpg" width="200" alt="更好的B站搬石 LOGO">
</p>

<h1 align="center">B站搬石插件</h1>

## 简介

随机从B站搬石到群的机器人插件。自动搜索关键词、随机选视频、下载并发送。支持手动指定关键词搬石、按BV号/AV号下载指定视频。

## 指令列表

> 💡 所有 `/bilibanshi` 指令均支持简写别名 `/banshi`，例如 `/banshi on`、`/banshi get BVxxxx`。
> `/banshi`（无参数）可查看本帮助。

### 👤 全员可用

| 指令 | 说明 |
|------|------|
| `/bilibanshi` / `/banshi` | 显示本帮助 |
| `/bilibanshi now` | 立即随机搬一个视频到当前聊天 |
| `/bilibanshi search <关键词> [序号]` | 按关键词搜索并搬视频（不指定序号则随机） |
| `/bilibanshi get <BV号\|AV号\|链接>` | 解析并下载指定视频发送到当前聊天 |

### 🔒 管理员专用

| 指令 | 说明 |
|------|------|
| `/bilibanshi list` | 查看当前运行状态与配置 |
| `/bilibanshi sub list` | 查看已订阅UP主列表 |
| `/bilibanshi on` | 开启定时搬石（开机自启动） |
| `/bilibanshi off` | 关闭定时搬石 |
| `/bilibanshi sub add <UID或昵称>` | 订阅UP主，新投稿自动推送 |
| `/bilibanshi sub remove <UID或昵称>` | 取消订阅UP主 |
| `/bilibanshi keyword add <关键词>` | 添加预设搜索关键词 |
| `/bilibanshi keyword remove <关键词>` | 删除预设搜索关键词 |
| `/bilibanshi blacklist add <群号>` | 添加黑名单群 |
| `/bilibanshi blacklist remove <群号>` | 移除黑名单群 |
| `/bilibanshi interval <秒>` | 设置搬石间隔（最小10秒） |
| `/bilibanshi maxduration <秒>` | 设置视频最大时长（默认600秒） |
| `/bilibanshi clean` | 手动清理临时文件 |

> **UP主订阅说明**: 订阅时会把当前最新投稿记为已推送，从下一次更新开始推送；每次最多推送3个新视频。基于B站搜索接口，新视频发布后可能有几分钟延迟。只搬最近N天内发布的新视频（sub_fresh_days，默认3天），更早的旧视频直接跳过。

---

## 更新日志

### v1.1.2
- 新增 `/banshi`（无参数）帮助指令，显示完整指令列表
- README 指令列表重新排版，按全员可用/管理员专用分类

### v1.1.1
- 新增 `/banshi` 简写别名，所有指令均可使用（如 `/banshi on`、`/banshi get BVxxxx`）
- 管理类指令增加管理员权限限制（on/off/sub/keyword/blacklist/interval/maxduration/clean）
- now/search/get/list/sub list 保持全员可用

## 功能说明

· **定时任务**：按设定间隔自动搜索并发送到机器人加入的所有群（黑名单除外）

· **手动触发**：/bilibanshi now 只发送到当前聊天

· **手动指定**：/bilibanshi search 指定关键词搬石；/bilibanshi get 按BV号/AV号下载指定视频

· **自动压缩**：视频超过 max_send_size_mb（默认100MB）会自动压缩后再发送；get 指令会强制压缩

· **黑名单**：屏蔽不想接收的群

## 依赖

· FFmpeg（用于视频合并和压缩）
## 许可证
MIT

## 🎖️ 致谢
本插件基于 https://github.com/Xuewu-awa/astrbot_plugin_bilibanshi 的核心逻辑进行改造。
