from .track import Track


class Artist:
    """The base class for a Spotify artist"""

    def __init__(self, data: dict, artist: dict) -> None:
        self.name = f"Top Tracks by {artist['name']}"
        self.artists = artist["name"]
        self.tracks = [Track(track) for track in data["tracks"]]
        self.total_tracks = len(data["tracks"]) if data["tracks"] and len(data["tracks"]) else 0
        self.id = artist["id"]
        if artist.get("images") and len(artist["images"]):
            self.image = artist["images"][-1]["url"]
        else:
            self.image = None
        self.uri = artist["external_urls"]["spotify"]

    def __repr__(self) -> str:
        return (
            f"<Pomice.spotify.Artist name={self.name} artists={self.artists} id={self.id} "
            f"total_tracks={self.total_tracks} tracks={self.tracks}>"
        )
