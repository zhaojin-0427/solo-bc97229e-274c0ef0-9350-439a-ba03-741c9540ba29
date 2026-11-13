"""基于 Playwright 的门户自动化操作（异步 API）。

每个任务独立 BrowserContext，所有步骤之间检查取消事件；
下载正文阶段按 tick 轮询取消信号：取消受理后若源站正文尚未发完，
立即取消浏览器内下载并关闭上下文，中断在途数据，而不是干等正文传完。
worker 保证同一时刻只有一个任务在操作门户（单会话、不交叉）。
"""
from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from . import config


class TaskCancelled(Exception):
    """任务被取消，浏览器操作立即中止。"""


class PortalBot:
    def __init__(
        self,
        task: dict,
        work_dir: Path,
        cancel_event: threading.Event,
        on_download_started=None,
    ):
        self.task = task
        self.work_dir = work_dir
        self.cancel_event = cancel_event
        # 下载事件（响应头到达、正文开始传输）时的回调，用于更新任务提示。
        self._on_download_started = on_download_started
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None

    # ---- 取消与超时辅助 -------------------------------------------------
    def _check_cancel(self) -> None:
        if self.cancel_event.is_set():
            raise TaskCancelled("收到取消信号，停止浏览器操作")

    async def _sleep_cancellable(self, seconds: float) -> None:
        # 不能用 threading.Event.wait：那会阻塞事件循环、卡住下载事件处理。
        # 按小步睡眠轮询取消信号，响应粒度为 DOWNLOAD_TICK_SECONDS。
        deadline = time.monotonic() + seconds
        while True:
            self._check_cancel()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            await asyncio.sleep(min(remaining, config.DOWNLOAD_TICK_SECONDS))

    # ---- 生命周期 -------------------------------------------------------
    async def _start(self) -> None:
        self._pw = await async_playwright().start()
        if config.BROWSER_CDP_URL:
            # 容器内：连接独立 Chromium 容器的 CDP 端点。
            self._browser = await self._pw.chromium.connect_over_cdp(
                config.BROWSER_CDP_URL, timeout=config.NAV_TIMEOUT_MS
            )
            self._context = await self._browser.new_context(accept_downloads=True)
        else:
            # 本地调试：自行启动浏览器。
            self._browser = await self._pw.chromium.launch(
                headless=config.HEADLESS, args=["--no-sandbox", "--disable-dev-shm-usage"]
            )
            self._context = await self._browser.new_context(accept_downloads=True)
        self._context.set_default_navigation_timeout(config.NAV_TIMEOUT_MS)
        self._context.set_default_timeout(config.OPERATION_TIMEOUT_MS)
        self._page = await self._context.new_page()

    async def close(self) -> None:
        # 任何情况下都关掉上下文，未完成的下载请求会被浏览器中断。
        try:
            if self._context is not None:
                await self._context.close()
        except Exception:
            pass
        try:
            if self._browser is not None:
                await self._browser.close()
        except Exception:
            pass
        if self._pw is not None:
            try:
                await self._pw.stop()
            except Exception:
                pass

    # ---- 门户步骤 -------------------------------------------------------
    async def _is_login_page(self) -> bool:
        self._check_cancel()
        return await self._page.locator("input[name='username']").count() > 0

    async def _login(self) -> None:
        page = self._page
        await page.goto(config.PORTAL_URL + "/login")
        await page.fill("input[name='username']", config.PORTAL_USERNAME)
        await page.fill("input[name='password']", config.PORTAL_PASSWORD)
        await page.click("button[type='submit']")
        await page.wait_for_load_state("networkidle")
        if await self._is_login_page():
            raise RuntimeError("门户登录失败：用户名或密码被拒绝")

    async def _ensure_session(self) -> None:
        self._check_cancel()
        page = self._page
        await page.goto(config.PORTAL_URL + "/")
        await page.wait_for_load_state("networkidle")
        if await self._is_login_page():
            await self._login()

    async def _apply_filter(self) -> None:
        self._check_cancel()
        page = self._page
        await page.select_option("select[name='line']", self.task["line"])
        await page.fill("input[name='date']", self.task["date"])
        # 查询表单的提交按钮
        await page.click("form[action='/query'] button[type='submit']")
        await page.wait_for_load_state("networkidle")
        if await self._is_login_page():
            raise RuntimeError("筛选过程中会话失效（可能被其他登录挤下线）")
        rows = page.locator("table tbody tr")
        await rows.first.wait_for(state="visible", timeout=config.OPERATION_TIMEOUT_MS)
        if await rows.count() == 0:
            raise RuntimeError("门户筛选结果为空，无法导出")

    async def _export_and_save(self) -> Path:
        """点击导出并取消感知地等待下载，落盘到任务工作目录。"""
        self._check_cancel()
        page = self._page
        collected: list = []
        page.on("download", lambda d: collected.append(d))

        # 下载响应不产生导航，禁止点击后等待导航，否则会无谓超时。
        await page.click(
            "form[action='/export'] button[type='submit']", no_wait_after=True
        )

        # 不使用阻塞式 expect_download：按 tick 轮询，使取消信号即时生效。
        deadline = time.monotonic() + config.OPERATION_TIMEOUT_MS / 1000
        while not collected:
            self._check_cancel()
            if time.monotonic() > deadline:
                raise PlaywrightTimeoutError("等待门户 CSV 下载超时")
            # 导出过程中若被挤下线，门户会跳回登录页。
            if await self._is_login_page():
                raise RuntimeError("导出过程中会话失效（可能被其他登录挤下线）")
            await self._sleep_cancellable(config.DOWNLOAD_TICK_SECONDS)

        download = collected[0]
        if self._on_download_started is not None:
            try:
                self._on_download_started()
            except Exception:
                pass
        suggested = download.suggested_filename or "export.csv"
        if not suggested.lower().endswith(".csv"):
            suggested += ".csv"
        target = self.work_dir / suggested

        # 关键：源站可能先发响应头、再缓慢发送正文。save_as 会阻塞到正文
        # 传完，因此包成异步任务按 tick 轮询取消信号；一旦受理取消，主动
        # 取消浏览器内下载并抛 TaskCancelled，由 close() 关闭上下文彻底
        # 中断在途数据——绝不在取消后还干等正文传完。
        save = asyncio.ensure_future(download.save_as(str(target)))
        try:
            while not save.done():
                if self.cancel_event.is_set():
                    try:
                        await download.cancel()
                    except Exception:
                        pass
                    raise TaskCancelled("下载正文传输期间收到取消信号，已中断在途下载")
                await asyncio.sleep(config.DOWNLOAD_TICK_SECONDS)
            await save  # 下载自身失败（连接中断等）在此抛出
        finally:
            if not save.done():
                save.cancel()
            # 消费 save 的次生异常（取消/上下文关闭导致），避免未消费告警。
            try:
                await save
            except BaseException:
                pass
        return target

    async def _logout(self) -> None:
        """正常结束时主动释放门户会话。"""
        try:
            self._check_cancel()
            await self._page.click("form[action='/logout'] button[type='submit']")
            await self._page.wait_for_load_state("networkidle")
        except Exception:
            pass

    # ---- 入口 -----------------------------------------------------------
    async def _run_async(self) -> Path:
        """执行完整流程，返回下载文件路径。任何一步取消都会抛 TaskCancelled。"""
        await self._start()
        try:
            await self._ensure_session()
            await self._apply_filter()
            path = await self._export_and_save()
            await self._logout()
            return path
        finally:
            await self.close()

    def run(self) -> Path:
        """同步入口：worker 线程内为每个任务独立运行一个事件循环。"""
        return asyncio.run(self._run_async())
