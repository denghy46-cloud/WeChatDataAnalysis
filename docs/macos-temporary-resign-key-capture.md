# macOS Apple Silicon 数据库密钥捕获

该实验性方案用于腾讯官方 macOS 微信因 Hardened Runtime 拒绝调试附加、常规本地 helper 返回 `TARGET_PROCESS_PROTECTED` 的情况。当前实现针对 Apple Silicon 和微信 4.1.x 的 `CCKeyDerivationPBKDF` 登录派生流程。

## 安全边界

- SIP 必须保持开启；该方案不修改 SIP。
- 只接受腾讯 Developer ID 签名、Apple 信任链可验证的 `com.tencent.xinWeChat` 应用。
- 开始捕获前创建两条恢复路径：原子换位后的官方原版槽，以及另一份已校验的 APFS 恢复副本。
- 临时副本只增加 `get-task-allow` 和关闭 library validation；密钥捕获结束、失败、超时或取消后都恢复捕获前的官方应用。
- 恢复时比对捕获前记录的 bundle ID、Team ID、CDHash 和 designated requirement，并再次执行 `codesign --verify --deep --strict`。
- 密钥不会写入恢复日志或命令文件。候选必须同时通过当前账号的消息库和会话库认证，才会交给现有解密流程。

## 使用条件

1. 微信安装在 `/Applications/WeChat.app`，且当前用户可写 `/Applications`。
2. 已安装 Xcode Command Line Tools 和 LLDB。
3. 选择的是当前账号真实的 `db_storage`，例如 `.../Documents/xwechat_files/<账号目录>/db_storage`。不要硬编码 `Documents/app_data/xwechat_files`；不同微信构建的数据布局不同。

`DevToolsSecurity` 在当前 macOS 上不是硬性前置条件：带 `get-task-allow` 的临时目标即使在其状态为 disabled 时也可以被系统 LLDB 附加。若实际返回 `LLDB_ATTACH_DENIED`，再把 `sudo /usr/sbin/DevToolsSecurity -enable` 作为排障项，并检查“系统设置 → 隐私与安全性 → 开发者工具”。

在项目页面点击“一键获取数据库密钥”。常规 helper 若返回 `TARGET_PROCESS_PROTECTED`，页面会展示风险说明并请求确认使用临时重签。临时微信出现后，允许 macOS 的容器访问提示，并在二维码页面重新登录数据库所属账号。

## 只读检查与紧急恢复

只读预检：

```bash
python -m wechat_decrypt_tool.macos_resign_key_capture status \
  --wechat-app /Applications/WeChat.app \
  --db-storage /absolute/path/to/db_storage
```

若 WCDA 或系统在捕获中意外退出，下次启动后端会先自动恢复。也可以在运行项目前手动执行：

```bash
python -m wechat_decrypt_tool.macos_resign_key_capture recover
```

对应的本地接口为：

- `GET /api/macos_key_capture/status`
- `POST /api/macos_key_capture/recover`

恢复失败时不要启动当前 `/Applications/WeChat.app`。保留 WCDA 数据目录中的 `macos-key-capture/transaction.json` 和 `/Applications/.WeChat.wcda-key-capture-*`，先执行上面的恢复命令；仍失败再从腾讯官方安装包重新安装微信。
