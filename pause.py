"""两轮之间「是电脑睡了，还是我们自己卡了」。

监听器原来只有一个判据：两轮间隔超过 90 秒就算休眠过。但一次 UIA 调用卡住
90 秒也是同样的间隔 —— 那时候基线被重置，卡顿期间来的真实消息会被当成
「唤醒后的积压」丢掉。

Windows 给了两个时钟，差值就是答案：

    GetTickCount64()                开机以来的毫秒数（**含**睡眠）
    QueryUnbiasedInterruptTime()    醒着的时间（10^-7 秒，**不含**睡眠）

墙钟走得多、醒着的时间走得少，差出来的那段就是睡过去了。跑得慢的时候
两个时钟一起走，差值是 0 —— 这时候该记一句「上一轮卡了 N 秒」，不是重置基线。

这两个 API 都拿不到时返回 None，调用方退回「只看间隔」的老判据。
"""
import ctypes


def clock_pair():
    """返回 (含睡眠的毫秒, 不含睡眠的秒)；拿不到返回 None。"""
    try:
        k32 = ctypes.windll.kernel32
        tick = k32.GetTickCount64()
        ub = ctypes.c_ulonglong()
        if not k32.QueryUnbiasedInterruptTime(ctypes.byref(ub)):
            return None
        return float(tick), ub.value / 10_000_000.0
    except Exception:
        return None


def slept_seconds(tick_delta_ms, awake_delta_s):
    """两次采样之间的睡眠时间（秒）。负数按 0 算（时钟精度误差）。"""
    try:
        return max(0.0, float(tick_delta_ms) / 1000.0 - float(awake_delta_s))
    except (TypeError, ValueError):
        return 0.0


def gap_kind(gap_s, slept_s, suspend_s=90.0, slow_s=60.0):
    """这一轮开始前的空档该怎么理解。

    resume  真的休眠过（睡的时间够长）
    slow    我们自己卡住了 / 上一轮跑了很久
    normal  正常节奏
    """
    slept_s = max(0.0, float(slept_s or 0.0))
    gap_s = max(0.0, float(gap_s or 0.0))
    if slept_s >= suspend_s:
        return "resume"
    if gap_s >= slow_s:
        return "slow"
    return "normal"


def check():
    """在 `.venv` 里跑 `python pause.py` 自检两个 API 通不通。"""
    pair = clock_pair()
    if pair is None:
        print("拿不到时钟：这两个 API 不可用，监听器会退回只看间隔")
        return 1
    print(f"含睡眠 {pair[0]/1000:.0f}s | 不含睡眠 {pair[1]:.0f}s")
    print("差值（睡眠）:", round(slept_seconds(pair[0], pair[1]), 3), "秒")
    return 0


if __name__ == "__main__":
    raise SystemExit(check())
