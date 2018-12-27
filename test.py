import asyncio
import unittest
from socket import (
    AF_INET,
    IPPROTO_TCP,
    SHUT_RDWR,
    SO_REUSEADDR,
    SOCK_STREAM,
    SOL_SOCKET,
    socket,
)
from ssl import (
    PROTOCOL_TLSv1_2,
    SSLCertVerificationError,
    SSLContext,
    SSLError,
    create_default_context,
)

from lowhaio import (
    connection_pool,
    ssl_handshake,
    ssl_unwrap_socket,
)


def async_test(func):
    def wrapper(*args, **kwargs):
        future = func(*args, **kwargs)
        loop = asyncio.get_event_loop()
        loop.run_until_complete(future)
    return wrapper


async def server(loop, pre_ssl_client_handler, client_handler):

    ssl_context = SSLContext(PROTOCOL_TLSv1_2)
    ssl_context.load_cert_chain('public.crt', keyfile='private.key')
    server_sock = socket(family=AF_INET, type=SOCK_STREAM, proto=IPPROTO_TCP)
    server_sock.setsockopt(SOL_SOCKET, SO_REUSEADDR, 1)
    server_sock.setblocking(False)
    server_sock.bind(('', 8080))
    server_sock.listen(IPPROTO_TCP)

    listening = asyncio.Event()

    def on_listening():
        listening.set()

    client_tasks = set()

    async def client_task(sock):
        await pre_ssl_client_handler(sock)

        ssl_sock = None
        try:
            try:
                ssl_sock = ssl_context.wrap_socket(
                    sock, server_side=True, do_handshake_on_connect=False)
                await ssl_handshake(loop, ssl_sock)
                await client_handler(ssl_sock)
            finally:
                try:
                    sock = await ssl_unwrap_socket(loop, ssl_sock) if ssl_sock else sock
                finally:
                    try:
                        sock.shutdown(SHUT_RDWR)
                    finally:
                        try:
                            # Can get ResourceWarning if not have this
                            ssl_sock.close()
                        finally:
                            sock.close()
        except BaseException:
            pass

    def create_client_task(sock):
        # Client task is created immediately after socket is connected, in a
        # sync function and without a yield, so cleanup always happens, even
        # if the server task is immediately cancelled

        task = loop.create_task(client_task(sock))
        client_tasks.add(task)

        def done(_):
            client_tasks.remove(task)

        task.add_done_callback(done)

    async def server_task():
        try:
            while server_sock.fileno() != -1:
                await sock_accept(loop, server_sock, on_listening, create_client_task)
        finally:
            server_sock.close()
            for task in client_tasks:
                task.cancel()
            await asyncio.sleep(0)

    task = loop.create_task(server_task())
    await listening.wait()
    return task


async def cancel(task):
    task.cancel()
    await asyncio.sleep(0)


async def sock_accept(loop, server_sock, on_listening, create_client_task):
    fileno = server_sock.fileno()
    done = asyncio.Future()

    def accept():
        try:
            sock, _ = server_sock.accept()
        except BlockingIOError:
            pass
        else:
            sock.setblocking(False)
            create_client_task(sock)
            if not done.cancelled():
                done.set_result(None)

    accept()
    on_listening()
    loop.add_reader(fileno, accept)

    try:
        return await done
    finally:
        loop.remove_reader(fileno)


async def null_handler(_):
    pass


class Test(unittest.TestCase):

    def add_async_cleanup(self, loop, coroutine, *args):
        self.addCleanup(loop.run_until_complete, coroutine(*args))

    @async_test
    async def test_server_close_after_client_not_raises(self):
        loop = asyncio.get_running_loop()

        server_task = await server(loop, null_handler, null_handler)
        self.add_async_cleanup(loop, cancel, server_task)

        async with \
                connection_pool(loop) as pool, \
                pool.connection('localhost', '127.0.0.1', 8080,
                                SSLContext(PROTOCOL_TLSv1_2)) as connection:
            connection.sock.send(b'-' * 128)
            await asyncio.sleep(0)

    @async_test
    async def test_server_cancel_then_client_send_raises(self):
        loop = asyncio.get_running_loop()

        server_wait_forever = asyncio.Future()

        async def server_client(_):
            await server_wait_forever

        server_task = await server(loop, null_handler, server_client)
        self.add_async_cleanup(loop, cancel, server_task)

        with self.assertRaises(ConnectionError):
            async with \
                    connection_pool(loop) as pool, \
                    pool.connection('localhost', '127.0.0.1', 8080,
                                    SSLContext(PROTOCOL_TLSv1_2)) as connection:
                server_task.cancel()
                while True:
                    connection.sock.send(b'-')
                    await asyncio.sleep(0)

    @async_test
    async def test_server_cancel_then_connection_raises(self):
        loop = asyncio.get_running_loop()

        server_task = await server(loop, null_handler, null_handler)
        self.add_async_cleanup(loop, cancel, server_task)

        with self.assertRaises(ConnectionRefusedError):
            async with connection_pool(loop) as pool:
                async with pool.connection('localhost', '127.0.0.1', 8080,
                                           SSLContext(PROTOCOL_TLSv1_2)):
                    pass

                server_task.cancel()
                await asyncio.sleep(0)
                await pool.connection('localhost', '127.0.0.1', 8080,
                                      SSLContext(PROTOCOL_TLSv1_2)).__aenter__()

    @async_test
    async def test_bad_context_raises(self):
        loop = asyncio.get_running_loop()

        server_task = await server(loop, null_handler, null_handler)
        self.add_async_cleanup(loop, cancel, server_task)

        with self.assertRaises(SSLCertVerificationError):
            async with connection_pool(loop) as pool:
                async with pool.connection(
                        'localhost', '127.0.0.1', 8080, SSLContext(PROTOCOL_TLSv1_2)):
                    pass

                context = create_default_context()
                await pool.connection('localhost', '127.0.0.1', 8080, context).__aenter__()

    @async_test
    async def test_bad_ssl_handshake_raises(self):
        loop = asyncio.get_running_loop()

        async def broken_pre_ssl_handler(sock):
            sock.send(b'-')

        server_task = await server(loop, broken_pre_ssl_handler, null_handler)
        self.add_async_cleanup(loop, cancel, server_task)

        with self.assertRaises(SSLError):
            async with connection_pool(loop) as pool:
                await pool.connection('localhost', '127.0.0.1', 8080,
                                      SSLContext(PROTOCOL_TLSv1_2)).__aenter__()

    @async_test
    async def test_bad_close_raises(self):
        loop = asyncio.get_running_loop()

        closed = asyncio.Event()

        async def early_close(ssl_sock):
            ssl_sock.close()
            closed.set()

        server_task = await server(loop, null_handler, early_close)
        self.add_async_cleanup(loop, cancel, server_task)

        with self.assertRaises(OSError):
            async with \
                    connection_pool(loop) as pool, \
                    pool.connection('localhost', '127.0.0.1', 8080, SSLContext(PROTOCOL_TLSv1_2)):
                await closed.wait()
