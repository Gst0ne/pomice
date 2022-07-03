from typing import List, Dict, Any
from .track import Track

class TopTracks:
    """The base class for Spotify Popular Tracks"""

    def __init__(self, data: dict, tracks: List[Dict[str, Any]]) -> None:
        self.data = data
        self.name = f"Top Tracks by {data['name']}"
        self.artists = data["name"]
        self.tracks = list(map(Track, tracks))
        self.total_tracks = len(tracks)
        self.id = data["id"]
#         self.image = data["images"][0]["url"]
#         self.uri = data["external_urls"]["spotify"]

    def __repr__(self) -> str:
        return (
            f"<Pomice.spotify.TopTracks name={self.name} artist={self.artists} id={self.id} "
            f"total_tracks={self.total_tracks} tracks={self.tracks}>"
        )
