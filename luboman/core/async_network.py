import asyncio
import aiohttp
import logging
import time
import json
from typing import List, Dict, Optional, Tuple, Any
from dataclasses import dataclass
import weakref

logger = logging.getLogger('luboman')


@dataclass
class NetworkRequest:
    """网络请求数据结构"""
    url: str
    method: str = 'GET'
    headers: Optional[Dict[str, str]] = None
    data: Optional[Any] = None
    timeout: float = 30.0
    room_id: Optional[str] = None
    request_type: str = 'general'  # status_check, download, api
    priority: int = 0


@dataclass
class NetworkResponse:
    """网络响应数据结构"""
    success: bool
    status_code: Optional[int] = None
    data: Optional[Any] = None
    error: Optional[str] = None
    response_time: float = 0.0
    room_id: Optional[str] = None
    request_type: str = 'general'


class AsyncNetworkManager:
    """异步网络请求管理器 - 解决网络IO阻塞问题"""
    
    def __init__(self, max_concurrent: int = 100, max_per_host: int = 20):
        self.max_concurrent = max_concurrent
        self.max_per_host = max_per_host
        self.session: Optional[aiohttp.ClientSession] = None
        self.semaphore = asyncio.Semaphore(max_concurrent)
        
        # 性能统计
        self.stats = {
            'requests_total': 0,
            'requests_success': 0,
            'requests_failed': 0,
            'average_response_time': 0.0,
            'concurrent_requests': 0
        }
        
        # 请求缓存 (简单的LRU缓存)
        self.cache: Dict[str, Tuple[Any, float]] = {}
        self.cache_ttl = 30.0  # 缓存30秒
        self.max_cache_size = 1000
        
        self._closed = False
    
    async def __aenter__(self):
        """异步上下文管理器入口"""
        await self.start()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """异步上下文管理器出口"""
        await self.close()
    
    async def start(self):
        """启动网络管理器"""
        if self.session and not self.session.closed:
            return
        
        # 配置连接器
        connector = aiohttp.TCPConnector(
            limit=self.max_concurrent * 2,  # 总连接池大小
            limit_per_host=self.max_per_host,
            ttl_dns_cache=300,  # DNS缓存5分钟
            use_dns_cache=True,
            enable_cleanup_closed=True,
            keepalive_timeout=60
        )
        
        # 配置超时
        timeout = aiohttp.ClientTimeout(
            total=30,  # 总超时
            connect=10,  # 连接超时
            sock_read=20  # 读取超时
        )
        
        self.session = aiohttp.ClientSession(
            connector=connector,
            timeout=timeout,
            headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
        )
        
        logger.info(f"异步网络管理器启动，最大并发: {self.max_concurrent}")
        self._closed = False
    
    async def close(self):
        """关闭网络管理器"""
        if self._closed:
            return
            
        self._closed = True
        
        if self.session and not self.session.closed:
            await self.session.close()
            # 等待连接完全关闭
            await asyncio.sleep(0.1)
        
        # 清理缓存
        self.cache.clear()
        
        logger.info("异步网络管理器已关闭")
    
    def _get_cache_key(self, request: NetworkRequest) -> str:
        """生成缓存键"""
        return f"{request.method}:{request.url}:{hash(str(request.headers))}"
    
    def _check_cache(self, cache_key: str) -> Optional[Any]:
        """检查缓存"""
        if cache_key in self.cache:
            data, timestamp = self.cache[cache_key]
            if time.time() - timestamp < self.cache_ttl:
                return data
            else:
                # 过期，删除
                del self.cache[cache_key]
        return None
    
    def _set_cache(self, cache_key: str, data: Any):
        """设置缓存"""
        # 简单的LRU：如果缓存满了，删除一些旧条目
        if len(self.cache) >= self.max_cache_size:
            # 删除最旧的20%
            items = list(self.cache.items())
            items.sort(key=lambda x: x[1][1])  # 按时间戳排序
            for i in range(len(items) // 5):  # 删除20%
                del self.cache[items[i][0]]
        
        self.cache[cache_key] = (data, time.time())
    
    async def single_request(self, request: NetworkRequest) -> NetworkResponse:
        """执行单个网络请求"""
        if self._closed:
            return NetworkResponse(
                success=False,
                error="网络管理器已关闭",
                room_id=request.room_id,
                request_type=request.request_type
            )
        
        # 检查缓存
        cache_key = self._get_cache_key(request)
        cached_data = self._check_cache(cache_key)
        if cached_data is not None and request.method == 'GET':
            return NetworkResponse(
                success=True,
                status_code=200,
                data=cached_data,
                response_time=0.001,  # 缓存响应时间
                room_id=request.room_id,
                request_type=request.request_type
            )
        
        start_time = time.time()
        
        try:
            async with self.semaphore:  # 控制并发数
                self.stats['concurrent_requests'] += 1
                
                # 准备请求参数
                kwargs = {
                    'headers': request.headers or {},
                    'timeout': aiohttp.ClientTimeout(total=request.timeout)
                }
                
                if request.data is not None:
                    if request.method in ['POST', 'PUT', 'PATCH']:
                        if isinstance(request.data, dict):
                            kwargs['json'] = request.data
                        else:
                            kwargs['data'] = request.data
                
                # 执行请求
                async with self.session.request(
                    request.method,
                    request.url,
                    **kwargs
                ) as response:
                    response_time = time.time() - start_time
                    
                    # 读取响应数据
                    content_type = response.headers.get('Content-Type', '').lower()
                    if 'application/json' in content_type:
                        response_data = await response.json()
                    else:
                        response_data = await response.text()
                    
                    # 更新统计
                    self.stats['requests_total'] += 1
                    self.stats['concurrent_requests'] -= 1
                    
                    if response.status < 400:
                        self.stats['requests_success'] += 1
                        
                        # 缓存成功的GET请求
                        if request.method == 'GET' and response.status == 200:
                            self._set_cache(cache_key, response_data)
                        
                        # 更新平均响应时间
                        total_requests = self.stats['requests_success']
                        old_avg = self.stats['average_response_time']
                        self.stats['average_response_time'] = (
                            (old_avg * (total_requests - 1) + response_time) / total_requests
                        )
                        
                        return NetworkResponse(
                            success=True,
                            status_code=response.status,
                            data=response_data,
                            response_time=response_time,
                            room_id=request.room_id,
                            request_type=request.request_type
                        )
                    else:
                        self.stats['requests_failed'] += 1
                        return NetworkResponse(
                            success=False,
                            status_code=response.status,
                            error=f"HTTP {response.status}: {response_data}",
                            response_time=response_time,
                            room_id=request.room_id,
                            request_type=request.request_type
                        )
        
        except asyncio.TimeoutError:
            self.stats['requests_failed'] += 1
            self.stats['concurrent_requests'] -= 1
            return NetworkResponse(
                success=False,
                error="请求超时",
                response_time=time.time() - start_time,
                room_id=request.room_id,
                request_type=request.request_type
            )
        
        except Exception as e:
            self.stats['requests_failed'] += 1
            self.stats['concurrent_requests'] -= 1
            return NetworkResponse(
                success=False,
                error=f"请求异常: {str(e)}",
                response_time=time.time() - start_time,
                room_id=request.room_id,
                request_type=request.request_type
            )
    
    async def batch_requests(self, requests: List[NetworkRequest]) -> List[NetworkResponse]:
        """批量执行网络请求"""
        if not requests:
            return []
        
        logger.debug(f"开始批量网络请求，数量: {len(requests)}")
        start_time = time.time()
        
        # 按优先级排序
        requests.sort(key=lambda x: x.priority)
        
        # 并发执行所有请求
        tasks = [self.single_request(req) for req in requests]
        responses = await asyncio.gather(*tasks, return_exceptions=True)
        
        # 处理异常
        final_responses = []
        for i, response in enumerate(responses):
            if isinstance(response, Exception):
                final_responses.append(NetworkResponse(
                    success=False,
                    error=f"批量请求异常: {str(response)}",
                    room_id=requests[i].room_id,
                    request_type=requests[i].request_type
                ))
            else:
                final_responses.append(response)
        
        total_time = time.time() - start_time
        success_count = sum(1 for r in final_responses if r.success)
        
        logger.info(
            f"批量网络请求完成 - "
            f"总数: {len(requests)}, "
            f"成功: {success_count}, "
            f"耗时: {total_time:.2f}s, "
            f"平均: {total_time/len(requests):.3f}s/req"
        )
        
        return final_responses
    
    async def batch_status_check(self, room_list: List[Dict]) -> List[NetworkResponse]:
        """批量检查直播状态"""
        requests = []
        
        for room in room_list:
            # 根据房间平台构造状态检查请求
            request = self._create_status_check_request(room)
            if request:
                requests.append(request)
        
        return await self.batch_requests(requests)
    
    def _create_status_check_request(self, room_data: Dict) -> Optional[NetworkRequest]:
        """根据房间数据创建状态检查请求"""
        platform = room_data.get('room_platform', '').lower()
        room_url = room_data.get('room_url', '')
        room_id = str(room_data.get('id', ''))
        
        if not room_url:
            return None
        
        # 这里需要根据实际的平台API来构造请求
        # 示例：针对不同平台的API
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer': room_url
        }
        
        return NetworkRequest(
            url=room_url,  # 实际应用中应该是平台的API地址
            method='GET',
            headers=headers,
            timeout=15.0,
            room_id=room_id,
            request_type='status_check',
            priority=1  # 状态检查优先级
        )
    
    async def batch_download_assets(self, asset_list: List[Dict]) -> List[NetworkResponse]:
        """批量下载资源（封面、头像等）"""
        requests = []
        
        for asset in asset_list:
            request = NetworkRequest(
                url=asset['url'],
                method='GET',
                headers=asset.get('headers', {}),
                timeout=30.0,
                room_id=asset.get('room_id'),
                request_type='download',
                priority=2  # 下载优先级较低
            )
            requests.append(request)
        
        return await self.batch_requests(requests)
    
    def get_stats(self) -> Dict[str, Any]:
        """获取网络管理器统计信息"""
        return {
            **self.stats,
            'cache_size': len(self.cache),
            'session_closed': self.session is None or self.session.closed if self.session else True
        }
    
    async def stop(self):
        """停止网络管理器 - 兼容组件接口"""
        await self.close()
    
    async def health_check(self) -> bool:
        """健康检查"""
        try:
            request = NetworkRequest(
                url='https://www.baidu.com',
                method='GET',
                timeout=5.0,
                request_type='health_check'
            )
            
            response = await self.single_request(request)
            return response.success
        except Exception:
            return False


# 全局异步网络管理器实例
async_network_manager = AsyncNetworkManager(max_concurrent=150, max_per_host=30)


# 高级批量操作
class NetworkBatchProcessor:
    """网络批量处理器 - 提供更高级的批量操作功能"""
    
    def __init__(self, network_manager: AsyncNetworkManager):
        self.network_manager = network_manager
    
    async def smart_batch_process(self, requests: List[NetworkRequest], 
                                batch_size: int = 50, 
                                delay_between_batches: float = 0.1) -> List[NetworkResponse]:
        """智能批量处理 - 分批执行以避免过载"""
        all_responses = []
        
        for i in range(0, len(requests), batch_size):
            batch = requests[i:i + batch_size]
            batch_responses = await self.network_manager.batch_requests(batch)
            all_responses.extend(batch_responses)
            
            # 批次间延迟
            if i + batch_size < len(requests):
                await asyncio.sleep(delay_between_batches)
        
        return all_responses
    
    async def retry_failed_requests(self, failed_responses: List[NetworkResponse], 
                                  original_requests: List[NetworkRequest],
                                  max_retries: int = 3) -> List[NetworkResponse]:
        """重试失败的请求"""
        retry_requests = []
        retry_indices = []
        
        for i, response in enumerate(failed_responses):
            if not response.success and i < len(original_requests):
                retry_requests.append(original_requests[i])
                retry_indices.append(i)
        
        if not retry_requests:
            return failed_responses
        
        logger.info(f"重试 {len(retry_requests)} 个失败的请求")
        
        for attempt in range(max_retries):
            if not retry_requests:
                break
            
            # 指数退避
            if attempt > 0:
                await asyncio.sleep(2 ** attempt)
            
            retry_responses = await self.network_manager.batch_requests(retry_requests)
            
            # 更新原始响应并准备下一轮重试
            new_retry_requests = []
            new_retry_indices = []
            
            for j, retry_response in enumerate(retry_responses):
                original_index = retry_indices[j]
                if retry_response.success:
                    failed_responses[original_index] = retry_response
                else:
                    new_retry_requests.append(retry_requests[j])
                    new_retry_indices.append(original_index)
            
            retry_requests = new_retry_requests
            retry_indices = new_retry_indices
        
        return failed_responses
