"""Feishu I/O in a plugin-owned process; the host's logging and SDK stay untouched."""
import asyncio
import json
import os
from pathlib import Path
import socket
import sys

from .manager import ManagementError

_MAX_FRAME = 1024 * 1024


class FeishuProcessTransport:
    """Private socketpair RPC. Credentials never enter argv, environment or console."""
    def __init__(self, credentials, on_message):
        self.credentials = credentials
        self.sensitive_values = (credentials['app_secret'],)
        self.on_message = on_message
        self.lifecycle_check = lambda: None
        self.process = self.reader = self.writer = self.reader_task = None
        self.pending = {}
        self.sequence = 0
        self.ready = None
        self._dispatch_enabled = asyncio.Event()
        self.events = asyncio.Queue(maxsize=256)
        self.event_task = None
        self._write_lock = asyncio.Lock()
        self._closed = False
        self._close_requested = False
        self._close_task = None

    def bind_lifecycle_guard(self, check):
        self.lifecycle_check = check

    async def start(self):
        plugin_root = Path(__file__).resolve().parents[1]
        parent, child = socket.socketpair()
        parent.setblocking(False)
        try:
            self.process = await asyncio.create_subprocess_exec(
                sys.executable, '-m', 'ghost_hermes_pm.owned_feishu_process', str(child.fileno()),
                pass_fds=(child.fileno(),), stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                cwd=plugin_root,
                env={'PATH': os.environ.get('PATH', ''), 'PYTHONPATH': os.pathsep.join([str(plugin_root), *sys.path]),
                     'PYTHONDONTWRITEBYTECODE': '1'})
            child.close()
            if self._close_requested:
                raise ManagementError('unavailable', 'Owned platform was unloaded during startup.')
            self.reader, self.writer = await asyncio.open_connection(sock=parent, limit=_MAX_FRAME)
            self.ready = asyncio.get_running_loop().create_future()
            self.reader_task = asyncio.create_task(self._read(), name='hermes-pm-feishu-process-reader')
            self.event_task = asyncio.create_task(self._events(), name='hermes-pm-feishu-process-events')
            await self._write({'operation': 'initialize', 'credentials': self.credentials})
            await asyncio.wait_for(asyncio.shield(self.ready), timeout=30)
            return True
        except BaseException:
            parent.close()
            child.close()
            await self.close()
            raise

    async def _write(self, value):
        payload = json.dumps(value, ensure_ascii=False).encode() + b'\n'
        if len(payload) > _MAX_FRAME:
            raise ManagementError('unavailable', 'Owned platform payload exceeded its transport limit.')
        async with self._write_lock:
            if self._close_requested or self._closed:
                raise ManagementError('unavailable', 'Owned platform is disconnected.')
            self.lifecycle_check()
            self.writer.write(payload)
            await self.writer.drain()

    async def _read(self):
        try:
            while raw := await self.reader.readline():
                if len(raw) > _MAX_FRAME:
                    break
                frame = json.loads(raw)
                if frame.get('kind') == 'ready':
                    if not self.ready.done():
                        self.ready.set_result(True)
                elif frame.get('kind') == 'event':
                    try:
                        self.events.put_nowait(frame['payload'])
                    except asyncio.QueueFull:
                        break
                elif frame.get('id') in self.pending:
                    future = self.pending.pop(frame['id'])
                    if not future.done():
                        if frame.get('ok') is True:
                            future.set_result(frame.get('result'))
                        else:
                            future.set_exception(ManagementError('unavailable', 'Owned platform request failed.'))
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        finally:
            error = ManagementError('unavailable', 'Owned platform connection ended.')
            if self.ready is not None and not self.ready.done():
                self.ready.set_exception(error)
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(error)
            self.pending.clear()
            if self.writer is not None:
                self.writer.close()

    def activate(self):
        self._dispatch_enabled.set()

    async def _events(self):
        await self._dispatch_enabled.wait()
        while True:
            payload = await self.events.get()
            try:
                await self.on_message(payload)
            except Exception:
                pass
            finally:
                self.events.task_done()

    async def call(self, operation, value):
        self.lifecycle_check()
        if self._closed or self._close_requested or self.reader_task is None or self.reader_task.done():
            raise ManagementError('unavailable', 'Owned platform is disconnected.')
        self.sequence += 1
        ident = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[ident] = future
        try:
            await self._write({'id': ident, 'operation': operation, 'value': value})
            result = await asyncio.wait_for(future, timeout=30)
            self.lifecycle_check()
            return result
        finally:
            self.pending.pop(ident, None)

    async def verify_identity(self, binding):
        return await self.call('verify_identity', binding)

    async def verify_channel(self, binding, evidence, challenge):
        return await self.call('verify_channel', [binding, evidence, challenge])

    async def send(self, segment):
        try:
            return await self.call('send', segment)
        except (ManagementError, TimeoutError, OSError):
            return {'status': 'unknown'}

    def request_close(self):
        self._close_requested = True
        if self.writer is not None:
            self.writer.close()

    async def close(self):
        self.request_close()
        if self._closed:
            return
        if self._close_task is None or self._close_task.done():
            self._close_task = asyncio.create_task(self._finish_close(), name='hermes-pm-feishu-process-close')
        # Cancelling a waiter does not abandon ownership of live resources.
        await asyncio.shield(self._close_task)

    async def _finish_close(self):
        if self.writer is not None:
            try:
                await asyncio.wait_for(self.writer.wait_closed(), timeout=2)
            except (OSError, TimeoutError):
                pass
        if self.process is not None:
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5)
            except asyncio.TimeoutError:
                try:
                    self.process.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(self.process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    try:
                        self.process.kill()
                    except ProcessLookupError:
                        pass
                    await asyncio.wait_for(self.process.wait(), timeout=5)
        for task in (self.event_task, self.reader_task):
            if task is not None and task is not asyncio.current_task():
                task.cancel()
        tasks = [task for task in (self.event_task, self.reader_task) if task is not None and task is not asyncio.current_task()]
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=5)
        while not self.events.empty():
            self.events.get_nowait()
            self.events.task_done()
        self._closed = True


def _serve(fd):
    # This process belongs only to this plugin. No host logger/factory/signal is altered.
    import logging
    logging.disable(sys.maxsize)
    import threading
    import queue
    import lark_oapi as lark
    from lark_oapi.ws import Client as WSClient
    from .feishu import NativeFeishuTransport

    channel = socket.socket(fileno=fd)
    source = channel.makefile('rb')
    target = channel.makefile('wb')
    write_lock = threading.Lock()

    def write(value):
        payload = json.dumps(value, ensure_ascii=False).encode() + b'\n'
        if len(payload) > _MAX_FRAME:
            return
        with write_lock:
            target.write(payload)
            target.flush()

    initial = json.loads(source.readline(_MAX_FRAME + 1))
    credentials = initial['credentials']
    domain = lark.LARK_DOMAIN if credentials.get('domain') == 'lark' else lark.FEISHU_DOMAIN
    native = lark.Client.builder().app_id(credentials['app_id']).app_secret(credentials['app_secret']).domain(domain).log_level(lark.LogLevel.CRITICAL).build()
    transport = NativeFeishuTransport(native)
    active = threading.Event()
    active.set()
    commands = queue.Queue(maxsize=256)

    def require_active():
        if not active.is_set():
            raise ManagementError('unavailable', 'Owned platform is disconnected.')

    transport.bind_lifecycle_guard(require_active)

    def event(data):
        # Marshall only official event data, over inherited private descriptors.
        write({'kind': 'event', 'payload': json.loads(lark.JSON.marshal(data))})

    handler = lark.EventDispatcherHandler.builder('', '').register_p2_im_message_receive_v1(event).build()
    ws = WSClient(credentials['app_id'], credentials['app_secret'], event_handler=handler,
                  log_level=lark.LogLevel.CRITICAL, domain=domain, extra_ua_tags=['channel'])

    def receive():
        try:
            while raw := source.readline(_MAX_FRAME + 1):
                if len(raw) > _MAX_FRAME:
                    break
                commands.put_nowait(json.loads(raw))
        except Exception:
            pass
        finally:
            active.clear()
            ws._auto_reconnect = False
            from lark_oapi.ws.client import loop
            try:
                asyncio.run_coroutine_threadsafe(ws._disconnect(), loop).result(timeout=3)
            except Exception:
                pass
            os._exit(0)

    def rpc():
        while active.is_set():
            frame = commands.get()
            try:
                require_active()
                operation, value = frame['operation'], frame['value']
                if operation not in {'verify_identity', 'verify_channel', 'send'}:
                    raise ValueError('unsupported operation')
                method = getattr(transport, operation)
                result = asyncio.run(method(*value) if operation == 'verify_channel' else method(value))
                require_active()
                write({'id': frame['id'], 'ok': True, 'result': result})
            except Exception:
                if active.is_set():
                    write({'id': frame.get('id'), 'ok': False})
            finally:
                commands.task_done()

    threading.Thread(target=rpc, daemon=True).start()
    threading.Thread(target=receive, daemon=True).start()
    # READY is a real successful WS handshake, not mere process creation.
    from lark_oapi.ws.client import loop
    loop.run_until_complete(ws._connect())
    write({'kind': 'ready'})
    loop.create_task(ws._ping_loop())
    loop.run_forever()


if __name__ == '__main__':
    _serve(int(sys.argv[1]))
