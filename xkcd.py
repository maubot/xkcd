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
from typing import Awaitable, Optional, Type, List, Iterable, Tuple
from io import BytesIO
from difflib import SequenceMatcher
import asyncio
import random

from sqlalchemy import Column, String, Integer, Text, orm, or_
from sqlalchemy.ext.declarative import declarative_base
from attr import dataclass
import aiohttp

from mautrix.types import ContentURI, RoomID, UserID, ImageInfo, SerializableAttrs
from mautrix.util.config import BaseProxyConfig, ConfigUpdateHelper
from maubot import Plugin, MessageEvent
from maubot.handlers import command

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


class XKCDIndex:
    __tablename__ = "xkcd_index"
    id: int = Column(Integer, primary_key=True)
    title: str = Column(Text)
    alt: str = Column(Text)
    transcript: str = Column(Text)

    def __init__(self, id: int, title: str, alt: str, transcript: str) -> None:
        self.id = id
        self.title = title
        self.alt = alt
        self.transcript = transcript

    def __lt__(self, other: 'XKCDIndex') -> bool:
        return self.id > other.id

    def __gt__(self, other: 'XKCDIndex') -> bool:
        return self.id < other.id

    def __eq__(self, other: 'XKCDIndex') -> bool:
        return self.id == other.id


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
        helper.copy("allow_reindex")
        helper.copy("max_search_results")


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
        await super().start()
        self.config.load_and_update()
        db_factory = orm.sessionmaker(bind=self.database)
        db_session = orm.scoped_session(db_factory)

        base = declarative_base()
        base.metadata.bind = self.database

        class MediaCacheImpl(MediaCache, base):
            query = db_session.query_property()

        class XKCDIndexImpl(XKCDIndex, base):
            query = db_session.query_property()

        class SubscriberImpl(Subscriber, base):
            query = db_session.query_property()

        self.media_cache = MediaCacheImpl
        self.subscriber = SubscriberImpl
        self.xkcd_index = XKCDIndexImpl
        base.metadata.create_all()

        self.db = db_session
        self.latest_id = 0

        self.poll_task = asyncio.ensure_future(self.poll_xkcd(), loop=self.loop)

    async def stop(self) -> None:
        await super().stop()
        self.poll_task.cancel()

    def _index_info(self, info: XKCDInfo) -> None:
        self.db.merge(self.xkcd_index(info.num, info.title, info.alt, info.transcript))
        self.db.commit()

    async def _get_xkcd_info(self, url: str) -> Optional[XKCDInfo]:
        resp = await self.http.get(url)
        if resp.status == 200:
            data = await resp.json()
            info = XKCDInfo.deserialize(data)
            self._index_info(info)
            return info
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
            uri = await self.client.upload_media(data, mime_type=mime_type)
            cache = self.media_cache(xkcd_url=image_url, mxc_uri=uri, file_name=file_name,
                                     mime_type=mime_type, width=width, height=height,
                                     size=len(data))
            self.db.add(cache)
            self.db.commit()
            return cache

    async def send_xkcd(self, room_id: RoomID, xkcd: XKCDInfo) -> None:
        try:
            await self._send_xkcd(room_id, xkcd)
        except Exception:
            self.log.exception(f"Failed to send xkcd {xkcd.num} to {room_id}")

    async def _send_xkcd(self, room_id: RoomID, xkcd: XKCDInfo) -> None:
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

    @command.new("xkcd", help="View an xkcd comic", require_subcommand=False, arg_fallthrough=False)
    @command.argument("number", parser=lambda val: int(val) if val else None, required=False)
    async def xkcd(self, evt: MessageEvent, number: Optional[int]) -> None:
        try:
            xkcd = await (self.get_xkcd(number) if number else self.get_latest_xkcd())
        except aiohttp.ClientResponseError as e:
            await evt.respond(f"Failed to get xkcd: {e.message}")
            return
        if xkcd:
            await self.send_xkcd(evt.room_id, xkcd)
            return
        await evt.respond("xkcd not found")

    async def _try_get_xkcd(self, number: int) -> Optional[XKCDInfo]:
        try:
            return await self.get_xkcd(number)
        except aiohttp.ClientResponseError:
            self.log.exception(f"Failed to get xkcd {number}")
            return None

    @xkcd.subcommand("reindex", help="Fetch and store info about every XKCD to date for searching.")
    async def reindex(self, evt: MessageEvent) -> None:
        if not self.config["allow_reindex"]:
            await evt.reply("Sorry, the reindex command has been disabled on this instance.")
            return
        self.config["allow_reindex"] = False
        self.config.save()
        latest = await self.get_latest_xkcd()
        await evt.reply("Reindexing XKCD database...")
        results = await asyncio.gather(*[self._try_get_xkcd(i) for i in range(1, latest.num)],
                                       loop=self.loop)
        nones = len([result for result in results if result is None])
        await evt.reply(f"Reindexing complete. Fetched {len(results) - nones} xkcds and failed to "
                        f"fetch {nones} xkcds.")

    def _index_similarity(self, result: XKCDIndex, query: str) -> float:
        query = query.lower()
        title_sim = SequenceMatcher(None, result.title.strip().lower(), query).ratio()
        alt_sim = SequenceMatcher(None, result.alt.strip().lower(), query).ratio()
        transcript_sim = SequenceMatcher(None, result.transcript.strip().lower(), query).ratio()
        sim = max(title_sim, alt_sim, transcript_sim)
        return round(sim * 100, 1)

    def _sort_search_results(self, results: List[XKCDIndex], query: str
                             ) -> Iterable[Tuple[XKCDIndex, float]]:
        similarity = (self._index_similarity(result, query) for result in results)
        return ((result, similarity) for similarity, result
                in sorted(zip(similarity, results), reverse=True))

    @xkcd.subcommand("search", help="Search for a relevant XKCD")
    @command.argument("query", pass_raw=True)
    async def search(self, evt: MessageEvent, query: str) -> None:
        sql_query = f"%{query}%"
        results = self.xkcd_index.query.filter(or_(self.xkcd_index.title.like(sql_query),
                                                   self.xkcd_index.alt.like(sql_query),
                                                   self.xkcd_index.transcript.like(sql_query))
                                               ).all()
        if len(results) == 0:
            await evt.reply("No results :(")
        else:
            results = list(self._sort_search_results(results, query))
            msg = "Results:\n\n"
            more_results = None
            limit = self.config["max_search_results"]
            if len(results) > limit:
                more_results = len(results) - limit, results[limit][1]
                results = results[:limit]
            msg += "\n".join(f"* [{result.id}](https://xkcd.com/{result.id}): "
                             f"{result.title} ({similarity} % match)"
                             for result, similarity in results)
            if more_results:
                number, similarity = more_results
                msg += (f"\n\nThere were {number} other results "
                        f"with a similarity lower than {similarity + 0.1} %")
            await evt.reply(msg)

    @xkcd.subcommand("subscribe", help="Subscribe to xkcd updates")
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

    @xkcd.subcommand("unsubscribe", help="Unsubscribe from xkcd updates")
    async def unsubscribe(self, evt: MessageEvent) -> None:
        sub = self.subscriber.query.get(evt.room_id)
        if sub is None:
            await evt.reply("This room is not subscribed to xkcd updates.")
            return
        self.db.delete(sub)
        self.db.commit()
        await evt.reply("Unsubscribed from xkcd updates successfully :(")
