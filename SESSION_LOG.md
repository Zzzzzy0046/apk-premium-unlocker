# 会话记录与竞品拆解结论（2026-08-27 ~ 08-28）

## 一、项目是什么

**竞品订阅解锁工具（APK Premium Unlocker）**：一键把竞品 APK/XAPK 解包、绕过 pairip 许可校验、解锁订阅/premium、重打包重签、装到真机。

- 纯网页形态：双击 exe → 自动开浏览器 → 网页选包、看实时日志
- 单文件 exe（自解压式，内置 JRE/apktool/apksigner/adb + frida 客户端，同事零依赖）
- 订阅解锁五层：SDK 模板 → 判断链扫描 → 缓存层规则 → Frida → 代理；DeepSeek AI 降级为兜底

## 二、v1.2 做了什么（08-27）

1. 阶段状态机 + Web UI 步进器：8 阶段（初始化/解包/反编译/补丁/重打包/签名/安装/验证），每阶段状态（running/done/failed/skipped）+ 进度条 + 阶段耗时 + 总耗时；失败阶段红色标出并给错误文案
2. 全流程进度：解包按文件数、签名按文件、验证 12 秒倒计时、AI 补丁按 smali 扫描进度
3. 状态机单元测试 + 前端 headless 渲染测试

## 三、v1.3 做了什么（08-28）：解锁手段五层化

**第 1 层 SDK 确定性模板（sdk_patches.py）**：Adapty / RevenueCat / Superwall / Qonversion / Apphud 各自的 getter / 判断方法补丁模板 + 通用 null 分支扫描（含 valueOf 入参寄存器的经典形态）。

**第 2 层 判断链扫描**：业务代码里返回 boolean 的 premium 判断方法（isPremium/isPro/hasPro/isUnlocked…）→ 恒 true。跳过 SDK/广告/系统目录、跳过 >60 条指令的方法（防误伤），每处补丁都打日志。

**第 3 层 Frida 模式（frida_mode.py）**：重打包失败（签名校验/加固/VMP）的兜底。dex 扫描检测 SDK → 生成 hook 脚本（SDK 方法 + SharedPreferences/RemoteConfig 通用缓存 hook）→ 按设备 ABI 自动下载/解压/推送/启动 frida-server → 注入运行。frida 客户端打包进 exe（build_windows.bat 一键），开发模式 lazy import 优雅降级。重打包失败时自动生成备选脚本到 xxx_unlocked/frida_script.js。

**第 4 层 代理模式（proxy_mode.py）**：服务端 entitlement 下发的 app（Air Tag Detector 类）改包无效，用 mitmdump 改 API JSON 响应（isPremium 等字段 bool→true / int 0→1 / inactive→active）。工具生成 addon、启动 mitmdump、adb reverse + 设设备代理、证书安装指引。mitmdump 未装时给一次性安装命令，不崩。

**第 5 层 缓存层规则**：SharedPreferences / Firebase Remote Config 的 premium key getBoolean 读点 → 恒 true（确定性，AI 之前先跑）。

配套：
- Web UI：Frida 勾选 + 包名输入、代理模式卡片（端口/改写字段/启停/状态）
- 状态机支持动态阶段列表（Frida 流程 7 阶段：初始化/解包/检测 SDK/生成脚本/部署 frida-server/注入运行/验证）
- Windows 一键打包 build_windows.bat（venv → pyinstaller → frida 打进 exe → 合并 runtime）
- 测试套件 46 项全过：五层补丁（合成 smali）、Frida 脚本生成 + JS 语法校验、代理 addon 走查逻辑、状态机动态阶段、假工具链全流程端到端（含重打包失败→Frida 兜底路径）

## 四、竞品拆解发现（可复用）

| 竞品 | 包名 | 订阅技术栈 | 结论 |
|---|---|---|---|
| Flightsky Flight Tracker 3D | com.live.flight.tracker | byelab 框架 + Adapty + Google Billing | premium 判断链 `f.O()`；补丁点 AccessLevel.isActive + null 分支 |
| iSeey / Lingua（AI 英语） | com.learn.speak.language.english.aitutor.spainish.lingua | Superwall SDK + RevenueCat + proxglobal PurchaseUtils | premium 判断在 `af/e.smali`：SubscriptionStatus.isActive() + PurchaseUtils.isRemoveAds() 双条件；v1.3 有 Superwall/RevenueCat 确定性模板 |
| Air Tag Detector | com.moniqtap.airtag.detector.scanner.finder | Google Billing（残留）+ Firebase Remote Config + 广告解锁 | 无本地 premium 判断点，大概率服务端 entitlement + 每日看广告解锁；走第 4 层代理模式或 Frida 缓存 hook |

**方法论结论**：订阅 SDK 判断 → 反编译后看 smali 包名（`com/adapty`、`com/revenuecat`、`com/superwall`）或扫 dex 域名（adaptytech / revenuecat）。客户端能解锁的是「本地布尔判断」；服务端 entitlement 下发的是补丁绕不过的——走代理改响应。

## 五、当前状态

- 工具 v1.3，五层解锁手段 + 步进器 UI
- 测试套件 46 项全过（/tmp/puzl_tests/test_all.py，macOS 可复跑）
- 待 Windows 打包：build_windows.bat → dist\竞品订阅解锁工具.exe

## 六、待办

1. Windows 跑一次 build_windows.bat 验证打包（frida 打进 exe 的首次验证）
2. 重跑 iSeey 解锁 premium（Superwall/RevenueCat 模板已就位）
3. 真机验证 Frida 模式（root 设备）与代理模式（装证书）
4. 判断链扫描误伤观察：如有非 premium 逻辑被改，加开关
5. SSL Pinning 的 app：Frida unpinning 脚本（后续版本）
