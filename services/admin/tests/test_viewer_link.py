"""The viewer link must stay on loopback, and the admin must say where to tunnel.

rerun's gRPC proxy sends `access-control-allow-origin` only for LOOPBACK
origins. Measured on 0.36.3: `http://127.0.0.1:9876` and
`http://localhost:9876` are echoed back; `http://172.16.13.1:9876` and
`http://10.1.2.3:9876` get no header at all.

So a viewer served on a routable address loads its page and is then refused
its own stream. Rewriting the link to the node's real IP -- which this admin
briefly did -- produces exactly that. The link stays on localhost, the
operator forwards both ports, and the admin publishes the address to forward
TO.
"""

from __future__ import annotations

from mec_cast_admin.protocol import NodeType
from mec_cast_admin.registry import NodeRecord

LOOPBACK = "http://localhost:9876/?url=rerun%2Bhttp%3A%2F%2Flocalhost%3A9877%2Fproxy"


def _rec(url: str | None, address: str) -> NodeRecord:
    r = NodeRecord(node_id="render-ue-0", node_type=NodeType.RENDER)
    r.address = address
    if url is not None:
        r.params = {"viewer_url": url}
    return r


class TestViewerLink:
    def test_a_loopback_link_is_left_alone(self):
        """Rewriting it to the node's IP gives a stream rerun will refuse."""
        got = _rec(LOOPBACK, "172.16.13.1").to_dict(30.0)["params"]["viewer_url"]
        assert got == LOOPBACK
        assert "172.16.13.1" not in got, (
            "rerun only allows loopback origins; a routable viewer_url loads "
            "a page that can never reach its own stream"
        )

    def test_the_address_is_published_for_the_tunnel(self):
        """The link cannot carry it, so the admin shows it separately."""
        assert _rec(LOOPBACK, "172.16.13.1").to_dict(30.0)["address"] == "172.16.13.1"

    def test_an_explicit_host_is_untouched(self):
        """VIEWER_HOST was set deliberately, even if rerun will refuse it."""
        url = "http://10.0.0.5:9876/?url=rerun%2Bhttp%3A%2F%2F10.0.0.5%3A9877%2Fproxy"
        assert _rec(url, "172.16.13.1").to_dict(30.0)["params"]["viewer_url"] == url

    def test_a_node_with_no_viewer_is_untouched(self):
        assert "viewer_url" not in _rec(None, "172.16.13.1").to_dict(30.0)["params"]
