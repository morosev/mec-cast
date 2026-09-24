"""The admin's viewer link must point at the renderer, not at the operator.

`viewer_host` defaults to `localhost` because a process cannot know which of
its addresses a browser can reach. Served unaltered, the admin's "viewer"
button sends the operator to their own machine. The admin does know the
address -- the node dialled it.
"""

from __future__ import annotations

from mec_cast_admin.protocol import NodeType
from mec_cast_admin.registry import NodeRecord

WEB = "http://localhost:9876"
SRC = "?url=rerun%2Bhttp%3A%2F%2Flocalhost%3A9877%2Fproxy"


def _rec(url: str | None, address: str) -> NodeRecord:
    r = NodeRecord(node_id="render-ue-0", node_type=NodeType.RENDER)
    r.address = address
    if url is not None:
        r.params = {"viewer_url": url}
    return r


class TestViewerLink:
    def test_a_loopback_link_is_repointed_at_the_node(self):
        got = _rec(WEB + SRC, "172.16.13.1").to_dict(30.0)["params"]["viewer_url"]
        assert "//172.16.13.1:9876" in got
        assert "localhost" not in got, (
            "the stream address in the query is fetched by the same browser -- "
            "repairing only the page host gives a viewer that loads and never fills"
        )

    def test_a_real_address_is_left_alone(self):
        """VIEWER_HOST was set deliberately; do not second-guess it."""
        url = "http://10.0.0.5:9876/?url=rerun%2Bhttp%3A%2F%2F10.0.0.5%3A9877%2Fproxy"
        assert _rec(url, "172.16.13.1").to_dict(30.0)["params"]["viewer_url"] == url

    def test_without_a_known_address_nothing_changes(self):
        """A node that never connected leaves the link as the node wrote it."""
        assert _rec(WEB + SRC, "").to_dict(30.0)["params"]["viewer_url"] == WEB + SRC

    def test_a_node_with_no_viewer_is_untouched(self):
        assert "viewer_url" not in _rec(None, "172.16.13.1").to_dict(30.0)["params"]

    def test_the_address_is_published(self):
        assert _rec(None, "172.16.13.1").to_dict(30.0)["address"] == "172.16.13.1"
