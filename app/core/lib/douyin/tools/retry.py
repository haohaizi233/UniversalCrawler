"""提供异步请求重试与文件占用交互重试装饰器。"""

from asyncio import sleep
from random import randint
# 独立加载该文件时使用同等默认值，避免包级循环导入中断重试逻辑。
try:
    from . import RETRY, wait
except ImportError:
    RETRY = 5

    async def wait() -> None:
        await sleep(randint(5, 20) * 0.1)
try:
    from ..translation import _
except ImportError:
    def _(x):
        return x

__all__ = ["Retry", "abort_if_stopped"]


def _is_stop_requested(owner: object) -> bool:
    """只把严格的 True 当作停止信号，避免测试替身的可调用对象被误判。"""
    flag = getattr(owner, "_stop_requested", None)
    if flag is True:
        return True
    check = getattr(owner, "_stop_check", None)
    if not callable(check):
        return False
    try:
        return check() is True
    except Exception:
        return False


async def abort_if_stopped(owner: object) -> bool:
    """停止时关闭正在使用的客户端，让挂起的请求尽快失败返回。"""
    if not _is_stop_requested(owner):
        return False
    client = getattr(owner, "client", None)
    close = getattr(client, "aclose", None)
    if callable(close):
        try:
            await close()
        except Exception:
            pass
    return True


class Retry:
    """以返回值真假判断本项目操作是否成功。"""
    @staticmethod
    def retry(function):
        """失败时按实例配置等待重试，并在耗尽后再执行最后一次。"""
        async def inner(self, *args, **kwargs):
            finished = kwargs.pop("finished", False)
            for i in range(self.max_retry):
                if await abort_if_stopped(self):
                    self.finished = True
                    return None
                if result := await function(self, *args, **kwargs):
                    return result
                if await abort_if_stopped(self):
                    self.finished = True
                    return None
                self.log.warning(_("正在进行第 {index} 次重试").format(index=i + 1))
                await wait()
            if await abort_if_stopped(self):
                self.finished = True
                return None
            if not (result := await function(self, *args, **kwargs)) and finished:
                self.finished = True
            return result
        return inner
    @staticmethod
    def retry_lite(function):
        """为不依赖实例状态的异步函数提供固定次数重试。"""
        async def inner(*args, **kwargs):
            def stopped() -> bool:
                for owner in (*args, *kwargs.values()):
                    if _is_stop_requested(owner):
                        return True
                return False

            if stopped():
                return None
            if r := await function(*args, **kwargs):
                return r
            for _ in range(RETRY):
                if stopped():
                    return None
                if r := await function(*args, **kwargs):
                    return r
                if stopped():
                    return None
                await wait()
            return r
        return inner
    @staticmethod
    def retry_limited(function):
        """文件操作失败时允许用户选择继续重试或跳过。"""
        def inner(self, *args, **kwargs):
            while True:
                if function(self, *args, **kwargs):
                    return
                if self.console.input(
                        _(
                            "如需重新尝试处理该对象，请关闭所有正在访问该对象的窗口或程序，然后直接按下回车键！\n"
                            "如需跳过处理该对象，请输入任意字符后按下回车键！"
                        ),
                ):
                    return
        return inner
    @staticmethod
    def retry_infinite(function):
        """文件操作失败时持续等待用户解除占用。"""
        def inner(self, *args, **kwargs):
            while True:
                if function(self, *args, **kwargs):
                    return
                self.console.input(
                    _("请关闭所有正在访问该对象的窗口或程序，然后按下回车键继续处理！")
                )
        return inner
