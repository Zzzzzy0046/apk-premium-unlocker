# -*- coding: utf-8 -*-
"""
启动入口（PyInstaller 的 entry script）。
保持 premium_unlocker 只以单一模块身份加载，避免 __main__ 二次加载导致日志缓冲分裂。
"""
import premium_unlocker

if __name__ == "__main__":
    premium_unlocker.main()
