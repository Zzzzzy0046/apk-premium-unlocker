# 竞品订阅解锁工具（Adapty Premium Unlocker）

一键把竞品 APK（APKPure / APKCombo 的 apk/xapk/zip）解锁订阅并装到设备，用于实机拆解 premium 功能。

## 能力

| 步骤 | 说明 |
|---|---|
| 解包 | xapk/zip 自动取 base + split；单 apk 直接处理 |
| pairip 绕过 | 形态A：application 包装 → 自动找 `.super` 换回业务 Application；形态B：删 LicenseContentProvider |
| Adapty 订阅解锁 | ① `AccessLevel.isActive()` getter 恒 true ② `Subscription.isActive()` 恒 true ③ map builder 的 null 分支恒 true（新用户无 premium level 的场景） |
| 重打包重签 | apktool b + apksigner 统一 debug 签名（base + 全部 split 同一 keystore） |
| 安装验证 | adb install-multiple → monkey 启动 → logcat 查 premium 生效日志 |

## 用法

1. 双击 exe，选择 APK/XAPK 文件
2. 勾选「自动安装到设备」（默认勾选）；不勾则只生成补丁包
3. 点「解锁并安装」，看日志
4. 产物在输入文件旁 `xxx_unlocked/signed/`，可复装

## 依赖（自动探测，缺失时提示）

- Java 17+（PATH）
- apktool.jar（`~/apktool.jar`、`~/tools/apktool.jar`、exe 同目录；都没有则自动从 GitHub 下载）
- Android SDK build-tools（apksigner）
- Android SDK platform-tools（adb，安装/验证用）
- debug keystore（`~/.android/debug.keystore`，缺失自动生成）

## 原理

Adapty 托管订阅在服务端校验购买凭证，本地伪造 Purchase 无效；正确做法是 patch 客户端读点：真实 profile 照常从 `api.adaptytech.com` 拉取，只在读 `isActive` 时返回 true。关键坑：新用户 profile 里没有 "premium" access level（null），所以必须同时 patch 调用点的 null 分支，不能只 patch getter。

详见 skill：`~/.claude/skills/adapty-subscription-bypass/SKILL.md`。

## 开发

```bash
python premium_unlocker.py          # 直接跑 GUI
python -m PyInstaller --onefile --windowed --name "竞品订阅解锁工具" premium_unlocker.py
```

纯标准库，无第三方依赖。日志写入 GUI 面板；CLI 调用 `Unlocker(log_fn).run(apk_path, install_flag)` 也可用。

## 注意

- 产物是 debug 重签包，**只用于内部验证/竞品拆解**，不要外发
- 修改的是客户端判断，服务端状态不变；真实付费流程仍走 Play Console license tester
- 若自动定位不到 null 分支，工具会提示，此时仅 getter 补丁生效（新用户可能仍显示未订阅），参考 skill 手工补
