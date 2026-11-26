# 0.5.0a3 独立发布复审

三名独立审核代理均使用 GPT-6.2-astra、max 推理等级，在同一份冻结源码、测试与部署单元上分别签署通过。总审重新计算了候选树中的 576 个文件对象及执行位，独立验证 57 项断言。下面保留三份签署原文及复现材料。

| 独审 | 复核范围 | 签署原文 SHA-256 |
|---|---|---|
| [整体发布](../../reports/five-leagues-0.5.0a3/independent/independent-overall.md) | 真并发和底层存储故障、时窗和缓存、纸面费用、v2、英超 1,900 场与五国 8,877 场、依赖和服务验收脚本 | `c29c6a68489f96c5a2b218e19ca76a963d3445dd181d6e80355e7c889060c128` |
| [盘口与费用](../../reports/five-leagues-0.5.0a3/independent/independent-evidence.md) | 旧库反证重建、费表异常、净份额兑付、功效估计与真实样本分母。9 个独立探针和 82 项专项测试通过 | `fe977e5875d0dcc565350aa3632a7daecfa3f4b2f961e7d224f8011ccf3cba57` |
| [英超新闻](../../reports/five-leagues-0.5.0a3/independent/independent-news.md) | 旧注册证书、逐项开原件、同窗首封损坏、单条配置损坏、零条回复和 20 队实采。7 个独立探针和 16 项专项测试通过 | `29ad6aa94060a55296374931778658477f0775a69610096f8ffe3ddb11f330e5` |

三个签署对应的候选 Git 树为 `4298e1e24836bc85b5b8bcbcf72ddc45d8c9cdbe`。发布提交另附审核文件与运行证据，可以用以下命令逐项核对发布检出的产品部分。[总审、专项脚本与证据包](../../reports/five-leagues-0.5.0a3/independent-reproduction.zip) · [本机三解释器及正式配方实测](../../reports/five-leagues-0.5.0a3/release-verification.json) · [干净安装与真实免费接口](../../reports/five-leagues-0.5.0a3/installed-verification.json)

```sh
git rev-parse HEAD:src HEAD:tests HEAD:scripts HEAD:.github HEAD:deploy
```

| 目录 | 审核通过的 Git 树 |
|---|---|
| src | `403a375b25e3da09ef2168584bb677aac5ce8696` |
| tests | `2bbe651a8d6e5fc238c1b707c2e2c6d98d091019` |
| scripts | `3d2c25446c05084701ab8ed6155eecf3444f11de` |
| .github | `c7d81fcaeb0715e426ea10a1ae8267506d3856d9` |
| deploy | `2157559a8bebdf420e6cd92f63b7d55cad185611` |

本机容器的 3.11.18、3.12.14、3.13.5 三套环境各运行 683 项，682 项通过，受宿主 Unix 数据报限制的 1 项跳过。Linux 的 systemd 实际进程重启、看门狗超时和停止结果另按 GitHub 运行记录核验。实验在隔离实例进行，部署机的新闻原件、已采固定窗和市场兑付仍须从本机记录读取。

独审签署后，GitHub 提交 `5b69aa1922c241d6aad98d59d9bc2d994d525cd8` 对应该完整候选树。GitHub 的 Ubuntu 原生 `systemd-deploy` 已通过。使用真正的用户级 systemd、安装后的 wheel 和交付的单元文件，外部 Gamma 端点采用受控响应。断网时进程能就绪、定时检查能告警；SIGKILL 后自动重启并收到四国新鲜目录；SIGSTOP 后日志记录看门狗超时，重启计数达到 2。最后正常停止，systemd 的结果为成功，业务检查立即返回停止。CI 临时将看门狗设为 35 秒，交付文件为 3 分钟。[对应 GitHub 提交](https://github.com/wmqfl861/polymarket861/commit/5b69aa1922c241d6aad98d59d9bc2d994d525cd8) · [原生服务验收运行](https://github.com/wmqfl861/polymarket861/actions/runs/45856035161/job/140034541405) · [日志摘录及条件](../../reports/five-leagues-0.5.0a3/systemd-deployment.json)
