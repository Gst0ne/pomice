import time
import inspect
from typing import (
    Any,
    List,
    Dict,
    Union,
    Optional
)

from discord import (
    Guild,
    VoiceProtocol
)
from discord.ext import commands

from . import events
from .enums import SearchType
from .events import PomiceEvent, TrackEndEvent, TrackStartEvent
from .exceptions import TrackInvalidPosition
from .filters import Filter, Timescale, Equalizer
from .objects import Track, Playlist
from .pool import Node, NodePool
from .utils import ClientType, GuildVoiceChannel


class Player(VoiceProtocol):
    """The base player class for Pomice.
       In order to initiate a player, you must pass it in as a cls when you connect to a channel.
       i.e: ```py
       await ctx.author.voice.channel.connect(cls=pomice.Player)
       ```
    """

    def __call__(self, client: ClientType, channel: GuildVoiceChannel):
        self.client: ClientType = client
        self.channel: GuildVoiceChannel = channel
        self._bot: ClientType = client
        self._guild: Guild = channel.guild

        return self

    def __init__(
        self, 
        client: ClientType = None, 
        channel: GuildVoiceChannel = None, 
        *, 
        node: Node = None
    ):
        self.client = client
        self._bot = client
        self.channel = channel
        self._guild: Guild = channel.guild if channel else None

        self._node = node or NodePool.get_node()
        self._current: Track = None
        self._volume = 100
        self._paused = False
        self._is_connected = False

        self._filters: List[Filter] = []
        self._filter_payload: Dict[str, Any] = {}

        self._position = 0
        self._last_position = 0
        self._last_update = 0
        self._ending_track: Optional[Track] = None

        self._voice_state = {}

    def __repr__(self):
        return (
            f"<Pomice.player bot={self.bot} guildId={self.guild.id} "
            f"is_connected={self.is_connected} is_playing={self.is_playing}>"
        )

    @property
    def position(self) -> float:
        """Property which returns the player's position in a track in milliseconds"""
        if not self.is_playing:
            return 0

        current = self._current.original

        if self.is_paused:
            return min(self._last_position, current.length)

        difference = (time.time() * 1000) - self._last_update
        
        if (timescale := next((f for f in self._filters if isinstance(f, Timescale)), None)):
            difference = difference * timescale.speed * timescale.rate

        position = self._last_position + difference

        return min(position, current.length)

    @property
    def is_playing(self) -> bool:
        """Property which returns whether or not the player is actively playing a track."""
        return self._is_connected and self.current is not None

    @property
    def is_connected(self) -> bool:
        """Property which returns whether or not the player is connected"""
        return self._is_connected

    @property
    def is_paused(self) -> bool:
        """Property which returns whether or not the player has a track which is paused or not."""
        return self._is_connected and self._paused

    @property
    def current(self) -> Track:
        """Property which returns the currently playing track"""
        return self._current

    @property
    def node(self) -> Node:
        """Property which returns the node the player is connected to"""
        return self._node

    @property
    def guild(self) -> Guild:
        """Property which returns the guild associated with the player"""
        return self._guild

    @property
    def volume(self) -> int:
        """Property which returns the players current volume"""
        return self._volume

    @property
    def filters(self) -> List[Filter]:
        """Property which returns the currently applied filter, if one is applied"""
        return self._filters

    @property
    def bot(self) -> ClientType:
        """Property which returns the bot associated with this player instance"""
        return self._bot

    @property
    def is_dead(self) -> bool:
        """Returns a bool representing whether the player is dead or not.
           A player is considered dead if it has been destroyed and removed from stored players.
        """
        return self._guild.id not in self._node._players

    async def _set_filter(self, position: Optional[float]=None):
        await self._node.send(op="filters", guildId=str(self.guild.id), **self._filter_payload)
        if position:
            await self._node.send(op="seek", guildId=str(self.guild.id), position=position)

    async def _update_state(self, data: dict):
        state: dict = data.get("state")
        self._last_update = time.time() * 1000
        self._is_connected = state.get("connected")
        self._last_position = state.get("position")

    async def _dispatch_voice_update(self, voice_data: Dict[str, Any]):
        if {"sessionId", "event"} == self._voice_state.keys():
            await self._node.send(
            op="voiceUpdate",
            guildId=str(self.guild.id),
            **voice_data
            )

    async def on_voice_server_update(self, data: dict):
        self._voice_state.update({"event": data})
        await self._dispatch_voice_update(self._voice_state)

    async def on_voice_state_update(self, data: dict):
        if not (channel_id := data.get("channel_id")):
            await self.disconnect()
            self._voice_state.clear()
            return

        self._voice_state.update({"sessionId": data.get("session_id")})
        self.channel = self.guild.get_channel(int(channel_id))

    async def _dispatch_event(self, data: dict):
        event_type = data.get("type")
        event: PomiceEvent = getattr(events, event_type)(data, self)

        if isinstance(event, TrackEndEvent) and event.reason != "REPLACED":
            self._current = None

        event.dispatch(self._bot)

        if isinstance(event, TrackStartEvent):
            self._ending_track = self._current

    async def get_tracks(
        self,
        query: str,
        *,
        ctx: Optional[commands.Context] = None,
        search_type: SearchType = SearchType.ytsearch,
        local: bool = False
    ) -> Union[Track, Playlist]:
        """Fetches tracks from the node's REST api to parse into Lavalink.

        If you passed in Spotify API credentials when you created the node,
        you can also pass in a Spotify URL of a playlist, album or track and it will be parsed
        accordingly.

        You can also pass in a discord.py Context object to get a
        Context object on any track you search.
        """
        return await self._node.get_tracks(query, ctx=ctx, search_type=search_type, local=local)

    async def connect(self, *, timeout: float, reconnect: bool, self_mute=False, self_deaf=False) -> None:
        """Makes the bot join a voice channel"""
        await self.guild.change_voice_state(channel=self.channel, self_mute=self_mute, self_deaf=self_deaf)
        self._node._players[self.guild.id] = self
        self._is_connected = True

    async def move_to(self, channel: GuildVoiceChannel, *, self_mute=False, self_deaf=False) -> None:
        """Moves the player to another channel"""
        await self.guild.change_voice_state(channel=channel, self_mute=self_mute, self_deaf=self_deaf)

    async def stop(self) -> None:
        """Stops the currently playing track."""
        self._current = None
        await self._node.send(op="stop", guildId=str(self.guild.id))

    async def disconnect(self, *, force: bool = False) -> None:
        """Disconnects the player from voice."""
        try:
            await self.guild.change_voice_state(channel=None)
        finally:
            self.cleanup()
            self._is_connected = False
            self.channel = None

    async def destroy(self) -> None:
        """Disconnects and destroys the player, and runs internal cleanup."""
        try:
            await self.disconnect()
        except AttributeError:
            # 'NoneType' has no attribute '_get_voice_client_key' raised by self.cleanup() ->
            # assume we're already disconnected and cleaned up
            assert self.channel is None and not self.is_connected

        self._node._players.pop(self._guild.id)
        await self._node.send(op="destroy", guildId=str(self._guild.id))

    async def play(
        self,
        track: Track,
        *,
        start: int = 0,
        end: int = 0,
        ignore_if_playing: bool = False
    ) -> Track:
        """Plays a track. If a Spotify track is passed in, it will be handled accordingly."""
        if track.spotify: 
            try:
                search = await self._node.get_tracks(track.isrc, ctx=track.ctx, search_type=SearchType.ytmsearch)
            except Exception as e:
                print(e)
                search = await self._node.get_tracks(f"{track.title} - {track.author}", ctx=track.ctx, search_type=SearchType.ytsearch)
            search: Track = search[0]
            track.original = search

            data = {
                "op": "play",
                "guildId": str(self.guild.id),
                "track": search.track_id,
                "startTime": str(start),
                "noReplace": ignore_if_playing
            }
        else:
            data = {
                "op": "play",
                "guildId": str(self.guild.id),
                "track": track.track_id,
                "startTime": str(start),
                "noReplace": ignore_if_playing
            }

        if end > 0:
            data["endTime"] = str(end)

        await self._node.send(**data)

        self._current = track
        return self._current

    async def seek(self, position: float) -> float:
        """Seeks to a position in the currently playing track milliseconds"""
        if position is None or position < 0 or position > self._current.original.length:
            raise TrackInvalidPosition(f"Seek position must be between 0 and the track length")

        await self._node.send(op="seek", guildId=str(self.guild.id), position=position)
        return self._position

    async def set_pause(self, pause: bool) -> bool:
        """Sets the pause state of the currently playing track."""
        await self._node.send(op="pause", guildId=str(self.guild.id), pause=pause)
        self._paused = pause
        return self._paused

    async def set_volume(self, volume: int) -> int:
        """Sets the volume of the player as an integer. Lavalink accepts values from 0 to 500."""
        await self._node.send(op="volume", guildId=str(self.guild.id), volume=volume)
        self._volume = volume
        return self._volume

    async def add_filter(self, filter_: Filter, /) -> List[Filter]:
        """Adds a filter to the player. Takes a pomice.Filter object."""
        position = self.position if self._current else None
        for f in self.filters:
            if type(f) == type(filter_):
                self._filters.remove(f)
        self._filters.append(filter_)
        self._filter_payload.update(filter_.payload)
        await self._set_filter(position)
        return self._filters

    async def remove_filter(self, filter_: Filter, /) -> Optional[Filter]:
        """Removes an existing filter from the player"""
        if isinstance(filter_, type) or inspect.ismethod(filter_):
            if Equalizer in (filter_, getattr(filter_, "__self__", None)):
                filter_ = Equalizer.flat()
            else:
                filter_ = filter_()
        position = self.position if self._current else None
        for i, f in enumerate(self._filters):
            if isinstance(f, filter_.__class__):
                filter_ = self._filters.pop(i)
        if self._filter_payload.pop(next(iter(filter_.payload.keys())), None):
            await self._set_filter(position)
            return filter_

    async def reset_filter(self) -> None:
        """Resets the current filter of the player."""
        position = self.position if self._current else None
        self._filters = []
        self._filter_payload = {}
        await self._set_filter(position)
