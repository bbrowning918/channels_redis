import asyncio
import functools
import logging
import types
import uuid

import msgpack
from redis import asyncio as aioredis

from .utils import _consistent_hash

logger = logging.getLogger(__name__)


def _wrap_close(proxy, loop):
    original_impl = loop.close

    def _wrapper(self, *args, **kwargs):
        if loop in proxy._layers:
            layer = proxy._layers[loop]
            del proxy._layers[loop]
            loop.run_until_complete(layer.flush())

        self.close = original_impl
        return self.close(*args, **kwargs)

    loop.close = types.MethodType(_wrapper, loop)


async def _async_proxy(obj, name, *args, **kwargs):
    # Must be defined as a function and not a method due to
    # https://bugs.python.org/issue38364
    layer = obj._get_layer()
    return await getattr(layer, name)(*args, **kwargs)


class RedisPubSubChannelLayer:
    def __init__(self, *args, **kwargs) -> None:
        self._args = args
        self._kwargs = kwargs
        self._layers = {}

    def __getattr__(self, name):
        if name in (
            "new_channel",
            "send",
            "receive",
            "group_add",
            "group_discard",
            "group_send",
            "flush",
        ):
            return functools.partial(_async_proxy, self, name)
        else:
            return getattr(self._get_layer(), name)

    def serialize(self, message):
        """
        Serializes message to a byte string.
        """
        return msgpack.packb(message)

    def deserialize(self, message):
        """
        Deserializes from a byte string.
        """
        return msgpack.unpackb(message)

    def _get_layer(self):
        loop = asyncio.get_running_loop()

        try:
            layer = self._layers[loop]
        except KeyError:
            layer = RedisPubSubLoopLayer(
                *self._args,
                **self._kwargs,
                channel_layer=self,
            )
            self._layers[loop] = layer
            _wrap_close(self, loop)

        return layer


class RedisPubSubLoopLayer:
    """
    Channel Layer that uses Redis's pub/sub functionality.
    """

    def __init__(
        self,
        hosts=None,
        prefix="asgi",
        on_disconnect=None,
        on_reconnect=None,
        channel_layer=None,
        **kwargs,
    ):
        if hosts is None:
            hosts = ["redis://localhost:6379"]
        assert (
            isinstance(hosts, list) and len(hosts) > 0
        ), "`hosts` must be a list with at least one Redis server"

        self.prefix = prefix

        self.on_disconnect = on_disconnect
        self.on_reconnect = on_reconnect
        self.channel_layer = channel_layer

        # Each consumer gets its own *specific* channel, created with the `new_channel()` method.
        # This dict maps `channel_name` to a queue of messages for that channel.
        self.channels = {}

        # A channel can subscribe to zero or more groups.
        # This dict maps `group_name` to set of channel names who are subscribed to that group.
        self.groups = {}

        # For each host, we create a `RedisSingleShardConnection` to manage the connection to that host.
        self._shards = [RedisSingleShardConnection(host, self) for host in hosts]

    def _get_shard(self, channel_or_group_name):
        """
        Return the shard that is used exclusively for this channel or group.
        """
        return self._shards[_consistent_hash(channel_or_group_name, len(self._shards))]

    def _get_group_channel_name(self, group):
        """
        Return the channel name used by a group.
        Includes '__group__' in the returned
        string so that these names are distinguished
        from those returned by `new_channel()`.
        Technically collisions are possible, but it
        takes what I believe is intentional abuse in
        order to have colliding names.
        """
        return f"{self.prefix}__group__{group}"

    async def _subscribe_to_channel(self, channel):
        self.channels[channel] = asyncio.Queue()
        shard = self._get_shard(channel)
        await shard.subscribe(channel)

    extensions = ["groups", "flush"]

    ################################################################################
    # Channel layer API
    ################################################################################

    async def send(self, channel, message):
        """
        Send a message onto a (general or specific) channel.
        """
        shard = self._get_shard(channel)
        await shard.publish(channel, self.channel_layer.serialize(message))

    async def new_channel(self, prefix="specific."):
        """
        Returns a new channel name that can be used by a consumer in our
        process as a specific channel.
        """
        channel = f"{self.prefix}{prefix}{uuid.uuid4().hex}"
        await self._subscribe_to_channel(channel)
        return channel

    async def receive(self, channel):
        """
        Receive the first message that arrives on the channel.
        If more than one coroutine waits on the same channel, a random one
        of the waiting coroutines will get the result.
        """
        if channel not in self.channels:
            await self._subscribe_to_channel(channel)

        q = self.channels[channel]
        try:
            message = await q.get()
        except asyncio.CancelledError:
            # We assume here that the reason we are cancelled is because the consumer
            # is exiting, therefore we need to cleanup by unsubscribe below. Indeed,
            # currently the way that Django Channels works, this is a safe assumption.
            # In the future, Dajngo Channels could change to call a *new* method that
            # would serve as the antithesis of `new_channel()`; this new method might
            # be named `delete_channel()`. If that were the case, we would do the
            # following cleanup from that new `delete_channel()` method, but, since
            # that's not how Django Channels works (yet), we do the cleanup below:
            if channel in self.channels:
                del self.channels[channel]
                try:
                    shard = self._get_shard(channel)
                    await shard.unsubscribe(channel)
                except BaseException:
                    logger.exception("Unexpected exception while cleaning-up channel:")
                    # We don't re-raise here because we want the CancelledError to be the one re-raised.
            raise

        return self.channel_layer.deserialize(message)

    ################################################################################
    # Groups extension
    ################################################################################

    async def group_add(self, group, channel):
        """
        Adds the channel name to a group.
        """
        if channel not in self.channels:
            raise RuntimeError(
                "You can only call group_add() on channels that exist in-process.\n"
                "Consumers are encouraged to use the common pattern:\n"
                f"   self.channel_layer.group_add({repr(group)}, self.channel_name)"
            )
        group_channel = self._get_group_channel_name(group)
        if group_channel not in self.groups:
            self.groups[group_channel] = set()
        group_channels = self.groups[group_channel]
        if channel not in group_channels:
            group_channels.add(channel)
        shard = self._get_shard(group_channel)
        await shard.subscribe(group_channel)

    async def group_discard(self, group, channel):
        """
        Removes the channel from a group.
        """
        group_channel = self._get_group_channel_name(group)
        assert group_channel in self.groups
        group_channels = self.groups[group_channel]
        assert channel in group_channels
        group_channels.remove(channel)
        if len(group_channels) == 0:
            del self.groups[group_channel]
            shard = self._get_shard(group_channel)
            await shard.unsubscribe(group_channel)

    async def group_send(self, group, message):
        """
        Send the message to all subscribers of the group.
        """
        group_channel = self._get_group_channel_name(group)
        shard = self._get_shard(group_channel)
        await shard.publish(group_channel, self.channel_layer.serialize(message))

    ################################################################################
    # Flush extension
    ################################################################################

    async def flush(self):
        """
        Flush the layer, making it like new. It can continue to be used as if it
        was just created. This also closes connections, serving as a clean-up
        method; connections will be re-opened if you continue using this layer.
        """
        self.channels = {}
        self.groups = {}
        for shard in self._shards:
            await shard.flush()


class RedisSingleShardConnection:
    def __init__(self, host, channel_layer):
        self.host = host.copy() if type(host) is dict else {"address": host}
        self.master_name = self.host.pop("master_name", None)
        self.channel_layer = channel_layer
        self._subscribed_to = set()
        self._lock = None
        self._redis = None
        self._pub_conn = None
        self._sub_conn = None
        self._receiver = None
        self._receive_task = None
        self._keepalive_task = None

    async def publish(self, channel, message):
        conn = await self._get_pub_conn()
        await conn.publish(channel, message)

    async def subscribe(self, channel):
        if channel not in self._subscribed_to:
            self._subscribed_to.add(channel)
            await self._get_sub_conn()
            await self._receiver.subscribe(channel)

    async def unsubscribe(self, channel):
        if channel in self._subscribed_to:
            self._subscribed_to.remove(channel)
            if self._receiver is not None and self._receiver.subscribed:
                await self._receiver.unsubscribe(channel)

    async def flush(self):
        for task in [self._keepalive_task, self._receive_task]:
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._keepalive_task = None
        self._receive_task = None
        self._receiver = None
        if self._sub_conn is not None:
            await self._sub_conn.close()
            await self._put_redis_conn(self._sub_conn)
            self._sub_conn = None
        if self._pub_conn is not None:
            await self._pub_conn.close()
            await self._put_redis_conn(self._pub_conn)
            self._pub_conn = None
        self._subscribed_to = set()

    async def _get_pub_conn(self):
        """
        Return the connection to this shard that is used for *publishing* messages.

        If the connection is dead, automatically reconnect.
        """
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if self._pub_conn is not None and self._pub_conn.connection is None:
                await self._put_redis_conn(self._pub_conn)
                self._pub_conn = None
            while self._pub_conn is None:
                try:
                    self._pub_conn = await self._get_redis_conn()
                except BaseException:
                    await self._put_redis_conn(self._pub_conn)
                    logger.warning(
                        f"Failed to connect to Redis publish host: {self.host}; will try again in 1 second..."
                    )
                    await asyncio.sleep(1)
            return self._pub_conn

    async def _get_sub_conn(self):
        """
        Return the connection to this shard that is used for *subscribing* to channels.

        If the connection is dead, automatically reconnect and resubscribe to all our channels!
        """
        if self._keepalive_task is None:
            self._keepalive_task = asyncio.ensure_future(self._do_keepalive())
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if self._sub_conn is not None and self._sub_conn.connection is None:
                await self._put_redis_conn(self._sub_conn)
                self._sub_conn = None
                self._notify_consumers(self.channel_layer.on_disconnect)
            if self._sub_conn is None:
                if self._receive_task is not None:
                    self._receive_task.cancel()
                    try:
                        await self._receive_task
                    except asyncio.CancelledError:
                        # This is the normal case, that `asyncio.CancelledError` is throw. All good.
                        pass
                    except BaseException:
                        logger.exception(
                            "Unexpected exception while canceling the receiver task:"
                        )
                        # Don't re-raise here. We don't actually care why `_receive_task` didn't exit cleanly.
                    self._receive_task = None
                while self._sub_conn is None:
                    try:
                        self._sub_conn = await self._get_redis_conn()
                    except BaseException:
                        await self._put_redis_conn(self._sub_conn)
                        logger.warning(
                            f"Failed to connect to Redis subscribe host: {self.host}; will try again in 1 second..."
                        )
                        await asyncio.sleep(1)
                self._receiver = self._sub_conn.pubsub()
                if not self._receiver.subscribed:
                    if self._subscribed_to:
                        await self._receiver.subscribe(*self._subscribed_to)
                    self._notify_consumers(self.channel_layer.on_reconnect)
                self._receive_task = asyncio.ensure_future(self._do_receiving())
            return self._sub_conn

    async def _do_receiving(self):
        while True:
            if self._receiver.subscribed:
                message = await self._receiver.get_message(
                    ignore_subscribe_messages=True, timeout=1
                )
                if message is not None:
                    name = message["channel"]
                    data = message["data"]
                    if isinstance(name, bytes):
                        # Reversing what happens here:
                        #   https://github.com/aio-libs/aioredis-py/blob/8a207609b7f8a33e74c7c8130d97186e78cc0052/aioredis/util.py#L17
                        name = name.decode()
                    if name in self.channel_layer.channels:
                        self.channel_layer.channels[name].put_nowait(data)
                    elif name in self.channel_layer.groups:
                        for channel_name in self.channel_layer.groups[name]:
                            if channel_name in self.channel_layer.channels:
                                self.channel_layer.channels[channel_name].put_nowait(
                                    data
                                )
            else:
                logger.warning("_do_receiving does not have subscribed receiver")
                await asyncio.sleep(1)

    def _notify_consumers(self, mtype):
        if mtype is not None:
            for channel in self.channel_layer.channels.values():
                channel.put_nowait(
                    self.channel_layer.channel_layer.serialize({"type": mtype})
                )

    async def _ensure_redis(self):
        if self._redis is None:
            if self.master_name is None:
                pool = aioredis.ConnectionPool.from_url(self.host["address"])
            else:
                # aioredis default timeout is way too low
                pool = aioredis.sentinel.SentinelConnectionPool(
                    self.master_name,
                    aioredis.sentinel.Sentinel(
                        self.host["sentinels"], socket_timeout=2
                    ),
                )
            self._redis = aioredis.Redis(connection_pool=pool)

    async def _get_redis_conn(self):
        await self._ensure_redis()
        return self._redis

    async def _put_redis_conn(self, conn):
        if conn:
            await conn.close()

    async def _do_keepalive(self):
        """
        This task's simple job is just to call `self._get_sub_conn()` periodically.

        Why? Well, calling `self._get_sub_conn()` has the nice side-effect that if
        that connection has died (because Redis was restarted, or there was a networking
        hiccup, for example), then calling `self._get_sub_conn()` will reconnect and
        restore our old subscriptions. Thus, we want to do this on a predictable schedule.
        This is kinda a sub-optimal way to achieve this, but I can't find a way in aioredis
        to get a notification when the connection dies. I find this (sub-optimal) method
        of checking the connection state works fine for my app; if Redis restarts, we reconnect
        and resubscribe *quickly enough*; I mean, Redis restarting is already bad because it
        will cause messages to get lost, and this periodic check at least minimizes the
        damage *enough*.

        Note you wouldn't need this if you were *sure* that there would be a lot of subscribe/
        unsubscribe events on your site, because such events each call `self._get_sub_conn()`.
        Thus, on a site with heavy traffic this task may not be necessary, but also maybe it is.
        Why? Well, in a heavy traffic site you probably have more than one Django server replicas,
        so it might be the case that one of your replicas is under-utilized and this periodic
        connection check will be beneficial in the same way as it is for a low-traffic site.
        """
        while True:
            await asyncio.sleep(3)
            try:
                await self._get_sub_conn()
            except Exception:
                logger.exception("Unexpected exception in keepalive task:")
