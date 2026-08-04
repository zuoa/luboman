"""抖音投稿插件：patchright 模拟创作者服务平台（creator.douyin.com）发布视频。

参考 dreammis/social-auto-upload 的 DouYinVideo 流程。与 B 站投稿不同：
- 抖音无分 P 概念，file_list 逐文件独立发布，全部成功才算 success
- 标题上限 30 字（渲染后截断）；发布必填「自主声明」
- web 上传上限：≤4G、≤15 分钟（整录基本不可用，主要服务切片场景）——发布前预检拦截

cookie 为扫码登录保存的 storage_state JSON（见 core/douyin_login.py），
发布成功后回写以延长有效期。cookie 失效/页面改版导致的失败会截图存
douyin_debug_dir（默认 /data/douyin-debug 或 ./data/douyin-debug）便于排查。
"""

import asyncio
import json
import logging
import os
import subprocess
import time
from typing import Any, Dict, List, Optional

from luboman.core.decorators import PluginTool
from luboman.core.upload import Uploader
from luboman.core.utils import format_live_prop_text

logger = logging.getLogger('luboman')

# ---- 页面地址与 selector（creator.douyin.com 前端改版时单点修改） ----
URL_UPLOAD = 'https://creator.douyin.com/creator-micro/content/upload'
URL_PUBLISH_PATTERNS = (
    'https://creator.douyin.com/creator-micro/content/publish?enter_from=publish_page',
    'https://creator.douyin.com/creator-micro/content/post/video?enter_from=publish_page',
)
URL_MANAGE_PATTERN = 'https://creator.douyin.com/creator-micro/content/manage**'

SEL_UPLOAD_INPUT = "div[class^='container'] input"
SEL_UPLOAD_DONE = '[class^="long-card"] div:has-text("重新上传")'
SEL_UPLOAD_FAILED = 'div.progress-div > div:has-text("上传失败")'
SEL_DESC_EDITOR = '.zone-container[contenteditable="true"]'
SEL_DECLARATION_ENTRY = '请选择自主声明'
SEL_DECLARATION_DIALOG = '.semi-modal-content'
DECLARATION_OPTION_TEXT = '内容为个人观点或见解'

# 平台限制
MAX_VIDEO_BYTES = 4 * 1024 * 1024 * 1024  # 4G
MAX_VIDEO_SECONDS = 15 * 60  # 15 分钟
TITLE_MAX_LEN = 30
# 定时发布合法窗口：提交后 2 小时 ~ 7 天
DTIME_MIN_DELAY_SECONDS = 2 * 3600
DTIME_MAX_DELAY_SECONDS = 7 * 24 * 3600


def _config_get(key: str, default=None):
    from luboman.config import config

    return config.get(key, default)


def _as_int(value, default=None):
    if value is None or value == '':
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _debug_dir() -> str:
    base = _config_get('douyin_debug_dir')
    if not base:
        base = (
            '/data/douyin-debug'
            if os.path.exists('/.dockerenv')
            else os.path.join(os.getcwd(), 'data', 'douyin-debug')
        )
    os.makedirs(base, exist_ok=True)
    return base


def _probe_duration_seconds(video_path: str) -> Optional[float]:
    """ffprobe 取视频时长（秒）；失败返回 None（不阻塞流程，由页面侧兜底）。"""
    try:
        proc = subprocess.run(
            [
                'ffprobe', '-v', 'error',
                '-show_entries', 'format=duration',
                '-of', 'default=noprint_wrappers=1:nokey=1',
                video_path,
            ],
            capture_output=True, text=True, timeout=60,
        )
        return float(proc.stdout.strip())
    except Exception:
        logger.warning('ffprobe 读取视频时长失败: %s', video_path, exc_info=True)
        return None


def _preflight_error(video_path: str) -> Optional[str]:
    """发布前预检，返回错误文案（None 表示通过）。避免在浏览器里白等。"""
    size = os.path.getsize(video_path)
    if size > MAX_VIDEO_BYTES:
        return f'视频超过抖音上传上限 4G（{size / 1024 ** 3:.1f}G）: {os.path.basename(video_path)}'
    duration = _probe_duration_seconds(video_path)
    if duration is not None and duration > MAX_VIDEO_SECONDS:
        return (
            f'视频超过抖音上传时长上限 15 分钟（{duration / 60:.1f} 分钟）: '
            f'{os.path.basename(video_path)}（整录请改用B站或切片后再投）'
        )
    return None


@PluginTool.upload(platform='douyin')
@PluginTool.upload(platform='douyin_web')
class DouyinUploader(Uploader):
    """Upload to Douyin via creator platform (patchright)."""

    def __init__(self, file_list, room_data):
        super().__init__(file_list)
        self.room_data = room_data or {}

    @staticmethod
    def _failure(message: str, **extra) -> Dict[str, Any]:
        return {'success': False, 'error_message': message, **extra}

    # ---------- 同步契约入口 ----------

    def upload(self):
        template_info = self.room_data.get('douyin_upload_template')
        if not template_info:
            message = '未设置抖音投稿模板'
            logger.warning(message)
            return self._failure(message)

        from luboman.core.douyin_login import resolve_account_cookie_path

        account = template_info.get('douyin_account') or {}
        cookie_path = resolve_account_cookie_path(account)
        if not cookie_path:
            message = (
                f'抖音账号「{account.get("account_name") or account.get("id")}」未登录，'
                '请先在抖音账号管理中扫码登录'
            )
            logger.error(message)
            return self._failure(message)

        video_paths = [
            file_info['video']
            for file_info in self.file_list
            if file_info.get('video') and os.path.exists(file_info['video'])
        ]
        if not video_paths:
            message = '没有可上传的视频文件'
            logger.warning(message)
            return self._failure(message)

        for video_path in video_paths:
            error = _preflight_error(video_path)
            if error:
                logger.error(error)
                return self._failure(error)

        try:
            return asyncio.run(self._upload_all(template_info, cookie_path, video_paths))
        except Exception as exc:
            message = f'抖音投稿执行异常: {exc}'
            logger.exception(message)
            return self._failure(message)

    # ---------- 异步发布流程 ----------

    async def _upload_all(self, template_info, cookie_path, video_paths) -> Dict[str, Any]:
        from patchright.async_api import async_playwright

        headless = bool(_config_get('douyin_headless', True))
        interval = _as_int(_config_get('douyin_publish_interval_seconds', 30), 30)

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=headless,
                args=['--no-sandbox', '--disable-dev-shm-usage', '--disable-blink-features=AutomationControlled'],
            )
            context = await browser.new_context(
                storage_state=cookie_path,
                viewport={'width': 1600, 'height': 900},
            )
            published = []
            failures = []
            try:
                for index, video_path in enumerate(video_paths):
                    if index > 0 and interval > 0:
                        await asyncio.sleep(interval)  # 逐发布间隔，降低风控触发概率
                    page = await context.new_page()
                    try:
                        await self._publish_one(page, video_path, template_info)
                        published.append(video_path)
                        logger.info('抖音发布成功: %s', video_path)
                    except Exception as exc:
                        failures.append(f'{os.path.basename(video_path)}: {exc}')
                        logger.exception('抖音发布失败: %s', video_path)
                        await self._dump_debug_snapshot(page, video_path)
                    finally:
                        await page.close()
            finally:
                if published and not failures:
                    # 全部成功才回写 storage_state，延长 cookie 有效期
                    try:
                        await context.storage_state(path=cookie_path)
                    except Exception:
                        logger.warning('回写抖音 cookie 失败: %s', cookie_path, exc_info=True)
                await context.close()
                await browser.close()

        if failures:
            message = '；'.join(failures)
            if published:
                message = f'部分发布失败（成功 {len(published)}/{len(video_paths)}）：{message}'
            return self._failure(message, published=published, failed=failures)
        return {'success': True, 'published': published}

    async def _publish_one(self, page, video_path: str, template_info: Dict[str, Any]):
        upload_timeout = _as_int(_config_get('douyin_upload_timeout_seconds', 1800), 1800) * 1000

        await page.goto(URL_UPLOAD)
        await page.wait_for_url(URL_UPLOAD, timeout=15000)
        if 'login' in page.url or await page.get_by_text('扫码登录', exact=True).count():
            raise RuntimeError('抖音 cookie 已失效，请重新扫码登录')

        logger.info('抖音开始上传视频: %s', video_path)
        await page.locator(SEL_UPLOAD_INPUT).set_input_files(video_path)

        # 等进入发布页（抖音存在两版发布页 URL）
        deadline = time.time() + 60
        while True:
            for pattern in URL_PUBLISH_PATTERNS:
                try:
                    await page.wait_for_url(pattern, timeout=3000)
                    break
                except Exception:
                    continue
            else:
                if time.time() > deadline:
                    raise RuntimeError('上传后未进入发布页（超时）')
                await asyncio.sleep(0.5)
                continue
            break

        await asyncio.sleep(1)
        await self._fill_title_description_tags(page, template_info)
        await self._wait_upload_complete(page, video_path, upload_timeout)
        await self._set_cover(page, template_info)
        await self._set_self_declaration(page)
        await self._set_schedule_time(page, template_info)

        # 点发布，等跳到内容管理页即成功；期间可能弹「请设置封面」兜底
        publish_deadline = time.time() + 120
        while True:
            try:
                publish_button = page.get_by_role('button', name='发布', exact=True)
                if await publish_button.count():
                    await publish_button.click()
                await page.wait_for_url(URL_MANAGE_PATTERN, timeout=5000)
                return
            except Exception:
                if time.time() > publish_deadline:
                    raise RuntimeError('点击发布后未跳转到内容管理页（超时）')
                await self._handle_auto_cover(page)
                await asyncio.sleep(1)

    async def _fill_title_description_tags(self, page, template_info: Dict[str, Any]):
        title_template = template_info.get('title') or '【{room_name}】{room_title}'
        desc_template = template_info.get('description') or ''
        title = format_live_prop_text(title_template, self.room_data)[:TITLE_MAX_LEN]
        description = format_live_prop_text(desc_template, self.room_data) if desc_template else ''
        tags = template_info.get('tags') or []
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(',') if t.strip()]

        description_section = (
            page.get_by_text('作品描述', exact=True)
            .locator('xpath=ancestor::div[2]')
            .locator('xpath=following-sibling::div[1]')
        )

        title_input = description_section.locator('input[type="text"]').first
        await title_input.wait_for(state='visible', timeout=10000)
        await title_input.fill(title)

        editor = description_section.locator(SEL_DESC_EDITOR).first
        await editor.wait_for(state='visible', timeout=10000)
        await editor.click()
        await page.keyboard.press('Control+KeyA')
        await page.keyboard.press('Delete')
        if description:
            await page.keyboard.type(description)

        for tag in tags:
            await page.keyboard.type(f' #{tag}')
            await page.keyboard.press('Space')
        logger.info('抖音标题/描述/话题填写完成: %s (话题 %d 个)', title, len(tags))

    async def _wait_upload_complete(self, page, video_path: str, timeout_ms: int):
        deadline = time.time() + timeout_ms / 1000
        while True:
            if await page.locator(SEL_UPLOAD_DONE).count():
                return
            if await page.locator(SEL_UPLOAD_FAILED).count():
                logger.warning('抖音上传失败，自动重试: %s', video_path)
                await page.locator("div.progress-div [class^='upload-btn-input']").set_input_files(video_path)
            if time.time() > deadline:
                raise RuntimeError('等待视频上传完成超时')
            await asyncio.sleep(2)

    async def _set_cover(self, page, template_info: Dict[str, Any]):
        """封面为可选项：任何失败仅告警，用抖音默认帧。"""
        cover_path = template_info.get('cover_path')
        if not cover_path or not os.path.exists(cover_path):
            return
        try:
            await page.click('text="选择封面"')
            cover_modal = page.locator('div[id*="creator-content-modal"]')
            await cover_modal.wait_for(timeout=10000)
            upload_input = cover_modal.locator(
                "div[class^='semi-upload upload'] >> input.semi-upload-hidden-input"
            )
            await page.wait_for_timeout(1000)
            await upload_input.set_input_files(cover_path)
            await page.wait_for_timeout(2000)
            await cover_modal.locator('button:visible:has-text("完成")').click()
            await page.wait_for_selector('div.extractFooter', state='detached', timeout=15000)
            logger.info('抖音封面设置完成: %s', cover_path)
        except Exception:
            logger.warning('抖音封面设置失败，使用默认帧: %s', cover_path, exc_info=True)

    async def _set_self_declaration(self, page):
        """「自主声明」为发布必选项：漏选会导致发布按钮不可用，找不到入口必须 fail。"""
        try:
            entry = page.get_by_text(SEL_DECLARATION_ENTRY).first
            await entry.wait_for(state='visible', timeout=8000)
            await entry.click()

            dialog = page.locator(SEL_DECLARATION_DIALOG).filter(has_text='对作品内容添加声明').first
            await dialog.wait_for(state='visible', timeout=8000)

            option = dialog.locator('.semi-radio').filter(has_text=DECLARATION_OPTION_TEXT).first
            if await option.count():
                await option.click(timeout=8000)
            else:
                await dialog.get_by_text(DECLARATION_OPTION_TEXT, exact=True).first.click(timeout=8000, force=True)
            await dialog.get_by_role('button', name='确定').click(timeout=8000)
            await dialog.wait_for(state='hidden', timeout=8000)
            logger.info('抖音自主声明已选择「%s」', DECLARATION_OPTION_TEXT)
        except Exception as exc:
            raise RuntimeError(f'设置抖音自主声明失败（发布必选项）: {exc}')

    async def _set_schedule_time(self, page, template_info: Dict[str, Any]):
        dtime = _as_int(template_info.get('dtime'))
        if not dtime:
            return
        now = int(time.time())
        if not (now + DTIME_MIN_DELAY_SECONDS <= dtime <= now + DTIME_MAX_DELAY_SECONDS):
            logger.warning('抖音定时发布时间 %s 不在合法窗口（2小时~7天内），降级为立即发布', dtime)
            return
        try:
            await page.locator("[class^='radio']:has-text('定时发布')").click()
            await asyncio.sleep(1)
            schedule_text = time.strftime('%Y-%m-%d %H:%M', time.localtime(dtime))
            await page.locator('.semi-input[placeholder="日期和时间"]').click()
            await page.keyboard.press('Control+KeyA')
            await page.keyboard.type(schedule_text)
            await page.keyboard.press('Enter')
            await asyncio.sleep(1)
            logger.info('抖音定时发布已设置: %s', schedule_text)
        except Exception:
            logger.warning('抖音定时发布设置失败，将立即发布', exc_info=True)

    async def _handle_auto_cover(self, page):
        """发布时若提示「请设置封面后再发布」，选第一个推荐封面。"""
        try:
            if not await page.get_by_text('请设置封面后再发布').first.is_visible():
                return
            recommend = page.locator('[class^="recommendCover-"]').first
            if await recommend.count():
                await recommend.click()
                await asyncio.sleep(1)
                if await page.get_by_text('是否确认应用此封面？').first.is_visible():
                    await page.get_by_role('button', name='确定').click()
                    await asyncio.sleep(1)
        except Exception:
            logger.debug('自动选择推荐封面失败', exc_info=True)

    async def _dump_debug_snapshot(self, page, video_path: str):
        try:
            name = f"{time.strftime('%Y%m%d-%H%M%S')}-{os.path.basename(video_path)}.png"
            path = os.path.join(_debug_dir(), name)
            await page.screenshot(path=path, full_page=True)
            logger.info('抖音发布失败页面快照: %s', path)
        except Exception:
            logger.debug('保存抖音调试快照失败', exc_info=True)
