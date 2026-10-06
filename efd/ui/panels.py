"""面板：把控件摆出来，并把计划/进度画上去。

只有「长什么样」——这里没有任何网络、磁盘或线程逻辑。所有回调都指向
``HandlersMixin`` 上的方法，由 :class:`~efd.ui.app.App` 通过 MRO 提供。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from .. import __version__
from ..core import settings
from ..core.throttle import MAX_JOBS, MIN_JOBS
from .theme import APP_TITLE, DEFAULT_TARGET, TAGLINE, apply_styles


class PanelsMixin:
    """构造全部控件。

    刻意不继承 ``tk.Tk``：这里只往 ``self`` 上挂控件，窗口类型由
    :class:`~efd.ui.app.App` 决定。
    """

    def _build(self) -> None:
        apply_styles(self)

        head = ttk.Frame(self)
        head.pack(fill="x", padx=16, pady=(12, 0))
        ttk.Label(head, text=APP_TITLE, style="Head.TLabel").pack(side="left")
        ttk.Label(head, text=TAGLINE, style="Tag.TLabel").pack(side="left",
                                                               padx=(12, 0), pady=(6, 0))
        ttk.Label(head, text=f"v{__version__}", style="Tag.TLabel").pack(side="right",
                                                                        pady=(6, 0))

        self._build_target()
        self._build_actions()
        self._build_plan()
        self._build_progress()
        self._build_log()

        if self.var_target.get():
            self.log("就绪。已找到游戏目录，确认无误后点【1. 检查】。")
        else:
            self.log("就绪。没能自动找到游戏目录，请点【浏览…】手动选择安装位置。")

    def _build_target(self) -> None:
        box = ttk.LabelFrame(self, text="安装目录")
        box.pack(fill="x", padx=16, pady=(10, 4))

        row = ttk.Frame(box)
        row.pack(fill="x", padx=10, pady=(10, 6))
        self.var_target = tk.StringVar(value=settings.initial_target() or DEFAULT_TARGET)
        entry = ttk.Entry(row, textvariable=self.var_target)
        entry.pack(side="left", fill="x", expand=True)
        entry.bind("<Return>", lambda _e: self.on_check())
        ttk.Button(row, text="浏览…", width=9, command=self._browse).pack(side="left", padx=(6, 0))
        ttk.Button(row, text="自动检测", width=9, command=self._autodetect).pack(side="left", padx=(6, 0))
        ttk.Button(row, text="打开目录", width=9, command=self._open_target).pack(side="left", padx=(6, 0))

        info = ttk.Frame(box)
        info.pack(fill="x", padx=10, pady=(0, 10))
        self.lbl_disk = ttk.Label(info, text="目标盘可用空间：—")
        self.lbl_disk.pack(side="left")
        self.lbl_need = ttk.Label(info, text="", style="Hint.TLabel")
        self.lbl_need.pack(side="right")

    def _build_actions(self) -> None:
        box = ttk.Frame(self)
        box.pack(fill="x", padx=16, pady=4)

        row = ttk.Frame(box)
        row.pack(fill="x")
        self.btn_check = ttk.Button(row, text="1. 检查", style="Action.TButton",
                                    command=self.on_check)
        self.btn_check.pack(side="left")
        self.btn_go = ttk.Button(row, text="2. 开始安装", style="Action.TButton",
                                 command=self.on_install, state="disabled")
        self.btn_go.pack(side="left", padx=(8, 0))
        self.btn_stop = ttk.Button(row, text="停止", style="Action.TButton",
                                   command=self.on_stop, state="disabled")
        self.btn_stop.pack(side="left", padx=(8, 0))

        self.var_tryout = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            box,
            text="快速试探：只装小文件、跳过 StreamingAssets（约 1.3 GB，先跑通流程用）",
            variable=self.var_tryout,
        ).pack(anchor="w", pady=(8, 0))

        # 网络选项单独一行。默认值刻意保守：预读到底有没有用取决于链路，
        # 没量过就别替用户开——开错了是要多花流量的。
        net = ttk.Frame(box)
        net.pack(fill="x", pady=(8, 2))

        ttk.Label(net, text="并发预读", style="Name.TLabel").pack(side="left")
        jobs0, rate0 = settings.remembered_net()
        self.var_jobs = tk.StringVar(value=jobs0)
        ttk.Spinbox(net, from_=MIN_JOBS, to=MAX_JOBS, width=4,
                    textvariable=self.var_jobs).pack(side="left", padx=(6, 0))
        ttk.Label(net, text="线程", style="Hint.TLabel").pack(side="left", padx=(4, 0))

        ttk.Label(net, text="限速", style="Name.TLabel").pack(side="left", padx=(20, 0))
        self.var_rate = tk.StringVar(value=rate0)
        ttk.Entry(net, textvariable=self.var_rate, width=8).pack(side="left", padx=(6, 0))
        ttk.Label(net, text="如 8M、512K；0 表示不限", style="Hint.TLabel").pack(
            side="left", padx=(4, 0))

    def _build_plan(self) -> None:
        box = ttk.LabelFrame(self, text="计划")
        box.pack(fill="x", padx=16, pady=4)

        grid = ttk.Frame(box)
        grid.pack(fill="x", padx=10, pady=(10, 2))
        grid.columnconfigure(1, weight=1)

        def kv(row: int, name: str, style: str = "Value.TLabel"):
            ttk.Label(grid, text=name, style="Name.TLabel").grid(
                row=row, column=0, sticky="w", pady=1)
            value = ttk.Label(grid, text="—", style=style)
            value.grid(row=row, column=1, sticky="w", padx=(14, 0), pady=1)
            return value

        self.v_version = kv(0, "游戏版本")
        self.v_need = kv(1, "待安装")
        self.v_skip = kv(2, "已跳过")
        self.v_download = kv(3, "需要下载")
        ttk.Separator(grid, orient="horizontal").grid(
            row=4, column=0, columnspan=3, sticky="ew", pady=5)
        self.v_peak = kv(5, "峰值磁盘")
        self.v_save = kv(6, "可省下", style="Save.TLabel")

        # 摆在第一行右侧，不占额外的纵向空间。
        self.btn_detail = ttk.Button(grid, text="详情 ▾", width=10, command=self._toggle_detail)
        self.btn_detail.grid(row=0, column=2, sticky="e")

        # 详情面板默认收起，但控件一直在——set_plan_text() 任何时候都能写进去。
        self.detail = ttk.Frame(box)
        self.txt_plan = tk.Text(self.detail, height=8, wrap="none", relief="flat",
                                background=self.cget("background"))
        self.txt_plan.pack(fill="x", padx=10, pady=(0, 8))
        self.txt_plan.configure(state="disabled")

    def _build_progress(self) -> None:
        box = ttk.LabelFrame(self, text="进度")
        box.pack(fill="x", padx=16, pady=4)

        row = ttk.Frame(box)
        row.pack(fill="x", padx=10, pady=(10, 4))
        self.bar = ttk.Progressbar(row, mode="determinate", maximum=1000)
        self.bar.pack(side="left", fill="x", expand=True)
        self.lbl_pct = ttk.Label(row, text="0.0%", style="Pct.TLabel", width=7, anchor="e")
        self.lbl_pct.pack(side="left", padx=(10, 0))

        self.lbl_stats = ttk.Label(box, text="—", style="Stat.TLabel")
        self.lbl_stats.pack(anchor="w", padx=10, pady=(0, 2))
        self.lbl_cur = ttk.Label(box, text="", style="Hint.TLabel")
        self.lbl_cur.pack(anchor="w", padx=10, pady=(0, 10))

    def _build_log(self) -> None:
        box = ttk.LabelFrame(self, text="日志")
        box.pack(fill="both", expand=True, padx=16, pady=(4, 14))

        bar = ttk.Frame(box)
        bar.pack(fill="x", padx=10, pady=(8, 0))
        self.lbl_state = ttk.Label(bar, text="就绪", style="Hint.TLabel")
        self.lbl_state.pack(side="left")
        ttk.Button(bar, text="清空", width=8, command=self._clear_log).pack(side="right")
        ttk.Button(bar, text="保存…", width=8, command=self._save_log).pack(side="right", padx=(0, 6))

        body = ttk.Frame(box)
        body.pack(fill="both", expand=True, padx=10, pady=(6, 10))
        scroll = ttk.Scrollbar(body)
        scroll.pack(side="right", fill="y")
        self.txt_log = tk.Text(body, height=8, wrap="word", font=("Consolas", 9),
                               yscrollcommand=scroll.set)
        self.txt_log.pack(side="left", fill="both", expand=True)
        scroll.configure(command=self.txt_log.yview)
