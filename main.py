import asyncio
import random
import re
import json
import os
import subprocess
import uuid
import aiohttp
import aiofiles
from pathlib import Path
from typing import List, Dict, Any, Optional, Set
from urllib.parse import quote, urlencode
from asyncio import Semaphore
import shutil
import hashlib
import time
from datetime import datetime

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig
from astrbot.api.message_components import *
from astrbot.core.message.message_event_result import MessageChain
from astrbot.api import star


@register("astrbot_plugin_banshi", "momola", "定时随机搬运B站视频到群聊，指定关键词搜索随机视频搬运，指定av/bv号视频搬运，订阅UP主更新自动搬运", "1.1.0")
class BilibiliPolluterPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        
        # AstrBot WebUI 配置
        self.config = config
        
        # 数据目录（使用插件自身目录下的 data 子目录）
        self.data_dir = Path(os.path.dirname(os.path.abspath(__file__))) / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        
        # 下载目录
        self.download_dir = self.data_dir / "downloads"
        self.download_dir.mkdir(parents=True, exist_ok=True)
        
        # 已绑定的群（运行时数据，单独存储在数据目录）
        self.bound_groups_path = self.data_dir / "bound_groups.json"
        self.bound_groups = self._load_bound_groups()
        
        # 运行状态
        self.running = False
        self.task: Optional[asyncio.Task] = None
        self.sub_task: Optional[asyncio.Task] = None
        
        # 临时文件追踪（使用Set避免重复）
        self.temp_files: Set[str] = set()
        
        # UP主订阅数据（uid -> 订阅信息）
        self.subscriptions_path = self.data_dir / "subscriptions.json"
        self.subscriptions: Dict[str, Dict[str, Any]] = self._load_subscriptions()
        
        
        # 并发控制信号量（最大5个并发请求）
        self.semaphore = Semaphore(5)
        
        # 共享的HTTP会话（aiohttp）
        self.session: Optional[aiohttp.ClientSession] = None
        
        # 请求头
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Referer': 'https://www.bilibili.com/',
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
        }
        
        # 检查ffmpeg
        self.ffmpeg_available = self._check_ffmpeg()
        if not self.ffmpeg_available:
            logger.warning("FFmpeg未安装，视频合并功能将不可用")
        
        logger.info(f"B站搬石已加载，下载目录: {self.download_dir}")

    async def _init_bilibili_cookies(self):
        """初始化B站cookies，获取buvid3/buvid4以避免412错误"""
        if not self.session:
            return
        try:
            async with self.session.get('https://api.bilibili.com/x/frontend/finger/spi') as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get('code') == 0:
                        b3 = data['data'].get('b_3', '')
                        b4 = data['data'].get('b_4', '')
                        if b3:
                            self.session.cookie_jar.update_cookies({'buvid3': b3})
                        if b4:
                            self.session.cookie_jar.update_cookies({'buvid4': b4})
                        logger.info("B站cookies初始化成功")
                    else:
                        logger.warning(f"获取B站cookies失败: {data}")
                else:
                    logger.warning(f"获取B站cookies请求失败: {resp.status}")
        except Exception as e:
            logger.warning(f"初始化B站cookies异常: {e}")

    async def initialize(self):
        """插件初始化 - 创建会话和启动定时任务"""
        # 创建共享的aiohttp会话
        timeout = aiohttp.ClientTimeout(total=30, connect=10, sock_read=20)
        self.session = aiohttp.ClientSession(
            headers=self.headers,
            timeout=timeout
        )
        
        # 初始化B站cookies（防止412错误）
        await self._init_bilibili_cookies()
        
        # 开机自启动
        if self.config.get('auto_start', True):
            self.running = True
            self.task = asyncio.create_task(self._timer_task())
            self.sub_task = asyncio.create_task(self._subscriber_task())
            logger.info("B站搬石已开机自启动")

    async def terminate(self):
        """插件卸载时清理"""
        self.running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                logger.info("定时任务已取消")
            except Exception as e:
                logger.error(f"定时任务停止时出错: {e}")
            self.task = None
        if self.sub_task:
            self.sub_task.cancel()
            try:
                await self.sub_task
            except asyncio.CancelledError:
                logger.info("订阅检查任务已取消")
            except Exception as e:
                logger.error(f"订阅检查任务停止时出错: {e}")
            self.sub_task = None
        
        # 关闭HTTP会话
        if self.session and not self.session.closed:
            await self.session.close()
        
        # 清理临时文件
        await self._cleanup_temp_files()

    def _load_bound_groups(self) -> Dict[str, str]:
        """加载已绑定的群数据"""
        if self.bound_groups_path.exists():
            try:
                with open(self.bound_groups_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"读取绑定群数据失败: {e}")
        return {}

    def _save_bound_groups(self):
        """保存绑定群数据"""
        try:
            with open(self.bound_groups_path, 'w', encoding='utf-8') as f:
                json.dump(self.bound_groups, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存绑定群数据失败: {e}")

    async def _cleanup_temp_files(self):
        """清理临时文件"""
        files_to_remove = list(self.temp_files)
        self.temp_files.clear()
        
        for file_path in files_to_remove:
            try:
                if os.path.exists(file_path):
                    os.remove(file_path)
                    logger.debug(f"已删除临时文件: {file_path}")
            except Exception as e:
                logger.error(f"删除临时文件失败 {file_path}: {e}")

    def _check_ffmpeg(self) -> bool:
        """检查ffmpeg是否可用"""
        try:
            result = subprocess.run(['ffmpeg', '-version'], capture_output=True, text=True)
            return result.returncode == 0
        except:
            return False

    def _clean_filename(self, filename: str) -> str:
        """清理文件名"""
        if not filename:
            return "untitled"
        
        # 提取书名号中的内容
        if '《' in filename and '》' in filename:
            title_match = re.search(r'《([^《》]+)》', filename)
            if title_match:
                filename = title_match.group(1)
        
        # 移除非法字符
        cleaned = re.sub(r'[<>:"/\\|?*]', '', filename)
        
        # 限制长度
        if len(cleaned) > 100:
            cleaned = cleaned[:100]
        
        return cleaned.strip()

    # ==================== 自动记录群 ====================
    
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        """自动记录群 unified_msg_origin（懒人模式）"""
        try:
            umo = event.unified_msg_origin
            group_id = str(event.message_obj.group_id)
            
            if not group_id or not umo:
                return
            
            # 如果是新群，自动记录
            if group_id not in self.bound_groups:
                self.bound_groups[group_id] = umo
                self._save_bound_groups()
                logger.info(f"✅ 自动记录新群: {group_id}")
                
        except Exception as e:
            logger.error(f"自动记录群失败: {e}")

    # ==================== 搜索部分 ====================

    def _parse_play_count(self, play_text: Any) -> int:
        """将播放量文本转换为数字"""
        if not play_text:
            return 0
        
        if isinstance(play_text, (int, float)):
            return int(play_text)
        
        play_text = str(play_text).strip()
        
        unit_multiplier = 1
        if '万' in play_text:
            play_text = play_text.replace('万', '')
            unit_multiplier = 10000
        elif '亿' in play_text:
            play_text = play_text.replace('亿', '')
            unit_multiplier = 100000000
        
        try:
            # 提取数字部分
            numbers = re.findall(r'\d+\.?\d*', play_text)
            if numbers:
                return int(float(numbers[0]) * unit_multiplier)
            return 0
        except:
            return 0

    def _parse_duration(self, duration_text: Any) -> int:
        """将时长文本转换为秒数"""
        if not duration_text:
            return 0
        
        duration_text = str(duration_text).strip()
        
        try:
            # 处理 "3:45" 或 "1:20:30" 格式
            if ':' in duration_text:
                parts = duration_text.split(':')
                if len(parts) == 2:  # MM:SS
                    return int(parts[0]) * 60 + int(parts[1])
                elif len(parts) == 3:  # HH:MM:SS
                    return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
            
            # 处理纯数字（秒）
            return int(duration_text)
        except:
            return 0

    def _format_duration(self, seconds: Any) -> str:
        """将秒数格式化为 时:分:秒 文本"""
        try:
            seconds = int(seconds)
            h, rem = divmod(seconds, 3600)
            m, s = divmod(rem, 60)
            if h > 0:
                return f"{h}:{m:02d}:{s:02d}"
            return f"{m}:{s:02d}"
        except:
            return "未知"

    def _should_include_video(self, title: str, keyword: Optional[str] = None) -> bool:
        """判断视频标题是否包含关键词"""
        if not title:
            return False
        
        title_lower = title.lower()
        # 指定了关键词时只匹配该关键词；否则匹配配置中的所有关键词
        keywords = [keyword] if keyword else self.config.get('search_keywords', [])
        for kw in keywords:
            if kw and kw.lower() in title_lower:
                return True
        
        return False

    async def _search_videos_by_keyword(self, keyword: str, max_pages: int = 3) -> List[Dict[str, Any]]:
        """使用指定关键词搜索视频，返回符合时长要求的视频列表"""
        if not self.session:
            logger.error("HTTP会话未初始化")
            return []
        
        videos = []
        max_duration = self.config.get('max_duration', 600)
        
        logger.info(f"搜索关键词: {keyword}, 最大时长: {max_duration}秒")
        
        for page in range(1, max_pages + 1):
            try:
                # 使用信号量控制并发
                async with self.semaphore:
                    api_url = f"https://api.bilibili.com/x/web-interface/search/all/v2?keyword={quote(keyword)}&page={page}"
                    
                    async with self.session.get(api_url) as response:
                        if response.status != 200:
                            continue
                        
                        data = await response.json()
                
                if data.get('code') != 0:
                    continue
                
                if 'data' not in data:
                    continue
                
                # 查找视频结果
                video_results = None
                for item in data['data'].get('result', []):
                    if item.get('result_type') == 'video':
                        video_results = item.get('data', [])
                        break
                
                if not video_results:
                    break
                
                for video in video_results:
                    try:
                        # 提取标题（去掉em标签）
                        title = video.get('title', '')
                        title = re.sub(r'<[^>]+>', '', title)
                        
                        # 检查标题是否包含关键词
                        if not self._should_include_video(title, keyword):
                            continue
                        
                        bvid = video.get('bvid', '')
                        if not bvid:
                            continue
                        
                        # 解析时长
                        duration_text = video.get('duration', '')
                        duration_seconds = self._parse_duration(duration_text)
                        
                        # 检查时长是否超过限制
                        if duration_seconds > max_duration:
                            logger.debug(f"视频时长 {duration_seconds}秒 超过限制 {max_duration}秒，跳过: {title}")
                            continue
                        
                        video_url = f"https://www.bilibili.com/video/{bvid}"
                        
                        # 播放量
                        play_count = self._parse_play_count(video.get('play', 0))
                        
                        # 作者
                        author = video.get('author', '')
                        
                        video_info = {
                            'title': title,
                            'url': video_url,
                            'bvid': bvid,
                            'play_count': play_count,
                            'duration': duration_text,
                            'duration_seconds': duration_seconds,
                            'author': author,
                            'search_keyword': keyword,
                        }
                        
                        videos.append(video_info)
                        logger.debug(f"找到符合时长的视频: {title} ({duration_text})")
                        
                    except Exception as e:
                        logger.error(f"解析视频信息失败: {e}")
                        continue
                
                # 页间延迟
                if page < max_pages:
                    await asyncio.sleep(random.uniform(0.5, 1))
                    
            except asyncio.TimeoutError:
                logger.warning(f"搜索关键词 '{keyword}' 第{page}页超时")
                continue
            except Exception as e:
                logger.error(f"搜索关键词 '{keyword}' 第{page}页出错: {e}")
                break
        
        logger.info(f"关键词 '{keyword}' 搜索完成: 找到 {len(videos)} 个符合时长的视频")
        return videos

    async def _select_random_video(self) -> Optional[Dict[str, Any]]:
        """随机选一个关键词，随机找一个符合时长要求的视频"""
        keywords = self.config.get('search_keywords', [])
        
        if not keywords:
            logger.error("没有搜索关键词")
            return None
        
        max_duration = self.config.get('max_duration', 600)
        max_attempts = 10  # 最大尝试次数，避免死循环
        
        for attempt in range(max_attempts):
            # 随机选一个关键词
            keyword = random.choice(keywords)
            logger.info(f"第{attempt+1}次尝试，选中关键词: {keyword}")
            
            # 随机选页数（1-3页）
            pages = random.randint(1, 3)
            
            # 搜索该关键词
            videos = await self._search_videos_by_keyword(keyword, pages)
            
            if not videos:
                logger.info(f"关键词 '{keyword}' 没有找到符合时长的视频")
                continue
            
            # 随机选一个视频
            selected = random.choice(videos)
            duration = selected.get('duration_seconds', 0)
            
            logger.info(f"随机选中视频: {selected['title']} (时长: {selected['duration']}, {duration}秒)")
            
            # 二次确认时长
            if duration <= max_duration:
                return selected
            else:
                logger.info(f"视频时长 {duration}秒 超过限制，重新选择")
                continue
        
        logger.error(f"尝试 {max_attempts} 次后仍未找到合适的视频")
        return None

    # ==================== 下载部分 ====================

    async def _resolve_video_identifier(self, text: str) -> Optional[Dict[str, Any]]:
        """解析BV号/AV号/视频链接，返回视频信息（通过view API）"""
        if not self.session:
            logger.error("HTTP会话未初始化")
            return None
        
        text = text.strip()
        # 提取BV号
        bv_match = re.search(r'(BV[0-9A-Za-z]{10})', text, re.IGNORECASE)
        # 提取AV号
        av_match = re.search(r'(?:av|AV)(\d+)', text)
        
        if not bv_match and not av_match:
            return None
        
        if bv_match:
            bvid = bv_match.group(1)
            api_url = f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
        else:
            aid = av_match.group(1)
            api_url = f"https://api.bilibili.com/x/web-interface/view?aid={aid}"
        
        try:
            async with self.semaphore:
                async with self.session.get(api_url) as response:
                    if response.status != 200:
                        logger.error(f"解析视频信息API失败: {response.status}")
                        return None
                    data = await response.json()
            
            if data.get('code') != 0:
                logger.error(f"解析视频信息失败: {data.get('message', '未知错误')}")
                return None
            
            d = data.get('data', {})
            owner = d.get('owner', {}) or {}
            stat = d.get('stat', {}) or {}
            bvid = d.get('bvid', '') or (bv_match.group(1) if bv_match else '')
            
            return {
                'title': d.get('title', '未知标题'),
                'bvid': bvid,
                'author': owner.get('name', ''),
                'duration_seconds': d.get('duration', 0),
                'duration': self._format_duration(d.get('duration', 0)),
                'url': f"https://www.bilibili.com/video/{bvid}",
                'play_count': stat.get('view', 0),
            }
        except Exception as e:
            logger.error(f"解析视频信息异常: {e}")
            return None

    async def _get_video_urls(self, bvid: str) -> tuple[Optional[str], Optional[str]]:
        """通过B站API获取视频和音频URL（避免网页412问题）"""
        if not self.session:
            logger.error("HTTP会话未初始化")
            return None, None
        
        try:
            # 第1步：通过API获取视频cid
            view_api = f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
            cid = None
            
            async with self.semaphore:
                async with self.session.get(view_api) as response:
                    if response.status != 200:
                        logger.error(f"获取视频信息API失败: {response.status}")
                        return None, None
                    
                    view_data = await response.json()
            
            if view_data.get('code') != 0:
                logger.error(f"获取视频信息失败: {view_data.get('message', '未知错误')}")
                return None, None
            
            cid = view_data.get('data', {}).get('cid')
            if not cid:
                # 尝试从pages中获取
                pages = view_data.get('data', {}).get('pages', [])
                if pages:
                    cid = pages[0].get('cid')
            
            if not cid:
                logger.error(f"未找到视频cid: {bvid}")
                return None, None
            
            logger.debug(f"获取到cid: {cid}")
            
            # 第2步：通过API获取播放地址（fnval=16表示DASH格式，返回分离的视频和音频流）
            playurl_api = f"https://api.bilibili.com/x/player/playurl?bvid={bvid}&cid={cid}&qn=80&fnval=16&fourk=1"
            
            async with self.semaphore:
                async with self.session.get(playurl_api) as response:
                    if response.status != 200:
                        logger.error(f"获取播放地址API失败: {response.status}")
                        return None, None
                    
                    playurl_data = await response.json()
            
            if playurl_data.get('code') != 0:
                logger.error(f"获取播放地址失败: {playurl_data.get('message', '未知错误')}")
                return None, None
            
            # 解析DASH视频和音频URL
            dash = playurl_data.get('data', {}).get('dash')
            if not dash:
                logger.error(f"未找到DASH数据: {bvid}")
                return None, None
            
            video_qualities = dash.get('video', [])
            audio_qualities = dash.get('audio', [])
            
            if not video_qualities:
                logger.error(f"没有找到视频流: {bvid}")
                return None, None
            
            if not audio_qualities:
                logger.error(f"没有找到音频流: {bvid}")
                return None, None
            
            # 选最高画质
            best_video = max(video_qualities, key=lambda x: x.get('bandwidth', 0))
            best_audio = max(audio_qualities, key=lambda x: x.get('bandwidth', 0))
            
            video_url = best_video.get('baseUrl') or best_video.get('base_url')
            audio_url = best_audio.get('baseUrl') or best_audio.get('base_url')
            
            if not video_url or not audio_url:
                logger.error(f"视频或音频URL为空: {bvid}")
                return None, None
            
            logger.info(f"成功获取视频流地址: {bvid}")
            return video_url, audio_url
            
        except json.JSONDecodeError as e:
            logger.error(f"解析API JSON失败 {bvid}: {e}")
            return None, None
        except Exception as e:
            logger.error(f"获取视频URL失败 {bvid}: {e}")
            return None, None

    async def _download_file(self, url: str, output_path: str) -> bool:
        """下载文件（使用aiohttp）"""
        if not self.session:
            logger.error("HTTP会话未初始化")
            return False
        
        try:
            async with self.semaphore:
                async with self.session.get(url) as response:
                    if response.status != 200:
                        logger.error(f"下载失败: HTTP {response.status}")
                        return False
                    
                    # 流式写入文件
                    async with aiofiles.open(output_path, 'wb') as f:
                        async for chunk in response.content.iter_chunked(8192):
                            await f.write(chunk)
                    
                    return True
                    
        except asyncio.TimeoutError:
            logger.error(f"下载超时: {url[:50]}")
            return False
        except Exception as e:
            logger.error(f"下载出错: {e}")
            return False

    async def _merge_video_audio(self, video_path: str, audio_path: str, output_path: str) -> bool:
        """合并视频和音频（使用subprocess，避免shell注入）"""
        try:
            # 使用列表形式调用，避免shell注入
            cmd = [
                'ffmpeg',
                '-i', video_path,
                '-i', audio_path,
                '-c:v', 'copy',
                '-c:a', 'aac',
                '-y',
                '-hide_banner',
                '-loglevel', 'error',
                output_path
            ]
            
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()
            
            if process.returncode == 0:
                # 成功时删除临时文件
                for path in [video_path, audio_path]:
                    try:
                        if os.path.exists(path):
                            os.remove(path)
                            self.temp_files.discard(path)
                    except Exception as e:
                        logger.warning(f"删除临时文件失败 {path}: {e}")
                return True
            else:
                logger.error(f"合并失败: {stderr.decode()[:200]}")
                return False
                
        except Exception as e:
            logger.error(f"合并出错: {e}")
            return False

    async def _compress_video(self, input_path: str, output_path: str) -> bool:
        """压缩视频（重新编码为H.264，限制分辨率/码率减小体积）"""
        try:
            cmd = [
                'ffmpeg',
                '-i', input_path,
                '-c:v', 'libx264',
                '-preset', 'veryfast',
                '-crf', '28',
                '-vf', 'scale=w=min(1280\\,iw):h=-2',
                '-c:a', 'aac',
                '-b:a', '128k',
                '-movflags', '+faststart',
                '-y',
                '-hide_banner',
                '-loglevel', 'error',
                output_path
            ]
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()
            if process.returncode == 0:
                return True
            logger.error(f"压缩失败: {stderr.decode()[:300]}")
            return False
        except Exception as e:
            logger.error(f"压缩出错: {e}")
            return False

    async def _maybe_compress(self, input_path: str, force_compress: bool = False) -> str:
        """按需压缩视频，返回最终文件路径"""
        try:
            size_mb = os.path.getsize(input_path) / 1024 / 1024
            max_mb = self.config.get('max_send_size_mb', 100)

            # 需要压缩：强制压缩 或 (开启自动压缩 且 超过大小限制)
            need = force_compress or (self.config.get('compress_video', True) and size_mb > max_mb)
            if not need:
                return input_path
            if not self.ffmpeg_available:
                logger.warning("FFmpeg不可用，无法压缩，发送原文件")
                return input_path

            logger.info(f"开始压缩视频: {size_mb:.1f}MB")
            compressed_path = self.download_dir / (Path(input_path).stem + "_compressed.mp4")
            self.temp_files.add(str(compressed_path))

            ok = await self._compress_video(input_path, str(compressed_path))
            if not ok:
                logger.warning("压缩失败，发送原文件")
                self.temp_files.discard(str(compressed_path))
                if os.path.exists(str(compressed_path)):
                    os.remove(str(compressed_path))
                return input_path

            # 压缩后没有变小则保留原文件
            if os.path.getsize(str(compressed_path)) >= os.path.getsize(input_path):
                logger.info("压缩后体积未变小，保留原文件")
                os.remove(str(compressed_path))
                self.temp_files.discard(str(compressed_path))
                return input_path

            os.remove(input_path)
            self.temp_files.discard(input_path)
            logger.info(f"压缩完成: {compressed_path} ({os.path.getsize(str(compressed_path))/1024/1024:.1f}MB)")
            return str(compressed_path)
        except Exception as e:
            logger.error(f"压缩流程异常: {e}")
            return input_path

    async def _download_video(self, video_info: Dict[str, Any], force_compress: bool = False) -> Optional[str]:
        """下载单个视频
        Args:
            video_info: 视频信息
            force_compress: 是否强制压缩（即使体积未超限）
        """
        bvid = video_info.get('bvid', '')
        title = video_info.get('title', '未知标题')
        
        if not bvid:
            logger.error("没有bvid，无法下载")
            return None
        
        try:
            # 清理文件名
            cleaned_title = self._clean_filename(title)
            unique_id = str(uuid.uuid4())[:8]
            
            # 临时文件路径
            video_temp = self.download_dir / f"video_{unique_id}.m4s"
            audio_temp = self.download_dir / f"audio_{unique_id}.m4s"
            output_path = self.download_dir / f"{cleaned_title}_{unique_id}.mp4"
            
            # 记录临时文件
            self.temp_files.add(str(video_temp))
            self.temp_files.add(str(audio_temp))
            self.temp_files.add(str(output_path))
            
            # 获取视频和音频URL
            logger.info(f"获取下载地址: {bvid}")
            video_url, audio_url = await self._get_video_urls(bvid)
            
            if not video_url:
                logger.error(f"获取视频地址失败: {bvid}")
                return None
            
            if not audio_url:
                logger.error(f"获取音频地址失败: {bvid}")
                return None
            
            # 下载视频
            logger.info(f"下载视频: {title}")
            video_success = await self._download_file(video_url, str(video_temp))
            if not video_success:
                logger.error(f"视频下载失败: {bvid}")
                return None
            
            # 下载音频
            logger.info(f"下载音频: {title}")
            audio_success = await self._download_file(audio_url, str(audio_temp))
            if not audio_success:
                logger.error(f"音频下载失败: {bvid}")
                return None
            
            # 合并
            if self.ffmpeg_available:
                logger.info(f"合并视频音频: {title}")
                merge_success = await self._merge_video_audio(str(video_temp), str(audio_temp), str(output_path))
                if merge_success:
                    logger.info(f"下载完成: {output_path}")
                    # 按需压缩（体积超限或强制压缩）
                    final_path = await self._maybe_compress(str(output_path), force_compress=force_compress)
                    logger.info(f"最终文件: {final_path}")
                    return final_path
                else:
                    logger.error("合并失败")
                    return None
            else:
                logger.error("FFmpeg不可用，无法合并")
                return None
                
        except Exception as e:
            logger.error(f"下载视频异常: {e}")
            return None

    # ==================== 发送部分 ====================

    def _create_video_message(self, video_info: Dict[str, Any], file_path: str, prefix: str = "【B站搬石】") -> List[Any]:
        """创建视频消息链"""
        chain = []
        
        # 根据配置决定是否附带文字信息
        if self.config.get('send_with_text', True):
            title = video_info.get('title', '未知标题')
            author = video_info.get('author', '未知UP主')
            play = video_info.get('play_count', 0)
            bvid = video_info.get('bvid', '')
            duration = video_info.get('duration', '未知')
            
            # 文字消息
            text_msg = (
                f"{prefix}\n"
                f"标题：{title}\n"
                f"UP主：{author}\n"
                f"时长：{duration}\n"
                f"播放量：{play}\n"
                f"链接：https://www.bilibili.com/video/{bvid}"
            )
            chain.append(Plain(text_msg))
        
        # 检查文件是否存在
        if file_path and os.path.exists(file_path) and os.path.getsize(file_path) > 0:
            video = Video.fromFileSystem(path=file_path)
            chain.append(video)
        
        return chain

    async def _send_to_current_chat(self, event: AstrMessageEvent, file_path: str, video_info: Dict[str, Any]):
        """发送到当前聊天"""
        chain = self._create_video_message(video_info, file_path)
        yield event.chain_result(chain)
        logger.info("已发送到当前聊天")

    async def _send_to_all_groups(self, file_path: str, video_info: Dict[str, Any], prefix: str = "【B站搬石】"):
        """发送到所有已绑定的群（串行发送，群之间带延迟防止风控）"""
        blacklist = set(str(b) for b in self.config.get('blacklist_groups', []))
        
        if not self.bound_groups:
            logger.warning("没有已绑定的群，等待自动记录...")
            return
        
        chain = self._create_video_message(video_info, file_path, prefix)
        message_chain = MessageChain(chain)
        
        # 群间发送延迟（秒），防风控
        send_delay = self.config.get('send_delay', 3)
        count = 0
        
        for gid, umo in self.bound_groups.items():
            if gid in blacklist:
                logger.debug(f"群 {gid} 在黑名单中，跳过")
                continue
            
            try:
                await self.context.send_message(umo, message_chain)
                count += 1
                logger.info(f"已发送到群 {gid} ({count}/{len(self.bound_groups)})")
            except Exception as e:
                logger.error(f"发送到群 {gid} 失败: {e}")
            
            # 非最后一个群则等待延迟
            if count > 0:
                await asyncio.sleep(send_delay)

    # ==================== UP主订阅部分 ====================

    async def _search_up_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        """通过昵称搜索UP主，返回 {uid, name}（search_type=bili_user）"""
        if not self.session:
            return None
        try:
            api_url = "https://api.bilibili.com/x/web-interface/search/type?" + urlencode({
                'search_type': 'bili_user',
                'keyword': name,
                'page': 1,
            })
            async with self.semaphore:
                async with self.session.get(api_url) as response:
                    if response.status != 200:
                        return None
                    data = await response.json()
            if data.get('code') != 0:
                logger.error(f"搜索UP主失败: {data.get('message', '未知错误')}")
                return None
            result = data.get('data', {}).get('result', [])
            if not result:
                return None
            first = result[0]
            return {
                'uid': str(first.get('mid', '')),
                'name': first.get('uname', name),
            }
        except Exception as e:
            logger.error(f"搜索UP主异常: {e}")
            return None

    async def _get_name_by_uid(self, uid: str) -> str:
        """通过UID获取UP主名字（card接口）"""
        if not self.session:
            return ''
        try:
            api_url = f"https://api.bilibili.com/x/web-interface/card?mid={uid}"
            async with self.semaphore:
                async with self.session.get(api_url) as response:
                    if response.status != 200:
                        return ''
                    data = await response.json()
            if data.get('code') == 0:
                name = data.get('data', {}).get('card', {}).get('name', '')
                if name:
                    return name
        except Exception as e:
            logger.error(f"获取UP主名字异常: {e}")
        return ''

    async def _resolve_uid(self, text: str) -> Optional[Dict[str, Any]]:
        """解析UID或昵称为订阅信息 {uid, name}"""
        text = text.strip()
        if text.isdigit():
            name = await self._get_name_by_uid(text)
            if not name:
                return None
            return {'uid': text, 'name': name}
        return await self._search_up_by_name(text)

    async def _get_up_latest_videos(self, uid: str, name: str, limit: int = 5) -> List[Dict[str, Any]]:
        """获取UP主最新投稿列表（搜索接口按发布时间排序，按作者mid精确过滤）"""
        if not self.session:
            return []
        try:
            api_url = "https://api.bilibili.com/x/web-interface/search/type?" + urlencode({
                'search_type': 'video',
                'keyword': name,
                'order': 'pubdate',
                'page': 1,
            })
            async with self.semaphore:
                async with self.session.get(api_url) as response:
                    if response.status != 200:
                        logger.error(f"查询UP主投稿API失败: {response.status}")
                        return []
                    data = await response.json()
            if data.get('code') != 0:
                logger.error(f"查询UP主投稿失败: {data.get('message', '未知错误')}")
                return []

            result = data.get('data', {}).get('result', [])
            if not isinstance(result, list):
                return []

            videos = []
            for v in result:
                # 只保留该UP主本人的视频
                if str(v.get('mid', '')) != str(uid):
                    continue
                bvid = v.get('bvid', '')
                if not bvid:
                    continue
                title = re.sub(r'<[^>]+>', '', v.get('title', ''))
                duration = v.get('duration', '')
                videos.append({
                    'title': title,
                    'url': f"https://www.bilibili.com/video/{bvid}",
                    'bvid': bvid,
                    'play_count': self._parse_play_count(v.get('play', 0)),
                    'duration': duration,
                    'duration_seconds': self._parse_duration(str(duration)),
                    'author': v.get('author', name),
                    'created': v.get('pubdate', 0),
                    'up_name': name,
                })
            # 按发布时间倒序
            videos.sort(key=lambda x: x.get('created', 0), reverse=True)
            return videos[:limit]
        except Exception as e:
            logger.error(f"获取UP主投稿异常: {e}")
            return []

    def _load_subscriptions(self) -> Dict[str, Dict[str, Any]]:
        """加载订阅数据"""
        if self.subscriptions_path.exists():
            try:
                with open(self.subscriptions_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"读取订阅数据失败: {e}")
        return {}

    def _save_subscriptions(self):
        """保存订阅数据"""
        try:
            with open(self.subscriptions_path, 'w', encoding='utf-8') as f:
                json.dump(self.subscriptions, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存订阅数据失败: {e}")

    async def _check_subscriptions(self):
        """检查所有订阅UP主是否有更新"""
        if not self.subscriptions:
            return
        if self._is_quiet_hours():
            logger.debug("免打扰时段，跳过订阅检查")
            return

        max_duration = self.config.get('sub_max_duration', 1200)
        fresh_days = self.config.get('sub_fresh_days', 3)
        cutoff = 0
        if fresh_days and fresh_days > 0:
            cutoff = int(time.time()) - fresh_days * 86400

        for uid, sub in list(self.subscriptions.items()):
            try:
                name = sub.get('name', uid)
                logger.info(f"检查UP主 {name}({uid}) 更新...")
                videos = await self._get_up_latest_videos(uid, name, limit=5)
                pushed = set(sub.get('pushed_bvids', []))

                new_videos = []
                for v in videos:
                    if v['bvid'] in pushed:
                        continue
                    if cutoff and v.get('created', 0) < cutoff:
                        # 超过新鲜期的旧视频不搬，直接标记跳过避免反复检查
                        logger.info(f"视频发布时间超过{fresh_days}天，跳过: {v['title']}")
                        pushed.add(v['bvid'])
                        continue
                    new_videos.append(v)
                new_videos.sort(key=lambda x: x.get('created', 0), reverse=True)

                if not new_videos:
                    logger.info(f"UP主 {name} 没有新视频")
                    continue

                # 每次最多推送3个，防止刷屏
                for v in new_videos[:3]:
                    duration = v.get('duration_seconds', 0)
                    if max_duration and duration > max_duration:
                        logger.info(f"视频超时跳过: {v['title']} ({duration}秒)")
                        pushed.add(v['bvid'])
                        continue

                    logger.info(f"发现新视频: {v['title']} ({v['bvid']})")
                    file_path = await self._download_video(v)
                    if not file_path or not os.path.exists(file_path):
                        logger.error(f"下载新视频失败: {v['bvid']}")
                        continue

                    await self._send_to_all_groups(file_path, v, prefix="【UP更新】")
                    await self._cleanup_after_send(file_path)
                    pushed.add(v['bvid'])

                # 只保留最近20个已推送记录
                sub['pushed_bvids'] = list(pushed)[-20:]
                self._save_subscriptions()

            except Exception as e:
                logger.error(f"检查UP主 {uid} 订阅异常: {e}")

    async def _subscriber_task(self):
        """订阅检查定时任务"""
        while self.running:
            try:
                await self._check_subscriptions()
                await asyncio.sleep(self.config.get('sub_check_interval', 600))
            except asyncio.CancelledError:
                logger.info("订阅检查任务被取消")
                break
            except Exception as e:
                logger.error(f"订阅检查任务异常: {e}")
                await asyncio.sleep(60)

    # ==================== 核心流程 ====================

    async def _execute_scan_and_download(self, event: Optional[AstrMessageEvent] = None):
        """
        执行扫描和下载的核心逻辑
        返回: (成功标志, 文件路径, 视频信息)
        """
        # 随机选择一个视频
        target = await self._select_random_video()
        
        if not target:
            logger.error("没有找到合适的视频")
            return False, None, None
        
        logger.info(f"选中视频: {target['title']} (BV: {target['bvid']}, 时长: {target['duration']})")
        
        # 下载视频
        file_path = await self._download_video(target)
        
        if not file_path or not os.path.exists(file_path):
            logger.error("下载失败")
            return False, None, target
        
        return True, file_path, target

    async def _cleanup_after_send(self, file_path: str):
        """发送后清理文件"""
        if self.config.get('delete_after_send', True) and file_path:
            try:
                if os.path.exists(file_path):
                    os.remove(file_path)
                    self.temp_files.discard(file_path)
                    logger.info(f"已删除文件: {file_path}")
            except Exception as e:
                logger.error(f"删除文件失败: {e}")

    async def _scan_and_download(self, event: Optional[AstrMessageEvent] = None):
        """
        扫描并下载视频的主流程
        Args:
            event: 如果有 event，说明是命令触发的，发送到当前聊天
                  如果没有 event，说明是定时任务，发送到所有已绑定的群
        """
        logger.info("开始随机搬石...")
        
        # 执行下载
        success, file_path, video_info = await self._execute_scan_and_download(event)
        
        if not success:
            if event:
                yield event.plain_result("❌ 没有找到合适的视频或下载失败")
            return
        
        # 发送消息
        if event:
            # 命令触发：发送到当前聊天
            async for result in self._send_to_current_chat(event, file_path, video_info):
                yield result
        else:
            # 定时任务：发送到所有群
            await self._send_to_all_groups(file_path, video_info)
        
        # 清理文件
        await self._cleanup_after_send(file_path)

    def _is_quiet_hours(self) -> bool:
        """检查当前是否在免打扰时段内"""
        quiet_start = self.config.get('quiet_hours_start', '')
        quiet_end = self.config.get('quiet_hours_end', '')
        
        if not quiet_start or not quiet_end:
            return False
        
        try:
            now = datetime.now().strftime('%H:%M')
            if quiet_start <= quiet_end:
                # 正常范围，如 01:00 - 08:00
                return quiet_start <= now < quiet_end
            else:
                # 跨午夜范围，如 23:00 - 08:00
                return now >= quiet_start or now < quiet_end
        except Exception as e:
            logger.error(f"解析免打扰时段失败: {e}")
            return False

    async def _timer_task(self):
        """定时任务（带异常恢复）"""
        while self.running:
            try:
                # 检查免打扰时段
                if self._is_quiet_hours():
                    logger.debug("当前在免打扰时段，跳过本次推送")
                    await asyncio.sleep(self.config.get('scan_interval', 60))
                    continue
                
                # 执行扫描和下载（_scan_and_download 是异步生成器，需要用 async for 消费）
                async for _ in self._scan_and_download():
                    pass
                
                # 等待下一次扫描
                await asyncio.sleep(self.config.get('scan_interval', 60))
                
            except asyncio.CancelledError:
                logger.info("定时任务被取消")
                break
            except Exception as e:
                logger.error(f"定时任务异常: {e}")
                # 发生异常时等待较长时间再重试，避免频繁失败
                await asyncio.sleep(60)

    # ==================== 指令区 ====================

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bilibanshi on", alias={"banshi on"})
    async def turn_on(self, event: AstrMessageEvent):
        """开启搬石"""
        if self.running:
            yield event.plain_result("搬石已经在运行了")
            return
        
        self.running = True
        self.task = asyncio.create_task(self._timer_task())
        yield event.plain_result("B站搬石已开启 (1分钟一次)")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bilibanshi off", alias={"banshi off"})
    async def turn_off(self, event: AstrMessageEvent):
        """关闭搬石"""
        if not self.running:
            yield event.plain_result("搬石已经关闭了")
            return
        
        self.running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                logger.info("定时任务已取消")
            except Exception as e:
                logger.error(f"停止定时任务时出错: {e}")
            self.task = None
        yield event.plain_result("B站搬石已关闭")

    @filter.command("bilibanshi now", alias={"banshi now"})
    async def scan_now(self, event: AstrMessageEvent):
        """立即执行一次（只发送到当前聊天，不推送所有群）"""
        yield event.plain_result("开始搬石...")
        
        # 执行下载
        success, file_path, video_info = await self._execute_scan_and_download(event)
        if not success:
            yield event.plain_result("❌ 没有找到合适的视频或下载失败")
            return
        
        # 只发送到当前聊天（不调用 _send_to_all_groups）
        async for result in self._send_to_current_chat(event, file_path, video_info):
            yield result
        
        # 清理文件
        await self._cleanup_after_send(file_path)

    @filter.command("bilibanshi search", alias={"banshi search"})
    async def search_keyword_fetch(self, event: AstrMessageEvent):
        """按指定关键词搬石: /bilibanshi search <关键词> [序号]"""
        parts = event.message_str.strip().split()
        if len(parts) < 3:
            yield event.plain_result("用法: /bilibanshi search <关键词> [序号]\n例: /bilibanshi search 高松灯 3")
            return
        
        args = parts[2:]
        # 可选序号：最后一个参数是数字时视为选择第N个结果
        index = None
        if len(args) >= 2 and args[-1].isdigit():
            index = int(args[-1])
            args = args[:-1]
        
        keyword = ' '.join(args).strip()
        if not keyword:
            yield event.plain_result("用法: /bilibanshi search <关键词> [序号]")
            return
        
        yield event.plain_result(f"🔍 正在搜索关键词「{keyword}」...")
        videos = await self._search_videos_by_keyword(keyword, self.config.get('max_pages', 3))
        
        if not videos:
            yield event.plain_result(f"❌ 关键词「{keyword}」没有找到符合条件（标题含关键词且不超时长）的视频")
            return
        
        if index is not None:
            if index < 1 or index > len(videos):
                yield event.plain_result(f"❌ 序号 {index} 超出范围（共 {len(videos)} 个）")
                return
            selected = videos[index - 1]
            yield event.plain_result(f"🎯 选择第 {index} 个: {selected['title']}")
        else:
            selected = random.choice(videos)
            yield event.plain_result(f"🎯 共找到 {len(videos)} 个视频，随机选中: {selected['title']}")
        
        file_path = await self._download_video(selected)
        if not file_path or not os.path.exists(file_path):
            yield event.plain_result("❌ 下载失败，请稍后重试")
            return
        
        async for result in self._send_to_current_chat(event, file_path, selected):
            yield result
        await self._cleanup_after_send(file_path)

    @filter.command("bilibanshi get", alias={"banshi get"})
    async def get_by_bvid(self, event: AstrMessageEvent):
        """按BV号/AV号下载视频并压缩发送: /bilibanshi get <BV号|AV号|链接>"""
        parts = event.message_str.strip().split()
        if len(parts) < 3:
            yield event.plain_result("用法: /bilibanshi get <BV号或AV号或视频链接>\n例: /bilibanshi get BV1GJ411x7h7 或 /bilibanshi get av170001")
            return
        
        identifier = ' '.join(parts[2:]).strip()
        yield event.plain_result(f"⏳ 正在解析: {identifier}")
        
        video_info = await self._resolve_video_identifier(identifier)
        if not video_info or not video_info.get('bvid'):
            yield event.plain_result("❌ 无法解析该视频，请检查BV号/AV号是否正确")
            return
        
        yield event.plain_result(
            f"📺 {video_info['title']}\n"
            f"UP主: {video_info['author']} | 时长: {video_info['duration']}"
        )
        
        file_path = await self._download_video(video_info, force_compress=True)
        if not file_path or not os.path.exists(file_path):
            yield event.plain_result("❌ 下载失败（视频可能不存在、需要登录或被删）")
            return
        
        yield event.plain_result("✅ 下载完成，正在发送...")
        async for result in self._send_to_current_chat(event, file_path, video_info):
            yield result
        await self._cleanup_after_send(file_path)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bilibanshi sub", alias={"banshi sub"})
    async def manage_subscription(self, event: AstrMessageEvent):
        """订阅UP主: /bilibanshi sub add <UID或昵称> | remove <UID> | list"""
        parts = event.message_str.strip().split()
        if len(parts) < 3:
            yield event.plain_result("用法:\n/bilibanshi sub add <UID或昵称>\n/bilibanshi sub remove <UID>\n/bilibanshi sub list")
            return

        action = parts[2].lower()

        if action == "list":
            if not self.subscriptions:
                yield event.plain_result("📭 暂无订阅的UP主")
                return
            lines = ["📋 已订阅UP主:"]
            for uid, sub in self.subscriptions.items():
                lines.append(f"· {sub.get('name', uid)} (UID: {uid}) - 已推送 {len(sub.get('pushed_bvids', []))} 个视频")
            yield event.plain_result("\n".join(lines))
            return

        if len(parts) < 4:
            yield event.plain_result("用法: /bilibanshi sub add <UID或昵称> 或 /bilibanshi sub remove <UID>")
            return

        target = ' '.join(parts[3:]).strip()

        if action == "add":
            yield event.plain_result(f"🔍 正在解析UP主: {target} ...")
            info = await self._resolve_uid(target)
            if not info or not info.get('uid'):
                yield event.plain_result("❌ 未找到该UP主，请检查UID或昵称是否正确")
                return
            uid = info['uid']
            if uid in self.subscriptions:
                yield event.plain_result(f"⚠️ 已订阅过该UP主: {info['name']} (UID: {uid})")
                return
            # 初始化：把当前最新投稿标记为已推送，避免补推旧视频
            videos = await self._get_up_latest_videos(uid, info['name'], limit=1)
            pushed = [v['bvid'] for v in videos]
            self.subscriptions[uid] = {
                'uid': uid,
                'name': info.get('name') or f"UID:{uid}",
                'pushed_bvids': pushed,
                'subscribed_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            }
            self._save_subscriptions()
            yield event.plain_result(f"✅ 已订阅UP主: {info['name']} (UID: {uid})\n之后有新投稿会自动下载并推送到所有群")

        elif action == "remove":
            del_key = None
            if target.isdigit() and target in self.subscriptions:
                del_key = target
            else:
                for uid, sub in self.subscriptions.items():
                    if sub.get('name', '') == target:
                        del_key = uid
                        break
            if not del_key:
                yield event.plain_result(f"❌ 未找到订阅: {target}\n用 /bilibanshi sub list 查看已订阅列表")
                return
            name = self.subscriptions[del_key].get('name', del_key)
            del self.subscriptions[del_key]
            self._save_subscriptions()
            yield event.plain_result(f"✅ 已取消订阅: {name} (UID: {del_key})")
        else:
            yield event.plain_result("未知操作，请使用 add / remove / list")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bilibanshi list", alias={"banshi list"})
    async def list_status(self, event: AstrMessageEvent):
        """查看当前状态"""
        max_duration = self.config.get('max_duration', 600)
        
        status = [
            "=== B站搬石状态 ===",
            f"运行状态: {'✅ 运行中' if self.running else '❌ 已停止'}",
            f"扫描间隔: {self.config.get('scan_interval', 60)}秒",
            f"最大时长: {max_duration}秒 ({max_duration//60}分钟)",
            f"已绑定的群: {len(self.bound_groups)} 个",
            f"黑名单群: {len(self.config.get('blacklist_groups', []))} 个",
            f"关键词: {len(self.config.get('search_keywords', []))} 个",
            f"数据目录: {self.data_dir}",
            f"FFmpeg: {'✅ 可用' if self.ffmpeg_available else '❌ 不可用'}",
        ]
        
        # 显示已绑定的群号
        if self.bound_groups:
            status.append(f"群列表: {', '.join(self.bound_groups.keys())}")
        
        yield event.plain_result("\n".join(status))

    @filter.command("bilibanshi help", alias={"banshi help", "bilibanshi"})
    async def show_help(self, event: AstrMessageEvent):
        """显示帮助"""
        help_text = """=== B站搬石 v1.1.1 ===

👤 全员可用:
/banshi — 显示本帮助
/banshi now — 立即随机搬一个视频
/banshi search <关键词> [序号] — 搜索搬石
/banshi get <BV号|AV号|链接> — 下载指定视频

🔒 管理员:
/banshi list — 查看状态
/banshi sub list — 查看订阅
/banshi sub add/remove — 订阅管理
/banshi on / off — 开关搬石
/banshi keyword add/remove — 关键词管理
/banshi blacklist add/remove — 黑名单管理
/banshi interval <秒> — 设置间隔
/banshi maxduration <秒> — 最大时长
/banshi clean — 清理临时文件"""

        yield event.plain_result(help_text)

    @filter.regex(r"^/banshi$")
    async def banshi_help(self, event: AstrMessageEvent):
        """精确匹配 /banshi（无参数）显示帮助"""
        help_text = """=== B站搬石 v1.1.1 ===

👤 全员可用:
/banshi — 显示本帮助
/banshi now — 立即随机搬一个视频
/banshi search <关键词> [序号] — 搜索搬石
/banshi get <BV号|AV号|链接> — 下载指定视频

🔒 管理员:
/banshi list — 查看状态
/banshi sub list — 查看订阅
/banshi sub add/remove — 订阅管理
/banshi on / off — 开关搬石
/banshi keyword add/remove — 关键词管理
/banshi blacklist add/remove — 黑名单管理
/banshi interval <秒> — 设置间隔
/banshi maxduration <秒> — 最大时长
/banshi clean — 清理临时文件"""
        yield event.plain_result(help_text)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bilibanshi blacklist", alias={"banshi blacklist"})
    async def manage_blacklist(self, event: AstrMessageEvent):
        """管理黑名单: /bilibanshi blacklist add 123456 或 /bilibanshi blacklist remove 123456"""
        parts = event.message_str.strip().split()
        if len(parts) < 4:
            yield event.plain_result("用法: /bilibanshi blacklist <add|remove> <群号>")
            return
        
        action = parts[2].lower()
        group_id = parts[3]
        
        if not group_id.isdigit():
            yield event.plain_result("群号必须是数字")
            return
        
        blacklist = self.config.get('blacklist_groups', [])
        
        if action == "add":
            if group_id not in blacklist:
                blacklist.append(group_id)
                self.config['blacklist_groups'] = blacklist
                self.config.save_config()
                yield event.plain_result(f"✅ 已添加黑名单群: {group_id}")
            else:
                yield event.plain_result("该群已在黑名单中")
        elif action == "remove":
            if group_id in self.config.get('blacklist_groups', []):
                self.config['blacklist_groups'].remove(group_id)
                self.config.save_config()
                yield event.plain_result(f"✅ 已移除黑名单群: {group_id}")
            else:
                yield event.plain_result("该群不在黑名单中")
        else:
            yield event.plain_result("未知操作，请使用 add 或 remove")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bilibanshi keyword", alias={"banshi keyword"})
    async def manage_keyword(self, event: AstrMessageEvent):
        """管理关键词: /bilibanshi keyword add 搞笑 或 /bilibanshi keyword remove 搞笑"""
        parts = event.message_str.strip().split()
        if len(parts) < 4:
            yield event.plain_result("用法: /bilibanshi keyword <add|remove> <关键词>")
            return
        
        action = parts[2].lower()
        keyword = ' '.join(parts[3:])  # 支持带空格的关键词
        
        keywords = self.config.get('search_keywords', [])
        
        if action == "add":
            if keyword not in keywords:
                keywords.append(keyword)
                self.config['search_keywords'] = keywords
                self.config.save_config()
                yield event.plain_result(f"✅ 已添加关键词: {keyword}")
            else:
                yield event.plain_result("该关键词已存在")
        elif action == "remove":
            if keyword in keywords:
                keywords.remove(keyword)
                self.config['search_keywords'] = keywords
                self.config.save_config()
                yield event.plain_result(f"✅ 已删除关键词: {keyword}")
            else:
                yield event.plain_result("未找到该关键词")
        else:
            yield event.plain_result("未知操作，请使用 add 或 remove")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bilibanshi interval", alias={"banshi interval"})
    async def set_interval(self, event: AstrMessageEvent):
        """设置扫描间隔(秒): /bilibanshi interval 60"""
        parts = event.message_str.strip().split()
        if len(parts) < 3:
            yield event.plain_result("用法: /bilibanshi interval <秒数>")
            return
        
        try:
            interval = int(parts[2])
            if interval < 10:
                yield event.plain_result("间隔不能小于10秒")
                return
            
            self.config['scan_interval'] = interval
            self.config.save_config()
            yield event.plain_result(f"已设置扫描间隔: {interval}秒")
        except ValueError:
            yield event.plain_result("请输入有效的数字")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bilibanshi maxduration", alias={"banshi maxduration"})
    async def set_max_duration(self, event: AstrMessageEvent):
        """设置最大时长（秒）: /bilibanshi maxduration 600"""
        parts = event.message_str.strip().split()
        if len(parts) < 3:
            yield event.plain_result("用法: /bilibanshi maxduration <秒数>")
            return
        
        try:
            duration = int(parts[2])
            if duration < 10:
                yield event.plain_result("时长不能小于10秒")
                return
            
            self.config['max_duration'] = duration
            self.config.save_config()
            yield event.plain_result(f"已设置最大时长: {duration}秒 ({duration//60}分钟)")
        except ValueError:
            yield event.plain_result("请输入有效的数字")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bilibanshi clean", alias={"banshi clean"})
    async def clean_temp_files(self, event: AstrMessageEvent):
        """手动清理临时文件"""
        count = len(self.temp_files)
        await self._cleanup_temp_files()
        yield event.plain_result(f"已清理 {count} 个临时文件")