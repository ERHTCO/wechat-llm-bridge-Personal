"""让显示器休息（关屏），但不影响系统运行。

原理：给所有窗口广播 SC_MONITORPOWER，等于手动触发"关闭显示器"。
- 系统不睡、不锁屏，后台程序照常跑
- 动一下鼠标或按任意键就能唤醒（标准行为）
- 发完**核对**：从电源通知里读显示器状态，没关成就重试，最多 3 次
  （以前只打印「已发送关屏指令」—— 发出去不等于屏幕黑了，这句是骗人的）

2026-11-08 记一笔：那天排查「息屏好像坏了」，屏幕其实关得好好的：
系统 5 分钟准时变暗、关屏，关了 45 分钟没人叫醒。真正把它弄亮的是
**这个脚本自己的 20 分钟自动唤醒**（原始输入日志里能看到两次 ±1 像素的
合成鼠标移动，正好在启动后 20 分钟）。所以默认改成不自动唤醒，
要自动唤醒就显式带分钟数。

用法：
    显示器休息.bat              关屏，不自动唤醒（动鼠标才醒）
    显示器休息.bat 20           20 分钟后自动唤醒
    python monitor_rest.py --no-wake     同上，不自动唤醒
"""
import argparse
import sys
import time

import dispstate

DEFAULT_MINUTES = 0          # 0 = 不自动唤醒


def monitor_off(verbose=True):
    """广播关屏并核对。返回 (状态, 说明)。"""
    ok, why = dispstate.monitor_off()
    if verbose:
        print(f"  {why}")
    return ok, why


def nudge():
    """轻推一下鼠标，把显示器唤醒（移动 1 像素再回来）。"""
    try:
        dispstate.user32.mouse_event(0x0001, 1, 0, 0, 0)   # MOUSEEVENTF_MOVE
        time.sleep(0.2)
        dispstate.user32.mouse_event(0x0001, -1, 0, 0, 0)
        return True
    except Exception as exc:
        print(f"唤醒失败: {exc}")
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("minutes", nargs="?", type=float, default=DEFAULT_MINUTES,
                    help="多少分钟后自动唤醒（0 = 不自动唤醒，默认 0）")
    ap.add_argument("--no-wake", action="store_true", help="不自动唤醒")
    args = ap.parse_args()

    minutes = 0.0 if args.no_wake else max(0.0, args.minutes)

    print("=" * 46)
    print("  3 秒后关闭显示器")
    print("  · 动鼠标/按键 随时唤醒")
    if minutes > 0:
        print(f"  · 或者等 {minutes:g} 分钟后自动唤醒")
    else:
        print("  · 不会自动唤醒（这轮就这样，动鼠标才醒）")
    print("=" * 46)
    for i in (3, 2, 1):
        time.sleep(1)
        print(f"  {i}...")

    ok, why = monitor_off()
    if ok is False:
        print("关屏没成功。可能是被策略拦了，或者有程序顶着显示器不让关。")
        print("  排查：powercfg /requests（要管理员）看有没有 DISPLAY 请求。")
        return 1
    if ok is None:
        print("指令发出去了，但读不到显示器状态，不敢跟你保证黑了 —— 你看一眼。")

    if minutes <= 0:
        print("不自动唤醒。动鼠标即可。")
        return 0

    total = int(minutes * 60)
    print(f"等待 {minutes:g} 分钟…（中途动鼠标不影响，只是会提前亮）")
    while total > 0:
        time.sleep(min(10, total))
        total -= 10

    print("时间到，唤醒显示器…")
    nudge()
    return 0


if __name__ == "__main__":
    sys.exit(main())
