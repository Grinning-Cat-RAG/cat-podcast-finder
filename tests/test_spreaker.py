"""The tools ask Spreaker without blocking the event loop of the instance (every agent and chat it serves), and never
wait for it longer than a timeout.

Run from the core root: ``python -m pytest cat/plugins/cat-podcast-finder/tests``.

The Cat imports every ``.py`` file of the plugin, tests included: at import time this module needs only the stdlib,
the Cat, httpx and the plugin are loaded in ``setUpClass`` (and in the fakes, when they run).
"""
import asyncio
import os
import sys
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

PLUGIN_PATH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOADED = set()

MODULE = None
SEARCH_SHOWS = SEARCH_EPISODES = None


def _load():
    """Loads the plugin once, from the test classes: the Cat imports the modules of the plugin (tests included) again
    when it loads it, so ``sys.modules`` (where unittest looks for ``setUpModule``) may hold another copy of this module:
    the globals are set here, in the module of the test classes."""
    global MODULE, SEARCH_SHOWS, SEARCH_EPISODES
    if PLUGIN_PATH in LOADED:
        return
    from cat.looking_glass.mad_hatter.plugin import Plugin

    plugin = Plugin(PLUGIN_PATH)
    plugin._load_decorated_functions()
    SEARCH_SHOWS = next(t for t in plugin.tools if t.name == "search_shows").func
    SEARCH_EPISODES = next(t for t in plugin.tools if t.name == "search_episodes").func
    MODULE = sys.modules[SEARCH_SHOWS.__module__]
    LOADED.add(PLUGIN_PATH)


class Spreaker:
    """api.spreaker.com: the search and the users. ``delay`` is the time it takes to answer; ``search_status`` and
    ``user_status`` are the HTTP status of the answers, ``unreachable`` the endpoints that do not answer."""

    def __init__(self, delay=0.0):
        self.delay = delay
        self.search_status = 200
        self.user_status = 200
        self.unreachable = set()
        self.requests = []

    async def get(self, url, params=None):
        import httpx

        self.requests.append((url, params))
        await asyncio.sleep(self.delay)
        kind = "search" if url.endswith("/search") else "user"
        if kind in self.unreachable:
            raise httpx.ReadTimeout("injected: no answer")
        response = MagicMock(status_code=self.search_status if kind == "search" else self.user_status)
        if kind == "user":
            author_id = url.rsplit("/", 1)[1]
            response.json.return_value = {"response": {"user": {"fullname": f"Author {author_id}"}}}
        elif params["type"] == "shows":
            response.json.return_value = {"response": {"items": [
                {"title": f"Show {i}", "author_id": i, "site_url": f"https://spreaker/show/{i}"} for i in range(2)
            ]}}
        else:
            response.json.return_value = {"response": {"items": [
                {"title": f"Episode {i}", "site_url": f"https://spreaker/episode/{i}",
                 "show": {"title": f"Show {i}", "author_id": i}} for i in range(2)
            ]}}
        return response


class Client:
    """Stand-in of the asynchronous HTTP client of the plugin, answering with ``server.get``."""

    def __init__(self, server):
        self.server = server
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.closed = True
        return False

    async def get(self, url, params=None):
        return await self.server.get(url, params=params)


def _cat(number_of_results=10):
    cat = MagicMock()
    cat.mad_hatter.get_plugin.return_value.load_settings = AsyncMock(
        return_value={"number_of_results": number_of_results}
    )
    return cat


def _run(tool, query, server, cat=None):
    async def main():
        with patch.object(MODULE, "_client", lambda: Client(server)):
            return await tool(query, cat or _cat())

    return asyncio.run(main())


class TestSearch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _load()

    def test_shows_with_their_authors(self):
        server = Spreaker()
        self.assertEqual(_run(SEARCH_SHOWS, "curious animals", server, _cat(3)), (
            "Show Name: Show 0 by Author 0\nLink: https://spreaker/show/0\n"
            "Show Name: Show 1 by Author 1\nLink: https://spreaker/show/1"
        ))
        self.assertEqual(server.requests[0], (
            "https://api.spreaker.com/v2/search", {"type": "shows", "q": "curious animals", "limit": 3},
        ))
        self.assertEqual([url for url, _ in server.requests[1:]], [
            "https://api.spreaker.com/v2/users/0", "https://api.spreaker.com/v2/users/1",
        ])

    def test_episodes_with_their_authors(self):
        server = Spreaker()
        self.assertEqual(_run(SEARCH_EPISODES, "alice", server), (
            "Show Title: Show 0\nEpisode Title: Episode 0 by Author 0\nLink: https://spreaker/episode/0\n"
            "Show Title: Show 1\nEpisode Title: Episode 1 by Author 1\nLink: https://spreaker/episode/1"
        ))
        self.assertEqual(server.requests[0][1], {"type": "episodes", "q": "alice", "limit": 10})

    def test_a_search_error_is_reported(self):
        for tool in (SEARCH_SHOWS, SEARCH_EPISODES):
            with self.subTest(tool=tool.__name__):
                server = Spreaker()
                server.search_status = 503
                self.assertEqual(_run(tool, "q", server), "Error: 503")
                self.assertEqual(len(server.requests), 1)

    def test_an_unreachable_spreaker_is_reported(self):
        for tool in (SEARCH_SHOWS, SEARCH_EPISODES):
            with self.subTest(tool=tool.__name__):
                server = Spreaker()
                server.unreachable.add("search")
                self.assertTrue(_run(tool, "q", server).startswith("Error: "))

    def test_an_unknown_author(self):
        for status, unreachable in ((404, set()), (200, {"user"})):
            with self.subTest(status=status, unreachable=unreachable):
                server = Spreaker()
                server.user_status = status
                server.unreachable = unreachable
                self.assertIn("Show 0 by Unknown Author", _run(SEARCH_SHOWS, "q", server))
                self.assertIn("Episode 1 by Unknown Author", _run(SEARCH_EPISODES, "q", server))


class TestNonBlocking(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _load()

    def test_a_slow_spreaker_never_blocks_the_other_requests(self):
        """Regression: the requests were synchronous (``requests.get``) inside the asynchronous tools: while Spreaker
        answered, the event loop was blocked, and so every other request served by the instance."""
        server = Spreaker(delay=0.1)

        async def burst():
            with patch.object(MODULE, "_client", lambda: Client(server)):
                begin = time.monotonic()
                await asyncio.gather(*(
                    tool(f"query {i}", _cat()) for i in range(3) for tool in (SEARCH_SHOWS, SEARCH_EPISODES)
                ))
                return time.monotonic() - begin

        # every search makes three requests of 0.1 seconds (the search, two authors): six searches in a row take 1.8
        self.assertLess(asyncio.run(burst()), 1.0, "the six searches run together")
        self.assertEqual(len(server.requests), 18)

    def test_the_client_has_a_timeout(self):
        client = MODULE._client()
        self.assertEqual(client.timeout.read, MODULE.TIMEOUT_SECONDS)
        self.assertEqual(client.timeout.connect, MODULE.TIMEOUT_SECONDS)
        asyncio.run(client.aclose())


class TestPluginSafety(unittest.TestCase):
    def test_every_file_passes_the_scanner_of_the_core(self):
        from cat.looking_glass.mad_hatter.plugin_extractor import PluginExtractor

        self.assertTrue(PluginExtractor._is_safe_plugin(PLUGIN_PATH))

    def test_tests_import_only_the_stdlib_at_import_time(self):
        import ast
        from pathlib import Path

        tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                for name in names:
                    self.assertIn(name.split(".")[0], sys.stdlib_module_names, name)


if __name__ == "__main__":
    unittest.main()
