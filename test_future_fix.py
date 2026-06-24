#!/usr/bin/env python3
"""
测试脚本：验证 EventManager Future 对象泄漏修复

使用方法：
    python test_future_fix.py

预期结果：
    - 所有测试通过
    - 无内存泄漏警告
    - Future 对象被正确清理
"""

import sys
import time
import logging

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# 导入需要测试的模块
try:
    from luboman.core.event import EventManager, Event, EventType
    logger.info("✅ 成功导入 EventManager")
except ImportError as e:
    logger.error(f"❌ 导入失败: {e}")
    sys.exit(1)


def test_basic_future_tracking():
    """测试1: 基本的 Future 追踪功能"""
    logger.info("\n" + "="*60)
    logger.info("测试1: 基本的 Future 追踪功能")
    logger.info("="*60)
    
    manager = EventManager()
    manager.start()
    
    # 注册一个处理器
    @manager.register(EventType.EVENT_CHECK_STATUS, "NORMAL")
    def test_handler(event):
        time.sleep(0.1)  # 模拟耗时操作
        logger.debug("处理事件")
        return None
    
    # 发送10个事件
    logger.info("发送 10 个事件...")
    for i in range(10):
        manager.send(Event(EventType.EVENT_CHECK_STATUS))
    
    # 立即检查统计
    time.sleep(0.05)  # 短暂等待
    stats = manager.get_stats()
    logger.info(f"事件发送后: {stats}")
    
    # 应该有一些活跃的 Future
    if stats['active_futures'] > 0:
        logger.info(f"✅ Future 正在追踪: {stats['active_futures']} 个")
    else:
        logger.warning("⚠️  没有活跃的 Future，可能已经完成")
    
    # 等待所有任务完成
    logger.info("等待任务完成...")
    time.sleep(2)
    
    # 再次检查
    final_stats = manager.get_stats()
    logger.info(f"任务完成后: {final_stats}")
    
    # 验证：大部分 Future 应该已经清理
    if final_stats['active_futures'] < 3:
        logger.info(f"✅ Future 自动清理成功: {final_stats['active_futures']} 个剩余")
    else:
        logger.warning(f"⚠️  仍有较多 Future: {final_stats['active_futures']} 个")
    
    # 停止管理器
    logger.info("停止事件管理器...")
    manager.stop()
    
    # 验证：停止后所有 Future 应该被清理
    stopped_stats = manager.get_stats()
    if stopped_stats['active_futures'] == 0:
        logger.info("✅ 停止后所有 Future 已清理")
        return True
    else:
        logger.error(f"❌ 停止后仍有 Future 未清理: {stopped_stats['active_futures']} 个")
        return False


def test_exception_handling():
    """测试2: 异常处理"""
    logger.info("\n" + "="*60)
    logger.info("测试2: 异常处理")
    logger.info("="*60)
    
    manager = EventManager()
    manager.start()
    
    # 注册一个会抛出异常的处理器
    @manager.register(EventType.EVENT_RECORD, "SLOW")
    def error_handler(event):
        time.sleep(0.1)
        raise ValueError("测试异常")
    
    logger.info("发送会触发异常的事件...")
    manager.send(Event(EventType.EVENT_RECORD))
    
    # 等待处理
    time.sleep(0.5)
    
    stats = manager.get_stats()
    logger.info(f"异常处理后: {stats}")
    
    # 即使有异常，Future 也应该被清理
    if stats['active_futures'] == 0:
        logger.info("✅ 异常情况下 Future 仍被正确清理")
        success = True
    else:
        logger.warning(f"⚠️  异常后仍有 Future: {stats['active_futures']} 个")
        success = False
    
    manager.stop()
    return success


def test_batch_processing():
    """测试3: 批量处理"""
    logger.info("\n" + "="*60)
    logger.info("测试3: 批量处理 (100个事件)")
    logger.info("="*60)
    
    manager = EventManager()
    manager.start()
    
    # 注册处理器
    processed_count = [0]  # 使用列表来在闭包中修改
    
    @manager.register(EventType.EVENT_UPLOAD, "NORMAL")
    def batch_handler(event):
        processed_count[0] += 1
        time.sleep(0.02)
        return None
    
    # 发送100个事件
    logger.info("发送 100 个事件...")
    start_time = time.time()
    
    for i in range(100):
        manager.send(Event(EventType.EVENT_UPLOAD))
        
        # 每20个检查一次
        if (i + 1) % 20 == 0:
            stats = manager.get_stats()
            logger.info(f"进度 {i+1}/100: 队列={stats['queue_size']}, "
                       f"Future={stats['active_futures']}")
    
    # 等待处理完成
    logger.info("等待处理完成...")
    time.sleep(5)
    
    elapsed = time.time() - start_time
    
    # 最终统计
    final_stats = manager.get_stats()
    logger.info(f"最终统计: {final_stats}")
    logger.info(f"处理时间: {elapsed:.2f} 秒")
    logger.info(f"已处理事件: {processed_count[0]}")
    
    # 验证
    success = True
    if final_stats['active_futures'] < 5:
        logger.info(f"✅ 批量处理后 Future 清理良好: {final_stats['active_futures']} 个")
    else:
        logger.warning(f"⚠️  批量处理后仍有较多 Future: {final_stats['active_futures']} 个")
        success = False
    
    if processed_count[0] >= 95:  # 允许有少量未处理
        logger.info(f"✅ 事件处理完成: {processed_count[0]}/100")
    else:
        logger.warning(f"⚠️  部分事件未处理: {processed_count[0]}/100")
        success = False
    
    manager.stop()
    return success


def test_memory_usage():
    """测试4: 内存使用监控"""
    logger.info("\n" + "="*60)
    logger.info("测试4: 内存使用监控")
    logger.info("="*60)
    
    try:
        import psutil
        import gc
    except ImportError:
        logger.warning("⚠️  psutil 未安装，跳过内存测试")
        return True
    
    process = psutil.Process()
    
    # 强制垃圾回收
    gc.collect()
    initial_memory = process.memory_info().rss / 1024 / 1024
    logger.info(f"初始内存: {initial_memory:.1f} MB")
    
    # 创建和销毁5个 EventManager，每个处理100个事件
    for cycle in range(5):
        logger.info(f"\n--- 周期 {cycle + 1}/5 ---")
        
        manager = EventManager()
        manager.start()
        
        @manager.register(EventType.EVENT_CHECK_STATUS, "SLOW")
        def memory_test_handler(event):
            # 模拟一些内存操作
            data = [i for i in range(1000)]
            time.sleep(0.01)
            return None
        
        # 发送100个事件
        for i in range(100):
            manager.send(Event(EventType.EVENT_CHECK_STATUS))
        
        # 等待处理
        time.sleep(2)
        
        # 停止并清理
        manager.stop()
        del manager
        
        # 强制垃圾回收
        gc.collect()
        time.sleep(0.5)
        
        # 检查内存
        current_memory = process.memory_info().rss / 1024 / 1024
        growth = current_memory - initial_memory
        logger.info(f"当前内存: {current_memory:.1f} MB (增长 {growth:.1f} MB)")
    
    # 最终检查
    gc.collect()
    final_memory = process.memory_info().rss / 1024 / 1024
    total_growth = final_memory - initial_memory
    
    logger.info(f"\n最终内存: {final_memory:.1f} MB")
    logger.info(f"总增长: {total_growth:.1f} MB")
    
    # 验证：内存增长应该在合理范围内
    if total_growth < 10:
        logger.info("✅ 内存使用正常，无明显泄漏")
        return True
    elif total_growth < 20:
        logger.warning(f"⚠️  内存有一定增长: {total_growth:.1f} MB")
        return True
    else:
        logger.error(f"❌ 内存增长过大，可能存在泄漏: {total_growth:.1f} MB")
        return False


def test_graceful_shutdown():
    """测试5: 优雅关闭"""
    logger.info("\n" + "="*60)
    logger.info("测试5: 优雅关闭")
    logger.info("="*60)
    
    manager = EventManager()
    manager.start()
    
    # 注册一个慢速处理器
    completed = []
    
    @manager.register(EventType.EVENT_NOTIFY, "SLOW")
    def slow_handler(event):
        task_id = event.args[0] if event.args else 0
        time.sleep(0.5)  # 较长的处理时间
        completed.append(task_id)
        logger.info(f"任务 {task_id} 完成")
        return None
    
    # 发送5个事件
    logger.info("发送 5 个慢速任务...")
    for i in range(5):
        manager.send(Event(EventType.EVENT_NOTIFY, (i,)))
    
    # 短暂等待，确保任务已开始
    time.sleep(0.2)
    
    # 立即停止（应该等待任务完成）
    logger.info("立即停止管理器（应该等待任务完成）...")
    start_stop = time.time()
    manager.stop()
    stop_time = time.time() - start_stop
    
    logger.info(f"停止耗时: {stop_time:.2f} 秒")
    logger.info(f"已完成任务: {len(completed)}/5")
    
    # 验证
    success = True
    if stop_time >= 0.25 or len(completed) == 5:  # 应该等待了一段时间，允许调度误差
        logger.info("✅ 停止时确实等待了任务完成")
    else:
        logger.warning("⚠️  停止时间很短，可能未等待")
        success = False
    
    if len(completed) >= 4:  # 允许有1个未完成
        logger.info(f"✅ 大部分任务在停止前完成: {len(completed)}/5")
    else:
        logger.warning(f"⚠️  较多任务未完成: {len(completed)}/5")
        success = False
    
    return success


def main():
    """运行所有测试"""
    logger.info("开始测试 EventManager Future 对象泄漏修复")
    logger.info("="*60)
    
    tests = [
        ("基本追踪", test_basic_future_tracking),
        ("异常处理", test_exception_handling),
        ("批量处理", test_batch_processing),
        ("内存监控", test_memory_usage),
        ("优雅关闭", test_graceful_shutdown),
    ]
    
    results = {}
    
    for test_name, test_func in tests:
        try:
            result = test_func()
            results[test_name] = result
        except Exception as e:
            logger.error(f"测试 '{test_name}' 执行失败: {e}", exc_info=True)
            results[test_name] = False
    
    # 打印总结
    logger.info("\n" + "="*60)
    logger.info("测试总结")
    logger.info("="*60)
    
    passed = sum(1 for r in results.values() if r)
    total = len(results)
    
    for test_name, result in results.items():
        status = "✅ 通过" if result else "❌ 失败"
        logger.info(f"{test_name}: {status}")
    
    logger.info("-"*60)
    logger.info(f"总计: {passed}/{total} 个测试通过")
    
    if passed == total:
        logger.info("🎉 所有测试通过！Future 对象泄漏修复成功！")
        return 0
    else:
        logger.warning(f"⚠️  有 {total - passed} 个测试失败，需要进一步检查")
        return 1


if __name__ == '__main__':
    try:
        exit_code = main()
        sys.exit(exit_code)
    except KeyboardInterrupt:
        logger.info("\n测试被中断")
        sys.exit(130)
    except Exception as e:
        logger.error(f"测试程序异常: {e}", exc_info=True)
        sys.exit(1)

