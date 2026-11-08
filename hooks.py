"""钩子：一条消息处理流程里，让别的模块能插手的两个点。

只有两个阶段，别加：

  before_reply  —— 调模型之前。返回值会被合并成 dict 传给 Context，
                   目前只有 memory 用，往 ctx 里塞一段「你记得的事」。
  after_reply   —— 回复落库之后。只读，不允许改回复。

为什么要有这层：原来 memory 的两处调用是硬写在 handle() 里的，
加第三个功能就要再改一次 handle()。有了注册表，功能自己注册，
handle() 只认这两个点。

规矩（都是踩过的）：
  - 任何钩子抛异常，只记一行日志，不影响回复、不影响别的钩子。
  - 执行顺序 = 注册顺序，不做优先级。
  - clear() 给测试用；生产不该调。
"""
import threading

STAGES = ("before_reply", "after_reply")

_LOCK = threading.Lock()
_HOOKS = {stage: [] for stage in STAGES}      # stage -> [(name, fn)]


def register(stage, name, fn):
    """挂一个钩子。同名同阶段重复注册会替换掉旧的（热调试时不至于跑两遍）。"""
    if stage not in _HOOKS:
        raise ValueError(f"没有这个阶段: {stage}（只有 {', '.join(STAGES)}）")
    if not callable(fn):
        raise TypeError(f"{name} 不是可调用的")
    with _LOCK:
        slot = _HOOKS[stage]
        for i, (old_name, _) in enumerate(slot):
            if old_name == name:
                slot[i] = (name, fn)
                return
        slot.append((name, fn))


def unregister(stage, name):
    with _LOCK:
        _HOOKS[stage][:] = [(n, f) for n, f in _HOOKS[stage] if n != name]


def clear(stage=None):
    with _LOCK:
        for s in ((stage,) if stage else STAGES):
            _HOOKS[s].clear()


def names(stage):
    with _LOCK:
        return [n for n, _ in _HOOKS[stage]]


def run(stage, **kw):
    """跑一遍这个阶段，返回 {钩子名: 返回值}。

    单个钩子出错只打印，continue —— 记忆模块挂了不能让人回不了消息。
    """
    if stage not in _HOOKS:
        raise ValueError(f"没有这个阶段: {stage}")
    with _LOCK:
        snapshot = list(_HOOKS[stage])
    out = {}
    for name, fn in snapshot:
        try:
            out[name] = fn(**kw)
        except Exception as exc:
            print(f"[钩子] {stage}/{name} 出错：{type(exc).__name__}: {exc}", flush=True)
            out[name] = None
    return out


def collect(stage, **kw):
    """跑一遍并把各钩子的 dict 返回值合起来，给 Context 用。"""
    merged = {}
    for value in run(stage, **kw).values():
        if isinstance(value, dict):
            merged.update(value)
    return merged
