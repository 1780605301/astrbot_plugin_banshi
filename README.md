<p align="center">
  <img src="logo.jpg" width="200" alt="更好的B站搬石 LOGO">
</p>

<h1 align="center">B站搬石插件</h1>

## 简介

随机从B站搬石到群的机器人插件。自动搜索关键词、随机选视频、下载并发送。支持手动指定关键词搬石、按BV号/AV号下载指定视频。

## 指令列表

### 基础控制

/bilibanshi on 开启定时搬石（开机自启动）
/bilibanshi off 关闭定时搬石
/bilibanshi now 立即随机搬一个视频并发送到当前聊天（不等待定时任务，只发当前会话）

### 指定关键词搬石

/bilibanshi search <关键词> [序号]
  按指定关键词搜索并搬一个视频（不指定序号时随机选一个结果）
  例: /bilibanshi search 高松灯 /bilibanshi search 搞笑 3

### 指定BV号/AV号下载

/bilibanshi get <BV号|AV号|视频链接>
  解析并下载指定视频，自动压缩后发送到当前聊天
  例: /bilibanshi get BV1GJ411x7h7 /bilibanshi get av170001

### UP主订阅

/bilibanshi sub add <UID或昵称> 订阅UP主，之后其新投稿会自动下载压缩并推送到所有群
/bilibanshi sub remove <UID或昵称> 取消订阅
/bilibanshi sub list 查看已订阅列表
  说明: 订阅时会把当前最新投稿记为已推送，从下一次更新开始推送；每次最多推送3个新视频
  说明: 基于B站搜索接口实现，新视频发布后搜索索引可能有几分钟延迟
  说明: 只搬最近N天内发布的新视频（sub_fresh_days，默认3天），更早的旧视频直接跳过
  配置: sub_check_interval 检查间隔(秒,默认600)；sub_max_duration 视频最大时长(秒,默认1200)；sub_fresh_days 新鲜期(天,默认3,0为不限)

### 配置管理

/bilibanshi list 查看当前状态
/bilibanshi interval <秒> 设置搬石间隔（如 3600，最小10秒）
/bilibanshi maxduration <秒> 设置视频最大时长（默认600秒/10分钟）
/bilibanshi clean 手动清理临时文件

### 关键词管理

/bilibanshi keyword add <关键词> 添加预设搜索关键词
/bilibanshi keyword remove <关键词> 删除预设搜索关键词

### 黑名单管理

/bilibanshi blacklist add <群号> 添加黑名单群
/bilibanshi blacklist remove <群号> 移除黑名单群

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
