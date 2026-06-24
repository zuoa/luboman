import asyncio
import functools
from typing import Any, Callable, Optional


async def run_blocking(func: Callable[..., Any], *args, executor: Optional[Any] = None, **kwargs) -> Any:
    """Run a blocking callable without pinning the event loop."""
    loop = asyncio.get_running_loop()
    call = functools.partial(func, *args, **kwargs)
    return await loop.run_in_executor(executor, call)
