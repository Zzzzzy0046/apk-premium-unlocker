# 竞品订阅解锁工具（Adapty Premium Unlocker）

一键把竞品 APK（APKPure / APKCombo 的 apk/xapk/zip）解锁订阅并装到设备，用于实机拆解 premium 功能。

## 解锁手段（五层）

| 层 | 手段 | 覆盖场景 |
|---|---|---|
| 0 | pairip 绕过 | 形态A：application 包装换回业务 Application；形态B：删 LicenseContentProvider |
| 1 | **SDK 确定性模板** | Adapty / RevenueCat / Superwall / Qonversion / Apphud 的 isActive 类 getter 或判断方法 → 恒 true（含 null 分支） |
| 2 | **判断链扫描** | 业务代码里返回 boolean 的 premium 判断方法（isPremium / hasPro / isUnlocked…）→ 恒 true，跳过 SDK/广告/系统目录与过长方法 |
| 3 | **Frida 动态 Hook** | 重打包失败（签名校验/加固/VMP）或不想改包的场景：运行时 hook SDK 判断方法 + SharedPreferences/RemoteConfig 缓存读点。frida-server 按设备 ABI 自动下载推送 |
| 4 | **MITM 代理模式** | 服务端 entitlement 下发、客户端无判断点的 app：mitmdump 改 API JSON 响应，isPremium 等字段翻 true |
| 5 | **缓存层规则** | SharedPreferences / Firebase Remote Config 的 premium key getBoolean 读点 → 恒 true（确定性，AI 之前先跑） |

AI 兜底（DeepSeek）降级为最后手段：只有五层确定性补丁全部无命中时才调用。

## 用法

1. 双击 exe，选择 APK/XAPK 文件
2. 勾选「自动安装到设备」（默认勾选）；不勾则只生成补丁包
3. 点「解锁并安装」，看步进器 + 日志
4. 产物在输入文件旁 `xxx_unlocked/signed/`，可复装

### Frida 模式

勾选「Frida 模式」并填写包名（xapk 会自动读取 manifest 里的包名）。要求：设备已 root、USB 调试开启。工具自动完成 frida-server 下载/推送/启动、生成 hook 脚本、注入运行。重打包失败时会自动生成一份备选 Frida 脚本到 `xxx_unlocked/frida_script.js`。

### 代理模式

页面「代理模式」卡片：填端口与改写字段（留空用默认），点启动。工具自动启动 mitmdump、adb reverse、设置设备代理，日志给证书安装指引（每个设备装一次 CA 证书）。未装 mitmproxy 时给一次性安装命令，不影响其他功能。

## 交付与打包（Windows）

**同事零依赖**：`build_windows.bat` 一键打包，产物是单文件 exe，内置 JRE / apktool / apksigner / adb / frida 客户端，首次运行自动解压。同事机器不需要装任何东西（Frida 模式的 frida-server 也会自动下载）。

```bat
build_windows.bat
```

产物：`dist\竞品订阅解锁工具.exe`。前置：打包机装过 Python 3.10+。

## 依赖（开发模式自动探测，缺失时提示）

- Java 17+（PATH）
- apktool.jar（`~/apktool.jar`、`~/tools/apktool.jar`、exe 同目录；都没有则自动从 GitHub 下载）
- Android SDK build-tools（apksigner）
- Android SDK platform-tools（adb，安装/验证用）
- debug keystore（`~/.android/debug.keystore`，缺失自动生成）

## 原理

Adapty 托管订阅在服务端校验购买凭证，本地伪造 Purchase 无效；正确做法是 patch 客户端读点：真实 profile 照常从 `api.adaptytech.com` 拉取，只在读 `isActive` 时返回 true。关键坑：新用户 profile 里没有 "premium" access level（null），所以必须同时 patch 调用点的 null 分支，不能只 patch getter。RevenueCat / Superwall / Qonversion / Apphud 同理，均有确定性模板。

服务端 entitlement 下发的（客户端无本地判断点）改包无效，走代理模式（第 4 层）改 API 响应。

详见 skill：`~/.claude/skills/adapty-subscription-bypass/SKILL.md`。

## 开发

```bash
python premium_unlocker.py          # 直接跑 Web UI
python premium_unlocker.py --cli <apk路径> [--no-install] [--force-single] [--frida --pkg 包名]
```

纯标准库，无第三方依赖（frida 客户端打包进 exe，开发模式按需 pip 安装）。

## 注意

- 产物是 debug 重签包，**只用于内部验证/竞品拆解**，不要外发
- 修改的是客户端判断，服务端状态不变；真实付费流程仍走 Play Console license tester
- 若自动定位不到 null 分支，工具会提示，此时仅 getter 补丁生效（新用户可能仍显示未订阅），参考 skill 手工补
- Frida 模式需要 root 设备；SSL Pinning 的 app 代理模式无效（需 Frida unpinning，后续版本）
