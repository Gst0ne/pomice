class Track:
    """The base class for a Spotify Track"""

    def __init__(self, data: dict) -> None:
        self.name = data["name"]
        self.artists = ", ".join(artist["name"] for artist in data["artists"])
        self.length = data["duration_ms"]
        self.id = data["id"]
        self.image = data["album"]["images"][0]["url"] if data.get("album") and data["album"]["images"] else None
        self.uri = None if data["is_local"] else data["external_urls"]["spotify"]
        self.isrc = data["external_ids"].get("isrc") if data.get("external_ids") else None

    def __repr__(self) -> str:
        return (
            f"<Pomice.spotify.Track name={self.name} artists={self.artists} "
            f"length={self.length} id={self.id}>"
        )
