"""Bounded reads that split on response bytes without retrying other failures."""

from app.adapters.contracts import ResponseTooLarge


async def read_batches(values, read, *, size=25, oversized=None):
    """Yield bounded results; a singleton may be isolated by an inventory caller."""

    async def part(batch):
        try:
            result = await read(batch)
        except ResponseTooLarge as error:
            # Release the failed request's response buffer before recursing.
            error.__traceback__ = None
            if len(batch) == 1:
                if oversized is None:
                    raise
                yield oversized(batch[0], error)
                return
        else:
            for item in result:
                yield item
            return
        middle = len(batch) // 2
        async for item in part(batch[:middle]):
            yield item
        async for item in part(batch[middle:]):
            yield item

    for offset in range(0, len(values), size):
        async for item in part(values[offset : offset + size]):
            yield item
