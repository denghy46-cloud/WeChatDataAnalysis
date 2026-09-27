# 个人分支与上游同步

## 仓库关系

- `upstream`：https://github.com/LifeArchiveProject/WeChatDataAnalysis
- `origin`：https://github.com/denghy46-cloud/WeChatDataAnalysis
- 日常提交推送到 `origin/main`；从 `upstream/main` 合并更新，不向上游直接推送。
- 2026-09-27 查询时，个人仓库为公开 Fork（PUBLIC），并非私有仓库。运行数据、真实密钥和日志不能随源码提交。

## 2026-09-27 同步记录

合并上游 `e67e685`（源码版本 2.7.0），纳入自上次同步以来的 134 个提交。
同步前个人主分支为 `3e46149`，保留在 `codex/pre-upstream-sync-20260927`。

上游已经包含 Apple Silicon 登录期密钥捕获、临时调试签名、双库验真和恢复流程，
并补充了捕获生命周期、精确 build 恢复与 LLDB watchdog 超时分类修复。
依据为上游 `docs/MACOS_KEY_CAPTURE.md`、`docs/macos-wcdb-key-capture.md`，
以及 `e0c7ba9`、`e1e09ad` 的实现和测试。

兼容性不能仅按“已合并”判断：旧验证记录包含微信 4.1.12，最新恢复改进的实测反馈为 4.1.7；
上游明确没有保证所有系统和微信版本。本次同步未重新登录真实微信或重新捕获真实密钥。

本次取舍：

- 上游核心捕获/恢复模块、捕获进度界面和默认 helper 流程保持上游实现。
- 保留个人 `macos_resign_key_capture.py` 及 `macos_resign_lldb` 入口作为备用，
  不自动代替上游流程；具体操作及恢复说明见 `macos-temporary-resign-key-capture.md`。
- 保留账号准备与自动解密、微信更新保护、Mac 数据路径筛选、原生运行库路径兼容、启动菜单及安装包备份清单。
- 朋友圈远程图片采用上游直接保留凭据所绑定尺寸的实现，替代个人旧的请求失败后重试缩略图逻辑。
  本地 V2 图片密钥自动解析仍保留。
- 桌面首次导航采用上游 `loadWithRedirect`，避免额外的旧跳转处理影响上游超时判断。

## 验证及边界

- Mac 捕获、恢复、独立提取工具、个人备用方案、账号准备、更新保护和解密页面相关 Python 回归：
  400 项通过，另有 35 项子测试通过。
- 朋友圈媒体非 HTML 导出回归：30 项通过，4 项子测试通过；6 项 HTML 导出测试单独记录如下。
- 捕获进度、桌面导航和更新保护界面 Node 回归：19 项通过。
- `npm --prefix frontend run build` 前端生产构建通过（有非阻断的样式、重复导入和包体积警告）。
- `bash -n wcda-menu.sh`、`node --check desktop/src/main.cjs` 和合并差异检查通过。
- 扩展检查中 7 项失败：1 项在 Mac 上模拟 Windows 配置路径，6 项 HTML 导出缺少可加载的
  `wce_integrity` 原生组件。在相同 Python 环境下对未修改的上游 `e67e685` 运行后，
  同样 7 项失败，确认不是本次合并新增的失败；不能据此声称完整导出链路已验收。
- 自动化测试使用合成数据与模拟进程，不能代替本机真实微信登录捕获及恢复验收。

## 后续同步

先确认工作区干净，保留更新前提交，再执行：

```bash
git fetch upstream
git merge --no-ff upstream/main
# 检查冲突、运行相关回归，再推送
git push origin main
```

根目录 `./wcda-menu.sh` 提供启动、停止、实时日志、重启和退出。
代码同步不会切换另一个 worktree 中正在运行的实例；首次运行新版之前，按 README 同步依赖。
只有新版上游在目标机器、目标微信 build 上通过真实捕获和官方应用恢复验收后，
才考虑删除个人备用流程；旧版本仍可从保留分支和 Git 历史恢复。

## 桌面入口与源码启动检查（2026-09-27）

桌面 `WCDA 管理菜单.command` 已由旧的 `WeChatDataAnalysis-macos-key-capture`
改为调用主目录 `WeChatDataAnalysis/wcda-menu.sh`，并转发命令参数。
五项管理菜单及 `desktop/scripts/dev.cjs` 入口仍兼容，因此未改菜单实现。
菜单语法、`status` 和交互退出已验证；后端依赖已按锁文件同步到 2.7.0。

**源码启动仍被上游运行包过期阻塞，不能将构建与回归通过理解为完整应用可启动。**
上游 `desktop/resources/native-core-source-macos.json` 仍指向
`macos-source-runtime-20260809-71122b5b-8e355001`，其到期时间为
`2026-09-22T06:48:28Z`。直接调用上游 `ensureSourceNativeCore` 已复现
“当前 WCDA 固定的 macOS 源码运行时已过期，请先拉取最新代码后再启动”。
查询上游公开发布记录后未发现比 8 月 9 日更新的 Mac 源码运行包。
需要上游发布有效运行包并更新固定引用；仅再次拉取当前相同提交不能解决。

上游另有 9 月 25 日发布的 2.7.0 Apple Silicon 安装包，但安装包与源码运行包
是独立交付渠道。本次未安装、替换或验证该安装包，也未修改原生组件的有效期校验。
