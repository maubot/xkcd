# xkcd - A maubot plugin to view xkcd comics
# Copyright (C) 2018 Tulir Asokan
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
from typing import Awaitable, Optional, Type, Tuple
from io import BytesIO
import asyncio
import random

from sqlalchemy import Column, String, Integer, orm
from sqlalchemy.ext.declarative import declarative_base
from attr import dataclass
import aiohttp

from mautrix.types import ContentURI, RoomID, UserID, ImageInfo, SerializableAttrs
from mautrix.util.config import BaseProxyConfig, ConfigUpdateHelper
from maubot import Plugin, CommandSpec, Command, Argument, MessageEvent

try:
    import magic
except ImportError:
    magic = None

try:
    from PIL import Image
except ImportError:
    Image = None

ARG_NUMBER = "$number"
COMMAND_XKCD = f"xkcd {ARG_NUMBER}"
COMMAND_LATEST_XKCD = "xkcd"
COMMAND_SUBSCRIBE_XKCD = "xkcd subscribe"
COMMAND_UNSUBSCRIBE_XKCD = "xkcd unsubscribe"


@dataclass
class XKCDInfo(SerializableAttrs['XKCDInfo']):
    year: str
    month: str
    day: str

    num: int

    title: str
    safe_title: str
    alt: str
    img: str

    transcript: str

    news: str
    link: str


class MediaCache:
    __tablename__ = "media_cache"
    query: orm.Query = None

    xkcd_url: str = Column(String(255), primary_key=True)
    mxc_uri: ContentURI = Column(String(255))
    file_name: str = Column(String(255))
    mime_type: str = Column(String(255))
    width: int = Column(Integer)
    height: int = Column(Integer)
    size: int = Column(Integer)

    def __init__(self, xkcd_url: str, mxc_uri: ContentURI, file_name: str, mime_type: str,
                 width: int, height: int, size: int) -> None:
        self.xkcd_url = xkcd_url
        self.mxc_uri = mxc_uri
        self.file_name = file_name
        self.mime_type = mime_type
        self.width = width
        self.height = height
        self.size = size


class Subscriber:
    __tablename__ = "subscriber"
    query: orm.Query = None

    room_id: RoomID = Column(String(255), primary_key=True)
    requested_by: UserID = Column(String(255))

    def __init__(self, room_id: RoomID, requested_by: UserID) -> None:
        self.room_id = room_id
        self.requested_by = requested_by


class Config(BaseProxyConfig):
    def do_update(self, helper: ConfigUpdateHelper) -> None:
        helper.copy("inline")
        helper.copy("poll_interval")
        helper.copy("spam_sleep")


class XKCDBot(Plugin):
    media_cache: Type[MediaCache]
    subscriber: Type[Subscriber]
    db: orm.Session
    latest_id: int
    poll_task: asyncio.Future

    @classmethod
    def get_config_class(cls) -> Type[BaseProxyConfig]:
        return Config

    async def start(self) -> None:
        self.config.load_and_update()
        db_engine = self.request_db_engine()
        db_factory = orm.sessionmaker(bind=db_engine)
        db_session = orm.scoped_session(db_factory)

        base = declarative_base()
        base.metadata.bind = db_engine

        class MediaCacheImpl(MediaCache, base):
            query = db_session.query_property()

        class SubscriberImpl(Subscriber, base):
            query = db_session.query_property()

        self.media_cache = MediaCacheImpl
        self.subscriber = SubscriberImpl
        base.metadata.create_all()

        self.db = db_session
        self.latest_id = 0

        self.set_command_spec(CommandSpec(
            commands=[
                Command(
                    syntax=COMMAND_XKCD,
                    description="Get a specific xkcd comic",
                    arguments={ARG_NUMBER: Argument(matches="[0-9]+", required=True,
                                                    description="The number of the comic to get")}
                ),
                Command(syntax=COMMAND_LATEST_XKCD, description="Get the latest xkcd comic"),
                Command(syntax=COMMAND_SUBSCRIBE_XKCD, description="Subscribe to xkcd updates"),
                Command(syntax=COMMAND_UNSUBSCRIBE_XKCD,
                        description="Unsubscribe from xkcd updates"),
            ],
        ))
        self.client.add_command_handler(COMMAND_XKCD, self.get)
        self.client.add_command_handler(COMMAND_LATEST_XKCD, self.latest)
        self.client.add_command_handler(COMMAND_SUBSCRIBE_XKCD, self.subscribe)
        self.client.add_command_handler(COMMAND_UNSUBSCRIBE_XKCD, self.unsubscribe)

        self.poll_task = asyncio.ensure_future(self.poll_xkcd(), loop=self.loop)

    async def stop(self) -> None:
        self.client.remove_command_handler(COMMAND_XKCD, self.get)
        self.client.remove_command_handler(COMMAND_LATEST_XKCD, self.latest)
        self.client.remove_command_handler(COMMAND_SUBSCRIBE_XKCD, self.subscribe)
        self.client.remove_command_handler(COMMAND_UNSUBSCRIBE_XKCD, self.unsubscribe)
        self.poll_task.cancel()

    async def _get_xkcd_info(self, url: str) -> Optional[XKCDInfo]:
        resp = await self.http.get(url)
        if resp.status == 200:
            data = await resp.json()
            return XKCDInfo.deserialize(data)
        resp.raise_for_status()
        return None

    def get_latest_xkcd(self) -> Awaitable[XKCDInfo]:
        return self._get_xkcd_info("https://xkcd.com/info.0.json")

    def get_xkcd(self, num: int) -> Awaitable[XKCDInfo]:
        return self._get_xkcd_info(f"https://xkcd.com/{num}/info.0.json")

    async def _get_media_info(self, image_url: str) -> MediaCache:
        cache = self.media_cache.query.get(image_url)
        if cache is not None:
            return cache
        resp = await self.http.get(image_url)
        if resp.status == 200:
            data = await resp.read()
            file_name = image_url.split("/")[-1]
            width = height = mime_type = None
            if magic is not None:
                mime_type = magic.from_buffer(data, mime=True)
            if Image is not None:
                image = Image.open(BytesIO(data))
                width, height = image.size
            uri = await self.client.upload_media(data)
            cache = self.media_cache(xkcd_url=image_url, mxc_uri=uri, file_name=file_name,
                                     mime_type=mime_type, width=width, height=height,
                                     size=len(data))
            self.db.add(cache)
            self.db.commit()
            return cache

    async def send_xkcd(self, room_id: RoomID, xkcd: XKCDInfo) -> None:
        info = await self._get_media_info(xkcd.img)
        if self.config["inline"]:
            await self.client.send_text(room_id, text=(f"{xkcd.num}: **{xkcd.title}\n"
                                                       f"{xkcd.img}\n{xkcd.alt}"),
                                        html=(f"{xkcd.num}: <strong>{xkcd.safe_title}</strong><br/>"
                                              f"<img src='{info.mxc_uri}' title='{xkcd.alt}'/>"))
        else:
            await self.client.send_text(room_id, text=f"{xkcd.num}: **{xkcd.title}**",
                                        html=f"{xkcd.num}: <strong>{xkcd.safe_title}</strong>")
            await self.client.send_image(room_id, url=info.mxc_uri, file_name=info.file_name,
                                         info=ImageInfo(
                                             mimetype=info.mime_type,
                                             size=info.size,
                                             width=info.width,
                                             height=info.height,
                                         ))
            await self.client.send_text(room_id, text=xkcd.alt)

    async def broadcast(self, xkcd: XKCDInfo) -> None:
        self.log.debug(f"Broadcasting xkcd {xkcd.num}")
        subscribers = list(self.subscriber.query.all())
        random.shuffle(subscribers)
        spam_sleep = self.config["spam_sleep"]
        if spam_sleep < 0:
            await asyncio.gather(*[self.send_xkcd(sub.room_id, xkcd)
                                   for sub in subscribers],
                                 loop=self.loop)
        else:
            for sub in subscribers:
                await self.send_xkcd(sub.room_id, xkcd)
                if spam_sleep > 0:
                    await asyncio.sleep(spam_sleep)

    async def poll_xkcd(self) -> None:
        try:
            await self._poll_xkcd()
        except asyncio.CancelledError:
            self.log.debug("Polling stopped")
            pass
        except Exception:
            self.log.exception("Failed to poll xkcd")

    async def _poll_xkcd(self) -> None:
        self.log.debug("Polling started")
        latest = await self.get_latest_xkcd()
        self.latest_id = latest.num
        while True:
            latest = await self.get_latest_xkcd()
            if latest.num > self.latest_id:
                self.latest_id = latest.num
                await self.broadcast(latest)
            await asyncio.sleep(self.config["poll_interval"], loop=self.loop)

    async def subscribe(self, evt: MessageEvent) -> None:
        sub = self.subscriber.query.get(evt.room_id)
        if sub is not None:
            await evt.reply("This room has already been subscribed to "
                            f"xkcd updates by {sub.requested_by}")
            return
        sub = self.subscriber(evt.room_id, evt.sender)
        self.db.add(sub)
        self.db.commit()
        await evt.reply("Subscribed to xkcd updates successfully!")

    async def unsubscribe(self, evt: MessageEvent) -> None:
        sub = self.subscriber.query.get(evt.room_id)
        if sub is None:
            await evt.reply("This room is not subscribed to xkcd updates.")
            return
        self.db.delete(sub)
        self.db.commit()
        await evt.reply("Unsubscribed from xkcd updates successfully :(")

    async def latest(self, evt: MessageEvent) -> None:
        try:
            xkcd = await self.get_latest_xkcd()
        except aiohttp.ClientResponseError as e:
            await evt.respond(f"Failed to get xkcd: {e.message}")
            return
        await self.send_xkcd(evt.room_id, xkcd)

    async def get(self, evt: MessageEvent) -> None:
        number = int(evt.content.command.arguments[ARG_NUMBER].lower())
        try:
            xkcd = await self.get_xkcd(number)
        except aiohttp.ClientResponseError as e:
            await evt.respond(f"Failed to get xkcd: {e.message}")
            return
        if xkcd:
            await self.send_xkcd(evt.room_id, xkcd)
            return
        await evt.respond("xkcd not found")
