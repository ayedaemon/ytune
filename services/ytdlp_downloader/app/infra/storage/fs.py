import os


def get_output_template(storage_root: str, outtmpl: str) -> str:
    """
    Prefixes the user-configured yt-dlp output template with storage_root.
    yt-dlp itself handles the templating, sanitization, and directory creation
    for whatever %(...)s fields and "/" subdirectories the template contains.
    """
    os.makedirs(storage_root, exist_ok=True)
    return os.path.join(storage_root, outtmpl)
