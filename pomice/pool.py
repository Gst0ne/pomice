from __future__ import annotations

import asyncio
import json
import random
import re
import socket
from typing import Dict, Optional, TYPE_CHECKING, Tuple
from urllib.parse import quote

import aiohttp
from discord.ext import commands


from . import (
    __version__, 
    spotify,
)

from aiohttp import WSMsgType

from .enums import SearchType
from .exceptions import (
    InvalidSpotifyClientAuthorization,
    NodeConnectionFailure,
    NodeCreationError,
    NodeNotAvailable,
    NoNodesAvailable,
    TrackLoadError
)
from .objects import Playlist, Track
from .utils import ClientType, NodeStats, Ping
from .backoff import Backoff

if TYPE_CHECKING:
    from .player import Player


SPOTIFY_URL_REGEX = re.compile(
    r"https?://open.spotify.com/(?P<type>album|artist|playlist|track)/(?P<id>[a-zA-Z0-9]+)"
)

DISCORD_MP3_URL_REGEX = re.compile(
    r"https?://cdn.discordapp.com/attachments/(?P<channel_id>[0-9]+)/"
    r"(?P<message_id>[0-9]+)/(?P<file>[a-zA-Z0-9_.]+)+"
)

URL_REGEX = re.compile(
    r"https?://(?:www\.)?.+"
)


class Node:
    """The base class for a node. 
       This node object represents a Lavalink node. 
       To enable Spotify searching, pass in a proper Spotify Client ID and Spotify Client Secret
    """

    def __init__(
        self,
        *,
        pool,
        bot: ClientType,
        host: str,
        port: int,
        password: str,
        identifier: str,
        secure: bool = False,
        heartbeat: Optional[int],
        resume_key: Optional[str],
        session: Optional[aiohttp.ClientSession],
        spotify_client_id: Optional[str],
        spotify_client_secret: Optional[str],

    ):
        self._bot = bot
        self._host = host
        self._port = port
        self._pool = pool
        self._password = password
        self._identifier = identifier
        self._secure = secure
        self._heartbeat = heartbeat
        self._resume_key = resume_key
        
        self._resuming_configured: bool = False

        self._websocket_uri: str = f"{'wss' if self._secure else 'ws'}://{self._host}:{self._port}"
        self._rest_uri: str = f"{'https' if self._secure else 'http'}://{self._host}:{self._port}"

        self._session: aiohttp.ClientSession = session or aiohttp.ClientSession()
        self._websocket: aiohttp.ClientWebSocketResponse = None
        self._task: asyncio.Task = None
        self._closed_msgtypes: Tuple[WSMsgType] = (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.CLOSING)

        self._connection_id = None
        self._metadata = None
        self._available = None

        self._headers = {
            "Authorization": self._password,
            "User-Id": str(self._bot.user.id),
            "Client-Name": f"Pomice/{__version__}"            
        }
        if self._resume_key:
            self._headers["Resume-Key"] = self._resume_key

        self._players: Dict[int, Player] = {}

        self._spotify_client_id = spotify_client_id
        self._spotify_client_secret = spotify_client_secret

        if self._spotify_client_id and self._spotify_client_secret:
            self._spotify_client = spotify.Client(
                self._spotify_client_id, self._spotify_client_secret
            )

        self._bot.add_listener(self._update_handler, "on_socket_response")

    def __repr__(self):
        return (
            f"<Pomice.node ws_uri={self._websocket_uri} rest_uri={self._rest_uri} "
            f"player_count={len(self._players)}>"
        )

    @property
    def identifier(self) -> str:
        """Returns the node's identifier."""
        return self._identifier

    @property
    def is_connected(self) -> bool:
        """"Property which returns whether this node is connected or not"""
        return self._websocket is not None and not self._websocket.closed


    @property
    def stats(self) -> NodeStats:
        """Property which returns the node stats."""
        return self._stats

    @property
    def players(self) -> Dict[int, Player]:
        """Property which returns a dict containing the guild ID and the player object."""
        return self._players

    @property
    def bot(self) -> ClientType:
        """Property which returns the discord.py client linked to this node"""
        return self._bot

    @property
    def player_count(self) -> int:
        """Property which returns how many players are connected to this node"""
        return len(self.players)

    @property
    def pool(self) -> NodePool:
        """Property which returns the pool this node is apart of"""
        return self._pool
    
    @property
    def latency(self) -> float:
        """Property which returns the latency of the node"""
        return Ping(self._host, port=self._port).get_ping()

    @property
    def resume_key(self) -> str:
        """Returns the resume key if there is one else None"""
        return self._resume_key

    @property
    def ws(self) -> aiohttp.ClientWebSocketResponse:
        """Returns the node's websocket"""
        return self._websocket

    @property
    def session_resumed(self) -> bool:
        """Returns True if the session was resumed by lavalink else False (This requires a resume key)"""
        if not self._resume_key:
            return False
        return True if self._websocket._response.headers.get("Session-Resumed") == "true" else False

    async def _update_handler(self, data: dict):
        await self._bot.wait_until_ready()

        if not data:
            return

        if data["t"] == "VOICE_SERVER_UPDATE":
            guild_id = int(data["d"]["guild_id"])
            try:
                player = self._players[guild_id]
                await player.on_voice_server_update(data["d"])
            except KeyError:
                return

        elif data["t"] == "VOICE_STATE_UPDATE":
            if int(data["d"]["user_id"]) != self._bot.user.id:
                return

            guild_id = int(data["d"]["guild_id"])
            try:
                player = self._players[guild_id]
                await player.on_voice_state_update(data["d"])
            except KeyError:
                return

    async def _listen(self):
        backoff = Backoff(base=1, maximum_time=60, maximum_tries=None)

        while True:
            msg = await self._websocket.receive()
            if msg.type in self._closed_msgtypes:
                retry = backoff.calculate()
                await asyncio.sleep(retry)

                if not self.is_connected:
                    print("WebSocket Closed, Attempting Reconnection...")
                    await self.connect()

            else:
                self._bot.loop.create_task(self._handle_payload(msg.json()))


    async def _handle_payload(self, data: dict):
        op = data.get("op", None)
        if not op:
            return

        if op == "stats":
            self._stats = NodeStats(data)
            return

        if not (player := self._players.get(int(data["guildId"]))):
            return

        if op == "event":
            await player._dispatch_event(data)
        elif op == "playerUpdate":
            await player._update_state(data)

    async def send(self, **data):
        if not self._available:
            raise NodeNotAvailable(
                f"The node '{self.identifier}' is unavailable."
            )

        await self._websocket.send_str(json.dumps(data))

    def get_player(self, guild_id: int):
        """Takes a guild ID as a parameter. Returns a pomice Player object."""
        return self._players.get(guild_id, None)

    async def connect(self):
        """Initiates a connection with a Lavalink node and adds it to the node pool."""
        await self._bot.wait_until_ready()

        try:
            self._websocket = await self._session.ws_connect(
                self._websocket_uri, headers=self._headers, heartbeat=self._heartbeat
            )
            if self._task is None:
                self._task = self._bot.loop.create_task(self._listen())
                self._bot.dispatch("pomice_node_ready", self)
            self._available = True

            if self._resume_key:
                await self.send(op='configureResuming', key=self._resume_key, timeout=60)
                self._resuming_configured = True

            return self

        except aiohttp.WSServerHandshakeError:
            raise NodeConnectionFailure(
                f"The password for node '{self._identifier}' is invalid."
            )
        except aiohttp.InvalidURL:
            raise NodeConnectionFailure(
                f"The URI for node '{self._identifier}' is invalid."
            )
        except socket.gaierror:
            raise NodeConnectionFailure(
                f"The node '{self._identifier}' failed to connect."
            )

    async def disconnect(self):
        """Disconnects a connected Lavalink node and removes it from the node pool.
           This also destroys any players connected to the node.
        """
        for player in self.players.copy().values():
            await player.destroy()

        await self._websocket.close()
        del self._pool.nodes[self._identifier]
        self._available = False
        self._task.cancel()

    async def build_track(
        self,
        identifier: str,
        ctx: Optional[commands.Context] = None
    ) -> Track:
        """
        Builds a track using a valid track identifier

        You can also pass in a discord.py Context object to get a
        Context object on the track it builds.
        """

        async with self._session.get(
            f"{self._rest_uri}/decodetrack?",
            headers={"Authorization": self._password},
            params={"track": identifier}
        ) as resp:
            if not resp.status == 200:
                raise TrackLoadError(
                    f"Failed to build track. Check if the identifier is correct and try again."
                )

            data: dict = await resp.json()
            return Track(track_id=identifier, ctx=ctx, info=data)

    async def get_tracks(
        self,
        query: str,
        *,
        ctx: Optional[commands.Context] = None,
        search_type: SearchType = SearchType.ytsearch,
        local: bool = False
    ):
        """Fetches tracks from the node's REST api to parse into Lavalink.

           If you passed in Spotify API credentials, you can also pass in a
           Spotify URL of a playlist, album or track and it will be parsed accordingly.

           You can also pass in a discord.py Context object to get a
           Context object on any track you search.
        """

        if not URL_REGEX.search(query) and not local:
            query = f"{search_type}:{query}"

        if SPOTIFY_URL_REGEX.match(query):
            if not self._spotify_client_id and not self._spotify_client_secret:
                raise InvalidSpotifyClientAuthorization(
                    "You did not provide proper Spotify client authorization credentials. "
                    "If you would like to use the Spotify searching feature, "
                    "please obtain Spotify API credentials here: https://developer.spotify.com/"
                )

            spotify_results = await self._spotify_client.search(query=query)

            if isinstance(spotify_results, spotify.Track):
                return [Track(
                        track_id=spotify_results.id,
                        ctx=ctx,
                        spotify=True,
                        info={
                            "identifier": spotify_results.id,
                            "isSeekable": True,
                            "author": spotify_results.artists,
                            "length": spotify_results.length,
                            "isStream": False,
                            "position": 0,
                            "sourceName": None,
                            "title": spotify_results.name,
                            "uri": spotify_results.uri, 
                            "thumbnail": spotify_results.image
                        }
                    )
               ]

            tracks = [
                Track(
                    track_id=track.id,
                    ctx=ctx,
                    spotify=True,
                    info={
                        "identifier": track.id,
                        "isSeekable": True,
                        "author": track.artists,
                        "length": track.length,
                        "isStream": False,
                        "position": 0,
                        "sourceName": None,
                        "title": track.name,
                        "uri": track.uri, 
                        "thumbnail": track.image
                    }
                ) for track in spotify_results.tracks
            ]

            return Playlist(
                playlist_info={"name": spotify_results.name, "selectedTrack": 0},
                tracks=tracks,
                ctx=ctx,
                spotify=True,
                thumbnail=spotify_results.image,
                uri=spotify_results.uri
            )

        elif discord_url := DISCORD_MP3_URL_REGEX.match(query):
            async with self._session.get(
                url=f"{self._rest_uri}/loadtracks?identifier={quote(query)}",
                headers={"Authorization": self._password}
            ) as response:
                data: dict = await response.json()

            track: dict = data["tracks"][0]
            info: dict = track.get("info")

            return [Track(
                    track_id=track["track"],
                    ctx=ctx,
                    info={
                        "identifier": info.get("identifier"),
                        "isSeekable": True,
                        "author": "Unknown",
                        "length": info.get("length"),
                        "isStream": False,
                        "position": 0,
                        "sourceName": "http",
                        "title": discord_url.group("file"),
                        "uri": info.get("uri"),
                    }
                )
            ]

        else:
            async with self._session.get(
                url=f"{self._rest_uri}/loadtracks?identifier={quote(query)}",
                headers={"Authorization": self._password}
            ) as response:
                data = await response.json()

        load_type = data.get("loadType")

        if not load_type:
            raise TrackLoadError("There was an error while trying to load this track. [NO_LOADTYPE]")

        elif load_type == "NO_MATCHES":
            raise TrackLoadError("No matches found. [NO_MATCHES]")

        elif load_type == "LOAD_FAILED":
            exception = data["exception"]
            raise TrackLoadError(f"{exception['message']} [{exception['severity']}]")

        elif load_type == "PLAYLIST_LOADED":
            return Playlist(
                playlist_info=data["playlistInfo"],
                tracks=data["tracks"],
                ctx=ctx,
                search_type=search_type
            )

        elif load_type == "SEARCH_RESULT" or load_type == "TRACK_LOADED":
            return (result := [
                Track(
                    track_id=track["track"],
                    info=track["info"],
                    ctx=ctx,
                    search_type=search_type
                )
                for track in data["tracks"]
            ]
        )

class NodePool:
    """The base class for the node pool.
       This holds all the nodes that are to be used by the bot.
    """

    _nodes = {}

    def __repr__(self):
        return f"<Pomice.NodePool node_count={self.node_count}>"

    @property
    def nodes(self) -> Dict[str, Node]:
        """Property which returns a dict with the node identifier and the Node object."""
        return self._nodes

    @property
    def node_count(self):
        return len(self._nodes.values())

    @classmethod
    def get_node(cls, *, identifier: str = None) -> Node:
        """Fetches a node from the node pool using it's identifier.
           If no identifier is provided, it will choose the best node.
        """
        available_nodes = {
            identifier: node
            for identifier, node in cls._nodes.items() if node._available
        }

        if not available_nodes:
            raise NoNodesAvailable("There are no nodes available.")

        if identifier is None:
            return sorted(list(available_nodes.values()), key=lambda n: len(n.players))[0]

        return available_nodes.get(identifier, None)

    @classmethod
    async def create_node(
        cls,
        *,
        bot: ClientType,
        host: str,
        port: str,
        password: str,
        identifier: str,
        secure: bool = False,
        heartbeat: Optional[int] = 60,
        resume_key: Optional[str] = None,
        session: Optional[aiohttp.ClientSession] = None,
        spotify_client_id: Optional[str] = None,
        spotify_client_secret: Optional[str] = None,

    ) -> Node:
        """Creates a Node object to be then added into the node pool.
           For Spotify searching capabilites, pass in valid Spotify API credentials.
        """
        if identifier in cls._nodes.keys():
            raise NodeCreationError(f"A node with identifier '{identifier}' already exists.")

        node = Node(
            pool=cls, bot=bot, host=host, port=port, password=password, identifier=identifier,
            secure=secure, heartbeat=heartbeat, resume_key=resume_key, session=session,
            spotify_client_id=spotify_client_id, spotify_client_secret=spotify_client_secret
        )

        await node.connect()
        cls._nodes[node._identifier] = node
        return node
