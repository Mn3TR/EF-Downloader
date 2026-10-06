## 这一版改了什么

**功能与 v0.2.3 完全相同** —— 一样是修「下载大文件时提前提示下载完成」。
区别只在内部：v0.2.3 的 exe 是用重构*之前*的源码构建的，这一版是用重构
*之后*的源码重建的。

### 代码结构

`efd/` 从平铺 12 个模块拆成四层：

```
efd/
├── core/   与界面无关的一切（config / util / detect / settings / seed /
│           throttle / archive / planner / installer）
├── net/    传输层（volume / http / stream / prefetch）
├── ui/     Tkinter 界面（app / panels / handlers / messages / theme / entry）
└── cli/    命令行（parser / commands / report / runner / exitcode）
```

`core/planner.py` 与 `core/installer.py` 仍是 CLI 与 GUI **唯一**的实现，
`cli/` 和 `ui/` 只负责显示与转发。零依赖底线没变：仍然只用标准库。

### 测试

**208 → 437 个用例**，语句覆盖率 **89.0% → 98.0%**，其中 16 个模块 100%。

补测试的重点是**错误路径**，因为这个项目出事的三次全在错误路径上
（v0.2.0 检查完不发结束消息、v0.2.2 停机只在文件之间检查、v0.2.3 出错
提前返回却报「安装完成」），而正常路径早就被点过无数遍了。

新增的 `tests/test_messages.py` 专门盯消息协议里那句谎话的源头，
三个用例做过分变异验证：把 worker 改回「只看 `result.stopped` 分流」
「检查完不发结束消息」「异常静默吞掉」，都会被抓住。

## 下载

`EFD.exe` 一个文件同时是 CLI 和 GUI：双击进图形界面，终端里也能
`EFD.exe plan --target D:\...`。

- 体积 14,323,756 B（13.7 MiB）
- sha256 `f1de21769f1016292431c65aabe7827baf2549b75ebc6284f86f2c741bc5cde8`
- **未做代码签名**，首次运行会有 SmartScreen「未知发布者」提示
