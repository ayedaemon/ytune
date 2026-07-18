"""
Thin wrapper around ytmusicapi.YTMusic.
All methods are synchronous — callers must use run_in_executor.

Normalises raw YTM response shapes into plain dicts with consistent keys:
  playlist:  {id, title, owner, song_count}
  track:     {video_id, title, artist, album, duration_seconds, raw}
  related:   {id, title}
"""
from __future__ import annotations

from ytmusicapi import YTMusic


class YTMClient:
    def __init__(self, auth_file: str) -> None:
        self._ytm = YTMusic(auth_file)

    # ── Library ────────────────────────────────────────────────────────────────

    def get_library_playlists(self) -> list[dict]:
        raw = self._ytm.get_library_playlists(limit=None) or []
        return [_norm_library_playlist(p) for p in raw]

    # ── Playlist detail ────────────────────────────────────────────────────────

    def get_playlist(
        self, playlist_id: str, *, related: bool = False
    ) -> tuple[list[dict], list[dict]]:
        """
        Returns (tracks, related_playlists).
        related_playlists is empty when related=False.
        """
        data = self._ytm.get_playlist(playlist_id, limit=None, related=related) or {}
        tracks = [_norm_track(t) for t in (data.get("tracks") or []) if t.get("videoId")]
        related_pls = [_norm_related_playlist(r) for r in (data.get("related") or [])] if related else []
        return tracks, related_pls


# ── Normalisers ────────────────────────────────────────────────────────────────

def _norm_library_playlist(raw: dict) -> dict:
    raw_count = raw.get("count") or ""
    if isinstance(raw_count, int):
        song_count = raw_count
    else:
        first = str(raw_count).split()[0] if raw_count else ""
        song_count = int(first) if first.isdigit() else 0
    return {
        "id": raw.get("playlistId") or raw.get("id", ""),
        "title": raw.get("title", ""),
        "owner": raw.get("author", "") or "",
        "song_count": song_count,
    }


def _norm_related_playlist(raw: dict) -> dict:
    return {
        "id": raw.get("playlistId") or raw.get("id", ""),
        "title": raw.get("title", ""),
        "owner": "",
        "song_count": 0,
    }


def _norm_track(raw: dict) -> dict:
    artists = raw.get("artists") or []
    artist = ", ".join(a["name"] for a in artists if a.get("name")) or None
    album_obj = raw.get("album") or {}
    album = album_obj.get("name") if isinstance(album_obj, dict) else None
    duration = raw.get("duration_seconds")
    return {
        "video_id": raw["videoId"],
        "title": raw.get("title"),
        "artist": artist,
        "album": album,
        "duration_seconds": int(duration) if duration else None,
        "raw": raw,
    }
