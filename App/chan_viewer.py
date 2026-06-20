"""
缠论看图器 - 简易 GUI (tkinter)

功能：
    - 手动输入代码，点击「分析」即可弹出缠论图（自动识别市场）：
        · A股/ETF/指数：6位数字（无需 sz/sh 前缀），如 600519 / 510300 / sh000001
        · 港股：5位数字，如 00700
        · 美股：ticker，如 AAPL / TSLA
    - 图直接嵌在窗口内，支持缩放/平移/保存（matplotlib 工具栏）
    - 数据走 akshare 新浪源（A股/港股/美股同源，稳定不依赖东财）
    - 注意：分钟级别仅 A股个股支持；港股/美股仅日/周/月线

运行：
    python App/chan_viewer.py

依赖：tkinter(Python自带) + matplotlib + akshare + chan.py 本体
"""
import sys
import threading
import traceback
from pathlib import Path

# 项目根目录加入路径，以便导入 chan.py 核心模块
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tkinter as tk
from tkinter import ttk, messagebox

import matplotlib
matplotlib.use("TkAgg")  # 嵌入 tkinter 必须用 TkAgg 后端

# 设置中文字体，避免标题/名称显示成方块（按可用性挑第一个）
from matplotlib.font_manager import fontManager as _fm
_avail = {f.name for f in _fm.ttflist}
for _font in ("Arial Unicode MS", "PingFang SC", "Hiragino Sans GB", "STHeiti", "Heiti SC", "Songti SC"):
    if _font in _avail:
        matplotlib.rcParams["font.sans-serif"] = [_font]
        break
matplotlib.rcParams["axes.unicode_minus"] = False  # 正常显示负号
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk

from Chan import CChan
from ChanConfig import CChanConfig
from Common.CEnum import AUTYPE, DATA_SRC, KL_TYPE
from Plot.PlotDriver import CPlotDriver
from DataAPI.AkshareAPI import get_security_name

# ---- 买卖点按类型分色 ----
# 主类型取 type 字符串首位（'1'/'2'/'3'），买用暖色、卖用冷色；
# 一类最醒目(红/绿)，二类(橙/青)，三类(紫/蓝)。
BSP_BUY_COLORS = {"1": "#d62728", "2": "#ff7f0e", "3": "#9467bd"}   # 1类红 2类橙 3类紫
BSP_SELL_COLORS = {"1": "#2ca02c", "2": "#17becf", "3": "#1f77b4"}  # 1类绿 2类青 3类蓝
BSP_DEFAULT_BUY = "#d62728"
BSP_DEFAULT_SELL = "#2ca02c"

# 图例说明（买卖点类型 -> 含义），显示在图上方便看图时对照
BSP_LEGEND = [
    ("b1/s1", BSP_BUY_COLORS["1"], "一类点(背驰)"),
    ("b2/s2", BSP_BUY_COLORS["2"], "二类点(回抽)"),
    ("b3/s3", BSP_BUY_COLORS["3"], "三类点(离开中枢)"),
]


def _bsp_main_type(type_str):
    """从 bsp.type 字符串取主类型首位（'1'/'2'/'3'）。type 可能是 '1'、'2s'、'3a'、'1,2' 等。"""
    for ch in str(type_str):
        if ch in ("1", "2", "3"):
            return ch
    return ""


def _bsp_common_draw_by_type(self, bsp_list, ax, buy_color, sell_color,
                             fontsize, arrow_l, arrow_h, arrow_w):
    """替换 PlotDriver.bsp_common_draw：几何完全沿用原实现，仅按买卖点主类型上色。

    保留原 buy_color/sell_color 作为兜底（取不到主类型时）。
    """
    x_begin = ax.get_xlim()[0]
    y_range = self.y_max - self.y_min
    for bsp in bsp_list:
        if bsp.x < x_begin:
            continue
        main = _bsp_main_type(bsp.type)
        if bsp.is_buy:
            color = BSP_BUY_COLORS.get(main, buy_color)
        else:
            color = BSP_SELL_COLORS.get(main, sell_color)
        verticalalignment = 'top' if bsp.is_buy else 'bottom'

        arrow_dir = 1 if bsp.is_buy else -1
        arrow_len = arrow_l * y_range
        arrow_head = arrow_len * arrow_h
        ax.text(bsp.x, bsp.y - arrow_len * arrow_dir, f'{bsp.desc()}',
                fontsize=fontsize, color=color,
                verticalalignment=verticalalignment, horizontalalignment='center')
        ax.arrow(bsp.x, bsp.y - arrow_len * arrow_dir, 0,
                 (arrow_len - arrow_head) * arrow_dir,
                 head_width=arrow_w, head_length=arrow_head, color=color)
        if bsp.y - arrow_len * arrow_dir < self.y_min:
            self.y_min = bsp.y - arrow_len * arrow_dir
        if bsp.y - arrow_len * arrow_dir > self.y_max:
            self.y_max = bsp.y - arrow_len * arrow_dir


# 全局替换库的绘制逻辑（只影响本看图器进程，不改库源码）
CPlotDriver.bsp_common_draw = _bsp_common_draw_by_type

# ---- 缠论计算 & 绘图配置（严格按缠师原文配置）----
CHAN_CONFIG = {
    # —— 笔：最严的一套（合并K线跨度≥4 + 严格分型校验）——
    "bi_algo": "normal",        # 标准笔（看跨度），非 fx 极宽松模式
    "bi_strict": True,          # 严格：合并K线跨度 >= 4
    "bi_fx_check": "strict",    # 分型有效性校验最严
    "bi_end_is_peak": True,     # 笔尾必须是区间极值
    # —— 线段：缠师特征序列法（非都业华 1+1、非线段破坏 break）——
    "seg_algo": "chan",
    # —— 中枢：标准定义，不允许单笔中枢 ——
    "zs_algo": "normal",
    "one_bi_zs": False,
    # —— 买卖点：一类点由「背驰」确认，背驰用 MACD 面积比 ——
    "macd_algo": "area",        # 比较前后两段红绿柱面积（缠师原文做法）
    "divergence_rate": 0.9,     # 后段力度 < 前段90% 视为背驰（有限阈值，不再是 inf）
    "min_zs_cnt": 1,            # 一类点前至少 1 个中枢
    "bs1_peak": True,           # 一类点必须是极值
    "bsp2_follow_1": True,      # 二类点必须跟在一类点后
    "bsp3_follow_1": True,      # 三类点必须跟在一类点后
    "bs_type": "1,2,3a,3b,1p,2s",  # 全部三类买卖点及其变体
    # —— 工程项 ——
    "trigger_step": False,
    "skip_step": 0,
    "print_warning": True,
}

PLOT_CONFIG = {
    "plot_kline": True,
    "plot_kline_combine": True,
    "plot_bi": True,
    "plot_seg": True,
    "plot_eigen": False,
    "plot_zs": True,
    "plot_macd": False,
    "plot_mean": False,
    "plot_channel": False,
    "plot_bsp": True,
    "plot_extrainfo": False,
    "plot_demark": False,
    "plot_marker": False,
    "plot_rsi": False,
    "plot_kdj": False,
}

PLOT_PARA = {
    "seg": {},
    "bi": {},
    "figure": {"x_range": 200},
    "marker": {},
}

LEVELS = {
    "日线": KL_TYPE.K_DAY,
    "周线": KL_TYPE.K_WEEK,
    "月线": KL_TYPE.K_MON,
    "60分钟(仅个股)": KL_TYPE.K_60M,
    "30分钟(仅个股)": KL_TYPE.K_30M,
    "15分钟(仅个股)": KL_TYPE.K_15M,
    "5分钟(仅个股)": KL_TYPE.K_5M,
}
class ChanViewer(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("缠论看图器 - chan.py")
        self.geometry("1280x860")
        self.canvas = None
        self.toolbar = None
        self._build_ui()

    def _build_ui(self):
        top = ttk.Frame(self, padding=8)
        top.pack(side=tk.TOP, fill=tk.X)

        ttk.Label(top, text="代码:").pack(side=tk.LEFT)
        self.code_var = tk.StringVar(value="600519")
        entry = ttk.Entry(top, textvariable=self.code_var, width=12)
        entry.pack(side=tk.LEFT, padx=(2, 2))
        entry.bind("<Return>", lambda e: self.on_analyze())
        ttk.Label(top, text="(A股6位/港股5位/美股ticker)", foreground="gray").pack(side=tk.LEFT, padx=(0, 10))

        ttk.Label(top, text="级别:").pack(side=tk.LEFT)
        self.lv_var = tk.StringVar(value="日线")
        ttk.Combobox(top, textvariable=self.lv_var, values=list(LEVELS.keys()),
                     state="readonly", width=14).pack(side=tk.LEFT, padx=(2, 10))

        ttk.Label(top, text="起始:").pack(side=tk.LEFT)
        self.begin_var = tk.StringVar(value="2018-01-01")
        ttk.Entry(top, textvariable=self.begin_var, width=12).pack(side=tk.LEFT, padx=(2, 10))

        ttk.Label(top, text="显示根数:").pack(side=tk.LEFT)
        self.xrange_var = tk.StringVar(value="200")
        ttk.Entry(top, textvariable=self.xrange_var, width=6).pack(side=tk.LEFT, padx=(2, 2))
        ttk.Label(top, text="(0=全部)", foreground="gray").pack(side=tk.LEFT, padx=(0, 10))

        self.btn = ttk.Button(top, text="分析", command=self.on_analyze)
        self.btn.pack(side=tk.LEFT, padx=4)

        self.status = ttk.Label(top, text="输入代码后点「分析」", foreground="gray")
        self.status.pack(side=tk.LEFT, padx=12)

        # 主体：左侧图表 + 右侧买卖点列表
        body = ttk.Frame(self)
        body.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        # 买卖点列表侧栏（先 pack，固定宽度，避免被图表 expand 挤出窗口）
        side = ttk.Frame(body, padding=(4, 0), width=340)
        side.pack(side=tk.RIGHT, fill=tk.Y)
        side.pack_propagate(False)  # 固定宽度，不被子控件撑缩
        ttk.Label(side, text="买卖点列表", foreground="gray").pack(side=tk.TOP, anchor="w")
        self.bsp_summary = ttk.Label(side, text="", foreground="gray")
        self.bsp_summary.pack(side=tk.BOTTOM, anchor="w", fill=tk.X)

        tree_box = ttk.Frame(side)
        tree_box.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        cols = ("time", "dir", "type", "price", "vis")
        self.bsp_tree = ttk.Treeview(tree_box, columns=cols, show="headings")
        headers = {"time": ("时间", 92), "dir": ("方向", 56), "type": ("类型", 64),
                   "price": ("价格", 68), "vis": ("可见", 40)}
        for c in cols:
            text, w = headers[c]
            self.bsp_tree.heading(c, text=text)
            self.bsp_tree.column(c, width=w, anchor="center")
        vsb = ttk.Scrollbar(tree_box, orient="vertical", command=self.bsp_tree.yview)
        self.bsp_tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.bsp_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # 图表容器（后 pack，占据剩余空间）
        self.plot_frame = ttk.Frame(body)
        self.plot_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    def on_analyze(self):
        code = self.code_var.get().strip()
        if not code:
            messagebox.showwarning("提示", "请输入股票/ETF代码")
            return
        lv = LEVELS[self.lv_var.get()]
        begin = self.begin_var.get().strip() or None
        try:
            x_range = int(self.xrange_var.get().strip() or "0")
            if x_range < 0:
                x_range = 0
        except ValueError:
            messagebox.showwarning("提示", "显示根数必须是整数（0=全部）")
            return

        # 计算在后台线程跑，避免界面卡死；绘图回到主线程
        self.btn.config(state=tk.DISABLED)
        self._set_status(f"正在加载 {code} ...", "blue")
        threading.Thread(target=self._worker, args=(code, lv, begin, x_range), daemon=True).start()

    def _worker(self, code, lv, begin, x_range):
        try:
            chan = CChan(
                code=code,
                begin_time=begin,
                end_time=None,
                data_src=DATA_SRC.AKSHARE,
                lv_list=[lv],
                config=CChanConfig(CHAN_CONFIG),
                autype=AUTYPE.QFQ,
            )
            # 仅把「数据加载+缠论计算」放后台；绘图(plt.subplots)必须回主线程
            self.after(0, lambda: self._draw(chan, code, x_range))
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            tb = traceback.format_exc()
            self.after(0, lambda: self._show_error(err, tb))

    def _draw(self, chan, code, x_range):
        try:
            # 每次复制一份 plot_para，按界面输入覆盖 x_range，避免污染全局配置
            plot_para = {k: dict(v) for k, v in PLOT_PARA.items()}
            plot_para.setdefault("figure", {})["x_range"] = x_range
            driver = CPlotDriver(chan, plot_config=PLOT_CONFIG, plot_para=plot_para)
            # 标题加上证券名称：原标题形如 "600519/DAY"，改为 "600519 贵州茅台/DAY"
            name = get_security_name(code)
            if name:
                for ax in driver.figure.axes:
                    t = ax.get_title(loc="left")
                    if t.startswith(code):
                        ax.set_title(t.replace(code, f"{code} {name}", 1), loc="left",
                                     fontsize=16, color="r")
                        break
            self._add_bsp_legend(driver.figure)
            self._fill_bsp_list(chan, x_range)
            self._show_figure(driver.figure, f"{code} {name}".strip(), len(chan[0].lst))
        except Exception as e:
            self._show_error(f"{type(e).__name__}: {e}", traceback.format_exc())

    def _fill_bsp_list(self, chan, x_range):
        """把当前级别所有买卖点按时间列入右侧侧栏，并标注是否在画面可见范围内。

        画面 x 轴用「原始K线」坐标（与 PlotMeta.klu_len 一致），只画最近 x_range 根
        （0=全部）；klu.idx < klu_len-x_range 的点被截断到画面外。
        """
        for item in self.bsp_tree.get_children():
            self.bsp_tree.delete(item)

        kl_list = chan[0]
        # 与 Plot/PlotMeta.klu_len 对齐：合并K线内含的原始K线总数（非合并根数）
        klu_len = sum(len(klc.lst) for klc in kl_list.lst)
        x_begin = (klu_len - x_range) if (x_range and klu_len > x_range) else 0

        # 颜色与图表一致：买暖色/卖冷色，按主类型(1/2/3)
        for tag, color in (("buy1", BSP_BUY_COLORS["1"]), ("buy2", BSP_BUY_COLORS["2"]),
                           ("buy3", BSP_BUY_COLORS["3"]), ("sell1", BSP_SELL_COLORS["1"]),
                           ("sell2", BSP_SELL_COLORS["2"]), ("sell3", BSP_SELL_COLORS["3"])):
            self.bsp_tree.tag_configure(tag, foreground=color)

        bsps = sorted(kl_list.bs_point_lst.bsp_iter(), key=lambda b: b.klu.idx)
        n_buy = n_sell = n_hidden = 0
        for b in bsps:
            main = _bsp_main_type(b.type2str())
            side = "买" if b.is_buy else "卖"
            label = f"{'b' if b.is_buy else 's'}{b.type2str()}"
            price = b.klu.low if b.is_buy else b.klu.high
            visible = b.klu.idx >= x_begin
            tag = f"{'buy' if b.is_buy else 'sell'}{main or '1'}"
            self.bsp_tree.insert(
                "", tk.END,
                values=(b.klu.time.to_str(), f"{side} {label}", label,
                        f"{price:.3f}", "✓" if visible else "✕"),
                tags=(tag,))
            n_buy += b.is_buy
            n_sell += not b.is_buy
            n_hidden += not visible

        self.bsp_summary.config(
            text=f"共 {len(bsps)} 个：买{n_buy} 卖{n_sell}"
                 + (f"，{n_hidden} 个在画面外" if n_hidden else ""))

    def _add_bsp_legend(self, figure):
        """在主K线图左上角加买卖点类型分色图例（plot_bsp 关闭时跳过）。"""
        if not PLOT_CONFIG.get("plot_bsp"):
            return
        axes = figure.axes
        if not axes:
            return
        from matplotlib.lines import Line2D
        ax = axes[0]  # 主K线图
        handles = [Line2D([0], [0], marker='o', color='w', markerfacecolor=color,
                          markersize=9, label=f"{tag} {desc}")
                   for tag, color, desc in BSP_LEGEND]
        ax.legend(handles=handles, loc="upper right", fontsize=10,
                  framealpha=0.85, title="买卖点(买暖/卖冷色)")

    def _show_figure(self, figure, code, n):
        # 清掉旧图
        for w in self.plot_frame.winfo_children():
            w.destroy()
        self.canvas = FigureCanvasTkAgg(figure, master=self.plot_frame)
        self.canvas.draw()
        self.toolbar = NavigationToolbar2Tk(self.canvas, self.plot_frame)
        self.toolbar.update()
        self.canvas.get_tk_widget().pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self._bind_pan_zoom(figure)  # 滚轮缩放 + 按住拖拽平移
        self._set_status(f"{code} 完成，共 {n} 根合并K线（滚轮缩放/Shift+滚轮左右移/拖拽平移）", "green")
        self.btn.config(state=tk.NORMAL)

    def _bind_pan_zoom(self, figure):
        """给 canvas 绑定滚轮缩放（以光标为中心）和左键拖拽平移，无需切工具栏按钮。

        所有子图共享 x 轴缩放/平移（K线/MACD 等对齐），y 轴各自缩放。
        """
        self._pan_state = {"x": None, "y": None, "ax": None}

        def _on_scroll(event):
            ax = event.inaxes
            if ax is None or event.xdata is None or event.ydata is None:
                return
            # Shift + 滚轮 = 左右平移；普通滚轮 = 以光标为中心缩放
            if event.key == "shift":
                x0, x1 = ax.get_xlim()
                step = (x1 - x0) * 0.15
                step = -step if event.button == "up" else step  # 上滚向左、下滚向右
                for a in figure.axes:  # x 轴所有子图同步平移
                    ax0, ax1 = a.get_xlim()
                    a.set_xlim(ax0 + step, ax1 + step)
                self.canvas.draw_idle()
                return
            scale = 0.8 if event.button == "up" else 1.25  # 上滚放大、下滚缩小
            for a in figure.axes:
                self._zoom_axis(a, "x", event.xdata, scale)
            self._zoom_axis(ax, "y", event.ydata, scale)
            self.canvas.draw_idle()

        def _on_press(event):
            # 仅左键、且未启用工具栏的平移/缩放模式时才接管拖拽
            if event.button != 1 or event.inaxes is None:
                return
            if getattr(self.toolbar, "mode", ""):  # 工具栏已激活 pan/zoom，交给它
                return
            self._pan_state.update(x=event.xdata, y=event.ydata, ax=event.inaxes)

        def _on_motion(event):
            st = self._pan_state
            if st["ax"] is None or event.x is None:
                return
            # 用像素坐标换算成数据坐标增量，避免拖动时坐标系跳变
            inv = st["ax"].transData.inverted()
            x_new, y_new = inv.transform((event.x, event.y))
            dx, dy = st["x"] - x_new, st["y"] - y_new
            for a in figure.axes:  # x 轴所有子图同步平移
                x0, x1 = a.get_xlim()
                a.set_xlim(x0 + dx, x1 + dx)
            y0, y1 = st["ax"].get_ylim()  # y 轴仅当前子图
            st["ax"].set_ylim(y0 + dy, y1 + dy)
            self.canvas.draw_idle()

        def _on_release(event):
            self._pan_state.update(x=None, y=None, ax=None)

        self.canvas.mpl_connect("scroll_event", _on_scroll)
        self.canvas.mpl_connect("button_press_event", _on_press)
        self.canvas.mpl_connect("motion_notify_event", _on_motion)
        self.canvas.mpl_connect("button_release_event", _on_release)

    @staticmethod
    def _zoom_axis(ax, which, center, scale):
        """以 center 为锚点缩放某轴（保持锚点在屏幕位置不变）。"""
        lo, hi = ax.get_xlim() if which == "x" else ax.get_ylim()
        new_lo = center - (center - lo) * scale
        new_hi = center + (hi - center) * scale
        if which == "x":
            ax.set_xlim(new_lo, new_hi)
        else:
            ax.set_ylim(new_lo, new_hi)

    def _show_error(self, err, tb):
        self._set_status("加载失败", "red")
        self.btn.config(state=tk.NORMAL)
        messagebox.showerror("加载失败", f"{err}\n\n{tb}")

    def _set_status(self, text, color="gray"):
        self.status.config(text=text, foreground=color)


if __name__ == "__main__":
    ChanViewer().mainloop()
