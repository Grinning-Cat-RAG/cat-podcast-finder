import httpx
from cat import tool


SPREAKER_API_BASE_URL = "https://api.spreaker.com/v2"
# Spreaker may be slow or unreachable: a request never waits longer than this
TIMEOUT_SECONDS = 10.0


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=TIMEOUT_SECONDS)


async def get_author_info(client, author_id):
    endpoint = f"{SPREAKER_API_BASE_URL}/users/{author_id}"
    try:
        response = await client.get(endpoint)
    except httpx.HTTPError:
        return None

    if response.status_code == 200:
        author_info = response.json()["response"]["user"]
        return author_info
    return None


async def _search(client, kind, query, cat):
    """The items Spreaker finds, or the error to return."""
    endpoint = f"{SPREAKER_API_BASE_URL}/search"
    settings = await cat.mad_hatter.get_plugin().load_settings()

    params = {
        "type": kind,
        "q": query,
        "limit": settings["number_of_results"],
    }
    try:
        response = await client.get(endpoint, params=params)
    except httpx.HTTPError as e:
        return None, f"Error: {e!r}"

    if response.status_code == 200:
        return response.json()["response"]["items"], None
    return None, f"Error: {response.status_code}"


@tool
async def search_shows(query, cat):
    """
    Search for a podcast (a show). The input is a query.

    For example, search for a podcast about curious animals.
    Query -> curious animals
    """
    async with _client() as client:
        results, error = await _search(client, "shows", query, cat)
        if error:
            return error

        shows_info = []
        for show in results:
            author_id = show["author_id"]
            author_info = await get_author_info(client, author_id)
            author_name = author_info["fullname"] if author_info else "Unknown Author"
            shows_info.append(f"Show Name: {show['title']} by {author_name}\nLink: {show['site_url']}")
        return "\n".join(shows_info)


@tool
async def search_episodes(query, cat):
    """
    Search for a podcast episode. The input is a query.

    For example, search for a podcast episode about Alice in wonderland.
    Query -> alice in wonderland
    """
    async with _client() as client:
        results, error = await _search(client, "episodes", query, cat)
        if error:
            return error

        episodes_info = []
        for episode in results:
            show_title = episode["show"]["title"]
            episode_title = episode["title"]
            author_id = episode["show"]["author_id"]
            author_info = await get_author_info(client, author_id)
            author_name = author_info["fullname"] if author_info else "Unknown Author"
            episodes_info.append(
                f"Show Title: {show_title}\nEpisode Title: {episode_title} by {author_name}\nLink: {episode['site_url']}")
        return "\n".join(episodes_info)
