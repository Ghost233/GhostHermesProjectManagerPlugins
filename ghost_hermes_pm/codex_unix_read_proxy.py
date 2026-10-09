"""Owned JSONL client bridge to an explicitly registered original Unix-WebSocket.

This file is executable with the host Python from any approved working directory.
It owns only a client connection, never an app-server or executor lifecycle.
"""
import asyncio
import json
import logging
import sys

READ_METHODS = frozenset({'initialize', 'initialized', 'thread/read', 'thread/list', 'thread/loaded/list',
                          'thread/backgroundTerminals/list', 'thread/turns/list', 'thread/items/list'})
MAX_FRAME_BYTES = 16 * 1024 * 1024


def _write(envelope):
    frame = (json.dumps(envelope, ensure_ascii=False, separators=(',', ':')) + '\n').encode()
    if len(frame) > MAX_FRAME_BYTES:
        raise ValueError('Frame exceeds the observation boundary.')
    sys.stdout.buffer.write(frame)
    sys.stdout.buffer.flush()


def _allowed(envelope):
    return (isinstance(envelope, dict) and not set(envelope) - {'id', 'method', 'params'}
            and isinstance(envelope.get('method'), str) and envelope['method'] in READ_METHODS
            and isinstance(envelope.get('params', {}), dict)
            and (envelope['method'] != 'thread/list' or envelope.get('params', {}).get('useStateDbOnly') is True)
            and ('id' not in envelope or type(envelope['id']) in (str, int)))


async def _send(reader, connection):
    while True:
        frame = await reader.readline()
        if not frame:
            return
        if len(frame) > MAX_FRAME_BYTES or not frame.endswith(b'\n'):
            raise ValueError('Incomplete or oversized observation input.')
        envelope = json.loads(frame)
        if not _allowed(envelope):
            if isinstance(envelope, dict) and type(envelope.get('id')) in (str, int):
                _write({'id': envelope['id'], 'error': {'code': -32601,
                        'message': 'Observation permits read methods only; server requests receive no answer.'}})
                continue
            raise ValueError('Invalid observation request.')
        await connection.send(frame[:-1].decode())


async def _receive(connection):
    async for frame in connection:
        if not isinstance(frame, str) or len(frame.encode()) + 1 > MAX_FRAME_BYTES:
            raise ValueError('Invalid original-service frame.')
        envelope = json.loads(frame)
        if not isinstance(envelope, dict):
            raise ValueError('Invalid original-service envelope.')
        _write(envelope)


async def _run(endpoint):
    from websockets.asyncio.client import unix_connect
    logger = logging.Logger('owned-observation-websocket')
    logger.disabled = True
    reader = asyncio.StreamReader(limit=MAX_FRAME_BYTES)
    transport, _ = await asyncio.get_running_loop().connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer)
    tasks = []
    try:
        async with unix_connect(endpoint, uri='ws://localhost/', max_size=MAX_FRAME_BYTES - 1,
                                open_timeout=10, close_timeout=1, logger=logger) as connection:
            tasks = [asyncio.create_task(_send(reader, connection)), asyncio.create_task(_receive(connection))]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        transport.close()


def main():
    # Protocol-only stdout and quiet failure: original endpoints/content never enter diagnostics.
    if len(sys.argv) != 3 or sys.argv[1] != '--sock' or not sys.argv[2].startswith('/'):
        return 2
    try:
        asyncio.run(_run(sys.argv[2]))
    except Exception:
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
