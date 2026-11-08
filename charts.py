"""手画的小图表，只用 QPainter，不引 QtCharts。

为什么不用 QtCharts：它在 PySide6-Addons 里，我为省体积在打包时排掉了；
而且它自带一套配色和字体，跟这里的深色主题对不上，调起来比手画还费劲。
这几个控件加起来几百行，颜色、圆角、留白都跟界面同一套。

约定：数据都是普通列表，`set_data()` 之后 `update()`，画的时候自己分配比例。
"""
import math
import random
import re

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import (QBrush, QColor, QFont, QFontMetrics, QPainter,
                           QPainterPath, QPen, QPixmap, QRadialGradient)
from PySide6.QtWidgets import QWidget

BG = "#0f131a"
LINE = "#232836"
TEXT = "#e6e9ef"
MUTED = "#8b93a3"
ACCENT = "#4c8dff"
OK = "#5ad48a"
WARN = "#ffb454"
BAD = "#ff7a8a"


def set_palette(c):
    """界面换配色时调一下，这几张图下一帧就用新颜色。"""
    global BG, LINE, TEXT, MUTED, ACCENT, OK, WARN, BAD
    BG = c.get("panel", BG)
    LINE = c.get("line", LINE)
    TEXT = c.get("text", TEXT)
    MUTED = c.get("muted", MUTED)
    ACCENT = c.get("accent", ACCENT)
    OK = c.get("ok", OK)
    WARN = c.get("warn", WARN)
    BAD = c.get("bad", BAD)


def _font(size=9, bold=False):
    f = QFont("Microsoft YaHei UI", size)
    f.setBold(bold)
    return f


class StackedBars(QWidget):
    """按时间堆叠的柱状图：每小时（或每天）干了多少，按类别分色。

    data = {"labels": [...], "series": [(名字, 颜色, [值...]), ...]}
    """

    def __init__(self, height=180):
        super().__init__()
        self.data = {"labels": [], "series": []}
        self.setMinimumHeight(height)
        self.setMouseTracking(True)
        self._hover = -1

    def set_data(self, labels, series):
        self.data = {"labels": list(labels), "series": list(series)}
        self.update()

    # 鼠标移到某根柱子上，标题栏显示这一格的具体数字
    def mouseMoveEvent(self, ev):
        n = len(self.data["labels"])
        if not n:
            return
        left, right = 34, 8
        w = max(1.0, (self.width() - left - right) / n)
        i = int((ev.position().x() - left) // w)
        i = max(0, min(n - 1, i))
        if i != self._hover:
            self._hover = i
            self.update()

    def leaveEvent(self, ev):
        self._hover = -1
        self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor(BG))
        labels = self.data["labels"]
        series = self.data["series"]
        if not labels or not series:
            p.setPen(QColor(MUTED))
            p.setFont(_font())
            p.drawText(self.rect(), Qt.AlignCenter, "还没有数据")
            return

        left, right, top, bottom = 34, 8, 20, 20
        plot = QRectF(left, top, max(1, self.width() - left - right),
                      max(1, self.height() - top - bottom))
        n = len(labels)
        stack = [sum(s[2][i] for s in series) for i in range(n)]
        peak = max(stack) or 1

        # 网格 + y 轴刻度（0 / 一半 / 顶）
        p.setFont(_font(8))
        for frac in (0, 0.5, 1.0):
            y = plot.bottom() - plot.height() * frac
            p.setPen(QPen(QColor(LINE), 1, Qt.DashLine if frac else Qt.SolidLine))
            p.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            p.setPen(QColor(MUTED))
            p.drawText(QRectF(0, y - 8, left - 6, 16),
                       Qt.AlignRight | Qt.AlignVCenter, str(int(round(peak * frac))))

        # 柱子
        slot = plot.width() / n
        bar_w = max(2.0, slot * 0.58)
        for i in range(n):
            x = plot.left() + slot * i + (slot - bar_w) / 2
            y = plot.bottom()
            for name, color, values in series:
                v = values[i] if i < len(values) else 0
                if not v:
                    continue
                h = plot.height() * v / peak
                y -= h
                path = QPainterPath()
                path.addRoundedRect(QRectF(x, y, bar_w, max(1.0, h)), 2, 2)
                p.fillPath(path, QColor(color))
            if i == self._hover:
                p.setPen(QPen(QColor(TEXT), 1))
                p.setBrush(Qt.NoBrush)
                p.drawRect(QRectF(plot.left() + slot * i, plot.top(), slot, plot.height()))

        # x 轴标签：只画一部分，免得挤成一团
        p.setPen(QColor(MUTED))
        p.setFont(_font(8))
        step = max(1, n // 8)
        for i in range(0, n, step):
            x = plot.left() + slot * i
            p.drawText(QRectF(x - 24, plot.bottom() + 3, 48, 14),
                       Qt.AlignCenter, labels[i])

        # 图例 + hover 详情
        if self._hover >= 0:
            parts = [f"{s[0]} {s[2][self._hover]}" for s in series if s[2][self._hover]]
            head = f"{labels[self._hover]}  " + (" · ".join(parts) or "无")
            p.setPen(QColor(TEXT))
            p.setFont(_font(8, False))
            p.drawText(QRectF(left, 2, plot.width(), 16), Qt.AlignLeft, head)


class Donut(QWidget):
    """环形占比图，中间写一个大数字。

    segments = [(名字, 颜色, 值), ...]，中心文字自己给。
    """

    def __init__(self, size=150):
        super().__init__()
        self.segments = []
        self.center_top = ""
        self.center_bottom = ""
        self.setMinimumSize(size, size)

    def set_data(self, segments, center_top="", center_bottom=""):
        self.segments = [(n, c, max(0, v)) for n, c, v in segments]
        self.center_top = center_top
        self.center_bottom = center_bottom
        self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor(BG))
        total = sum(s[2] for s in self.segments)
        side = min(self.width(), self.height())
        pad = side * 0.12
        ring = QRectF((self.width() - side) / 2 + pad, (self.height() - side) / 2 + pad,
                      side - pad * 2, side - pad * 2)
        thickness = max(8.0, side * 0.11)

        pen = QPen(QColor(LINE), thickness)
        pen.setCapStyle(Qt.FlatCap)
        p.setPen(pen)
        p.drawArc(ring, 0, 360 * 16)

        if total > 0:
            start = 90 * 16                     # 从 12 点开始，顺时针
            for name, color, value in self.segments:
                if value <= 0:
                    continue
                span = -int(360 * 16 * value / total)
                pen = QPen(QColor(color), thickness)
                pen.setCapStyle(Qt.FlatCap)
                p.setPen(pen)
                p.drawArc(ring, start, span)
                start += span

        p.setPen(QColor(TEXT))
        p.setFont(_font(14, True))
        p.drawText(ring, Qt.AlignCenter, self.center_top or "—")
        if self.center_bottom:
            p.setPen(QColor(MUTED))
            p.setFont(_font(8))
            r = QRectF(ring.left(), ring.center().y() + 10, ring.width(), 16)
            p.drawText(r, Qt.AlignHCenter | Qt.AlignTop, self.center_bottom)


class HBars(QWidget):
    """横向条形图，用来排「为什么丢弃」。

    rows = [(标签, 值, 颜色), ...]；条长按最大值归一，右边写数字。
    """

    def __init__(self, row_h=26):
        super().__init__()
        self.rows = []
        self.row_h = row_h
        self.empty = "还没有丢弃或失败"

    def set_data(self, rows, empty=None):
        self.rows = list(rows)
        if empty is not None:
            self.empty = empty
        self.setMinimumHeight(max(60, self.row_h * max(1, len(self.rows)) + 8))
        self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor(BG))
        if not self.rows:
            p.setPen(QColor(MUTED))
            p.setFont(_font())
            p.drawText(self.rect(), Qt.AlignCenter, self.empty)
            return

        peak = max(r[1] for r in self.rows) or 1
        label_w = min(210, max(96, int(self.width() * 0.34)))
        num_w = 46
        fm = QFontMetrics(_font(8))
        for i, (label, value, color) in enumerate(self.rows):
            y = i * self.row_h + 4
            p.setPen(QColor(MUTED))
            p.setFont(_font(8))
            text = fm.elidedText(label, Qt.ElideRight, int(label_w - 10))
            p.drawText(QRectF(0, y, label_w - 8, self.row_h - 8),
                       Qt.AlignRight | Qt.AlignVCenter, text)

            track = QRectF(label_w, y + 5, max(1, self.width() - label_w - num_w),
                           self.row_h - 16)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(LINE))
            p.drawRoundedRect(track, 3, 3)
            w = track.width() * value / peak
            if w > 0:
                p.setBrush(QColor(color))
                p.drawRoundedRect(QRectF(track.left(), track.top(), max(2.0, w),
                                         track.height()), 3, 3)
            p.setPen(QColor(TEXT))
            p.setFont(_font(9))
            p.drawText(QRectF(track.right() + 6, y, num_w, self.row_h - 8),
                       Qt.AlignLeft | Qt.AlignVCenter, str(value))


# ------------------------------------------------------------------ 记忆星云
# kind 到颜色的映射。不新增主题键，全从已有语义色派生：
# 事实用主色、偏好用 ok、计划用 warn、事件用 bad，「关系」没有对应语义色，
# 单独给一个紫——三套主题下都能看清，切主题时其余几个一起跟着变。
KIND_COLORS = {"fact": "accent", "preference": "ok", "relation": "#a78bfa",
               "plan": "warn", "event": "bad", "unknown": "muted"}
KIND_LABELS = {"fact": "事实", "preference": "偏好", "relation": "关系",
               "plan": "计划", "event": "事件", "unknown": "其他"}

_SPRITES = {}          # 发光贴图缓存：(r,g,b) -> QPixmap
_GOLDEN = math.pi * (3.0 - math.sqrt(5.0))


def _on_sphere(i, n, r):
    """斐波那契球面：第 i 个（共 n 个）在半径 r 的球面上的均匀位置。

    3D 力导向有个通病：如果从球心附近的小随机位置出发，z 方向初始分离太小，
    斥力会把整团压成一张平面。一开始就撒在球面上就不会塌。
    """
    y = 1.0 - (i / max(1, n - 1)) * 2.0
    rad = math.sqrt(max(0.0, 1.0 - y * y))
    th = i * _GOLDEN
    return math.cos(th) * rad * r, y * r, math.sin(th) * rad * r


def _kind_color(kind):
    v = KIND_COLORS.get(kind, "muted")
    if v.startswith("#"):
        return QColor(v)
    return QColor({"accent": ACCENT, "ok": OK, "warn": WARN,
                   "bad": BAD, "muted": MUTED}.get(v, MUTED))


def _grams(s):
    """字符二元组，用来判断两条记忆算不算「说得是一件事」。"""
    s = re.sub(r"[^\w\u4e00-\u9fff]+", "", (s or "").lower())
    if len(s) < 2:
        return {s} if s else set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _sprite(color):
    """把一个发光点预渲染成 64×64，只按 RGB 缓存（alpha 用 setOpacity 控制）。

    3D 时每帧要画上百个点，每个都现配一次 QRadialGradient 太慢；预渲染成
    QPixmap 之后每帧只剩 drawPixmap。alpha 不进缓存键，否则每种深度都是
    一张新图，缓存会爆。
    """
    key = (color.red(), color.green(), color.blue())
    pm = _SPRITES.get(key)
    if pm is not None:
        return pm
    r, g, b = key
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    gr = QRadialGradient(32.0, 32.0, 31.0)
    gr.setColorAt(0.00, QColor(255, 255, 255, 245))
    gr.setColorAt(0.20, QColor(r, g, b, 235))
    gr.setColorAt(0.52, QColor(r, g, b, 110))
    gr.setColorAt(1.00, QColor(r, g, b, 0))
    p.setBrush(QBrush(gr))
    p.setPen(Qt.NoPen)
    p.drawEllipse(QRectF(0, 0, 64, 64))
    p.end()
    _SPRITES[key] = pm
    return pm


class MemoryGraph(QWidget):
    """记忆星云：同一份数据，两种看法。

    2D  力导向铺在平面上，看「哪些记忆说的事情挨得近」
    3D  同一套力导向在球里跑，透视投影 + 发光 + 按深度排序，可拖拽旋转

    3D 是手写的，不引 three.js / pyqtgraph：投影 = 绕 y 轴、绕 x 轴各转一次
    再除以透视分母；发光 = 径向渐变预渲染成 QPixmap。整张图仍然只用 QPainter。

    坐标一律是**以控件中心为原点**的局部坐标：2D 时 z 恒为 0，3D 时 x/y/z
    都在半径 SPHERE×短边 的球里。所以切模式不用换坐标，跑一遍力导向就位。
    """

    MAX_NODES = 240
    FOV = 900.0       # 透视焦距：越大越平，越小「近大远小」越夸张
    SPHERE = 0.70     # 3D 球半径 / 控件短边

    def __init__(self, height=320):
        super().__init__()
        self.mode = "2d"
        self.nodes = []
        self.edges = []
        self.temp = 0.0
        self._cool = 1.0
        self._hover = None
        self._drag = None
        self._turn = None
        self._note = ""
        self.yaw = 0.35
        self.pitch = -0.22
        self.zoom = 1.0
        self._stars = []
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._step)
        self.setMinimumHeight(height)
        self.setMouseTracking(True)

    # -------------------------------------------------------------- 模式
    def set_mode(self, mode):
        """切换 2D / 3D。切完重新加热，让点自己走过去，不是瞬移。"""
        mode = "3d" if str(mode).lower().startswith("3") else "2d"
        if mode == self.mode:
            return
        self.mode = mode
        self._drag = None
        self._turn = None
        self._hover = None
        if mode == "3d":
            # 切到 3D 时如果所有点还贴在 z=0 的平面上，力导向永远拉不出来 ——
            # 斥力的 z 分量正比于 dz，全为 0 就恒为 0。所以先手动撒回球面。
            # 这条只在「先 2D 加载、再切 3D」时触发；3D 下直接 set_data 不会进来。
            if self.nodes and max(abs(nd["z"]) for nd in self.nodes) < 0.5:
                R = self._radius()
                n = len(self.nodes)
                for i, nd in enumerate(self.nodes):
                    nd["x"], nd["y"], nd["z"] = _on_sphere(i, n, R * 0.82)
        else:
            for nd in self.nodes:
                nd["z"] = 0.0
        self._restart(0.85)
        self.update()

    def mode_name(self):
        return "3D" if self.mode == "3d" else "2D"

    # -------------------------------------------------------------- 数据
    def set_data(self, rows, note=""):
        """rows 用 memory.list_atoms() 那种字典。"""
        rows = [r for r in (rows or []) if (r.get("text") or "").strip()]
        rows.sort(key=lambda r: -float(r.get("importance") or 0))
        rows = rows[:self.MAX_NODES]
        old = {n["id"]: (n["x"], n["y"], n["z"]) for n in self.nodes}
        R = self._radius()
        self._note = note
        self.nodes = []
        for i, r in enumerate(rows):
            if r.get("id") in old:
                x, y, z = old[r.get("id")]
            elif self.mode == "3d":
                x, y, z = _on_sphere(i, len(rows), R * 0.82)
            else:
                x, y, z = (random.uniform(-30, 30), random.uniform(-30, 30), 0.0)
            imp = max(0.0, min(1.0, float(r.get("importance") or 0.5)))
            self.nodes.append({
                "id": r.get("id"),
                "text": (r.get("text") or "").strip(),
                "kind": (r.get("kind") or "unknown").lower(),
                "importance": imp,
                "uses": int(r.get("uses") or 0),
                "status": r.get("status") or "active",
                "r": 3.0 + 7.0 * imp,
                "x": float(x), "y": float(y), "z": 0.0 if self.mode == "2d" else float(z),
            })
        self._build_edges()
        self._restart()
        self.update()

    def _restart(self, frac=1.0):
        """重新加热。温度是像素量：从 0.1 倍短边降到 0 大约三百帧。"""
        if not self.nodes:
            self._timer.stop()
            return
        w = max(200, self.width() or 900)
        h = max(150, self.height() or 600)
        t0 = 0.10 * min(w, h)
        self.temp = t0 * max(0.15, min(1.0, frac))
        # 降温要慢到「力能追上」。原来地板 0.45，t0=40 时 87 帧就冻住了，
        # 那时斥力刚把点推到边上、中心引力还没机会把它们拉回来。
        self._cool = max(0.10, t0 / 300.0)
        self._timer.start()

    def _build_edges(self):
        """两条记忆的二元组重合度够高就连一条线。"""
        self.edges = []
        n = len(self.nodes)
        if n < 2:
            return
        grams = [_grams(d["text"]) for d in self.nodes]
        for i in range(n):
            if not grams[i]:
                continue
            for j in range(i + 1, n):
                if not grams[j]:
                    continue
                inter = len(grams[i] & grams[j])
                if inter < 2:
                    continue
                w = inter / min(len(grams[i]), len(grams[j]))
                if w >= 0.22:
                    self.edges.append((i, j, w))

    def _radius(self):
        w = max(1, self.width())
        h = max(1, self.height())
        return (self.SPHERE if self.mode == "3d" else 0.46) * min(w, h)

    # -------------------------------------------------------------- 力导向
    def _step(self):
        """Fruchterman-Reingold：斥力 k²/d、引力 d²/k，每帧位移不超过温度。

        2D 和 3D 共用这一套，区别只有 z 参不参与、往圆心还是往球心收。
        """
        n = len(self.nodes)
        if not n:
            self._timer.stop()
            return
        if self.temp <= 0.5:
            self._timer.stop()
            self.temp = 0.0
            return
        three = self.mode == "3d"
        temp = self.temp
        w = max(1, self.width())
        h = max(1, self.height())
        k = max(24.0, min(70.0, 0.55 * min(w, h) / math.sqrt(n)))
        disp = [[0.0, 0.0, 0.0] for _ in range(n)]

        for i in range(n):
            a = self.nodes[i]
            ax, ay, az = a["x"], a["y"], a["z"]
            for j in range(i + 1, n):
                b = self.nodes[j]
                dx = ax - b["x"]
                dy = ay - b["y"]
                dz = (az - b["z"]) if three else 0.0
                d = math.sqrt(dx * dx + dy * dy + dz * dz)
                if d < 0.01:
                    dx = random.random() - 0.5
                    dy = random.random() - 0.5
                    dz = (random.random() - 0.5) if three else 0.0
                    d = 0.7
                # 3D 的斥力要比 2D 弱：46 个点的斥力叠加起来足以把每个点都推到
                # 边界上，径向约束拉不回来，球就变成一层空壳。
                #
                # 2D 还要再除以 (n-1)：斥力是「每对」算的，17 个点就是叠加 16 次，
                # 再小的系数也会把每个点直接顶到边界（实测改系数、改上限都没用，
                # 范围永远停在 ±170 = 夹取半径）。除完之后是平均场，量级才对。
                f = (k * k * (0.26 if three else 0.55)) / d
                if not three:
                    f /= max(1, n - 1)
                if f > k * 0.9:
                    f = k * 0.9
                ux, uy, uz = dx / d, dy / d, (dz / d if three else 0.0)
                disp[i][0] += ux * f
                disp[i][1] += uy * f
                disp[i][2] += uz * f
                disp[j][0] -= ux * f
                disp[j][1] -= uy * f
                disp[j][2] -= uz * f

        for i, j, wgt in self.edges:
            a, b = self.nodes[i], self.nodes[j]
            dx = a["x"] - b["x"]
            dy = a["y"] - b["y"]
            dz = (a["z"] - b["z"]) if three else 0.0
            d = math.sqrt(dx * dx + dy * dy + dz * dz) or 0.01
            # 线性引力，不是 Fruchterman-Reingold 原来的 d²/k：d=200 时 d²/k 有
            # 一千多，而球壳约束每帧只推几 px，3D 里整团会被拉成一条链。
            # 但 2D 又不能太弱，否则斥力把点全推到上下边缘、中间空掉，所以分开取系。
            coef = 0.06 if three else 0.55
            f = (d - k) * coef * min(1.0, wgt + 0.15)
            disp[i][0] -= dx / d * f
            disp[i][1] -= dy / d * f
            disp[i][2] -= dz / d * f
            disp[j][0] += dx / d * f
            disp[j][1] += dy / d * f
            disp[j][2] += dz / d * f

        R = self._radius()
        for idx, nd in enumerate(self.nodes):
            dx, dy, dz = -nd["x"], -nd["y"], (-nd["z"] if three else 0.0)
            dd = math.sqrt(dx * dx + dy * dy + dz * dz)
            if three:
                # 3D 不用「往球心的向心力」——那会把点全收在球心，投影成一坨。
                # 改成径向约束，但目标是**球体内部**而不是球壳：只推到壳上的话，
                # 换个角度一看就穿帮，整个球中间是空的、像个甜甜圈。
                target = R * (0.25 + 0.68 * (1.0 - nd["importance"]))
                if dd > 0.01:
                    move = (dd - target) * 0.22
                    disp[idx][0] += dx / dd * move
                    disp[idx][1] += dy / dd * move
                    disp[idx][2] += dz / dd * move
            else:
                # 2D 也走「径向目标」，不是朝圆心的裸弹簧。
                # 裸弹簧（力度 ∝ 离中心距离）的平衡点在无穷远：斥力再弱也能把点
                # 一路推到外圈，最后是一圈贴边的散沙、中间空掉。给每个点一个目标
                # 半径（importance 越大越靠里），它才会停在中间几层、图才是「一团」。
                target = R * (0.15 + 0.70 * (1.0 - nd["importance"]))
                if dd > 0.01:
                    move = (dd - target) * 0.22
                    disp[idx][0] += dx / dd * move
                    disp[idx][1] += dy / dd * move

        step = max(0.0, temp)
        for idx, nd in enumerate(self.nodes):
            dx, dy, dz = disp[idx]
            mag = math.sqrt(dx * dx + dy * dy + dz * dz)
            if mag > 0.001:
                s = min(mag, step) / mag
                nd["x"] += dx * s
                nd["y"] += dy * s
                nd["z"] += dz * s
            if three:
                d = math.sqrt(nd["x"] ** 2 + nd["y"] ** 2 + nd["z"] ** 2)
                lim = max(1.0, R - nd["r"] - 4)
                if d > lim and d > 0.01:
                    s = lim / d
                    nd["x"] *= s
                    nd["y"] *= s
                    nd["z"] *= s
            else:
                nd["z"] *= 0.7              # 从 3D 切回来时把 z 慢慢压平
                if abs(nd["z"]) < 0.5:
                    nd["z"] = 0.0
                # 2D 也收进「半径 R 的圆」里，不是收进外接矩形。
                # 收矩形的话点会贴到四边、中间空一大片（实测 17 个点里有 13 个
                # 落在中间一半高度之外），图看着像一圈散沙而不是一团。
                d = math.sqrt(nd["x"] ** 2 + nd["y"] ** 2)
                lim = max(1.0, R - nd["r"] - 4)
                if d > lim and d > 0.01:
                    s = lim / d
                    nd["x"] *= s
                    nd["y"] *= s

        self.temp = max(0.0, temp - self._cool)
        self.update()

    # -------------------------------------------------------------- 投影
    def _project(self, nd):
        """局部坐标 → 屏幕坐标，返回 (x, y, scale, depth)。"""
        w = max(1, self.width())
        h = max(1, self.height())
        if self.mode == "2d":
            return w / 2 + nd["x"], h / 2 + nd["y"], 1.0, 0.0
        cy, sy = math.cos(self.yaw), math.sin(self.yaw)
        cp, sp = math.cos(self.pitch), math.sin(self.pitch)
        x0, y0, z0 = nd["x"], nd["y"], nd["z"]
        x = x0 * cy - z0 * sy
        z = x0 * sy + z0 * cy
        y = y0 * cp - z * sp
        z = y0 * sp + z * cp
        s = self.FOV / (self.FOV + z + 420.0) * self.zoom
        return w / 2 + x * s, h / 2 + y * s, s, z

    def _nearest(self, pos):
        """屏幕上离鼠标最近、且落在光点里的那个。"""
        best, best_d = None, float("inf")
        px, py = pos.x(), pos.y()
        for i, nd in enumerate(self.nodes):
            sx, sy, s, _z = self._project(nd)
            d2 = (sx - px) ** 2 + (sy - py) ** 2
            rr = (nd["r"] * s + 9) ** 2
            if d2 <= rr and d2 < best_d:
                best, best_d = i, d2
        return best

    # -------------------------------------------------------------- 交互
    def mousePressEvent(self, ev):
        i = self._nearest(ev.position())
        if i is not None:
            self._drag = i
            self._hover = i
            self.update()
            return
        if self.mode == "3d":
            self._turn = (ev.position().x(), ev.position().y())

    def mouseMoveEvent(self, ev):
        if self._drag is not None and self._drag < len(self.nodes):
            self._move_node(self.nodes[self._drag], ev.position())
            if self.temp <= 0.5:
                self._restart(0.30)
            self.update()
            return
        if self._turn is not None:
            lx, ly = self._turn
            self.yaw += (ev.position().x() - lx) * 0.006
            self.pitch = max(-1.30, min(1.30,
                                        self.pitch + (ev.position().y() - ly) * 0.005))
            self._turn = (ev.position().x(), ev.position().y())
            self.update()
            return
        i = self._nearest(ev.position())
        if i != self._hover:
            self._hover = i
            self.update()

    def mouseReleaseEvent(self, ev):
        self._drag = None
        self._turn = None

    def wheelEvent(self, ev):
        if self.mode != "3d":
            return
        step = 1.08 if ev.angleDelta().y() > 0 else 1 / 1.08
        self.zoom = max(0.5, min(2.6, self.zoom * step))
        self.update()

    def _move_node(self, nd, pos):
        """把鼠标位置换算回局部坐标。3D 下按当前缩放比反算，够用。"""
        w = max(1, self.width())
        h = max(1, self.height())
        if self.mode == "2d":
            nd["x"] = pos.x() - w / 2
            nd["y"] = pos.y() - h / 2
            nd["z"] = 0.0
            return
        sx, sy, s, _z = self._project(nd)
        inv = 1.0 / max(0.2, s)
        dx = (pos.x() - sx) * inv
        dy = (pos.y() - sy) * inv
        nd["x"] += dx * math.cos(-self.yaw)
        nd["z"] += dx * math.sin(-self.yaw)
        nd["y"] += dy

    def leaveEvent(self, ev):
        self._hover = None
        self.update()

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._make_stars()
        if self.nodes and self.temp <= 0.5:
            self._restart(0.35)

    def _make_stars(self):
        """背景星点。固定种子，窗口大小不变就一直是同一片星空。"""
        rnd = random.Random(20261031)
        n = max(40, (max(1, self.width()) * max(1, self.height())) // 9000)
        self._stars = [(rnd.uniform(0, self.width()), rnd.uniform(0, self.height()),
                        rnd.uniform(0.4, 1.3), rnd.randint(22, 85)) for _ in range(n)]

    # -------------------------------------------------------------- 绘制
    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor(BG))
        if not self.nodes:
            p.setPen(QColor(MUTED))
            p.setFont(_font())
            p.drawText(self.rect(), Qt.AlignCenter,
                       "还没有记忆 —— 聊满几轮，后台才会攒第一批")
            return

        three = self.mode == "3d"
        if three:
            self._draw_stars(p)

        hot = self._hover if (self._hover is not None and self._hover < len(self.nodes)) else None
        pts = [self._project(nd) for nd in self.nodes]

        # 连线压在点下面；连到 hover 的那几条提亮
        for i, j, _w in self.edges:
            ax, ay, _as, az = pts[i]
            bx, by, _bs, bz = pts[j]
            if hot in (i, j):
                p.setPen(QPen(QColor(TEXT), 1.2))
            else:
                depth = max(-1.0, min(1.0, (az + bz) / 600.0)) if three else 0.0
                c = QColor(LINE)
                c.setAlpha(int(255 * max(0.04, 0.26 * (1.0 - depth * 0.35))))
                p.setPen(QPen(c, 1.0))
            p.drawLine(QPointF(ax, ay), QPointF(bx, by))

        # 点：3D 时按深度从远到近画
        order = (sorted(range(len(self.nodes)), key=lambda i: pts[i][3])
                 if three else range(len(self.nodes)))
        for idx in order:
            nd = self.nodes[idx]
            sx, sy, s, _z = pts[idx]
            c = _kind_color(nd["kind"])
            if nd["status"] != "active":
                c = QColor(c)
                c.setAlpha(70)
            if three:
                # s 的中景值是 0.68（z=0 时），拿它归一化：中间的点跟 2D 一样大，
                # 近的更大、远的更小更暗，深度才看得出来。
                radius = max(1.6, nd["r"] * (s / 0.68))
                glow = radius * 3.4
                alpha = 0.45 + 0.55 * max(0.0, min(1.0, (s - 0.50) / 0.45))
            else:
                radius = nd["r"]
                glow = radius * 2.6
                alpha = 0.92
            p.setOpacity(alpha)
            p.drawPixmap(QRectF(sx - glow / 2, sy - glow / 2, glow, glow),
                         _sprite(c), QRectF(0, 0, 64, 64))
            p.setOpacity(1.0)
            if idx == hot:
                p.setPen(QPen(QColor(TEXT), 1.3))
                p.setBrush(Qt.NoBrush)
                p.drawEllipse(QPointF(sx, sy), radius + 5, radius + 5)

        # 标名字：2D 按重要度，3D 按远近；和已画的标签重叠就跳过
        p.setFont(_font(8))
        cand = (sorted(range(len(self.nodes)), key=lambda i: pts[i][3]) if three
                else sorted(range(len(self.nodes)),
                            key=lambda i: -self.nodes[i]["importance"]))
        taken = []
        for idx in cand[:8 if three else 10]:
            nd = self.nodes[idx]
            sx, sy, s, _z = pts[idx]
            t = nd["text"]
            if len(t) > 9:
                t = t[:9] + "…"
            box = QRectF(sx - 56, sy + nd["r"] * s + 3, 112, 14)
            if any(box.intersects(o) for o in taken):
                continue
            taken.append(box)
            p.setPen(QColor(TEXT if nd["importance"] >= 0.6 else MUTED))
            p.drawText(box, Qt.AlignHCenter | Qt.AlignTop, t)

        # 底部一行：正常是统计，hover 时换成那条记忆的原文
        p.setPen(Qt.NoPen)
        shade = QColor(BG)
        shade.setAlpha(215)
        p.setBrush(shade)
        p.drawRect(QRectF(0, self.height() - 22, self.width(), 22))

        p.setFont(_font(8))
        y = self.height() - 18
        if hot is not None:
            nd = self.nodes[hot]
            label = KIND_LABELS.get(nd["kind"], nd["kind"])
            head = f"[{label}] 重要 {nd['importance']:.2f} · 想起来 {nd['uses']} 次"
            fm = QFontMetrics(_font(8))
            p.setPen(QColor(_kind_color(nd["kind"])))
            p.drawText(QRectF(8, y, self.width() - 16, 15), Qt.AlignLeft, head)
            hw = fm.horizontalAdvance(head) + 14
            room = max(40, int(self.width() - 24 - hw))
            p.setPen(QColor(TEXT))
            p.drawText(QRectF(8 + hw, y, room, 15), Qt.AlignLeft,
                       fm.elidedText(nd["text"], Qt.ElideRight, room))
        else:
            p.setPen(QColor(MUTED))
            tip = self._note or f"{len(self.nodes)} 条记忆 · {len(self.edges)} 条连线"
            p.drawText(QRectF(8, y, self.width() - 200, 15), Qt.AlignLeft, tip)
            p.drawText(QRectF(self.width() - 210, y, 194, 15), Qt.AlignRight,
                       "拖空白旋转 · 滚轮缩放" if three else "拖节点能挪，悬停看全文")

    def _draw_stars(self, p):
        if not self._stars:
            return
        p.setPen(Qt.NoPen)
        for x, y, r, a in self._stars:
            c = QColor(TEXT)
            c.setAlpha(a)
            p.setBrush(c)
            p.drawEllipse(QPointF(x, y), r, r)
