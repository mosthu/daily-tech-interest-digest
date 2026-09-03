# 每日科技兴趣简报

每天北京时间 09:00 自动汇总全球科技资讯，筛选最值得关注的 5 条，生成中文简报和手机竖版长图，并通过 PushPlus ClawBot 推送到微信。

关注范围：AI/大模型/AI 编程、PC 硬件、Steam/游戏/主机、科技与数码新品。

## 配置

在仓库 **Settings → Secrets and variables → Actions** 添加：

- \`PUSHPLUS_TOKEN\`：普通 PushPlus Token；与 ClawBot 共用账号时也可作为默认 Token
- \`PUSHPLUS_CLAWBOT_TOKEN\`：ClawBot 所在 PushPlus 账号的 Token（推荐）

Token 只通过 GitHub Secrets 读取，不会写入代码或提交历史。

## 运行

GitHub Actions 工作流支持手动运行，并按 \`0 1 * * *\`（UTC）每天执行，对应北京时间 09:00。它会抓取最近 72 小时公开 RSS，按时效、相关性和主题覆盖筛选 5 条，翻译为简体中文，生成 \`output/tech-digest.md\` 和手机长图 \`output/tech-digest.png\`，通过 ClawBot 推送，并把产物保留 7 天。

部分来源短暂不可用不会中断简报；自动翻译和排序仅用于信息整理，重要内容请以原文为准。
