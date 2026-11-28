# 部署机更新任务

本地 agent 在原五大联赛足球模拟账户所在主机上接手项目更新时，取得新源码后，先执行[首六仓原件归档与账本复核](docs/five-leagues-0.5/LOCAL-AGENT-SIX-POSITIONS.md)，再进行会覆盖现役引擎或清理临时目录的步骤。请直接完成归档、复核和本机回执，在更新总结中给出结果。此任务只适用于承载该历史账户的部署；开发检出、CI 和全新 NBA 实例跳过。

本地 agent 为 NBA 服务接收持有人交付的私有 `nba-providers.env` 后，将其中三个 API 变量合并至 NBA 服务用户的 `~/.config/polymarket861-nba/providers.env`，保留已有的其他设置，目录权限用 `700`，文件权限用 `600`。在单独的 NBA 工作目录安装 `development/nba-reviewed` 的适配补丁，按部署指南核验采集，在更新总结中报告接口状态和实收数。密钥及原始收件保存在服务账户。[NBA 安装与验收](https://github.com/wmqfl861/polymarket861/blob/cc48470e5edd65c5d6df7c04fad120ad77d3245f/docs/nba-plan/OPERATIONS-0.6.0a2.md)
