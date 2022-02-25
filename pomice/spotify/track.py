class Track:
    """The base class for a Spotify Track"""

    def __init__(self, data: dict) -> None:
        self.data = data
        self.name = data["name"]
        self.artists = ", ".join(artist["name"] for artist in data["artists"])
        self.length = data["duration_ms"]
        self.id = data["id"]

        if data.get("album") and data["album"].get("images"):
            self.image = data["album"]["images"][0]["url"]
        else:
            self.image = None

        if data["is_local"]:
            self.uri = None
        else:
            self.uri = data["external_urls"]["spotify"]

    @property
    def isrc(self):
        try:
            return self.data["external_ids"]["isrc"]
        except KeyError:
            return None

    def __repr__(self) -> str:
        return (
            f"<Pomice.spotify.Track name={self.name} artists={self.artists} "
            f"length={self.length} id={self.id}>"
        )
